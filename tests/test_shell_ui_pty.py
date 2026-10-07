"""Real REPL PTY release gates (POSIX); deterministic provider, isolated home."""

from __future__ import annotations
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX PTY gate; Windows uses portable pipe-input interaction gates",
)


class Terminal:
    def __init__(self, tmp_path):
        import pty
        import fcntl
        import termios
        import struct

        self.fd, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.initial = termios.tcgetattr(slave)
        self.slave = slave
        env = {
            key: value
            for key, value in os.environ.items()
            if not any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL"))
        }
        env.update(
            {
                "HOME": str(tmp_path),
                "CODERAI_SHELL_CONTROLLER": "1",
                "TERM": "xterm-256color",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "NO_COLOR": "1",
            }
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "fixtures/shell_ui_provider.py"),
                str(tmp_path),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
        )
        self.output = ""

    def send(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def resize(self, columns, rows=24):
        import fcntl
        import signal
        import struct
        import termios

        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        os.kill(self.process.pid, signal.SIGWINCH)

    def until(self, text, timeout=8):
        deadline = time.monotonic() + timeout
        while text not in self.output:
            if time.monotonic() > deadline:
                raise AssertionError(f"Missing {text!r}: {self.output[-5000:]}")
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.output += data.decode(errors="replace")
                # Respond to PTK cursor-position queries as a terminal would.
                if b"\x1b[6n" in data:
                    self.send(b"\x1b[1;1R")
            if self.process.poll() is not None:
                raise AssertionError(
                    f"Shell exited {self.process.returncode}: {self.output[-5000:]}"
                )
        assert text in self.output

    def close(self):
        import termios

        self.send("/exit\r")
        try:
            # A real terminal keeps consuming output during shutdown. Drain
            # redraws and the exit summary so the PTY buffer cannot block it.
            deadline = time.monotonic() + 5
            while self.process.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([self.fd], [], [], 0.05)
                if ready:
                    data = os.read(self.fd, 65536)
                    self.output += data.decode(errors="replace")
                    if b"\x1b[6n" in data:
                        self.send(b"\x1b[1;1R")
            self.process.wait(timeout=5)
            assert self.process.returncode == 0, self.output[-2000:]
            restored = termios.tcgetattr(self.slave)
            # macOS sets PENDIN when switching back to canonical mode. It is
            # a driver-owned pending-input flag, not an input-mode setting.
            mask = ~getattr(termios, "PENDIN", 0)
            restored[3] &= mask
            self.initial[3] &= mask
            assert restored == self.initial
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            os.close(self.fd)
            os.close(self.slave)


def test_real_shell_queues_steers_browses_and_restores_terminal(tmp_path):
    terminal = Terminal(tmp_path)
    try:
        terminal.until("\x1b[?2004h")
        terminal.send("delay first\r")
        terminal.until("FIXTURE_BEGIN delay first")
        terminal.resize(40)
        terminal.send("followup\r")
        terminal.until("Queued #1")
        terminal.send("change direction\x13")
        terminal.until("FIXTURE_STEER change direction")
        terminal.resize(120)
        terminal.send("/activity\r")
        terminal.until("Live activity")
        terminal.until("FIXTURE_HEARTBEAT")
        terminal.send(b"\x03")
        terminal.until("FIXTURE_BEGIN followup")
        terminal.until("FIXTURE_FINISH followup")
        terminal.send("delay interrupt\r")
        terminal.until("FIXTURE_BEGIN delay interrupt")
        terminal.send(b"\x03")
        terminal.until("FIXTURE_CANCEL delay interrupt")
        terminal.send("after cancel\r")
        terminal.until("FIXTURE_FINISH after cancel")
        plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", terminal.output)
        assert plain.count("FIXTURE_BEGIN followup") == 1
        assert plain.count("FIXTURE_FINISH followup") == 1
    finally:
        terminal.close()


def test_real_shell_approval_answer_and_draft_survive_generation(tmp_path):
    terminal = Terminal(tmp_path)
    try:
        terminal.until("\x1b[?2004h")
        terminal.send("approval\r")
        terminal.until("Approval: write")
        terminal.send("\r")
        terminal.until("FIXTURE_PERMISSION")
        terminal.until("FIXTURE_FINISH permission handled")
        terminal.send("question\r")
        terminal.until("Choose a color")
        terminal.send(b"\x1b[B\r")
        terminal.until("FIXTURE_BEGIN Choose a color: Blue")
        terminal.until("FIXTURE_FINISH Choose a color: Blue")
        terminal.send("delay draft\r")
        terminal.until("FIXTURE_BEGIN delay draft")
        terminal.send("saved draft")
        terminal.until("FIXTURE_FINISH delay draft")
        terminal.send("\r")
        terminal.until("FIXTURE_BEGIN saved draft")
        terminal.until("FIXTURE_FINISH saved draft")
    finally:
        terminal.close()


def test_real_shell_approval_and_question_cancellation(tmp_path):
    terminal = Terminal(tmp_path)
    try:
        terminal.until("\x1b[?2004h")
        terminal.send("approval\r")
        terminal.until("Approval: write")
        terminal.send(b"\x03")
        terminal.until("Turn interrupted")
        assert "FIXTURE_PERMISSION" not in terminal.output
        terminal.send("question\r")
        terminal.until("Choose a color")
        terminal.send(b"\x03")
        terminal.until("Turn interrupted", timeout=5)
        assert "FIXTURE_BEGIN Choose a color:" not in terminal.output
        terminal.send("working again\r")
        terminal.until("FIXTURE_FINISH working again")
    finally:
        terminal.close()


def test_real_shell_attachment_removal_output_inspection_and_session_switch(tmp_path):
    (tmp_path / "with spaces.py").write_text("fixture file\nsecond line\n")
    terminal = Terminal(tmp_path)
    try:
        terminal.until("\x1b[?2004h")
        terminal.send('review @"with spaces.py":1-2 carefully\x14')
        terminal.until("Draft attachments")
        terminal.send("\r")
        terminal.send("\r")
        terminal.until("FIXTURE_FINISH review  carefully")
        terminal.send("tool output\r")
        terminal.until("/output t-full")
        terminal.send("/output t-full\r")
        terminal.until("Ctrl-F search")
        terminal.send(b"\x1b[1;5F")
        terminal.until("complete-output-99")
        terminal.send(b"\x03")
        terminal.send("/fork\r")
        terminal.until("Restored")
        terminal.send("/new\r")
        terminal.send("/sessions tool output\r")
        terminal.until("Sessions")
        terminal.send("\r")
        terminal.until("/history opens the complete transcript")
        terminal.send("after resume\r")
        terminal.until("FIXTURE_FINISH after resume")
    finally:
        terminal.close()


def test_stop_keeps_queue_paused_after_replacement_prompt(tmp_path):
    terminal = Terminal(tmp_path)
    try:
        terminal.until("\x1b[?2004h")
        terminal.send("delay first\r")
        terminal.until("FIXTURE_BEGIN delay first")
        terminal.send("old queued instruction\r")
        terminal.until("Queued #1")
        terminal.send(b"\x03")
        terminal.until("FIXTURE_CANCEL delay first")
        terminal.until("queue paused")
        terminal.send("replacement instruction\r")
        terminal.until("FIXTURE_FINISH replacement instruction")
        terminal.send("/queue list\r")
        terminal.until("Queue paused; /queue run resumes.")
        assert "FIXTURE_BEGIN old queued instruction" not in terminal.output
        terminal.send("/queue run\r")
        terminal.until("FIXTURE_FINISH old queued instruction")
    finally:
        terminal.close()


def test_unsent_draft_recovers_after_process_crash(tmp_path):
    terminal = Terminal(tmp_path)
    try:
        terminal.until("\x1b[?2004h")
        terminal.send("unsent recovered draft")
        # Wait for the debounced, atomic recovery file, not a terminal redraw.
        deadline = time.monotonic() + 5
        recovered = False
        while time.monotonic() < deadline:
            paths = list((tmp_path / ".coderai" / "shell").glob("*/view-*.json"))
            if any("unsent recovered draft" in path.read_text() for path in paths):
                recovered = True
                break
            time.sleep(0.05)
        assert recovered
    finally:
        terminal.process.kill()
        terminal.process.wait()
        os.close(terminal.fd)
        os.close(terminal.slave)
    restarted = Terminal(tmp_path)
    try:
        restarted.until("Recovered draft and attachments")
        restarted.until("\x1b[?2004h")
        restarted.send("\r")
        restarted.until("FIXTURE_FINISH unsent recovered draft")
    finally:
        restarted.close()
