"""Isolated side-question calls.

``run_side_question`` answers a question against a read-only snapshot of the
conversation history. It never mutates the session: no tools, text-only.
``inject_dmail_and_continue`` appends a D-Mail directive and re-activates the
turn so the agent obeys it on the next steps.
"""

from __future__ import annotations

import asyncio
from typing import Any

BTW_SYSTEM = (
    "You are answering a quick side question about an ongoing coding session. "
    "You see the conversation history for context but you MUST NOT modify anything. "
    "Answer concisely in plain text. No tools are available."
)

# The side instance is told tools are visible only
# for prompt-cache alignment and must never be called (maxTurns=2 retry).
SIDE_QUESTION_SYSTEM_REMINDER = """\
This is a side question from the user. Answer directly in a single response.

IMPORTANT:
- You are a separate, lightweight instance answering one question.
- The main agent continues independently — do NOT reference being interrupted.
- Do NOT call any tools. All tool calls are disabled and will be rejected.
- Respond ONLY with text based on what you already know from the conversation.
- This is a one-off response — no follow-up turns.
- If you don't know the answer, say so directly."""

DMAIL_SYSTEM_PREFIX = "[D-Mail / time-leap directive — obey immediately] "


def build_side_messages(
    history: list[dict[str, str]], question: str, limit: int = 20
) -> list[dict[str, str]]:
    """Build a read-only message list: system + tail history + question."""
    msgs: list[dict[str, str]] = [{"role": "system", "content": BTW_SYSTEM}]
    tail = history[-limit:] if len(history) > limit else history
    for m in tail:
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system"):
            continue
        content = str(m.get("content", ""))[:4000]
        if content.strip():
            msgs.append({"role": role, "content": content})
    # Side instance gets the no-tools reminder + question as one
    # user message, so cache-friendly history stays intact.
    msgs.append(
        {
            "role": "user",
            "content": f"<system-reminder>\n{SIDE_QUESTION_SYSTEM_REMINDER}\n</system-reminder>\n\n{question[:4000]}",
        }
    )
    return msgs


async def run_side_question(
    mgr: Any,
    session_id: str | None,
    question: str,
    *,
    timeout_s: float = 90.0,
    on_chunk: Any | None = None,
) -> str:
    """Answer a side question without mutating the session. Returns text."""
    history: list[dict[str, str]] = []
    try:
        if session_id:
            for m in mgr.list_session_messages(session_id)[-20:]:
                if getattr(m, "role", "") in ("user", "assistant", "system"):
                    history.append(
                        {"role": m.role, "content": str(getattr(m, "content", ""))}
                    )
    except Exception:
        pass
    messages = build_side_messages(history, question)

    # BtwBegin emitted before the call, BtwEnd after.
    bid = ""
    try:
        from coderai.wire.emitter import get_emitter

        bid = get_emitter().btw_begin(question[:500])
    except Exception:
        bid = ""

    def _call() -> str:
        from coderai.llm import create_openai_client

        info = mgr.create_openai_client() if hasattr(mgr, "create_openai_client") else create_openai_client(mgr.project_root if hasattr(mgr, "project_root") else ".")
        client = info.get("client")
        model = info.get("model")
        if client is None:
            raise RuntimeError("API key not found — cannot answer side question.")
        resp = client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.2,
            timeout=timeout_s,
        )
        text = ""
        try:
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            text = ""
        return text or "(no answer)"

    try:
        answer = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout_s + 10)
    except Exception as e:
        try:
            from coderai.wire.emitter import get_emitter

            get_emitter().btw_end(bid, None, str(e))
        except Exception:
            pass
        raise RuntimeError(f"side question failed: {e}") from e
    if on_chunk is not None:
        try:
            if asyncio.iscoroutinefunction(on_chunk):
                await on_chunk(answer)
            else:
                on_chunk(answer)
        except Exception:
            pass
    try:
        from coderai.wire.emitter import get_emitter

        get_emitter().btw_end(bid, answer, None)
    except Exception:
        pass
    return answer


async def inject_dmail_and_continue(mgr: Any, session_id: str, message: str) -> None:
    """Append a D-Mail directive message and re-activate the turn."""
    text = message.strip()
    if not text:
        return
    mgr._append_message(
        mgr._build_message(
            session_id,
            "user",
            f"{DMAIL_SYSTEM_PREFIX}{text[:4000]}",
            meta={"isDMail": True},
        )
    )
    await mgr._activate(session_id)
