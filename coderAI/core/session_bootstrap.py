"""Shared Agent/session bootstrap for the TUI and headless entry points.

Phase 4.5 of the architecture remediation. ``--resume``/``--continue``
resolution, load-vs-create session, and delegate-tool wiring were implemented
three times (``tui/session_setup``, ``cli/run_cmd``, ``cli/main``); this module
is the single owner. The TUI layers bridge/streaming wiring on top of the
returned agent; the headless path adds its ``confirmation_override``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from coderAI.core.agent import Agent
from coderAI.system.history import history_manager

logger = logging.getLogger(__name__)

# Callback used by the TUI to surface a non-fatal warning (e.g. a resume that
# failed and fell back to a fresh session) through its event sink.
WarnFn = Callable[[str], None]


class BootstrapError(Exception):
    """A user-facing bootstrap failure that carries a process exit code.

    Lets each caller render the message in its own idiom (click echo / TUI
    event) without re-deriving whether it's a usage error (2) or runtime (1).
    """

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def resolve_resume_id(resume: Optional[str], continue_latest: bool) -> Optional[str]:
    """Validate ``--resume``/``--continue`` and resolve ``--continue`` to an id.

    Returns the session id to resume (or ``None`` for a fresh session). Raises
    :class:`BootstrapError` with exit code 2 for the mutually-exclusive flag
    conflict, or 1 when ``--continue`` finds no previous session.
    """
    if resume and continue_latest:
        raise BootstrapError("Pass either --resume or --continue, not both.", exit_code=2)
    if continue_latest and not resume:
        latest = history_manager.get_latest_session_id()
        if not latest:
            raise BootstrapError("No previous sessions found to continue.", exit_code=1)
        return latest
    return resume


def bootstrap_agent(
    *,
    model: Optional[str] = None,
    resume_id: Optional[str] = None,
    continue_latest: bool = False,
    streaming: bool = True,
    auto_approve: bool = False,
    persona: Optional[str] = None,
    resume_fresh_on_failure: bool = False,
    warn: Optional[WarnFn] = None,
) -> Agent:
    """Build an :class:`Agent` with its session loaded or created (Phase 4.5).

    Owns the steps shared by every entry point: flag resolution, load-vs-create
    session, and delegate-tool context wiring.

    ``resume_fresh_on_failure`` selects the recovery policy when a requested
    resume can't be loaded: the TUI falls back to a fresh session (and reports
    via ``warn``); the headless path raises :class:`BootstrapError` so the user
    learns the session id was bad instead of silently starting over.
    """
    resume_id = resolve_resume_id(resume_id, continue_latest)

    agent = Agent(
        model=model,
        streaming=streaming,
        auto_approve=auto_approve,
        persona_name=persona,
    )

    if resume_id:
        session = None
        try:
            session = agent.load_session(resume_id)
        except Exception:
            logger.exception("Failed to load session %s", resume_id)
            if not resume_fresh_on_failure:
                raise BootstrapError(f"Could not load session {resume_id}.")
            if warn is not None:
                try:
                    warn(f"Failed to resume session {resume_id}. Starting a fresh session.")
                except Exception:
                    logger.debug("resume-failure warn callback raised", exc_info=True)
        if session is None:
            if not resume_fresh_on_failure:
                raise BootstrapError(f"Could not load session {resume_id}.")
            agent.create_session()
    else:
        agent.create_session()

    agent._configure_delegate_tool_context()
    return agent
