# mypy: disable-error-code="attr-defined, has-type, misc, no-any-return"
"""Human-approval and preview boundary for tool execution."""

import asyncio
import json
import logging
from typing import Any, Optional

from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.permissions import (
    ApprovalRules,
    is_high_risk_no_blanket,
    tool_requires_confirmation,
)
from coderAI.core.ports import await_approval
from coderAI.core.services import get_services

logger = logging.getLogger(__name__)
PREVIEW_FILE_CACHE_MAX_ENTRIES = 50
PREVIEW_FILE_CACHE_MAX_BYTES = 5 * 1024 * 1024


class ConfirmationGate:
    def _cache_preview(self, path: str, mtime: float, content: str) -> None:
        self._preview_file_cache[path] = (mtime, content)
        self._preview_file_cache.move_to_end(path)
        while (
            len(self._preview_file_cache) > PREVIEW_FILE_CACHE_MAX_ENTRIES
            or sum(len(v[1]) for v in self._preview_file_cache.values())
            > PREVIEW_FILE_CACHE_MAX_BYTES
        ):
            self._preview_file_cache.popitem(last=False)

    def _is_call_preapproved(self, tool_name: str, arguments: Optional[dict[str, Any]]) -> bool:
        """True if this exact call is covered by an "always allow" rule (Phase 4.2).

        The real agent carries an :class:`ApprovalRules`, which scopes high-risk
        tools to a reviewed command-prefix / path (a bare-name allow of
        ``run_command`` never authorizes a *different* command). A plain set of
        names is still accepted as a legacy/test shim, but only for tools that
        are not high-risk (see :func:`is_high_risk_no_blanket`).
        """
        rules = self.runtime.tool_approval_allowlist
        if rules is None:
            return False
        if isinstance(rules, ApprovalRules):
            return rules.is_allowed(tool_name, arguments)
        try:
            name_allowed = tool_name in rules
        except TypeError:
            return False
        return bool(name_allowed) and not is_high_risk_no_blanket(tool_name)

    def _enter_waiting_for_user(
        self, tool_name: str
    ) -> Optional[tuple[AgentStatus, Optional[str]]]:
        info = self.agent.tracker_info
        if not info:
            return None
        previous = (info.status, info.current_tool)
        self.agent.tracker_update(status=AgentStatus.WAITING_FOR_USER, current_tool=tool_name)
        return previous

    def _exit_waiting_for_user(self, previous: Optional[tuple[AgentStatus, Optional[str]]]) -> None:
        info = self.agent.tracker_info
        if not info or previous is None:
            return
        if info.status == AgentStatus.CANCELLED:
            self.agent.tracker_update()
            return
        prev_status, prev_tool = previous
        self.agent.tracker_update(status=prev_status, current_tool=prev_tool)

    @staticmethod
    def _truncate_preview(text: str) -> str:
        """Cap an approval preview at 32KB with a visible truncation marker."""
        if len(text) > 32768:
            hidden = len(text) - 32768
            return text[:32768] + f"\n... (diff truncated) {hidden} chars hidden"
        return text

    def _compute_preview_diff(self, tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
        """Render an approval diff for a file-editing call (Phase 4.3).

        The editing *semantics* live on the tool (:meth:`Tool.preview`); this
        method owns only the trust-boundary plumbing: project-scope check, the
        mtime-keyed original-content cache, unified-diff rendering, and 32KB
        truncation. A tool either returns the new file content (rendered here as
        a diff) or a pre-rendered diff shown verbatim.
        """
        tools = self.runtime.tools
        tool = tools.get(tool_name) if tools is not None else None
        if tool is None:
            return None

        path = arguments.get("path")
        if not path:
            return None

        from pathlib import Path
        import difflib

        try:
            path_obj = Path(path).expanduser().resolve()

            from coderAI.tools.filesystem import _allows_outside_project

            config = self.runtime.config
            if config is not None and not _allows_outside_project():
                project_root = Path(config.project_root).resolve()
                try:
                    path_obj.relative_to(project_root)
                except ValueError:
                    return None

            # Read the current file text (None when it doesn't exist yet) via the
            # mtime cache so repeated previews don't re-read unchanged files.
            original: Optional[str] = None
            if path_obj.exists():
                try:
                    resolved = str(path_obj.resolve())
                    current_mtime = path_obj.stat().st_mtime
                    cached = self._preview_file_cache.get(resolved)
                    if cached is not None and cached[0] == current_mtime:
                        self._preview_file_cache.move_to_end(resolved)
                        original = cached[1]
                    else:
                        original = path_obj.read_text(encoding="utf-8")
                        self._cache_preview(resolved, current_mtime, original)
                except Exception:
                    return None

            preview = tool.preview(arguments, original)
            if preview is None:
                return None
            if preview.rendered_diff is not None:
                return self._truncate_preview(preview.rendered_diff)
            if preview.new_content is None:
                return None

            original_text = original or ""
            diff_lines = list(
                difflib.unified_diff(
                    original_text.splitlines(keepends=True),
                    preview.new_content.splitlines(keepends=True),
                    fromfile=f"a/{path_obj.name}",
                    tofile=f"b/{path_obj.name}",
                    n=3,
                )
            )
            return self._truncate_preview("".join(diff_lines))
        except Exception as e:
            logger.debug("Preview diff computation failed for %s: %s", tool_name, e)
            return None

    async def _precompute_diffs(self, parsed_calls: list) -> dict[int, Optional[str]]:
        gated: list[tuple[int, dict]] = []
        for i, pc in enumerate(parsed_calls):
            if pc.get("parse_error") or pc.get("arguments") is None:
                continue
            tool = self.agent.tools.get(pc.get("tool_name", ""))
            if tool is not None and tool_requires_confirmation(tool):
                gated.append((i, pc))

        if not gated:
            return {}

        async def _one(idx: int, pc: dict) -> tuple[int, Optional[str]]:
            diff = await asyncio.to_thread(
                self._compute_preview_diff, pc["tool_name"], pc["arguments"]
            )
            return idx, diff

        diffs: dict[int, Optional[str]] = {}
        results = await asyncio.gather(*(_one(i, pc) for i, pc in gated))
        for idx, diff in results:
            if diff is not None:
                diffs[idx] = diff
        return diffs

    async def _confirmation_callback(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool_id: Optional[str] = None,
        precomputed_diff: Optional[str] = None,
        *,
        force_confirm: bool = False,
    ) -> bool:
        # Headless / non-interactive override (e.g. `coderAI run`): when set,
        # decide here instead of prompting. A forced MCP-tainted mutation may
        # be denied by an override, but an override allow is not a human
        # confirmation and therefore falls through to the real approval path.
        if self.runtime.auto_approve and not force_confirm:
            return True

        # Compute diff even for override path so headless logging has it
        headless_diff = (
            precomputed_diff
            if precomputed_diff is not None
            else await asyncio.to_thread(self._compute_preview_diff, tool_name, arguments)
        )
        # Headless diff logging: ensure diff is observable even when override decides
        if headless_diff:
            logger.info("Headless diff for '%s':\n%s", tool_name, headless_diff[:4000])
            try:
                get_services().events.emit("tool_diff", tool_name=tool_name, diff=headless_diff)
            except Exception:
                pass

        override = self.runtime.confirmation_override
        if override is not None:
            override_allowed = bool(await override(tool_name, arguments))
            if not override_allowed or not force_confirm:
                return override_allowed

        port = self.runtime.approval_port

        if force_confirm and port is None:
            import sys

            if not sys.stdin.isatty():
                logger.warning(
                    "Forced human approval unavailable for MCP-tainted mutation '%s'; denying.",
                    tool_name,
                )
                return False

        # Reuse headless_diff computed above to avoid double preview computation
        diff = headless_diff

        async with self._confirm_lock:
            # Always (a) may have enabled YOLO while this call was queued
            # behind another approval — honour it without a second prompt.
            if self.runtime.auto_approve and not force_confirm:
                return True
            if port is None:
                args_preview = json.dumps(arguments, indent=2)
                if len(args_preview) > 300:
                    args_preview = args_preview[:300] + "\n  ... (truncated)"

                diff_preview = f"\n\nDiff Preview:\n{diff}" if diff else ""

                get_services().events.emit(
                    "agent_status",
                    message=(
                        f"\n⚠ Tool '{tool_name}' requires confirmation."
                        f"\n{args_preview}"
                        f"{diff_preview}"
                    ),
                )

            previous = self._enter_waiting_for_user(tool_name)
            try:
                if port is not None:
                    timeout_s = int(
                        getattr(self.runtime.config, "approval_timeout_seconds", 300) or 0
                    )
                    return await await_approval(
                        port,
                        tool_name,
                        arguments,
                        preview=diff,
                        timeout_s=timeout_s,
                    )

                try:
                    from prompt_toolkit import PromptSession

                    prompt_session: Any = PromptSession()
                    answer = await prompt_session.prompt_async("Allow this tool? (y/n) > ")
                except (ImportError, EOFError, KeyboardInterrupt):
                    try:
                        loop = asyncio.get_running_loop()
                        answer = await loop.run_in_executor(
                            None, lambda: input("Allow this tool? (y/n) > ")
                        )
                    except (EOFError, KeyboardInterrupt):
                        answer = "n"

                return answer.strip().lower() in ("y", "yes")
            finally:
                self._exit_waiting_for_user(previous)
