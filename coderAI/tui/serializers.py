"""Pure serialization helpers for the UI bridge.

Shapes ``AgentInfo``, plan, and task data into the payloads documented in
``docs/CHAT_EVENTS.md``. No event names or payload keys may change here
without updating the doc and ``tests/test_event_contract.py`` in the same
commit.

Moved here from ``coderAI/bridge/serializers.py`` (Phase 3 bridge demolition).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from coderAI.core.agent_tracker import AgentInfo, AgentStatus, agent_tracker


def _infer_error_hint(category: str, message: str) -> Optional[str]:
    """Best-effort canonical hint for an error message.

    Kept server-side so every consumer (Textual UI, logs, CLI fallbacks)
    sees the same hint. The UI no longer tries to re-derive hints from
    message text.
    """
    lower = (message or "").lower()
    if category == "provider":
        if "localhost:1234" in lower or "lmstudio" in lower:
            return "Start LM Studio: open the app -> Developer -> Start Server."
        if "localhost:11434" in lower or "ollama" in lower:
            return "Start Ollama: run `ollama serve` in another terminal."
        if "anthropic" in lower and "key" in lower:
            return "Set ANTHROPIC_API_KEY, or run `coderAI config set anthropic_api_key <KEY>`."
        if "openai" in lower and "key" in lower:
            return "Set OPENAI_API_KEY, or run `coderAI config set openai_api_key <KEY>`."
        if "groq" in lower and "key" in lower:
            return "Set GROQ_API_KEY, or run `coderAI config set groq_api_key <KEY>`."
        if "deepseek" in lower and "key" in lower:
            return "Set DEEPSEEK_API_KEY, or run `coderAI config set deepseek_api_key <KEY>`."
        if "gemini" in lower and "key" in lower:
            return "Set GEMINI_API_KEY, or run `coderAI config set gemini_api_key <KEY>`."
        if any(k in lower for k in ("api key", "401", "unauthorized", "authentication")):
            return "Missing/invalid API key -- run `coderAI setup` or `coderAI doctor`."
        if any(k in lower for k in ("rate limit", "429", "too many requests")):
            return (
                "Rate limited -- wait a few seconds and retry, or switch models with /model <name>."
            )
        if "context" in lower and "length" in lower:
            return "Context window exceeded. Try /compact to summarize, or /clear to reset."
        if any(k in lower for k in ("quota", "insufficient", "billing")):
            return "Provider reports quota/billing exhausted. Top up credits or switch providers."
        if "timeout" in lower or "timed out" in lower:
            return "Request timed out. Try again; if persistent, check your network and /model."
        if any(
            k in lower
            for k in (
                "cannot connect",
                "connection refused",
                "econnrefused",
                "getaddrinfo",
            )
        ):
            return "Network/service unreachable. Check the endpoint URL, DNS, and firewall."
        if "ssl" in lower or "certificate" in lower:
            return "TLS handshake failed. Check your system clock and corporate proxy/CA certs."
    if category == "tool":
        if any(k in lower for k in ("permission denied", "eacces", "eperm")):
            return "The tool lacks filesystem permissions. Check file ownership/mode."
        if "not found" in lower or "enoent" in lower:
            return "Target path or command wasn't found. Double-check the argument."
        if "timeout" in lower or "timed out" in lower:
            return "Tool timed out. For long shells try run_background, or raise timeout in args."
        if "cancelled" in lower or "cancel" in lower:
            return "Cancelled."
    return None


def _agent_info_dict(info: AgentInfo) -> Dict[str, Any]:
    return {
        "id": info.agent_id,
        "name": info.name,
        "role": info.role,
        "parentId": info.parent_id,
        "status": info.status.value if isinstance(info.status, AgentStatus) else str(info.status),
        "task": info.current_task,
        "tool": info.current_tool,
        "model": info.model,
        "tokens": info.total_tokens,
        "costUsd": info.cost_usd,
        "ctxUsed": info.context_used_tokens,
        "ctxLimit": info.context_limit_tokens,
        "elapsedMs": int(info.elapsed_seconds * 1000),
        "depth": _compute_agent_depth(info),
    }


def _compute_agent_depth(info: AgentInfo) -> int:
    depth = 0
    pid = info.parent_id
    while pid:
        parent = agent_tracker.get(pid)
        if parent is None:
            break
        depth += 1
        pid = parent.parent_id
    return depth


def _format_plan_message(plan: Dict[str, Any]) -> str:
    """Plain-text summary for UI toast (matches `plan` tool show semantics)."""
    title = plan.get("title") or "Untitled"
    steps = plan.get("steps") or []
    total = len(steps)
    try:
        current = int(plan.get("current_step", 0))
    except (TypeError, ValueError):
        current = 0
    completed = sum(1 for s in steps if s.get("status") == "done")
    cur_desc = steps[current]["description"] if current < total else "All steps completed"
    lines = [
        f"Plan: {title}",
        f"Progress: {completed}/{total} steps . current: {cur_desc}",
        "",
    ]
    for i, s in enumerate(steps):
        mark = "\u2713" if s.get("status") == "done" else "\u25cb"
        prefix = "\u2192" if i == current and i < total else " "
        desc = s.get("description", "")
        lines.append(f"  {prefix}{mark} {i + 1}. {desc}")
    return "\n".join(lines)


def _serialize_plan_for_ui(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize plan data into a structured payload for the plan_card UI event."""
    steps = plan.get("steps") or []
    total = len(steps)
    try:
        current = int(plan.get("current_step", 0))
    except (TypeError, ValueError):
        current = 0
    completed = sum(1 for s in steps if s.get("status") == "done")
    return {
        "title": plan.get("title", "Untitled"),
        "completed": completed,
        "total": total,
        "currentIdx": current,
        "steps": [
            {
                "index": i + 1,
                "description": s.get("description", ""),
                "status": s.get("status", "pending"),
            }
            for i, s in enumerate(steps)
        ],
    }


_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_COMPLETED_CAP = 5


def _task_ui_item(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(task.get("id") or 0),
        "title": str(task.get("title") or ""),
        "priority": str(task.get("priority") or "medium"),
        "status": str(task.get("status") or "pending"),
    }


def _serialize_tasks_for_ui(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Serialize tasks into a grouped payload for the tasks_card UI event."""

    def _sort_key(t: Dict[str, Any]) -> int:
        return _PRIORITY_ORDER.get(str(t.get("priority", "medium")), 1)

    in_progress = sorted(
        [_task_ui_item(t) for t in tasks if t.get("status") == "in_progress"],
        key=_sort_key,
    )
    pending = sorted(
        [_task_ui_item(t) for t in tasks if t.get("status") == "pending"],
        key=_sort_key,
    )
    completed_all = [t for t in tasks if t.get("status") == "completed"]
    completed = [_task_ui_item(t) for t in completed_all[-_COMPLETED_CAP:]]

    return {
        "summary": (
            f"{len(in_progress)} in-progress, "
            f"{len(pending)} pending, "
            f"{len(completed_all)} completed"
        ),
        "inProgress": in_progress,
        "pending": pending,
        "completed": completed,
        "total": len(tasks),
    }


def _load_tasks_from_disk(project_root: str) -> List[Dict[str, Any]]:
    from coderAI.tools.tasks import get_tasks_file

    tasks_file = get_tasks_file(project_root)
    if not tasks_file.exists():
        return []
    try:
        import json

        with open(tasks_file, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            return []
        for t in raw:
            if isinstance(t, dict) and "priority" not in t:
                t["priority"] = "medium"
        return raw
    except (json.JSONDecodeError, OSError):
        return []
