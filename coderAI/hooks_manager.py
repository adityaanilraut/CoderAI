"""Project hooks manager for CoderAI.

Supported hook types:
- PreToolUse: Fired before a tool is executed.
- PostToolUse: Fired after a tool is executed.
- on_user_prompt: Fired when the user sends a new message to the agent.
- on_stop: Fired when the agent loop finishes processing (all iterations complete).
- on_subagent_stop: Fired when a sub-agent finishes its task.
- on_compact: Fired after context compaction/summarization.
- chat.message: Fired when a new user message is received (for message transformation).
- permission.ask: Fired when a tool permission check is needed (can override approval).
- shell.env: Fired when setting up the shell environment for run_command.

Hooks can optionally return a JSON-structured response via stdout. The hook
output is parsed as JSON and merged into a result dict that callers can use
to modify behavior. If the output is not valid JSON it is treated as a plain
string (backward-compatible with existing hook scripts).
"""

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

# All recognized hook types in the order they are typically evaluated.
# New hook types added per-project are discovered from hooks.json;
# this list documents the built-in contract.
VALID_HOOK_TYPES: Tuple[str, ...] = (
    "on_user_prompt",
    "PreToolUse",
    "PostToolUse",
    "on_stop",
    "on_subagent_stop",
    "on_compact",
    "chat.message",
    "permission.ask",
)

# Valid status values returned by permission.ask hooks
VALID_PERMISSION_STATUSES: Tuple[str, ...] = ("allow", "deny", "ask")

# Shared regex and sanitizer for shell metacharacter removal. Used by both
# run_hooks() and run_hooks_structured() to prevent hook scripts from being
# hijacked by adversarial tool arguments.
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


