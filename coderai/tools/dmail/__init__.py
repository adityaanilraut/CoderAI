"""SendDMail tool.

DenwaRenji steering: ``(message, checkpoint_id)`` is validated against the
soul's checkpoint count (``core/denwarenji.py``) so the directive re-steers
from a known-good point, then injected into the live context ("El Psy
Kongroo"). Without a live manager the directive returns as a follow-up user
message so the loop still obeys it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from coderai.soul.denwarenji import DenwaRenjiError, DMail
from coderai.tools.legacy.types import ToolResult

DMAIL_PREFIX = "[D-Mail / time-leap directive — obey immediately] "

# Re-exported so existing ``from coderai.tools.dmail import DenwaRenjiError``
# imports keep working after the move to ``core/denwarenji.py``.
__all__ = ["DenwaRenjiError", "DMAIL_PREFIX", "format_dmail", "handle_send_dmail_tool"]


def format_dmail(message: str) -> str:
    return f"{DMAIL_PREFIX}{message.strip()[:4000]}"


def _coerce_checkpoint_id(raw: Any) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def handle_send_dmail_tool(args: dict[str, Any], context: Any) -> ToolResult:
    message = args.get("message") or args.get("content") or args.get("text")
    if not isinstance(message, str) or not message.strip():
        return ToolResult(ok=False, name="SendDMail", error="message must be a non-empty string.")
    checkpoint_id = _coerce_checkpoint_id(args.get("checkpoint_id", 0))
    directive = format_dmail(message)
    try:
        session_id = getattr(context, "session_id", None) or getattr(context, "sid", None)
        mgr = getattr(context, "manager", None) or getattr(context, "session_manager", None)
        if mgr is not None and session_id:
            # Phase 2: validate against the soul's checkpoint space so a
            # stale checkpoint_id fails loudly instead of steering nowhere.
            try:
                from coderai.soul.denwarenji import DenwaRenji

                soul = mgr.get_soul(session_id) if hasattr(mgr, "get_soul") else None
                renji = DenwaRenji()
                n_ckpt = soul.checkpoint_count() if soul is not None else 1
                renji.set_n_checkpoints(max(1, n_ckpt))
                renji.send_dmail(DMail(message=message.strip()[:4000], checkpoint_id=checkpoint_id))
                renji.fetch_pending_dmail()
            except DenwaRenjiError as exc:
                return ToolResult(ok=False, name="SendDMail", error=str(exc))
            except Exception:
                pass
            mgr._append_message(
                mgr._build_message(
                    session_id,
                    "user",
                    directive,
                    meta={"isDMail": True, "checkpointId": checkpoint_id},
                )
            )
            # Re-activate Schannel-style: schedule continuation without
            # blocking the tool result path.
            try:
                activate = getattr(mgr, "_activate", None)
                if callable(activate):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(activate(session_id))
                    except RuntimeError:
                        pass
            except Exception:
                pass
            return ToolResult(
                ok=True,
                name="SendDMail",
                output="El Psy Kongroo — directive injected; turn re-activated.",
            )
    except Exception as e:
        return ToolResult(ok=False, name="SendDMail", error=f"Failed to send D-Mail: {e}")
    # No live manager (offline/test): return directive as follow-up so the
    # agent still obeys it on the next step.
    res = ToolResult(
        ok=True,
        name="SendDMail",
        output="El Psy Kongroo — D-Mail queued as follow-up.",
    )
    try:
        res.follow_up_messages = [{"role": "user", "content": directive}]
    except Exception:
        pass
    return res
