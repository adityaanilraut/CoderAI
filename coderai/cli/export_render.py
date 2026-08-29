"""Session export utilities (Markdown & JSON export with syntax formatting)."""

from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

from coderai.core.session import SessionEntry, SessionManager, SessionMessage


def export_session_to_markdown(
    mgr: SessionManager,
    session_id: str,
    output_path: str | None = None,
) -> str:
    """Export the active session conversation to a beautifully formatted Markdown file."""
    entry: SessionEntry | None = mgr.get_session(session_id)
    messages: list[SessionMessage] = mgr.list_session_messages(session_id)

    title = entry.summary if entry and entry.summary else f"Session {session_id[:8]}"
    model = mgr.get_active_model()
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = [
        f"# 🤖 CoderAI Session Export: {title}",
        "",
        f"- **Session ID**: `{session_id}`",
        f"- **Model**: `{model}`",
        f"- **Export Date**: {date_str}",
        f"- **Total Messages**: {len(messages)}",
    ]

    if entry and entry.usage:
        prompt_tokens = entry.usage.get("prompt_tokens", 0)
        completion_tokens = entry.usage.get("completion_tokens", 0)
        total_tokens = entry.usage.get("total_tokens", 0)
        cached_tokens = entry.usage.get("cached_tokens", 0)
        hit_rate = (cached_tokens / prompt_tokens * 100.0) if prompt_tokens > 0 else 0.0
        cached_str = (
            f" | Cached: {cached_tokens:,} ({hit_rate:.1f}% hit)" if cached_tokens > 0 else ""
        )
        lines.append(
            f"- **Token Usage**: Prompt: {prompt_tokens:,} | Completion: {completion_tokens:,} | Total: {total_tokens:,}{cached_str}"
        )

    lines.extend(["", "---", ""])

    for idx, msg in enumerate(messages, 1):
        if msg.role == "system":
            continue

        if msg.role == "user":
            lines.append(f"### 👤 User (Turn {idx})")
            lines.append("")
            lines.append(msg.content or "")
            lines.append("")
            lines.append("---")
            lines.append("")
        elif msg.role == "assistant":
            lines.append(f"### 🤖 CoderAI Assistant (Turn {idx})")
            lines.append("")

            if msg.thinking:
                lines.append("<details>")
                lines.append("<summary><b>✧ Reasoning Trace</b></summary>")
                lines.append("")
                lines.append(f"```text\n{msg.thinking}\n```")
                lines.append("</details>")
                lines.append("")

            if msg.content:
                lines.append(msg.content)
                lines.append("")

            if msg.tool_calls:
                lines.append("**Tool Invocations:**")
                for tc in msg.tool_calls:
                    fn = (
                        tc.get("function", {})
                        if isinstance(tc, dict)
                        else getattr(tc, "function", None)
                    )
                    fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "")
                    fn_args = (
                        fn.get("arguments")
                        if isinstance(fn, dict)
                        else getattr(fn, "arguments", "")
                    )
                    lines.append(f"- Invoked `{fn_name}` with arguments:")
                    lines.append(f"  ```json\n  {fn_args}\n  ```")
                lines.append("")
            lines.append("---")
            lines.append("")
        elif msg.role == "tool":
            lines.append(f"#### 🛠️ Tool Result (`{msg.tool_call_id or 'tool'}`)")
            lines.append("")
            try:
                parsed = json.loads(msg.content or "{}")
                tool_name = parsed.get("name", "tool")
                ok = parsed.get("ok", True)
                status_emoji = "✓" if ok else "✗"
                lines.append(f"**Status**: {status_emoji} `{tool_name}`")
                if parsed.get("output"):
                    out_str = str(parsed["output"])
                    lines.append(f"```text\n{out_str}\n```")
                elif parsed.get("error"):
                    lines.append(f"**Error**: `{parsed['error']}`")
            except Exception:
                lines.append(f"```text\n{msg.content}\n```")
            lines.append("")
            lines.append("---")
            lines.append("")

    content = "\n".join(lines)

    if not output_path:
        filename = f"coderai-session-{session_id[:8]}.md"
        target = pathlib.Path(mgr.project_root) / filename
    else:
        target = pathlib.Path(output_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target.resolve())


def export_session_to_json(
    mgr: SessionManager,
    session_id: str,
    output_path: str | None = None,
) -> str:
    """Export the raw session data and message array into a JSON file."""
    entry: SessionEntry | None = mgr.get_session(session_id)
    messages: list[SessionMessage] = mgr.list_session_messages(session_id)

    data: dict[str, Any] = {
        "session": {
            "id": session_id,
            "summary": entry.summary if entry else "",
            "active_tokens": entry.active_tokens if entry else 0,
            "usage": entry.usage if entry else {},
            "plan_mode": entry.plan_mode if entry else False,
            "model": mgr.get_active_model(),
        },
        "messages": [mgr._serialize_message(m) for m in messages],
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    content = json.dumps(data, indent=2) + "\n"

    if not output_path:
        filename = f"coderai-session-{session_id[:8]}.json"
        target = pathlib.Path(mgr.project_root) / filename
    else:
        target = pathlib.Path(output_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target.resolve())
