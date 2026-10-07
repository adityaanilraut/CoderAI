"""Single presentation path: transient prompt preview, one scrollback commit.

No Rich Live owns the terminal. prompt-toolkit redraws the pending preview;
finished messages are emitted once and tool payloads stay inspectable by ID.
"""

from __future__ import annotations
from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import time
from typing import Any
from rich.markdown import Markdown
from rich.text import Text
from coderai.ui.shell.output import OutputStore


def _is_check_command(command: str) -> bool:
    """Recognize common verification invocations, rather than filename substrings."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    clauses: list[list[str]] = [[]]
    for word in words:
        if word and all(char in ";&|" for char in word):
            clauses.append([])
        else:
            clauses[-1].append(word)
    for args in clauses:
        while args and "=" in args[0]:
            args = args[1:]
        if not args:
            continue
        program = Path(args[0]).name
        if program in {"uv", "poetry", "pipenv"} and args[1:2] == ["run"]:
            args = args[2:]
            if not args:
                continue
            program = Path(args[0]).name
        if program in {"pytest", "ruff", "mypy", "pyright", "tox", "nox"}:
            return True
        if program.startswith("python") and args[1:2] == ["-m"]:
            if args[2:3] and args[2] in {"pytest", "ruff", "mypy", "unittest"}:
                return True
        if program in {"make", "cargo", "npm", "pnpm", "yarn", "bun"}:
            if args[1:2] == ["run"]:
                args = [args[0], *args[2:]]
            if args[1:2] and args[1] in {
                "test",
                "check",
                "lint",
                "build",
                "typecheck",
                "clippy",
                "format-check",
            }:
                return True
    return False


class ShellPresentation:
    def __init__(self, console: Any, preferences: Any, invalidate: Any) -> None:
        self.console = console
        self.preferences = preferences
        self.invalidate = invalidate
        self.outputs = OutputStore()
        self.session_id: str | None = None
        self.pending = ""
        self.thinking = ""
        self.seen: set[str] = set()
        self.changed_files: set[str] = set()
        self.checks: list[str] = []
        self._held = 0
        self._deferred: list[Any] = []
        self.committed = ""
        self.started_at = self.last_progress = time.monotonic()
        self.finished_at: float | None = None
        self.active_tools: dict[str, str] = {}
        self.todos: list[dict] = []

    def write(self, renderable: Any) -> None:
        if self._held:
            self._deferred.append(renderable)
        elif self.console:
            self.console.print(renderable)
        else:
            print(renderable)

    @contextmanager
    def hold(self):
        """Keep completed scrollback out of focused browsers and external editors."""
        self._held += 1
        try:
            yield
        finally:
            self._held -= 1
            if not self._held:
                deferred, self._deferred = self._deferred, []
                for renderable in deferred:
                    self.write(renderable)

    def emit(self, text: str, style: str = "") -> None:
        from coderai.ui.theme import get_semantic_rich_styles

        semantic = {"green": "success", "yellow": "warning", "red": "error", "dim": "muted"}.get(
            style, style
        )
        style = get_semantic_rich_styles().get(semantic, style)
        self.write(Text(text, style=style if not self.preferences.accessible else ""))

    def switch(self, session_id: str | None) -> None:
        self.session_id = session_id
        self.pending = self.thinking = ""
        self.seen.clear()
        self.outputs.clear()
        self.todos.clear()
        self.start_turn()

    def start_turn(self) -> None:
        self.pending = self.thinking = ""
        self.committed = ""
        self.started_at = self.last_progress = time.monotonic()
        self.finished_at = None
        self.active_tools.clear()
        self.changed_files.clear()
        self.checks.clear()

    def chunk(self, text: str) -> None:
        self.pending += text
        self.last_progress = time.monotonic()
        # Commit complete Markdown blocks, leaving the parser's final block
        # in the transient preview. Final messages only render the remainder.
        if "\n" in text and not self.preferences.accessible:
            from coderai.ui.shell.visualize._markdown_boundary import find_committed_boundary

            boundary = find_committed_boundary(self.pending)
            if boundary:
                prefix, self.pending = self.pending[:boundary], self.pending[boundary:]
                self.committed += prefix
                self.write(Markdown(prefix)) if self.console else self.emit(prefix)
        self.invalidate()

    def thinking_chunk(self, text: str) -> None:
        self.thinking += text
        self.last_progress = time.monotonic()
        self.invalidate()

    def preview(self, height: int, width: int) -> list[tuple[str, str]]:
        if self.preferences.accessible:
            return []
        if not self.pending and self.thinking:
            return [("class:toolbar", "Thinking...\n")]
        if not self.pending:
            return []
        # Bound transient content by cells as well as lines. Full text commits
        # when the message completes and remains in /history.
        from prompt_toolkit.utils import get_cwidth

        wrapped = []
        tail_budget = max(1, height // 3) * max(1, width) * 3
        for line in self.pending[-tail_budget:].splitlines():
            row = ""
            cells = 0
            for char in line:
                size = get_cwidth(char)
                if cells + size > max(1, width - 2):
                    wrapped.append(row)
                    row, cells = "", 0
                row += char
                cells += size
            wrapped.append(row)
        return [("", "\n".join(wrapped[-max(1, height // 3) :]) + "\n")]

    def message(self, message: Any, should_connect: bool = False) -> None:
        if message.session_id != self.session_id or message.id in self.seen:
            return
        self.seen.add(message.id)
        self.last_progress = time.monotonic()
        meta = message.meta or {}
        if meta.get("asThinking"):
            if self.preferences.detail == "verbose":
                self.emit(message.content, "dim")
            return
        if message.thinking and self.preferences.detail == "verbose":
            self.emit(message.thinking, "muted")
        if message.role == "tool":
            from coderai.ui.shell.visualize._tool_cards import parse_tool_message

            name, summary, ok, metadata = parse_tool_message(message)
            self.active_tools.pop(message.tool_call_id or message.id, None)
            self.outputs.retain(message.tool_call_id or message.id, name, message.content)
            try:
                payload = json.loads(message.content)
            except ValueError:
                payload = {}
            output = (
                payload.get("output", message.content)
                if isinstance(payload, dict)
                else message.content
            )
            title = f"{'OK' if ok else 'FAIL'} {name}"
            if not ok or not isinstance(output, str) or not output:
                title += f": {summary}"
            self.emit(title, "success" if ok else "error")
            if isinstance(output, str):
                limit = 40 if self.preferences.detail == "verbose" else 6
                lines = output.splitlines()
                preview = "\n".join(lines[:limit])[
                    : 8000 if self.preferences.detail == "verbose" else 1600
                ]
                self.emit(preview)
                if len(preview) < len(output):
                    self.emit(
                        f"{len(output) - len(preview):,} more characters: /output {message.tool_call_id or message.id}",
                        "dim",
                    )
            metadata = metadata or {}
            if isinstance(metadata.get("todos"), list):
                self.todos = metadata["todos"]
            if ok and name in ("edit", "write", "apply_patch", "str_replace_editor"):
                path = metadata.get("file_path") or metadata.get("target_path")
                if path:
                    self.changed_files.add(str(path))
            command = str(metadata.get("command", ""))
            if _is_check_command(command):
                self.checks.append(f"{'passed' if ok else 'failed'}: {command}")
            return
        content = message.content or ""
        if self.committed and content.startswith(self.committed):
            content = content[len(self.committed) :]
        self.pending = self.thinking = self.committed = ""
        if content:
            if self.console and not self.preferences.accessible:
                self.write(Markdown(content))
            else:
                self.emit(content)
        for call in message.tool_calls or []:
            name = (
                call.get("function", {}).get("name", "tool") if isinstance(call, dict) else "tool"
            )
            self.emit(f"Invoking {name}...", "dim")
            if isinstance(call, dict):
                self.active_tools[str(call.get("id", name))] = name
        self.invalidate()

    def complete(self, outcome: str) -> None:
        self.finished_at = time.monotonic()
        if self.pending:
            self.emit(self.pending)
        self.pending = self.thinking = self.committed = ""
        self.active_tools.clear()
        files = ", ".join(sorted(self.changed_files)) or "none reported"
        checks = "; ".join(self.checks) or "none reported"
        pending = "none reported" if outcome in ("ready", "complete", "completed") else outcome
        self.emit(
            f"Turn {outcome} | changed files: {files} | checks: {checks} | pending: {pending}",
            "dim",
        )
        if self.changed_files:
            self.emit("/diff inspects changes; /undo selects the restoration scope.", "dim")
