# Ported from kimi_cli/app.py - kimi structure.
"""Headless engine coordinator and CLI application lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import sys
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import kaos
from kaos.path import KaosPath

from coderai.agentspec import DEFAULT_AGENT_FILE
from coderai.auth.oauth import KIMI_CODE_OAUTH_KEY, OAuthManager
from coderai.config import Config, LLMModel, LLMProvider, load_config
from coderai.constant import VERSION
from coderai.llm import create_llm
from coderai.session import Session
from coderai.share import get_share_dir
from coderai.soul import RunCancelled
from coderai.soul.agent import Runtime, load_agent
from coderai.soul.approval import Approval
from coderai.soul.context import Context
from coderai.soul.kimisoul import KimiSoul
from coderai.soul.toolset import KimiToolset
from coderai.utils.logging import logger, open_original_stderr, redirect_stderr_to_logger
from coderai.wire import Wire, WireUISide
from coderai.wire.file import WireFile
from coderai.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    ContentPart,
    TextPart,
    TurnBegin,
    TurnEnd,
    WireMessage,
)


def _patch_session_id(record: dict[str, Any]) -> None:
    """Inject the current session ID into log records."""
    try:
        from coderai.soul.toolset import get_session_id

        sid = get_session_id()
        record["extra"]["sid"] = sid if sid else ""
    except Exception:
        record["extra"].setdefault("sid", "")


def enable_logging(debug: bool = False, *, redirect_stderr: bool = True) -> None:
    """Configure loguru logging with rotation and session context."""
    logger.remove()
    logger.enable("coderai")
    if debug:
        logger.enable("kosong")
    log_dir = get_share_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "coderai.log",
        level="TRACE" if debug else "INFO",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} | {extra[sid]} - {message}"
        ),
        rotation="06:00",
        retention="10 days",
    )
    logger.configure(extra={"sid": ""}, patcher=_patch_session_id)
    if redirect_stderr:
        try:
            redirect_stderr_to_logger()
        except Exception:
            pass


def _write_original_stderr(text: str) -> None:
    """Write a notice to the original stderr stream."""
    try:
        with open_original_stderr() as stream:
            if stream is not None:
                stream.write(text.encode("utf-8", errors="replace"))
                stream.flush()
                return
    except Exception:
        pass
    sys.stderr.write(text)


class KimiCLI:
    """Headless engine coordinator providing session execution over Wire."""

    @staticmethod
    async def create(
        session: Session,
        *,
        config: Config | Path | None = None,
        model_name: str | None = None,
        thinking: bool | None = None,
        yolo: bool = False,
        afk: bool = False,
        runtime_afk: bool = False,
        plan_mode: bool = False,
        resumed: bool = False,
        ui_mode: str = "shell",
        agent_file: Path | None = None,
        mcp_configs: list[Any] | None = None,
        skills_dirs: list[KaosPath] | None = None,
        max_steps_per_turn: int | None = None,
        max_retries_per_step: int | None = None,
        max_ralph_iterations: int | None = None,
        startup_progress: Callable[[str], None] | None = None,
        defer_mcp_loading: bool = False,
    ) -> KimiCLI:
        if startup_progress is not None:
            startup_progress("Loading configuration...")

        loaded_config = config if isinstance(config, Config) else load_config(config)

        # Determine LLM model & provider
        effective_model_name = model_name or loaded_config.default_model
        llm = None
        if effective_model_name and effective_model_name in loaded_config.models:
            model_spec = loaded_config.models[effective_model_name]
            provider_spec = loaded_config.providers.get(model_spec.provider)
            if provider_spec:
                try:
                    llm = create_llm(
                        provider_spec,
                        model_spec,
                        session_id=session.id,
                        thinking=thinking if thinking is not None else loaded_config.default_thinking,
                    )
                except Exception as exc:
                    logger.warning("Failed to create initial LLM: %s", exc)

        approval = Approval(yolo=yolo)
        if afk or runtime_afk:
            approval.set_afk(True)

        oauth = OAuthManager()

        runtime = Runtime(
            config=loaded_config,
            session=session,
            llm=llm,
            role="root",
            work_dir=str(session.work_dir),
            approval=approval,
            oauth=oauth,
        )
        runtime.ui_mode = ui_mode
        runtime.resumed = resumed

        if agent_file is None:
            agent_file = DEFAULT_AGENT_FILE

        if startup_progress is not None:
            startup_progress("Loading agent...")

        agent = await load_agent(
            agent_file,
            runtime,
            mcp_configs=mcp_configs or [],
            start_mcp_loading=not defer_mcp_loading,
        )

        context = Context(session.context_file)
        await context.restore()

        soul = KimiSoul(agent, context=context)

        # Hook engine injection
        try:
            from coderai.hooks.engine import HookEngine

            hook_engine = HookEngine(loaded_config.hooks, cwd=str(session.work_dir))
            soul.set_hook_engine(hook_engine)
            runtime.hook_engine = hook_engine
        except Exception as exc:
            logger.debug("Hook engine initialization skipped: %s", exc)

        return KimiCLI(soul=soul, runtime=runtime, env_overrides={})

    def __init__(
        self,
        soul: KimiSoul,
        runtime: Runtime,
        env_overrides: dict[str, str],
        bg_refresh_task: asyncio.Task[None] | None = None,
    ) -> None:
        self._soul = soul
        self._runtime = runtime
        self._env_overrides = env_overrides
        self._bg_refresh_task = bg_refresh_task

    @property
    def soul(self) -> KimiSoul:
        """Get the KimiSoul instance."""
        return self._soul

    @property
    def session(self) -> Session:
        """Get the Session instance."""
        return self._runtime.session

    async def shutdown_background_tasks(self) -> None:
        """Clean up background tasks on exit."""
        if self._bg_refresh_task is not None and not self._bg_refresh_task.done():
            self._bg_refresh_task.cancel()

        try:
            toolset = self.soul.agent.toolset
            if isinstance(toolset, KimiToolset):
                await toolset.cleanup()
        except Exception:
            logger.warning("Error during toolset cleanup; continuing", exc_info=True)

    async def await_bg_tasks_shutdown(self, timeout: float = 2.0) -> None:
        """Await completion of background tasks."""
        task = self._bg_refresh_task
        if task is None or task.done():
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    @contextlib.asynccontextmanager
    async def _env(self) -> AsyncGenerator[None]:
        original_cwd = KaosPath.cwd()
        await kaos.chdir(self._runtime.session.work_dir)
        try:
            yield
        finally:
            await kaos.chdir(original_cwd)

    async def run(
        self,
        user_input: str | list[ContentPart],
        cancel_event: asyncio.Event,
        merge_wire_messages: bool = False,
    ) -> AsyncGenerator[WireMessage]:
        """Run the engine turn and yield Wire messages."""
        async with self._env():
            wire = Wire(file_backend=self._runtime.session.wire_file)
            from coderai.soul import _current_wire

            wire_token = _current_wire.set(wire)

            # Record turn begin
            turn_begin = TurnBegin(user_input=user_input)
            wire.soul_side.send(turn_begin)

            try:
                # Run the soul or step execution
                text_input = (
                    user_input
                    if isinstance(user_input, str)
                    else "".join(p.text for p in user_input if isinstance(p, TextPart))
                )

                # Append user turn to context
                from kosong.message import Message, TextPart as KTextPart

                await self.soul.context.append_message(
                    Message(role="user", content=[KTextPart(text=text_input)])
                )

                # Run soul execution loop if available
                soul_task = asyncio.create_task(self.soul.run(user_input))
                cancel_task = asyncio.create_task(cancel_event.wait())

                done, pending = await asyncio.wait(
                    [soul_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for t in pending:
                    t.cancel()

                if cancel_event.is_set():
                    raise RunCancelled("Turn was cancelled by user")

                # Flush messages from wire
                while True:
                    msg = wire.ui_side.try_receive()
                    if msg is None:
                        break
                    yield msg

                turn_end = TurnEnd()
                wire.soul_side.send(turn_end)
                yield turn_end
            finally:
                wire.shutdown()
                _current_wire.reset(wire_token)


CoderAICLI = KimiCLI

__all__ = ["CoderAICLI", "KimiCLI", "enable_logging"]
