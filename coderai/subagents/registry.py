from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from coderai.subagents.models import BUILTIN_SUBAGENT_TYPES, SubagentTypeDefinition, ToolPolicyMode

logger = logging.getLogger(__name__)

# Map common tool aliases between PascalCase and snake_case (CoderAI Core)
TOOL_ALIASES: dict[str, set[str]] = {
    "read": {"read", "readfile", "read_file"},
    "readfile": {"read", "readfile", "read_file"},
    "readmediafile": {"readmediafile", "read_media_file", "read_media"},
    "write": {"write", "writefile", "write_file"},
    "writefile": {"write", "writefile", "write_file"},
    "edit": {"edit", "strreplacefile", "str_replace_file", "str_replace_editor"},
    "strreplacefile": {"edit", "strreplacefile", "str_replace_file", "str_replace_editor"},
    "glob": {"glob"},
    "grep": {"grep"},
    "bash": {"bash", "shell"},
    "shell": {"bash", "shell"},
    "websearch": {"websearch", "searchweb", "web_search", "search_web"},
    "searchweb": {"websearch", "searchweb", "web_search", "search_web"},
    "webfetch": {"webfetch", "fetchurl", "web_fetch", "fetch_url"},
    "fetchurl": {"webfetch", "fetchurl", "web_fetch", "fetch_url"},
    "think": {"think"},
    "askuserquestion": {"askuserquestion", "ask_user_question"},
    "todo_write": {"todo_write", "settodolist", "set_todo_list", "update_plan"},
    "settodolist": {"todo_write", "settodolist", "set_todo_list", "update_plan"},
    "subagent": {"subagent", "agent"},
    "agent": {"subagent", "agent"},
}


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str] | None:
    """Parse YAML frontmatter from markdown content. Fails closed on syntax errors."""
    fm_pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
    match = fm_pattern.match(content)
    if not match:
        return {}, content.strip()
    fm_text, body = match.group(1), match.group(2).strip()
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(fm_text)
        if isinstance(data, dict):
            return data, body
        logger.warning("Frontmatter root is not a YAML dictionary; rejecting spec.")
        return None
    except Exception as exc:
        logger.warning("YAML parse error in frontmatter: %s", exc)
        return None


