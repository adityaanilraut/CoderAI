"""TUI timeline export, plus re-exports of session transcript renderers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from coderAI.system.session_render import session_to_html, session_to_markdown

__all__ = ["timeline_to_markdown", "session_to_html", "session_to_markdown"]


def timeline_to_markdown(items: list[dict[str, Any]]) -> str:
    md = "# CoderAI Session\n\n"
    md += f"Exported: {datetime.now(timezone.utc).isoformat()}\n\n---\n\n"
    for item in items:
        kind = item.get("kind")
        if kind == "user":
            md += f"**You:**\n\n{item.get('text', '')}\n\n---\n\n"
        elif kind == "assistant":
            md += f"**Assistant:**\n\n{item.get('content', '')}\n"
            reasoning = (item.get("reasoning") or "").strip()
            if reasoning:
                md += (
                    f"\n<details><summary>Reasoning ({len(reasoning):,} chars)</summary>\n\n"
                    f"{reasoning}\n\n</details>\n"
                )
            md += "\n---\n\n"
        elif kind == "tool":
            ok = item.get("ok")
            mark = "✓" if ok else "✗" if ok is False else "…"
            md += f"**Tool:** `{item.get('name', '')}` — {mark}\n\n"
            if item.get("preview"):
                md += "> " + str(item["preview"]).replace("\n", "\n> ") + "\n"
            if item.get("error"):
                md += f"> {item['error']}\n"
            md += "\n---\n\n"
        elif kind == "diff":
            md += f"**Diff:** `{item.get('path', '')}`\n\n```diff\n{item.get('diff', '')}\n```\n\n---\n\n"
        elif kind == "error":
            md += f"**Error:** {item.get('message', '')}\n"
            if item.get("details"):
                md += f"\n```\n{item['details']}\n```\n"
            md += "\n---\n\n"
        elif kind == "toast":
            md += f"**Toast ({item.get('level', 'info')}):** {item.get('message', '')}\n\n---\n\n"
        elif kind == "separator":
            md += f"*{item.get('message', '')}*\n\n---\n\n"
        elif kind == "welcome":
            md += f"**Welcome:** model `{item.get('model', '')}` provider `{item.get('provider', '')}`\n\n"
            if item.get("cwd"):
                md += f"CWD: `{item.get('cwd')}`\n\n"
            md += "---\n\n"
        elif kind == "skill_card":
            md += f"**Skill:** `{item.get('name', '')}`\n\n{item.get('description', '')}\n\n---\n\n"
        elif kind == "plan_card":
            md += f"**Plan:**\n\n{item.get('markdown', '')}\n\n---\n\n"
        elif kind == "approval":
            md += f"**Approval:** `{item.get('tool', '')}` — {item.get('decided', 'pending')}\n\n"
            if item.get("risk"):
                md += f"Risk: {item.get('risk')}\n\n"
            md += "---\n\n"
        else:
            md += f"**{kind or 'unknown'}:**\n\n```json\n{item}\n```\n\n---\n\n"
    return md
