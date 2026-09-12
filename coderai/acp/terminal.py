"""ACP-client terminal bridge over :mod:`coderai.terminal.manager`.

The ACP protocol lets *agents* drive *client*-hosted terminals; it has no
standard method for clients to drive agent-hosted PTYs. This module fills
that gap: :class:`TerminalBridge` exposes the server-side persistent
:class:`~coderai.terminal.manager.TerminalManager` to ACP clients through
``ext_method`` (``terminal/list|open|send|read|signal|close``), namespaced
per ACP session so concurrent sessions cannot touch each other's terminals.
"""

from __future__ import annotations

from typing import Any

from coderai.utils.logging import logger

#: ext_method names served by the bridge (``terminal/<op>``).
TERMINAL_METHODS = ("list", "open", "send", "read", "signal", "close")

_DEFAULT_READ_TIMEOUT_MS = 2000
_DEFAULT_SEND_TIMEOUT_MS = 10000
_MAX_TIMEOUT_MS = 30000


def _clamp_timeout_ms(value: Any, default: int) -> float:
    try:
        timeout_ms = float(value)
    except (TypeError, ValueError):
        return default / 1000.0
    if timeout_ms <= 0:
        return default / 1000.0
    return min(timeout_ms, _MAX_TIMEOUT_MS) / 1000.0


class TerminalBridge:
    """Per-ACP-session view over the process-global ``TerminalManager``.

    :param acp_session_id: owning ACP session; terminal names are prefixed
        with it and listing is filtered to owned terminals.
    :param work_dir: default cwd for terminals opened without one.
    :param manager: injectable manager (tests); defaults to the singleton.
    """

    def __init__(
        self,
        acp_session_id: str,
        work_dir: str = ".",
        manager: Any = None,
    ) -> None:
        self._acp_session_id = acp_session_id
        self._work_dir = work_dir or "."
        if manager is None:
            from coderai.terminal.manager import get_terminal_manager

            manager = get_terminal_manager()
        self._manager = manager
        self._owned: set[str] = set()

    # -- helpers ---------------------------------------------------------
    def _prefix(self, name: str) -> str:
        return f"acp:{self._acp_session_id}:{name}" if name else f"acp:{self._acp_session_id}"

    def _own(self, term: Any) -> bool:
        return getattr(term, "session_id", None) in self._owned

    def _lookup(self, session_id: str) -> Any | None:
        term = self._manager.get_session(session_id)
        if term is not None and not self._own(term):
            return None
        return term

    @staticmethod
    def _status(term: Any) -> dict[str, Any]:
        status = term.status()
        return status.to_dict() if hasattr(status, "to_dict") else dict(status)

    # -- ops -------------------------------------------------------------
    def list_terminals(self) -> dict[str, Any]:
        terminals = [
            self._status(term)
            for term_id in sorted(self._owned)
            if (term := self._manager.get_session(term_id)) is not None
        ]
        return {"terminals": terminals}

    def open_terminal(
        self,
        command: Any = None,
        name: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            term = self._manager.open_session(
                command=command,
                name=self._prefix(name or ""),
                cwd=cwd or self._work_dir,
                env=dict(env) if isinstance(env, dict) else None,
            )
        except Exception as exc:
            logger.warning("ACP terminal open failed: %s", exc)
            return {"error": f"Failed to open terminal: {exc}"}
        self._owned.add(term.session_id)
        info = self._status(term)
        try:
            info["output"] = term.read_available(timeout_s=0.2)
        except Exception:
            info["output"] = ""
        return info

    def send_terminal(
        self,
        session_id: str,
        text: str = "",
        submit: bool = True,
        timeout_ms: Any = None,
    ) -> dict[str, Any]:
        term = self._lookup(session_id)
        if term is None:
            return {"error": f"Terminal session `{session_id}` not found."}
        try:
            term.send(text, submit=bool(submit))
            output = term.read_available(timeout_s=_clamp_timeout_ms(timeout_ms, _DEFAULT_SEND_TIMEOUT_MS))
        except Exception as exc:
            return {"error": f"Failed to send to terminal `{session_id}`: {exc}"}
        return {
            "sessionId": session_id,
            "output": output,
            "isAlive": term.is_alive,
            "exitCode": term.exit_code,
        }

    def read_terminal(self, session_id: str, timeout_ms: Any = None) -> dict[str, Any]:
        term = self._lookup(session_id)
        if term is None:
            return {"error": f"Terminal session `{session_id}` not found."}
        try:
            output = term.read_available(
                timeout_s=_clamp_timeout_ms(timeout_ms, _DEFAULT_READ_TIMEOUT_MS)
            )
        except Exception as exc:
            return {"error": f"Failed to read terminal `{session_id}`: {exc}"}
        return {
            "sessionId": session_id,
            "output": output,
            "isAlive": term.is_alive,
            "exitCode": term.exit_code,
        }

    def signal_terminal(self, session_id: str, signal: str = "SIGINT") -> dict[str, Any]:
        term = self._lookup(session_id)
        if term is None:
            return {"error": f"Terminal session `{session_id}` not found."}
        try:
            term.send_signal(signal)
        except Exception as exc:
            return {"error": f"Failed to signal terminal `{session_id}`: {exc}"}
        return {
            "sessionId": session_id,
            "signal": signal,
            "isAlive": term.is_alive,
            "exitCode": term.exit_code,
        }

    def close_terminal(self, session_id: str) -> dict[str, Any]:
        term = self._lookup(session_id)
        if term is None:
            return {"error": f"Terminal session `{session_id}` not found."}
        try:
            closed = self._manager.close_session(term.session_id)
        except Exception as exc:
            return {"error": f"Failed to close terminal `{session_id}`: {exc}"}
        self._owned.discard(term.session_id)
        if not closed:
            return {"error": f"Terminal session `{session_id}` not found or already closed."}
        return {"sessionId": session_id, "closed": True}


__all__ = ["TERMINAL_METHODS", "TerminalBridge"]
