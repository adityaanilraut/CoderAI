"""Interactive persistent PTY terminal session manager."""

from __future__ import annotations

import atexit
import errno
import os
import select
import signal
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

try:
    import pty
except (ImportError, ModuleNotFoundError):
    pty = None  # type: ignore[assignment]

DEFAULT_MAX_BUFFER_CHARS = 500_000


@dataclass
class TerminalSessionStatus:
    session_id: str
    name: str
    process_type: str
    pid: int
    is_alive: bool
    exit_code: int | None = None
    cwd: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "name": self.name,
            "type": self.process_type,
            "pid": self.pid,
            "isAlive": self.is_alive,
            "exitCode": self.exit_code,
            "cwd": self.cwd,
            "createdAt": self.created_at,
        }


class TerminalSession:
    """Represents a single persistent interactive PTY terminal session."""

    def __init__(
        self,
        session_id: str,
        command: list[str] | str,
        name: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sandbox_mode: str | None = None,
        workspace_root: str | None = None,
        owner_session_id: str | None = None,
        execution_root: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.owner_session_id = owner_session_id
        self.workspace_root = str(Path(workspace_root or cwd or os.getcwd()).resolve())
        self.execution_root = str(Path(execution_root or self.workspace_root).resolve())
        from coderai.sandbox import DEFAULT_SANDBOX_MODE, parse_sandbox_mode

        # None represents the historical unsandboxed terminal API; do not
        # confuse it with an explicitly wrapped sandbox when deciding reuse.
        self.sandbox_mode = (
            (parse_sandbox_mode(sandbox_mode) or DEFAULT_SANDBOX_MODE) if sandbox_mode else None
        )
        self.name = name or f"terminal-{session_id}"
        # Central cwd policy: clamp the spawn directory inside the workspace
        # root (fail-closed; arbitrary cwd/Popen outside the root is refused).
        from coderai.sandbox import resolve_exec_cwd

        try:
            self.cwd = resolve_exec_cwd(
                cwd or self.execution_root, self.workspace_root, self.execution_root
            )
        except (ValueError, OSError) as exc:
            raise ValueError(f"Terminal cwd rejected: {exc}") from exc
        self.created_at = time.time()
        import codecs

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.output_buffer: list[str] = []
        self._unread_buffer: list[str] = []
        self.max_buffer_chars = DEFAULT_MAX_BUFFER_CHARS

        # Prepare environment without ambient secrets; explicit per-terminal
        # env is kept.
        from coderai.utils.subprocess_env import scrub_subprocess_env

        run_env = scrub_subprocess_env(dict(os.environ), preserve_keys=set((env or {}).keys()))
        if env:
            run_env.update(env)
        run_env["TERM"] = "xterm-256color"
        run_env["PAGER"] = "cat"

        if isinstance(command, str):
            cmd_args = [command]
        else:
            cmd_args = list(command)
        self.process_type = os.path.basename(cmd_args[0])

        self._sandbox_meta: dict[str, Any] = {}
        if sandbox_mode:
            from coderai.sandbox import wrap_sandbox_command

            cmd_args, self._sandbox_meta = wrap_sandbox_command(
                cmd_args,
                mode=sandbox_mode,
                workspace_root=self.execution_root,
                cwd=self.cwd,
            )

        # Open pseudo-terminal pair
        openpty_fn = getattr(pty, "openpty", None)
        if callable(openpty_fn):
            self.master_fd, self.slave_fd = openpty_fn()
        else:
            raise RuntimeError("Pseudo-terminals (pty) are not supported on this platform")

        # Spawn subprocess attached to slave fd
        try:
            preexec = getattr(os, "setsid", None)
            self.proc = subprocess.Popen(
                cmd_args,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                cwd=self.cwd,
                env=run_env,
                preexec_fn=preexec,
                close_fds=True,
            )
        except Exception:
            profile = self._sandbox_meta.get("sandboxProfile")
            if profile:
                from coderai.sandbox import delete_seatbelt_profile

                delete_seatbelt_profile(profile)
            os.close(self.master_fd)
            os.close(self.slave_fd)
            raise

        # Close slave fd in parent process (child holds it)
        try:
            os.close(self.slave_fd)
        except OSError:
            pass
        self.slave_fd = -1

        # Set master_fd to non-blocking
        set_blocking_fn = getattr(os, "set_blocking", None)
        if callable(set_blocking_fn):
            set_blocking_fn(self.master_fd, False)

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else -1

    @property
    def is_alive(self) -> bool:
        if not self.proc:
            return False
        return self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        if not self.proc:
            return None
        return self.proc.poll()

    def _append_to_buffer(self, buf: list[str], text: str) -> None:
        buf.append(text)
        total = sum(len(c) for c in buf)
        while buf and total > self.max_buffer_chars:
            first = buf[0]
            excess = total - self.max_buffer_chars
            if len(first) <= excess:
                buf.pop(0)
                total -= len(first)
            else:
                buf[0] = first[excess:]
                total -= excess
                break

    def send(self, text: str, submit: bool = True) -> None:
        """Write text to terminal stdin with write loop for non-blocking fd."""
        if not self.is_alive:
            raise RuntimeError(
                f"Terminal {self.session_id} is not running (exit code: {self.exit_code})"
            )

        payload = text
        if submit and not payload.endswith("\n"):
            payload += "\n"

        data = payload.encode("utf-8")
        total_written = 0
        data_len = len(data)
        deadline = time.time() + 10.0

        while total_written < data_len:
            rem = max(0.0, deadline - time.time())
            if rem <= 0:
                raise TimeoutError(f"Timed out writing to terminal {self.session_id}")
            _, wlist, _ = select.select([], [self.master_fd], [], rem)
            if not wlist:
                raise TimeoutError(
                    f"Timed out waiting for terminal {self.session_id} to be writable"
                )
            try:
                written = os.write(self.master_fd, data[total_written:])
                total_written += written
            except (BlockingIOError, OSError) as e:
                if getattr(e, "errno", None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.01)
                    continue
                raise

    def read_available(self, timeout_s: float = 0.1) -> str:
        """Read available data from master_fd without blocking indefinitely."""
        if self.master_fd < 0:
            return ""

        chunks: list[str] = []
        deadline = time.time() + timeout_s

        while True:
            remaining = max(0.0, deadline - time.time())
            rlist, _, _ = select.select([self.master_fd], [], [], remaining)
            if not rlist:
                break

            try:
                raw = os.read(self.master_fd, 8192)
                if not raw:
                    break
                text = self._decoder.decode(raw, final=False)
                if text:
                    chunks.append(text)
                    self._append_to_buffer(self.output_buffer, text)
                    self._append_to_buffer(self._unread_buffer, text)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                elif e.errno == errno.EIO:
                    # Child process exited
                    break
                else:
                    break

            if time.time() >= deadline:
                break

        return "".join(chunks)

    def read_unread(self) -> str:
        """Return and clear unread output."""
        self.read_available(timeout_s=0.05)
        text = "".join(self._unread_buffer)
        self._unread_buffer.clear()
        return text

    def get_full_output(self) -> str:
        """Return complete terminal output history."""
        return "".join(self.output_buffer)

    def send_signal(self, sig_name: str) -> None:
        """Send signal to child process group."""
        if not self.is_alive:
            return
        sig = getattr(signal, sig_name.upper(), None)
        if sig is None:
            raise ValueError(f"Unknown signal: {sig_name}")

        getpgid_fn = getattr(os, "getpgid", None)
        killpg_fn = getattr(os, "killpg", None)
        if callable(getpgid_fn) and callable(killpg_fn):
            try:
                pgid = getpgid_fn(self.proc.pid)
                killpg_fn(pgid, sig)
            except OSError:
                try:
                    self.proc.send_signal(sig)
                except OSError:
                    pass
        else:
            try:
                self.proc.send_signal(sig)
            except OSError:
                pass

    def close(self) -> None:
        """Terminate process tree with 3-stage escalation and close fds."""
        if self.is_alive:
            try:
                from coderai.utils.subprocess_env import escalated_kill_process_tree

                escalated_kill_process_tree(self.proc.pid, int_grace_sec=0.2, term_grace_sec=0.3)
                if self.is_alive:
                    self.proc.wait(timeout=0.5)
            except Exception:
                pass

        profile = getattr(self, "_sandbox_meta", {}).get("sandboxProfile")
        if profile:
            from coderai.sandbox import delete_seatbelt_profile

            delete_seatbelt_profile(profile)

        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1

        if self.slave_fd >= 0:
            try:
                os.close(self.slave_fd)
            except OSError:
                pass
            self.slave_fd = -1

    def status(self) -> TerminalSessionStatus:
        return TerminalSessionStatus(
            session_id=self.session_id,
            name=self.name,
            process_type=self.process_type,
            pid=self.pid,
            is_alive=self.is_alive,
            exit_code=self.exit_code,
            cwd=self.cwd,
            created_at=self.created_at,
        )


class TerminalManager:
    """Manages persistent terminal sessions across the agent session."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._next_id = 1

    def open_session(
        self,
        command: list[str] | str | None = None,
        name: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sandbox_mode: str | None = None,
        workspace_root: str | None = None,
        owner_session_id: str | None = None,
        execution_root: str | None = None,
    ) -> TerminalSession:
        """Create and spawn a new persistent terminal session."""
        if cwd is not None or workspace_root is not None:
            from coderai.sandbox import resolve_exec_cwd

            try:
                cwd = resolve_exec_cwd(
                    cwd or execution_root or workspace_root, workspace_root or cwd, execution_root
                )
            except (ValueError, OSError) as exc:
                raise ValueError(f"Terminal cwd rejected: {exc}") from exc
        if command is None:
            # Default to bash or sh
            shell = os.environ.get("SHELL") or "/bin/bash"
            if not os.path.exists(shell):
                shell = "/bin/sh"
            command = [shell]

        session_id = f"term_{self._next_id}"
        self._next_id += 1

        term = TerminalSession(
            session_id=session_id,
            command=command,
            name=name,
            cwd=cwd,
            env=env,
            sandbox_mode=sandbox_mode,
            workspace_root=workspace_root,
            owner_session_id=owner_session_id,
            execution_root=execution_root,
        )
        self._sessions[session_id] = term
        return term

    @staticmethod
    def _matches_owner(
        term: TerminalSession, owner_session_id: str | None, workspace_root: str | None
    ) -> bool:
        if term.owner_session_id != owner_session_id:
            return False
        return workspace_root is None or term.workspace_root == str(Path(workspace_root).resolve())

    def get_session(
        self,
        session_id: str,
        *,
        owner_session_id: str | None = None,
        workspace_root: str | None = None,
    ) -> TerminalSession | None:
        term = self._sessions.get(session_id)
        if term is not None:
            return term if self._matches_owner(term, owner_session_id, workspace_root) else None
        for term in self._sessions.values():
            if term.name == session_id and self._matches_owner(
                term, owner_session_id, workspace_root
            ):
                return term
        return None

    def list_sessions(
        self,
        *,
        owner_session_id: str | None = None,
        workspace_root: str | None = None,
    ) -> list[TerminalSessionStatus]:
        return [
            s.status()
            for s in self._sessions.values()
            if self._matches_owner(s, owner_session_id, workspace_root)
        ]

    def close_session(
        self,
        session_id: str,
        *,
        owner_session_id: str | None = None,
        workspace_root: str | None = None,
    ) -> bool:
        term = self.get_session(
            session_id, owner_session_id=owner_session_id, workspace_root=workspace_root
        )
        if term:
            self._sessions.pop(term.session_id, None)
            term.close()
            return True
        return False

    def close_all(self) -> None:
        """Process-wide shutdown only; session cleanup must use close_owned."""
        for term in list(self._sessions.values()):
            term.close()
        self._sessions.clear()

    def close_owned(self, owner_session_id: str, *, workspace_root: str) -> None:
        """Close only terminals created by this session in this workspace."""
        for term in list(self._sessions.values()):
            if self._matches_owner(term, owner_session_id, workspace_root):
                self.close_session(
                    term.session_id,
                    owner_session_id=owner_session_id,
                    workspace_root=workspace_root,
                )


_default_terminal_manager: TerminalManager | None = None


def get_terminal_manager() -> TerminalManager:
    global _default_terminal_manager
    if _default_terminal_manager is None:
        _default_terminal_manager = TerminalManager()
    return _default_terminal_manager


def cleanup_all_terminals() -> None:
    """Close and terminate all persistent terminal sessions on exit."""
    global _default_terminal_manager
    if _default_terminal_manager is not None:
        try:
            _default_terminal_manager.close_all()
        except Exception:
            pass


atexit.register(cleanup_all_terminals)
