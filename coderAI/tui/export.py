"""Session export utilities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def timeline_to_markdown(items: List[Dict[str, Any]]) -> str:
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
    return md
