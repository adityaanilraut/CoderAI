"""Interactive persistent PTY terminal session manager."""

from __future__ import annotations

import errno
import os
import pty
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

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
    ) -> None:
        self.session_id = session_id
        self.name = name or f"terminal-{session_id}"
        self.cwd = cwd or os.getcwd()
        self.created_at = time.time()
        self.output_buffer: list[str] = []
        self._unread_buffer: list[str] = []

        # Prepare environment
        run_env = os.environ.copy()
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
            from coderai.core.sandbox import wrap_sandbox_command

            cmd_args, self._sandbox_meta = wrap_sandbox_command(
                cmd_args,
                mode=sandbox_mode,
                workspace_root=workspace_root or self.cwd,
                cwd=self.cwd,
            )

        # Open pseudo-terminal pair
        self.master_fd, self.slave_fd = pty.openpty()

        # Spawn subprocess attached to slave fd
        try:
            self.proc = subprocess.Popen(
                cmd_args,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                cwd=self.cwd,
                env=run_env,
                preexec_fn=os.setsid,
                close_fds=True,
            )
        except Exception:
            profile = self._sandbox_meta.get("sandboxProfile")
            if profile:
                from coderai.core.sandbox import delete_seatbelt_profile

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
        os.set_blocking(self.master_fd, False)

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

    def send(self, text: str, submit: bool = True) -> None:
        """Write text to terminal stdin."""
        if not self.is_alive:
            raise RuntimeError(
                f"Terminal {self.session_id} is not running (exit code: {self.exit_code})"
            )

        payload = text
        if submit and not payload.endswith("\n"):
            payload += "\n"

        data = payload.encode("utf-8")
        os.write(self.master_fd, data)

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
                text = raw.decode("utf-8", errors="replace")
                chunks.append(text)
                self.output_buffer.append(text)
                self._unread_buffer.append(text)
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

        try:
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, sig)
        except OSError:
            try:
                self.proc.send_signal(sig)
            except OSError:
                pass

    def close(self) -> None:
        """Terminate process and close fds."""
        if self.is_alive:
            try:
                self.send_signal("SIGTERM")
                time.sleep(0.05)
                if self.is_alive:
                    self.send_signal("SIGKILL")
                self.proc.wait(timeout=0.5)
            except Exception:
                pass

        profile = getattr(self, "_sandbox_meta", {}).get("sandboxProfile")
        if profile:
            from coderai.core.sandbox import delete_seatbelt_profile

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
    ) -> TerminalSession:
        """Create and spawn a new persistent terminal session."""
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
        )
        self._sessions[session_id] = term
        return term

    def get_session(self, session_id: str) -> TerminalSession | None:
        if session_id in self._sessions:
            return self._sessions[session_id]
        for term in self._sessions.values():
            if term.name == session_id:
                return term
        return None

    def list_sessions(self) -> list[TerminalSessionStatus]:
        return [s.status() for s in self._sessions.values()]

    def close_session(self, session_id: str) -> bool:
        term = self._sessions.pop(session_id, None)
        if term:
            term.close()
            return True
        return False

    def close_all(self) -> None:
        for term in list(self._sessions.values()):
            term.close()
        self._sessions.clear()


_default_terminal_manager: TerminalManager | None = None


def get_terminal_manager() -> TerminalManager:
    global _default_terminal_manager
    if _default_terminal_manager is None:
        _default_terminal_manager = TerminalManager()
    return _default_terminal_manager
