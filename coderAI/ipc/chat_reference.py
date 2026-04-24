"""Plain-text reference material for the Ink UI (slash commands / IPC).

Mirrors ``coderAI models``, ``cost``, ``status``, ``config show``, ``info``, and
``tasks list`` as strings suitable for multi-line toast output.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import __version__
from ..config import config_manager
from ..cost import MODEL_PRICING, CostTracker
from ..history import history_manager

_MAX_CHARS = 16_000


def _truncate(text: str, max_chars: int = _MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 48].rstrip() + "\n\n… (truncated — run the CLI for full output)"


def _mask_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    for key in ("openai_api_key", "anthropic_api_key", "groq_api_key", "deepseek_api_key"):
        v = out.get(key)
        if isinstance(v, str) and len(v) > 12:
            out[key] = f"{v[:8]}...{v[-4:]}"
        elif v:
            out[key] = "(set)"
    return out


def build_models_text() -> str:
    cfg = config_manager.load()
    lines: List[str] = [
        "Models & providers (see also: /default <name> for saved default)",
        "",
        "OpenAI — requires OPENAI or config openai_api_key",
        "  gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, o1, o1-mini, o3-mini",
        "",
        "Anthropic — requires ANTHROPIC or config anthropic_api_key",
        "  claude-4-sonnet, claude-4.7-opus, claude-4.5-haiku, claude-3.5-sonnet, …",
        "",
        "Groq — requires GROQ or config groq_api_key",
        "  openai/gpt-oss-120b, openai/gpt-oss-20b, llama3-70b-8192, …",
        "",
        "DeepSeek — requires DEEPSEEK or config deepseek_api_key",
        "  deepseek-v3.2, deepseek-r1, …",
        "",
        "Local",
        "  lmstudio — LM Studio at lmstudio_endpoint",
        "  ollama — Ollama at ollama_endpoint",
        "",
        f"Saved default model (config): {cfg.default_model}",
    ]
    return _truncate("\n".join(lines))


def build_cost_text() -> str:
    cfg = config_manager.load()
    lines: List[str] = [
        "API cost & pricing",
        "Session spend: use /status or /tokens for live totals in this chat.",
        "",
    ]
    if cfg.budget_limit and cfg.budget_limit > 0:
        lines.append(
            f"Budget limit (config): {CostTracker.format_cost(cfg.budget_limit)} per session"
        )
        lines.append("")
    lines.append("Reference pricing (per 1M tokens, USD):")
    for model, pricing in sorted(MODEL_PRICING.items()):
        if pricing["input"] == 0 and pricing["output"] == 0:
            lines.append(f"  {model}: free (local)")
        else:
            lines.append(
                f"  {model}: {CostTracker.format_cost(pricing['input'])} in / "
                f"{CostTracker.format_cost(pricing['output'])} out"
            )
    return _truncate("\n".join(lines))


def build_system_text() -> str:
    cfg = config_manager.load()
    sessions = history_manager.list_sessions()
    lines: List[str] = [
        "System status (like `coderAI status`)",
        "",
        "Paths",
        f"  Config dir: {config_manager.config_dir}",
        f"  History dir: {history_manager.history_dir}",
        "",
        "Core",
        f"  default_model: {cfg.default_model}",
        f"  streaming: {cfg.streaming}",
        f"  save_history: {cfg.save_history}",
        f"  log_level: {cfg.log_level}",
        f"  reasoning_effort: {cfg.reasoning_effort}",
        "",
        "API keys",
        f"  OpenAI:     {'yes' if cfg.openai_api_key else 'no'}",
        f"  Anthropic:  {'yes' if cfg.anthropic_api_key else 'no'}",
        f"  Groq:       {'yes' if cfg.groq_api_key else 'no'}",
        f"  DeepSeek:   {'yes' if cfg.deepseek_api_key else 'no'}",
        "",
        "LM Studio",
        f"  endpoint: {cfg.lmstudio_endpoint}",
        f"  model:    {cfg.lmstudio_model}",
        "",
        "Ollama",
        f"  endpoint: {cfg.ollama_endpoint}",
        f"  model:    {cfg.ollama_model}",
        "",
        "History",
        f"  sessions on disk: {len(sessions)}",
    ]
    return _truncate("\n".join(lines))


def build_config_text(agent: Any) -> str:
    raw = agent.config.model_dump(exclude_none=True)
    masked = _mask_keys(raw)
    lines = [
        "Effective configuration (this session; API keys masked)",
        "",
    ]
    for key in sorted(masked.keys()):
        lines.append(f"  {key}: {masked[key]}")
    return _truncate("\n".join(lines))


def _flatten_model_info(obj: Any, indent: int = 0) -> List[str]:
    pad = "  " * indent
    lines: List[str] = []
    if isinstance(obj, dict):
        for k, v in sorted(obj.items(), key=lambda x: str(x[0])):
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.extend(_flatten_model_info(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}[{i}]:")
                lines.extend(_flatten_model_info(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
    else:
        lines.append(f"{pad}{obj}")
    return lines


def build_info_text(agent: Any) -> str:
    lines: List[str] = [
        f"CoderAI {__version__}",
        f"Config dir: {config_manager.config_dir}",
        f"History dir: {history_manager.history_dir}",
        "",
        "Current model (session)",
        f"  model:    {agent.model}",
        f"  provider: {agent.provider.__class__.__name__}",
        "",
        "Provider / model details",
    ]
    try:
        mi = agent.get_model_info()
        lines.extend(_flatten_model_info(mi, 1))
    except Exception as e:
        lines.append(f"  (could not load: {e})")

    lines.extend(["", "Tools (name — short description)"])
    try:
        tools = agent.tools.get_all()
        for t in tools[:48]:
            desc = t.description.replace("\n", " ").strip()
            if len(desc) > 72:
                desc = desc[:69] + "…"
            lines.append(f"  {t.name} — {desc}")
        if len(tools) > 48:
            lines.append(f"  … and {len(tools) - 48} more")
    except Exception as e:
        lines.append(f"  (could not list: {e})")

    return _truncate("\n".join(lines))


async def build_tasks_text(project_root: str) -> str:
    from ..tools.tasks import ManageTasksTool

    tool = ManageTasksTool()
    result = await tool.execute("list", project_root=project_root)
    if not result.get("success"):
        err = result.get("error", "Unknown error")
        return f"Tasks: could not load ({err})"

    lines: List[str] = [result.get("summary", "Tasks"), ""]
    for status in ("in_progress", "pending", "completed"):
        bucket = result.get(status, [])
        if not bucket:
            continue
        label = "In progress" if status == "in_progress" else status.title()
        lines.append(f"{label}:")
        for t in bucket:
            desc = f" — {t['description']}" if t.get("description") else ""
            lines.append(f"  [{t['id']}] {t['title']}{desc}")
        lines.append("")
    text = "\n".join(lines).strip()
    return _truncate(text)


def resolve_reference_text(topic: str, agent: Any) -> str:
    t = topic.lower().strip()
    if t in ("version", "v"):
        return f"CoderAI {__version__}"
    if t in ("models", "providers"):
        return build_models_text()
    if t in ("cost", "pricing"):
        return build_cost_text()
    if t in ("system", "diagnostics", "diag"):
        return build_system_text()
    if t == "config":
        return build_config_text(agent)
    if t == "info":
        return build_info_text(agent)
    raise ValueError(
        f"Unknown topic {topic!r}. Use: version, models, cost, system, config, info, tasks."
    )
