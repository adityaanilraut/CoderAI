"""Reference, discovery, and model-catalog application queries for the TUI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

from coderAI.system.config import config_manager
from coderAI.system.redaction import redact_secrets

if TYPE_CHECKING:
    from coderAI.tui.controller import UIBridge

_MAX_CHARS = 16_000


def _truncate(text: str, max_chars: int = _MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 48].rstrip() + "\n\n… (truncated — run the CLI for full output)"


def _mask_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible TUI helper routed through central redaction."""
    redacted = redact_secrets(data)
    assert isinstance(redacted, dict)
    return redacted


def _build_models_text() -> str:

    from coderAI.llm.registry import ALL_SPECS, specs_by_provider

    cfg = config_manager.load()
    label_map = {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "gemini": "Gemini",
        "deepseek": "DeepSeek",
        "groq": "Groq",
        "meta": "Meta",
        "lmstudio": "LM Studio (local)",
        "ollama": "Ollama (local)",
    }
    lines = [
        "Models & providers (see also: /default <name> for saved default)",
        "",
    ]
    for provider, specs in specs_by_provider().items():
        heading = label_map.get(provider, provider.title())
        req = specs[0].requirement if specs else ""
        suffix = f" — {req}" if req else ""
        lines.append(f"{heading}{suffix}")
        ids = ", ".join(spec.id for spec in specs)
        aliases = [alias for spec in specs for alias in spec.aliases[:2]]
        extra = f"  (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(f"  {ids}{extra}")
        lines.append("")
    lines.append(f"Saved default model (config): {cfg.default_model}")
    lines.append(f"Catalog size: {len(ALL_SPECS)} models")
    return _truncate("\n".join(lines))


