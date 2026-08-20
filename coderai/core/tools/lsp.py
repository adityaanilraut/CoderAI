"""LSP tool — Language Server Protocol code intelligence (goToDefinition, findReferences, goToImplementation, hover, documentSymbol)."""

from __future__ import annotations

import json
from typing import Any

from coderai.core.lsp.client import get_lsp_client
from coderai.core.tools.types import ToolResult, as_str

LSP_OPERATIONS = [
    "goToDefinition",
    "findReferences",
    "goToImplementation",
    "hover",
    "documentSymbol",
    "workspaceSymbol",
]


def handle_lsp_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Handle LSP query tool call."""
    operation = as_str(args.get("operation", "")).strip()
    file_path = as_str(args.get("file_path", "")).strip()
    line = args.get("line", 1)
    character = args.get("character", 1)

    if not operation:
        return ToolResult(
            ok=False,
            name="lsp",
            error=f"Missing required parameter `operation`. Allowed: {LSP_OPERATIONS}",
        )

    if operation not in LSP_OPERATIONS:
        return ToolResult(
            ok=False,
            name="lsp",
            error=f"Invalid operation `{operation}`. Allowed operations are: {LSP_OPERATIONS}",
        )

    if not file_path and operation != "workspaceSymbol":
        return ToolResult(
            ok=False,
            name="lsp",
            error="Missing required parameter `file_path`.",
        )

    try:
        line_num = int(line)
        char_num = int(character)
    except (ValueError, TypeError):
        return ToolResult(
            ok=False,
            name="lsp",
            error="Parameters `line` and `character` must be integers (1-based).",
        )

    project_root = getattr(context, "project_root", ".") if context else "."
    client = get_lsp_client(workspace_root=project_root)

    res = client.query(
        operation=operation,
        file_path=file_path,
        line=line_num,
        character=char_num,
        project_root=project_root,
    )

    if not res.get("ok"):
        return ToolResult(
            ok=False,
            name="lsp",
            error=res.get("error", "LSP query failed"),
            metadata=res,
        )

    # Format output for model readability
    if operation in ("goToDefinition", "goToImplementation"):
        locs = res.get("locations", [])
        if not locs:
            out = f"No definitions found for symbol at `{file_path}:{line_num}:{char_num}`."
        else:
            lines = [f"Found {len(locs)} definition(s):"]
            for loc in locs:
                lines.append(
                    f"- {loc.get('path')}:{loc.get('line')}:{loc.get('character')} | {loc.get('snippet', '')}"
                )
            out = "\n".join(lines)
    elif operation == "findReferences":
        locs = res.get("locations", [])
        if not locs:
            out = f"No references found for symbol at `{file_path}:{line_num}:{char_num}`."
        else:
            lines = [f"Found {len(locs)} reference(s):"]
            for loc in locs:
                lines.append(
                    f"- {loc.get('path')}:{loc.get('line')}:{loc.get('character')} | {loc.get('snippet', '')}"
                )
            out = "\n".join(lines)
    elif operation == "hover":
        hover_data = res.get("hover")
        if hover_data:
            out = hover_data.get("contents", "")
        else:
            out = (
                f"No hover information available for symbol at `{file_path}:{line_num}:{char_num}`."
            )
    elif operation == "documentSymbol":
        syms = res.get("symbols", [])
        if not syms:
            out = f"No symbols found in `{file_path}`."
        else:
            lines = [f"Symbols in {file_path}:"]
            for sym in syms:
                lines.append(f"- [{sym.get('kind')}] {sym.get('name')} (line {sym.get('line')})")
            out = "\n".join(lines)
    else:
        out = json.dumps(res, indent=2)

    return ToolResult(
        ok=True,
        name="lsp",
        output=out,
        metadata=res,
    )
