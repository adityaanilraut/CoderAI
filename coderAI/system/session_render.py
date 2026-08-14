"""Session transcript renderers (HTML / Markdown).

Lives under ``system/`` so tools do not import the TUI package.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from coderAI.system.history import Session


def session_to_markdown(session: "Session", title: Optional[str] = None) -> str:
    """Render a Session instance to comprehensive Markdown."""
    created_dt = datetime.fromtimestamp(session.created_at, tz=timezone.utc).isoformat()
    lines = [
        f"# {title or session.name or session.session_id}",
        "",
        f"- **Session ID**: `{session.session_id}`",
        f"- **Model**: `{session.model}`",
        f"- **Created**: {created_dt}",
        f"- **Total Tokens**: {session.total_tokens:,} (Prompt: {session.prompt_tokens:,} · Completion: {session.completion_tokens:,})",
        f"- **Total Cost**: ${session.total_cost_usd:.4f}",
        "",
        "---",
        "",
    ]

    for i, msg in enumerate(session.messages, 1):
        role_label = msg.role.capitalize()
        lines.append(f"### Turn {i}: {role_label}")
        if msg.reasoning_content:
            lines.append("<details>")
            lines.append(
                f"<summary>🧠 Reasoning ({len(msg.reasoning_content):,} chars)</summary>\n"
            )
            lines.append(msg.reasoning_content)
            lines.append("</details>\n")

        if msg.content:
            lines.append(msg.content)
            lines.append("")

        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "tool")
                args = fn.get("arguments", "{}")
                lines.append(f"**Tool Call**: `{name}`")
                lines.append("```json")
                lines.append(args if isinstance(args, str) else str(args))
                lines.append("```\n")

        if msg.name:  # Tool result message
            lines.append(f"**Tool Result (`{msg.name}`)**:")
            lines.append("```")
            lines.append(str(msg.content or ""))
            lines.append("```\n")

        lines.append("---\n")

    return "\n".join(lines)


def session_to_html(session: "Session", title: Optional[str] = None) -> str:
    """Render a Session instance to a modern self-contained dark-mode HTML report."""
    created_dt = datetime.fromtimestamp(session.created_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    disp_title = title or session.name or session.session_id

    messages_html: list[str] = []
    for i, msg in enumerate(session.messages, 1):
        role = msg.role.lower()
        role_badge_class = (
            "badge-user"
            if role == "user"
            else "badge-assistant"
            if role == "assistant"
            else "badge-tool"
        )

        msg_body: list[str] = []
        if msg.reasoning_content:
            msg_body.append(
                f'<details class="reasoning-box"><summary>🧠 Reasoning ({len(msg.reasoning_content):,} chars)</summary>'
                f"<pre>{html.escape(msg.reasoning_content)}</pre></details>"
            )

        if msg.content:
            msg_body.append(
                f'<div class="content-text">{html.escape(msg.content).replace(chr(10), "<br>")}</div>'
            )

        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                t_name = fn.get("name", "tool")
                t_args = fn.get("arguments", "{}")
                msg_body.append(
                    f'<div class="tool-call-box"><strong>⚡ Tool Call:</strong> <code>{html.escape(str(t_name))}</code>'
                    f"<pre>{html.escape(str(t_args))}</pre></div>"
                )

        if msg.name:
            msg_body.append(
                f'<div class="tool-result-box"><strong>📦 Result from <code>{html.escape(str(msg.name))}</code>:</strong>'
                f"<pre>{html.escape(str(msg.content or ''))}</pre></div>"
            )

        messages_html.append(
            f'<div class="message-card message-{role}">'
            f'<div class="message-header"><span class="badge {role_badge_class}">{html.escape(role.upper())}</span>'
            f'<span class="turn-num">#{i}</span></div>'
            f"{''.join(msg_body)}</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(disp_title)} - CoderAI Session</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --card-bg: #1e293b;
      --card-user: #172554;
      --card-assistant: #1e293b;
      --card-tool: #0f172a;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #3b82f6;
      --success: #10b981;
      --border: #334155;
      --code-bg: #090d16;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
      margin: 0;
      padding: 2rem 1rem;
    }}
    .container {{
      max-width: 900px;
      margin: 0 auto;
    }}
    header {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.5rem 2rem;
      margin-bottom: 2rem;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
    }}
    h1 {{
      margin-top: 0;
      color: #60a5fa;
      font-size: 1.75rem;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin-top: 1rem;
      font-size: 0.9rem;
    }}
    .meta-item strong {{
      display: block;
      color: var(--text-muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .message-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.25rem;
    }}
    .message-user {{
      background: var(--card-user);
      border-color: #1e40af;
    }}
    .message-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.75rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.2rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: bold;
      letter-spacing: 0.05em;
    }}
    .badge-user {{ background: #2563eb; color: #fff; }}
    .badge-assistant {{ background: #059669; color: #fff; }}
    .badge-tool {{ background: #475569; color: #cbd5e1; }}
    .turn-num {{ color: var(--text-muted); font-size: 0.8rem; }}
    pre {{
      background: var(--code-bg);
      border: 1px solid var(--border);
      padding: 0.75rem 1rem;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 0.85rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: #93c5fd;
    }}
    .reasoning-box, .tool-call-box, .tool-result-box {{
      margin-top: 0.75rem;
      background: rgba(0,0,0,0.2);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.75rem;
    }}
    summary {{
      cursor: pointer;
      color: #fbbf24;
      font-weight: 500;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>{html.escape(disp_title)}</h1>
      <div class="meta-grid">
        <div class="meta-item"><strong>Session ID</strong><code>{html.escape(session.session_id)}</code></div>
        <div class="meta-item"><strong>Model</strong>{html.escape(session.model)}</div>
        <div class="meta-item"><strong>Created</strong>{html.escape(created_dt)}</div>
        <div class="meta-item"><strong>Total Tokens</strong>{session.total_tokens:,}</div>
        <div class="meta-item"><strong>Total Cost</strong>${session.total_cost_usd:.4f}</div>
      </div>
    </header>
    <main>
      {"".join(messages_html)}
    </main>
  </div>
</body>
</html>
"""