def _build_cost_text() -> str:
    from coderAI.system.cost import MODEL_PRICING, CostTracker

    cfg = config_manager.load()
    lines = [
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


def _build_system_text() -> str:
    from coderAI.system.history import history_manager

    cfg = config_manager.load()
    sessions = history_manager.list_sessions()
    lines = [
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
        f"  Gemini:     {'yes' if cfg.gemini_api_key else 'no'}",
        f"  Meta:       {'yes' if cfg.meta_api_key else 'no'}",
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


def _build_config_text(agent: Any) -> str:
    raw = agent.config.model_dump(exclude_none=True)
    masked = _mask_keys(raw)
    lines = [
        "Effective configuration (this session; API keys masked)",
        "",
    ]
    for key in sorted(masked.keys()):
        lines.append(f"  {key}: {masked[key]}")
    return _truncate("\n".join(lines))


def _flatten_model_info(obj: Any, indent: int = 0) -> list:
    pad = "  " * indent
    lines = []
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


def _build_info_text(agent: Any) -> str:
    from coderAI import __version__ as _ver
    from coderAI.system.history import history_manager

    lines = [
        f"CoderAI {_ver}",
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


async def _build_tasks_text(project_root: str) -> str:
    from ..tools.tasks import ManageTasksTool

    tool = ManageTasksTool()
    result = await tool.execute("list", project_root=project_root)
    if not result.get("success"):
        err = result.get("error", "Unknown error")
        return f"Tasks: could not load ({err})"

    lines = [result.get("summary", "Tasks"), ""]
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


def _resolve_reference_text(topic: str, agent: Any) -> str:
    from coderAI import __version__ as _ver

    resolvers: dict[str, Callable[[], str]] = {
        "version": lambda: f"CoderAI {_ver}",
        "v": lambda: f"CoderAI {_ver}",
        "models": _build_models_text,
        "providers": _build_models_text,
        "cost": _build_cost_text,
        "pricing": _build_cost_text,
        "system": _build_system_text,
        "diagnostics": _build_system_text,
        "diag": _build_system_text,
        "config": lambda: _build_config_text(agent),
        "info": lambda: _build_info_text(agent),
    }
    resolver = resolvers.get(topic.lower().strip())
    if resolver is None:
        # `tasks` is handled upstream in _cmd_reference, not here.
        raise ValueError(
            f"Unknown topic {topic!r}. Use: version, models, cost, system, config, info."
        )
    return resolver()


async def _cmd_search_codebase(server: UIBridge, msg: dict[str, Any]) -> None:
    query = msg.get("query", "")
    if not query:
        return
    try:
        from coderAI.embeddings import create_embedding_provider
        from coderAI.context.code_indexer import CodeIndexer

        project_root = getattr(server.agent.config, "project_root", ".")
        config = server.agent.config
        provider = create_embedding_provider(config)
        if provider is None:
            server.emit(
                "warning",
                message=(
                    "No embedding provider is available: the OpenAI backend needs an "
                    "API key. Set openai_api_key or select embedding_backend=local."
                ),
            )
            return
        indexer = CodeIndexer(str(Path(project_root).resolve()), provider)
        results = await indexer.search(query=query, top_k=10)
        if not results:
            server.emit("info", message=f"No semantic search results found for '{query}'.")
            return
        out = [f"Semantic search results for '{query}':\n"]
        for r in results:
            snippet = r["text"].strip().split("\n")[0][:80]
            out.append(
                f"• {r['file_path']} L{r['start_line']}-{r['end_line']} (score: {r['score']:.2f})\n  {snippet}..."
            )
        server.emit("info", message="\n".join(out))
    except Exception as e:
        server.emit("warning", message=f"Codebase search failed: {e}")


async def _cmd_list_models(server: UIBridge, _msg: dict[str, Any]) -> None:
    """Return all available models grouped by provider for the model-picker UI.

    Unified picker: delegates to registry so every provider shows
    frontier/mid/small tier badges and reasoning support.
    """
    from coderAI.llm.registry import ALL_SPECS

    grouped: dict[str, list[str]] = {}
    details: dict[str, dict] = {}
    for spec in ALL_SPECS:
        provider_label = (
            spec.provider.title()
            if spec.provider not in ("lmstudio", "ollama")
            else spec.provider.title()
        )
        # normalize to match existing UI labels
        display_provider = {
            "Openai": "OpenAI",
            "Anthropic": "Anthropic",
            "Gemini": "Gemini",
            "Deepseek": "DeepSeek",
            "Groq": "Groq",
            "Meta": "Meta",
            "Lmstudio": "Local",
            "Ollama": "Local",
        }.get(provider_label, provider_label)
        grouped.setdefault(display_provider, []).append(spec.id)
        # local providers collapse under "Local"
        if spec.provider in ("lmstudio", "ollama"):
            grouped.setdefault("Local", [])
            if spec.id not in grouped["Local"]:
                # ensure Local contains both but not duplicate specs already grouped
                pass
        details[spec.id] = {
            "label": spec.label,
            "tier": spec.tier,
            "supports_reasoning": spec.supports_reasoning,
            "context_window": spec.context_window,
            "input_price": spec.input_price,
            "output_price": spec.output_price,
        }
    # sort lists
    for k in grouped:
        grouped[k] = sorted(grouped[k])
    # ensure Local only once and ordered
    if "Lmstudio" in grouped:
        del grouped["Lmstudio"]
    if "Ollama" in grouped:
        del grouped["Ollama"]

    server.emit(
        "available_models",
        current=server.agent.model,
        models=grouped,
        details=details,
    )


async def _cmd_reference(server: UIBridge, msg: dict[str, Any]) -> None:
    """Emit long-form help text (models, cost, system status, config, info, tasks)."""
    topic = str(msg.get("topic", "")).strip()
    if not topic:
        server.emit(
            "warning",
            message="Missing topic. Try /version, /models, /cost, /system, /config, /info, /tasks.",
        )
        return
    t = topic.lower()
    if t in ("tasks", "todos", "task"):
        pr = getattr(server.agent.config, "project_root", None) or "."
        try:
            text = await _build_tasks_text(pr)
        except Exception as e:
            server.emit("warning", message=f"Tasks: {e}")
            return
        server.emit("info", message=text)
        return
    try:
        # Off-loop: list_sessions (the /sessions topic) scans the history
        # directory — same treatment app.py gives its own list_sessions call.
        # Resolve through the historical adapter module so callers that patch
        # ``coderAI.tui.commands._resolve_reference_text`` keep working.
        from coderAI.tui import commands as command_adapter

        text = await asyncio.to_thread(
            command_adapter._resolve_reference_text,
            t,
            server.agent,
        )
    except ValueError as e:
        server.emit("warning", message=str(e))
        return
    except Exception as e:
        server.emit("warning", message=f"Reference failed: {e}")
        return
    server.emit("info", message=text)


async def _cmd_set_default_model(server: UIBridge, msg: dict[str, Any]) -> None:
    """Persist default_model in global config (like ``coderAI set-model``)."""
    from ..llm.factory import get_all_model_ids

    model_name = str(msg.get("model") or "").strip()
    if not model_name:
        server.emit("warning", message="Usage: /default <model>")
        return

    if model_name not in get_all_model_ids():
        server.emit(
            "warning",
            message=(
                f"Invalid model name: {model_name}. "
                "Use /models for groups; names must match provider IDs exactly."
            ),
        )
        return
    config_manager.set("default_model", model_name)
    current = server.agent.model
    if current != model_name:
        server.emit(
            "info",
            message=(
                f"Saved default model → {model_name}. "
                f"Current session is still using {current}; "
                f"use /model {model_name} to switch now."
            ),
        )
    else:
        server.emit(
            "info",
            message=f"Saved default model → {model_name} (already active).",
        )
