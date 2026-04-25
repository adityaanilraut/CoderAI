"""Project hooks manager for CoderAI."""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent_tracker import AgentStatus
from .events import event_emitter

logger = logging.getLogger(__name__)


class HooksManager:
    """Manages project-specific tool hook configuration and execution."""

    def __init__(self, agent):
        self.agent = agent
        # Cache for project hooks.json (keyed by path → (mtime_ns, parsed))
        self._hooks_cache: Dict[str, Tuple[int, Optional[Dict[str, Any]]]] = {}

    def load_hooks(self) -> Optional[Dict[str, Any]]:
        """Load project hooks from .coderAI/hooks.json (cached by mtime)."""
        try:
            hfile = Path(self.agent.config.project_root) / ".coderAI" / "hooks.json"
            if not hfile.exists():
                return None
            mtime_ns = hfile.stat().st_mtime_ns
            cached = self._hooks_cache.get(str(hfile))
            if cached and cached[0] == mtime_ns:
                return cached[1]
            with open(hfile, "r") as f:
                parsed = json.load(f)
            self._hooks_cache[str(hfile)] = (mtime_ns, parsed)
            return parsed
        except Exception as e:
            logger.debug(f"Failed to load hooks: {e}")
            return None

    async def run_hooks(
        self, tool_name: str, hook_type: str, arguments: dict, hooks_data: Optional[Dict[str, Any]]
    ) -> List[str]:
        """Run hooks for a tool stage; parallel-executes multiple hooks."""
        hooks_results: List[str] = []
        if not hooks_data:
            return hooks_results

        try:
            from .tools.terminal import is_command_blocked

            matching_hooks = [
                h for h in hooks_data.get("hooks", [])
                if h.get("type") == hook_type and (h.get("tool") == "*" or h.get("tool") == tool_name)
            ]
            if not matching_hooks:
                return hooks_results

            # Per-command approval cache
            if not self.agent.auto_approve:
                cache = self.agent._hooks_approved
                hooks_to_run = []
                for h in matching_hooks:
                    cmd = h.get("command")
                    if not cmd:
                        continue
                    decision = cache.get(cmd)
                    if decision is True:
                        hooks_to_run.append(h)
                    elif decision is False:
                        continue
                    else:
                        approved = await self.request_hooks_approval([h])
                        cache[cmd] = bool(approved)
                        if approved:
                            hooks_to_run.append(h)
                matching_hooks = hooks_to_run
                if not matching_hooks:
                    return hooks_results

            env = os.environ.copy()
            env["CODERAI_TOOL_NAME"] = tool_name

            # Write full args to a tempfile; hook scripts can read the JSON
            # without concerning themselves with shell quoting. This is the
            # preferred path — the env-var copies below are still exported
            # for convenience but are aggressively sanitized.
            args_file_path: Optional[str] = None
            try:
                args_json = json.dumps(arguments, default=str)
            except Exception:
                args_json = "{}"
            try:
                fd, args_file_path = tempfile.mkstemp(
                    prefix="coderai-hook-args-", suffix=".json"
                )
                try:
                    os.chmod(args_file_path, 0o600)
                except OSError:
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(args_json)
                env["CODERAI_ARGS_FILE"] = args_file_path
            except Exception as e:
                logger.debug(f"Could not write hook args tempfile: {e}")
                env["CODERAI_ARGS_FILE"] = ""

            # Legacy env var for backward compatibility. Do NOT use this in
            # hook commands unless you quote it — prefer reading
            # $CODERAI_ARGS_FILE instead.
            env["CODERAI_ARGS_JSON"] = args_json

            _DANGEROUS_METACHARS = re.compile(r"[\x00$`\\;&|<>(){}\"'\n\r]")

            def _sanitize_env_value(val: Any, max_len: int = 4096) -> str:
                """Strip shell metacharacters from env var values.

                Users' hook scripts may use these env vars unquoted; stripping
                ``$``, backticks, quotes, redirection and command separators
                prevents those scripts from being hijacked by adversarial
                tool arguments.
                """
                s = str(val)[:max_len]
                return _DANGEROUS_METACHARS.sub("", s)

            for i, (arg_key, arg_val) in enumerate(arguments.items()):
                safe_key = "".join(c if c.isalnum() or c == "_" else "_" for c in str(arg_key)).upper()
                safe_val = _sanitize_env_value(arg_val)
                env[f"CODERAI_ARG_{safe_key}"] = safe_val
                env[f"CODERAI_ARG_{i}"] = safe_val

            runnable = []
            for hook in matching_hooks:
                cmd = hook.get("command")
                if not cmd:
                    continue
                if is_command_blocked(cmd):
                    hooks_results.append(f"[{hook_type} Hook BLOCKED]: {cmd}")
                else:
                    runnable.append(cmd)

            if runnable:
                async def _exec_hook(cmd: str):
                    event_emitter.emit("agent_status", message=f"[dim]Running {hook_type} hook...[/dim]")
                    proc = await asyncio.create_subprocess_shell(
                        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
                    )
                    stdout, stderr = await proc.communicate()
                    return stdout.decode("utf-8").strip() if proc.returncode == 0 else None

                try:
                    outputs = await asyncio.gather(*(_exec_hook(c) for c in runnable), return_exceptions=True)
                    for out in outputs:
                        if out and not isinstance(out, Exception):
                            hooks_results.append(f"[{hook_type} Hook Output]: {out}")
                finally:
                    if args_file_path:
                        try:
                            os.unlink(args_file_path)
                        except OSError:
                            pass
        except Exception as e:
            logger.error(f"Error running hooks: {e}")
        return hooks_results

    async def request_hooks_approval(self, matching_hooks: list) -> bool:
        """Ask user for permission to run project hooks."""
        cmds_preview = ", ".join(h.get("command", "?")[:60] for h in matching_hooks)
        event_emitter.emit(
            "agent_status", 
            message=f"\n[bold yellow]⚠ Project hooks detected[/bold yellow]\n[dim]Commands: {cmds_preview}[/dim]"
        )

        info = getattr(self.agent, "tracker_info", None)
        previous = None
        if info is not None:
            previous = (info.status, info.current_tool)
            info.status = AgentStatus.WAITING_FOR_USER
            info.current_tool = "project_hooks"
            self.agent._sync_tracker()

        ipc_server = getattr(self.agent, "ipc_server", None)
        try:
            if ipc_server:
                import uuid
                approved = await ipc_server.request_tool_approval(
                    tool_id=str(uuid.uuid4()),
                    tool_name="project_hooks",
                    arguments={"commands": [h.get("command") for h in matching_hooks]},
                )
                return bool(approved)

            try:
                from prompt_toolkit import PromptSession
                ps = PromptSession()
                answer = await ps.prompt_async("Allow project hooks to run? (y/n) > ")
            except Exception as e:
                logger.warning(f"prompt_async failed: {e}", exc_info=True)
                answer = input("Allow project hooks to run? (y/n) > ")

            return answer.strip().lower() in ("y", "yes")
        finally:
            if info is not None and previous is not None:
                if info.status != AgentStatus.CANCELLED:
                    info.status, info.current_tool = previous
                self.agent._sync_tracker()