class HooksManager:
    """Manages project-specific tool hook configuration and execution."""

    def __init__(self, agent):
        self.agent = agent
        # Cache for project hooks.json (keyed by path → (mtime_ns, parsed))
        self._hooks_cache: Dict[str, Tuple[int, Optional[Dict[str, Any]]]] = {}

    def _prepare_hook_environment(
        self, tool_name: str, hook_type: str, arguments: dict
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """Build the env dict and optional temp-file path for hook execution.

        Returns (env, args_file_path) — callers MUST ``os.unlink(args_file_path)``
        after hook execution completes.
        """
        env = os.environ.copy()
        env["CODERAI_TOOL_NAME"] = tool_name

        args_file_path: Optional[str] = None
        try:
            args_json = json.dumps(arguments, default=str)
        except Exception:
            args_json = "{}"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", prefix="coderai-hook-args-", suffix=".json",
                delete=False, encoding="utf-8"
            ) as f:
                f.write(args_json)
                args_file_path = f.name
            try:
                os.chmod(args_file_path, 0o600)
            except OSError:
                pass
            env["CODERAI_ARGS_FILE"] = args_file_path
        except Exception as e:
            logger.debug("Could not write hook args tempfile: %s", e)
            env["CODERAI_ARGS_FILE"] = ""

        env["CODERAI_ARGS_JSON"] = args_json

        for i, (arg_key, arg_val) in enumerate(arguments.items()):
            safe_key = "".join(
                c if c.isalnum() or c == "_" else "_" for c in str(arg_key)
            ).upper()
            safe_val = _sanitize_env_value(arg_val)
            env[f"CODERAI_ARG_{safe_key}"] = safe_val
            env[f"CODERAI_ARG_{i}"] = safe_val

        return env, args_file_path

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
            if matching_hooks:
                matching_hooks = [h for h in matching_hooks if h.get("command")]

            if not matching_hooks:
                return hooks_results

            # Per-command approval cache
            if not self.agent.auto_approve:
                cache = self.agent._hooks_approved
                hooks_to_run = []
                for h in matching_hooks:
                    cmd = h.get("command")
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
                # Reassign matching_hooks to only hooks that survived approval;
                # hooks without a "command" field are silently excluded here.
                matching_hooks = hooks_to_run
                if not matching_hooks:
                    return hooks_results

            env, args_file_path = self._prepare_hook_environment(tool_name, hook_type, arguments)

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
                    stdout_text = stdout.decode("utf-8", errors="replace").strip()
                    stderr_text = stderr.decode("utf-8", errors="replace").strip()
                    if proc.returncode != 0:
                        detail = stderr_text or stdout_text or f"exit code {proc.returncode}"
                        return f"[{hook_type} Hook ERROR]: {cmd} :: {detail}"
                    return stdout_text or None

                try:
                    outputs = await asyncio.gather(*(_exec_hook(c) for c in runnable), return_exceptions=True)
                    for out in outputs:
                        if isinstance(out, Exception):
                            hooks_results.append(f"[{hook_type} Hook ERROR]: {out}")
                        elif out:
                            hooks_results.append(f"[{hook_type} Hook Output]: {out}")
                finally:
                    if args_file_path:
                        try:
                            os.unlink(args_file_path)
                        except OSError:
                            pass
        except Exception as e:
            logger.error(f"Error running hooks: {e}")
            hooks_results.append(f"[{hook_type} Hook ERROR]: {e}")
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
                answer = await asyncio.to_thread(input, "Allow project hooks to run? (y/n) > ")

            return answer.strip().lower() in ("y", "yes")
        finally:
            if info is not None and previous is not None:
                if info.status != AgentStatus.CANCELLED:
                    info.status, info.current_tool = previous
                self.agent._sync_tracker()

    async def run_hooks_structured(
        self,
        tool_name: str,
        hook_type: str,
        arguments: dict,
        hooks_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Run hooks and return structured results for behavior-modifying hooks.

        Unlike ``run_hooks`` which returns plain strings, this method
        parses each hook's stdout as JSON and collects the results into a
        dict keyed by hook index. This allows hooks like ``permission.ask``
        and ``shell.env`` to return structured data that modifies agent
        behavior (e.g. ``{"status": "allow"}`` or ``{"env": {"KEY": "val"}}``).

        Plain-text (non-JSON) output is preserved under the ``_text`` key
        for backward compatibility.
        """
        hooks_results: Dict[str, Any] = {}
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

            env, args_file_path = self._prepare_hook_environment(tool_name, hook_type, arguments)

            async def _exec_structured_hook(cmd: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
                """Run a hook and attempt JSON parsing of stdout."""
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env
                )
                stdout, stderr = await proc.communicate()
                stdout_text = stdout.decode("utf-8", errors="replace").strip()
                stderr_text = stderr.decode("utf-8", errors="replace").strip()
                if proc.returncode != 0:
                    detail = stderr_text or stdout_text or f"exit code {proc.returncode}"
                    return None, f"[{hook_type} Hook ERROR]: {cmd} :: {detail}"
                try:
                    parsed = json.loads(stdout_text)
                    return parsed, None
                except (json.JSONDecodeError, TypeError):
                    return {"_text": stdout_text}, None

            runnable = []
            for hook in matching_hooks:
                cmd = hook.get("command")
                if not cmd:
                    continue
                if is_command_blocked(cmd):
                    hooks_results[f"_blocked_{len(hooks_results)}"] = cmd
                else:
                    runnable.append(cmd)

            if runnable:
                try:
                    outputs = await asyncio.gather(
                        *(_exec_structured_hook(c) for c in runnable),
                        return_exceptions=True,
                    )
                    for idx, out in enumerate(outputs):
                        if isinstance(out, Exception):
                            hooks_results[f"_error_{idx}"] = str(out)
                        else:
                            parsed, error = out
                            if error:
                                hooks_results[f"_error_{idx}"] = error
                            elif parsed:
                                hooks_results[str(idx)] = parsed
                finally:
                    if args_file_path:
                        try:
                            os.unlink(args_file_path)
                        except OSError:
                            pass
        except Exception as e:
            logger.error(f"Error running structured hooks: {e}")
            hooks_results["_error"] = str(e)
        return hooks_results

    async def run_chat_message_hooks(
        self,
        user_message: str,
        hooks_data: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Run ``chat.message`` hooks that can transform the user message.

        Returns the (possibly transformed) message text. If no transformation
        is applied, returns ``None`` (caller should use the original).
        """
        if not hooks_data:
            return None
        results = await self.run_hooks_structured(
            tool_name="*",
            hook_type="chat.message",
            arguments={"text": user_message},
            hooks_data=hooks_data,
        )
        if not results:
            return None
        for key in sorted(results.keys(), key=lambda k: (not k.isdigit(), k)):
            entry = results[key]
            if isinstance(entry, dict) and "message" in entry:
                return entry["message"]
        return None

    async def run_permission_hooks(
        self,
        tool_name: str,
        arguments: dict,
        hooks_data: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Run ``permission.ask`` hooks that can override tool approval.

        Returns one of ``"allow"``, ``"deny"``, ``"ask"``, or ``None``
        (no hook intervened — caller should use normal approval logic).
        """
        if not hooks_data:
            return None
        results = await self.run_hooks_structured(
            tool_name=tool_name,
            hook_type="permission.ask",
            arguments=arguments,
            hooks_data=hooks_data,
        )
        if not results:
            return None
        for key in sorted(results.keys(), key=lambda k: (not k.isdigit(), k)):
            entry = results[key]
            if isinstance(entry, dict):
                status = entry.get("status")
                if status in VALID_PERMISSION_STATUSES:
                    return status
        return None

