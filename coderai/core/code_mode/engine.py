"""Code Mode & Interactive Execution Engine for CoderAI.

Provides stateful, sandboxed Python code execution with direct access to workspace
tools, variable retention across turns, AST expression evaluation, and stdout/stderr capture.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import logging
import os
import pathlib
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CodeModeResult:
    """Output from a code_mode execution."""

    stdout: str = ""
    stderr: str = ""
    result: Any = None
    variables: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "result": repr(self.result) if self.result is not None else None,
            "variables": self.variables,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
        }

    def format_markdown(self) -> str:
        lines = ["### Code Execution Result"]
        if self.error:
            lines.append(f"\n> ❌ **Error**: {self.error}\n")
        if self.stdout:
            lines.append(f"**Standard Output**:\n```\n{self.stdout.strip()}\n```")
        if self.stderr:
            lines.append(f"**Standard Error**:\n```\n{self.stderr.strip()}\n```")
        if self.result is not None:
            lines.append(f"**Evaluation Value**:\n```python\n{repr(self.result)}\n```")
        if self.variables:
            lines.append(f"**Active Variables**: `{', '.join(self.variables)}`")
        if not self.stdout and not self.stderr and self.result is None and not self.error:
            lines.append("*(Execution completed with no output)*")
        return "\n".join(lines)


class CodeModeSandbox:
    """Stateful execution sandbox retaining variables across multiple code_mode turns."""

    def __init__(self, project_root: str) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.globals: dict[str, Any] = {}
        self.reset()

    def reset(self) -> None:
        """Reset the sandbox state to initial defaults."""
        self.globals = {
            "__name__": "__code_mode__",
            "__doc__": None,
            "project_root": self.project_root,
            # Helper workspace tools
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "glob_search": self._glob_search,
            "grep_search": self._grep_search,
            "run_command": self._run_command,
        }

    def _resolve_path(self, file_path: str) -> str:
        p = pathlib.Path(file_path)
        if not p.is_absolute():
            p = pathlib.Path(self.project_root, p)
        return str(p.resolve())

    def _read_file(
        self, file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        target = self._resolve_path(file_path)
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = max(0, (offset or 1) - 1)
        end = start + limit if limit else len(lines)
        return "".join(lines[start:end])

    def _write_file(self, file_path: str, content: str) -> None:
        target = self._resolve_path(file_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    def _edit_file(self, file_path: str, old_str: str, new_str: str) -> bool:
        target = self._resolve_path(file_path)
        with open(target, "r", encoding="utf-8") as f:
            text = f.read()
        if old_str not in text:
            raise ValueError(f"old_str not found in {file_path}")
        text = text.replace(old_str, new_str, 1)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        return True

    def _glob_search(self, pattern: str, path: str = ".") -> list[str]:
        base = pathlib.Path(self._resolve_path(path))
        if not base.exists():
            return []
        matches = [str(p.relative_to(base)) for p in base.glob(pattern) if p.is_file()]
        return sorted(matches)

    def _grep_search(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> list[dict[str, Any]]:
        import re

        base = pathlib.Path(self._resolve_path(path))
        regex = re.compile(pattern)
        results: list[dict[str, Any]] = []

        for root, _, files in os.walk(base):
            for file in files:
                if include and not pathlib.Path(file).match(include):
                    continue
                fp = pathlib.Path(root, file)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for line_idx, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append(
                                    {
                                        "file": str(fp.relative_to(base)),
                                        "line": line_idx,
                                        "content": line.rstrip(),
                                    }
                                )
                except Exception:
                    continue
        return results

    def _run_command(self, cmd: str, timeout: float = 30.0) -> tuple[int, str, str]:
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.returncode, res.stdout, res.stderr

    async def execute(self, code: str, timeout_seconds: float = 30.0) -> CodeModeResult:
        """Execute code in the stateful sandbox, capturing output and evaluating expressions."""
        start_time = time.time()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        def _run_sync() -> tuple[Any, str | None]:
            # Parse AST to support evaluating the last expression
            try:
                tree = ast.parse(code, filename="<code_mode>", mode="exec")
            except SyntaxError as e:
                return None, f"SyntaxError: {e}"

            last_expr = None
            statements = tree.body

            if statements and isinstance(statements[-1], ast.Expr):
                last_expr = ast.Expression(statements[-1].value)
                statements = statements[:-1]

            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                try:
                    if statements:
                        mod = ast.Module(body=statements, type_ignores=[])
                        compiled_stmts = compile(mod, filename="<code_mode>", mode="exec")
                        exec(compiled_stmts, self.globals)

                    eval_result = None
                    if last_expr is not None:
                        compiled_expr = compile(last_expr, filename="<code_mode>", mode="eval")
                        eval_result = eval(compiled_expr, self.globals)

                    return eval_result, None
                except Exception as exc:
                    return None, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

        try:
            eval_res, err = await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            eval_res = None
            err = f"TimeoutError: Code execution exceeded {timeout_seconds}s limit."
        except asyncio.CancelledError:
            eval_res = None
            err = "CancelledError: Code execution was cancelled."
        except Exception as e:
            eval_res = None
            err = str(e)

        duration = max(0.0, time.time() - start_time)
        user_vars = [
            k
            for k in self.globals.keys()
            if not k.startswith("_")
            and k
            not in (
                "project_root",
                "read_file",
                "write_file",
                "edit_file",
                "glob_search",
                "grep_search",
                "run_command",
            )
        ]

        return CodeModeResult(
            stdout=stdout_buf.getvalue(),
            stderr=stderr_buf.getvalue(),
            result=eval_res,
            variables=sorted(user_vars),
            error=err,
            duration_seconds=duration,
        )


_sandboxes: dict[str, CodeModeSandbox] = {}


def get_code_mode_sandbox(session_id: str, project_root: str) -> CodeModeSandbox:
    """Get or create the stateful sandbox for a given session."""
    key = f"{session_id}:{project_root}"
    if key not in _sandboxes:
        _sandboxes[key] = CodeModeSandbox(project_root)
    return _sandboxes[key]


def clear_code_mode_sandbox(session_id: str) -> None:
    """Clear sandboxes associated with a session."""
    for key in list(_sandboxes.keys()):
        if key.startswith(f"{session_id}:"):
            _sandboxes.pop(key, None)
