"""SendDMail tool.

Stages a validated ``(message, checkpoint_id)`` on the live session. The turn
loop rewinds the context to that checkpoint and injects the directive.
Without a live manager the directive is returned as a follow-up message.
"""

from __future__ import annotations

from typing import Any

# Bound before the soul import so ``import coderai.tools.dmail`` never
# triggers a circular import (NAME must exist before any soul import runs).
NAME = "SendDMail"

from coderai.soul.denwarenji import DenwaRenjiError
from coderai.tools.legacy.types import ToolResult

DMAIL_PREFIX = "[D-Mail / time-leap directive — obey immediately] "

# Re-exported so existing ``from coderai.tools.dmail import DenwaRenjiError``
# imports keep working after the move to ``core/denwarenji.py``.
__all__ = [
    "NAME",
    "DenwaRenjiError",
    "DMAIL_PREFIX",
    "format_dmail",
    "handle_send_dmail_tool",
]


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
        if mgr is not None and session_id and hasattr(mgr, "stage_dmail"):
            try:
                mgr.stage_dmail(session_id, message.strip()[:4000], checkpoint_id)
            except DenwaRenjiError as exc:
                return ToolResult(ok=False, name="SendDMail", error=str(exc))
            # The turn loop rewinds to the checkpoint and injects the directive.
            # This text is what the model sees only when the rewind does not happen.
            return ToolResult(
                ok=True,
                name="SendDMail",
                output=(
                    "If you see this message, the D-Mail was NOT sent successfully. "
                    "This may be because some other tool that needs approval was rejected."
                ),
                metadata={"brief": "El Psy Kongroo"},
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