def parse_markdown_agent_spec(file_path: Path) -> SubagentTypeDefinition | None:
    """Parse a Markdown agent role spec (.md with YAML frontmatter)."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    parsed = _parse_frontmatter(content)
    if parsed is None:
        return None
    meta, body = parsed

    name = str(meta.get("name") or file_path.stem).strip().lower()
    description = str(meta.get("description") or f"Specialized {name} agent").strip()
    tools_meta = meta.get("tools")
    allowed_tools: tuple[str, ...] | None = None
    if isinstance(tools_meta, (list, tuple)):
        allowed_tools = tuple(str(t).strip() for t in tools_meta if t and str(t).strip())
    elif isinstance(tools_meta, str):
        allowed_tools = tuple(x.strip() for x in tools_meta.split(",") if x.strip())
    elif tools_meta is not None:
        allowed_tools = ()

    raw_mode = meta.get("mode")
    if raw_mode:
        mode = str(raw_mode).strip().lower()
    elif allowed_tools is not None and (
        len(allowed_tools) == 0
        or all(
            t.lower() in ("read", "readfile", "read_file", "grep", "glob", "websearch", "webfetch")
            for t in allowed_tools
        )
    ):
        mode = "read_only"
    else:
        mode = "general"

    raw_model = meta.get("model")
    model = str(raw_model).strip() if raw_model else None

    raw_exclude = meta.get("exclude_tools")
    if raw_exclude is None:
        exclude_tools: tuple[str, ...] = ()
    elif isinstance(raw_exclude, str):
        exclude_tools = (raw_exclude.strip(),) if raw_exclude.strip() else ()
    elif isinstance(raw_exclude, (list, tuple)):
        exclude_tools = tuple(str(t).strip() for t in raw_exclude if t and str(t).strip())
    else:
        exclude_tools = ()

    raw_bg = meta.get("supports_background", True)
    if isinstance(raw_bg, str):
        supports_background = raw_bg.strip().lower() not in ("false", "0", "no", "off")
    else:
        supports_background = bool(raw_bg)

    return SubagentTypeDefinition(
        name=name,
        description=description,
        when_to_use=str(meta.get("when_to_use") or description),
        allowed_tools=allowed_tools,
        exclude_tools=exclude_tools,
        supports_background=supports_background,
        system_prompt=body,
        mode=mode,
        model=model,
        source=f"custom:{file_path.name}",
        source_path=file_path.resolve(),
    )


def discover_custom_agents(project_root: str | None = None) -> dict[str, SubagentTypeDefinition]:
    """Scan project and user directories for custom agent specs (.coderai/agents/, .agents/agents/)."""
    discovered: dict[str, SubagentTypeDefinition] = {}
    candidate_dirs: list[Path] = []

    root_str = project_root or os.environ.get("CODERAI_PROJECT_ROOT") or "."
    root = Path(root_str).resolve()
    candidate_dirs.extend(
        [
            root / ".coderai" / "agents",
            root / ".agents" / "agents",
        ]
    )

    home = Path.home()
    candidate_dirs.extend(
        [
            home / ".coderai" / "agents",
            home / ".agents" / "agents",
        ]
    )

    cwd = Path.cwd().resolve()
    if cwd != root and cwd != home:
        candidate_dirs.extend(
            [
                cwd / ".coderai" / "agents",
                cwd / ".agents" / "agents",
            ]
        )

    for cdir in candidate_dirs:
        if not cdir.is_dir():
            continue
        for md_file in sorted(cdir.glob("*.md")):
            if md_file.is_file() and not md_file.name.startswith("."):
                defn = parse_markdown_agent_spec(md_file)
                if defn and defn.name not in discovered:
                    discovered[defn.name] = defn
    return discovered


def discover_markdown_agents(
    project_root: str | Path | None = None,
) -> list[SubagentTypeDefinition]:
    """Return a list of all discovered markdown agent definitions."""
    return list(discover_custom_agents(str(project_root) if project_root else None).values())


_parse_markdown_agent_file = parse_markdown_agent_spec


def _load_definitions(project_root: str | None = None) -> dict[str, SubagentTypeDefinition]:
    """Load builtin types from bundled YAML and custom roles from workspace/user markdown specs."""
    defs: dict[str, SubagentTypeDefinition] = {}
    try:
        from coderai.agentspec import DEFAULT_AGENT_FILE, load_agent_spec

        root = load_agent_spec(DEFAULT_AGENT_FILE)
        for name, ref in root.subagents.items():
            path = ref.get("path", "")
            try:
                from coderai.agentspec import load_agent_spec as _load

                spec = _load(Path(path)) if path else None
            except Exception as exc:
                logger.warning("Failed to load subagent spec %s from %s: %s", name, path, exc)
                spec = None

            role_name = str(name)
            mode = "read_only" if role_name in ("explore", "plan") else "general"
            sys_prompt = ""
            if spec and getattr(spec, "system_prompt_args", None):
                sys_prompt = spec.system_prompt_args.get("ROLE_ADDITIONAL") or ""

            defs[role_name] = SubagentTypeDefinition(
                name=role_name,
                description=str(ref.get("description", "")),
                when_to_use=spec.when_to_use if spec else "",
                allowed_tools=tuple(spec.allowed_tools) if spec and spec.allowed_tools else None,
                exclude_tools=tuple(spec.exclude_tools) if spec else (),
                system_prompt=sys_prompt,
                mode=mode,
                model=getattr(spec, "model", None) if spec else None,
                source="builtin",
            )
    except Exception as exc:
        logger.warning("Failed to load builtin subagent definitions: %s", exc)

    # Discover custom Markdown agents
    custom = discover_custom_agents(project_root)
    for name, defn in custom.items():
        if name not in defs:
            defs[name] = defn
        else:
            logger.warning(
                "Discovered custom agent %r at %s is shadowed by builtin subagent definition.",
                name,
                defn.source_path,
            )

    return defs


def format_subagent_types_description(project_root: str | None = None) -> str:
    """Format a dynamic description of all available subagent types for tool definitions."""
    defs = list_subagent_types(project_root)
    items: list[str] = []
    for d in defs:
        desc = d.when_to_use or d.description
        items.append(f"{d.name} ({desc})" if desc else d.name)
    return (
        "Builtin and discovered agent flavors: " + ", ".join(items)
        if items
        else "coder, explore, plan."
    )


def list_subagent_types(project_root: str | None = None) -> list[SubagentTypeDefinition]:
    """Return all available subagent type definitions (builtins + discovered workspace roles)."""
    defs = _load_definitions(project_root)
    # Order: builtins first, then custom alphabetically
    res: list[SubagentTypeDefinition] = []
    for name in BUILTIN_SUBAGENT_TYPES:
        if name in defs:
            res.append(defs[name])
    for name in sorted(defs.keys()):
        if name not in BUILTIN_SUBAGENT_TYPES:
            res.append(defs[name])
    return res


def get_subagent_definition(
    name: str, project_root: str | None = None
) -> SubagentTypeDefinition | None:
    """Look up a subagent definition by name (case-insensitive)."""
    if not name:
        return None
    defs = _load_definitions(project_root)
    name_clean = name.strip().lower()
    for k, v in defs.items():
        if k.lower() == name_clean:
            return v
    return None


def resolve_tool_policy(
    subagent_type: str | None,
    requested: list[str] | tuple[str, ...] | None = None,
    project_root: str | None = None,
) -> tuple[ToolPolicyMode, tuple[str, ...]]:
    """Resolve the effective tool policy for a subagent launch.

    Returns ``(mode, tools)`` where ``inherit`` means no restriction.
    An explicit ``requested`` allowlist always wins. Deny-by-default: an
    explicitly named but unknown subagent type resolves to an empty
    allowlist (nothing permitted) instead of unrestricted ``inherit``; only
    an absent type (no restriction requested) inherits.
    """
    if requested is not None:
        return "allowlist", tuple(requested)
    if not subagent_type:
        return "inherit", ()
    definition = get_subagent_definition(subagent_type, project_root)
    if definition is None:
        return "allowlist", ()
    if definition.allowed_tools is None:
        return "inherit", ()
    return "allowlist", tuple(definition.allowed_tools)


def is_tool_allowed(tool_name: str, mode: ToolPolicyMode, tools: tuple[str, ...]) -> bool:
    """Whether ``tool_name`` may run under a resolved policy with alias harmonization."""
    if mode == "inherit":
        return True
    if not tools:
        return False
    t_clean = tool_name.strip().lower()
    # Direct match or alias match
    known_aliases = TOOL_ALIASES.get(t_clean, {t_clean})
    for allowed in tools:
        a_clean = allowed.strip().lower()
        allowed_aliases = TOOL_ALIASES.get(a_clean, {a_clean})
        if t_clean == a_clean or (known_aliases & allowed_aliases):
            return True
    return False


async def build_explore_extra_context(project_root: str) -> str:
    """Git context block for explore prompts (``""`` when unavailable)."""
    try:
        from coderai.subagents.git_context import collect_git_context

        return await collect_git_context(project_root)
    except Exception:
        return ""
