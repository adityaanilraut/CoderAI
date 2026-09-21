from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from coderai.subagents.models import BUILTIN_SUBAGENT_TYPES, SubagentTypeDefinition, ToolPolicyMode

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


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content."""
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
    except Exception:
        pass

    # Fallback key-value parsing
    data = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip().strip("\"'")
            if val.startswith("[") and val.endswith("]"):
                items = [x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()]
                data[key] = items
            else:
                data[key] = val
    return data, body


def parse_markdown_agent_spec(file_path: Path) -> SubagentTypeDefinition | None:
    """Parse a Markdown agent role spec (.md with YAML frontmatter)."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    meta, body = _parse_frontmatter(content)
    name = str(meta.get("name") or file_path.stem).strip().lower()
    description = str(meta.get("description") or f"Specialized {name} agent").strip()
    tools_meta = meta.get("tools")
    allowed_tools: tuple[str, ...] | None = None
    if isinstance(tools_meta, list):
        allowed_tools = tuple(str(t) for t in tools_meta if t)
    mode = str(
        meta.get("mode")
        or (
            "read_only"
            if allowed_tools
            and all(t.lower() in ("read", "readfile", "grep", "glob") for t in allowed_tools)
            else "general"
        )
    )

    return SubagentTypeDefinition(
        name=name,
        description=description,
        when_to_use=str(meta.get("when_to_use") or description),
        allowed_tools=allowed_tools,
        exclude_tools=tuple(str(t) for t in meta.get("exclude_tools", [])),
        supports_background=bool(meta.get("supports_background", True)),
        system_prompt=body,
        mode=mode,
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
            except Exception:
                spec = None
            defs[str(name)] = SubagentTypeDefinition(
                name=str(name),
                description=str(ref.get("description", "")),
                when_to_use=spec.when_to_use if spec else "",
                allowed_tools=tuple(spec.allowed_tools) if spec and spec.allowed_tools else None,
                exclude_tools=tuple(spec.exclude_tools) if spec else (),
                source="builtin",
            )
    except Exception:
        pass

    # Discover custom Markdown agents
    custom = discover_custom_agents(project_root)
    for name, defn in custom.items():
        if name not in defs:
            defs[name] = defn

    return defs


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
    subagent_type: str | None, requested: list[str] | None = None, project_root: str | None = None
) -> tuple[ToolPolicyMode, tuple[str, ...]]:
    """Resolve the effective tool policy for a subagent launch.

    Returns ``(mode, tools)`` where ``inherit`` means no restriction.
    An explicit ``requested`` allowlist always wins. Deny-by-default: an
    explicitly named but unknown subagent type resolves to an empty
    allowlist (nothing permitted) instead of unrestricted ``inherit``; only
    an absent type (no restriction requested) inherits.
    """
    if requested:
        return "allowlist", tuple(requested)
    if not subagent_type:
        return "inherit", ()
    definition = get_subagent_definition(subagent_type, project_root)
    if definition is None:
        return "allowlist", ()
    if definition.allowed_tools is None:
        return "inherit", ()
    return "allowlist", definition.allowed_tools


def is_tool_allowed(tool_name: str, mode: ToolPolicyMode, tools: tuple[str, ...]) -> bool:
    """Whether ``tool_name`` may run under a resolved policy with alias harmonization."""
    if mode == "inherit":
        return True
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
