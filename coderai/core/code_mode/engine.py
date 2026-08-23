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
import sys
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
        self.code_history: list[str] = []
        self.reset()

    def reset(self) -> None:
        """Reset the sandbox state to initial defaults."""
        self.code_history = []
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

    async def execute(
        self, code: str, timeout_seconds: float = 30.0, use_subprocess: bool = True
    ) -> CodeModeResult:
        """Execute code in the stateful sandbox, capturing output and evaluating expressions.

        When use_subprocess is True (default), executes within an isolated CPython worker
        subprocess (port of @deepseek-ai/dsh-code-runtime-python) with process-group isolation,
        timeout termination, and structured JSON IPC for variables and result extraction.
        """
        start_time = time.time()

        if use_subprocess:
            try:
                res = await self._execute_subprocess(code, timeout_seconds)
                res.duration_seconds = max(0.0, time.time() - start_time)
                return res
            except Exception as e:
                logger.warning(f"Subprocess code runtime fallback due to: {e}")

        # In-process execution fallback
        return await self._execute_in_process(code, timeout_seconds, start_time)

    async def _execute_subprocess(self, code: str, timeout_seconds: float) -> CodeModeResult:
        """Run code inside an isolated CPython worker subprocess."""
        import json
        import tempfile

        # Prepare serializable state snapshot
        state_payload = {}
        for k, v in self.globals.items():
            if not k.startswith("_") and k not in (
                "project_root",
                "read_file",
                "write_file",
                "edit_file",
                "glob_search",
                "grep_search",
                "run_command",
            ):
                try:
                    json.dumps(v)
                    state_payload[k] = v
                except (TypeError, OverflowError):
                    state_payload[k] = repr(v)

        runner_script = """
import ast
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import traceback

def main():
    if len(sys.argv) < 3:
        sys.exit(1)
    
    code_path = sys.argv[1]
    data_path = sys.argv[2]
    
    with open(code_path, "r", encoding="utf-8") as f:
        code = f.read()
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    project_root = data.get("project_root", os.getcwd())
    initial_vars = data.get("variables", {})
    history_code = data.get("history_code", "")

    def _resolve(fp):
        p = pathlib.Path(fp)
        return str(p.resolve() if p.is_absolute() else (pathlib.Path(project_root) / p).resolve())

    def _read_file(fp, offset=None, limit=None):
        with open(_resolve(fp), "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = max(0, (offset or 1) - 1)
        end = start + limit if limit else len(lines)
        return "".join(lines[start:end])

    def _write_file(fp, content):
        target = _resolve(fp)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    def _edit_file(fp, old_str, new_str):
        target = _resolve(fp)
        with open(target, "r", encoding="utf-8") as f:
            text = f.read()
        if old_str not in text:
            raise ValueError(f"old_str not found in {fp}")
        with open(target, "w", encoding="utf-8") as f:
            f.write(text.replace(old_str, new_str, 1))
        return True

    def _glob_search(pattern, path="."):
        base = pathlib.Path(_resolve(path))
        if not base.exists():
            return []
        return sorted([str(p.relative_to(base)) for p in base.glob(pattern) if p.is_file()])

    def _grep_search(pattern, path=".", include=None):
        import re
        base = pathlib.Path(_resolve(path))
        regex = re.compile(pattern)
        results = []
        for root, _, files in os.walk(base):
            for file in files:
                if include and not pathlib.Path(file).match(include):
                    continue
                fp = pathlib.Path(root, file)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for line_idx, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append({
                                    "file": str(fp.relative_to(base)),
                                    "line": line_idx,
                                    "content": line.rstrip(),
                                })
                except Exception:
                    continue
        return results

    def _run_command(cmd, timeout=30.0):
        res = subprocess.run(cmd, shell=True, cwd=project_root, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr

    env_globals = {
        "__name__": "__code_mode__",
        "__doc__": None,
        "project_root": project_root,
        "read_file": _read_file,
        "write_file": _write_file,
        "edit_file": _edit_file,
        "glob_search": _glob_search,
        "grep_search": _grep_search,
        "run_command": _run_command,
    }
    env_globals.update(initial_vars)

    if history_code:
        try:
            compiled_hist = compile(ast.parse(history_code, filename="<code_mode_history>", mode="exec"), filename="<code_mode_history>", mode="exec")
            exec(compiled_hist, env_globals)
        except Exception:
            pass

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    eval_res = None
    err = None

    try:
        tree = ast.parse(code, filename="<code_mode>", mode="exec")
        last_expr = None
        statements = tree.body
        if statements and isinstance(statements[-1], ast.Expr):
            last_expr = ast.Expression(statements[-1].value)
            statements = statements[:-1]

        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            if statements:
                compiled_stmts = compile(ast.Module(body=statements, type_ignores=[]), filename="<code_mode>", mode="exec")
                exec(compiled_stmts, env_globals)
            if last_expr is not None:
                compiled_expr = compile(last_expr, filename="<code_mode>", mode="eval")
                eval_res = eval(compiled_expr, env_globals)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}\\n{traceback.format_exc()}"

    # Extract user variables
    user_vars = {}
    for k, v in env_globals.items():
        if not k.startswith("_") and k not in (
            "project_root", "read_file", "write_file", "edit_file",
            "glob_search", "grep_search", "run_command",
        ):
            try:
                json.dumps(v)
                user_vars[k] = v
            except Exception:
                user_vars[k] = repr(v)

    out_payload = {
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "result": repr(eval_res) if eval_res is not None else None,
        "raw_result": eval_res if isinstance(eval_res, (int, float, str, bool, list, dict, type(None))) else repr(eval_res),
        "variables": sorted(list(user_vars.keys())),
        "variable_values": user_vars,
        "error": err,
    }

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(out_payload, f)

if __name__ == "__main__":
    main()
"""

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = pathlib.Path(tmpdir)
            code_file = tmp_path / "user_code.py"
            data_file = tmp_path / "data.json"
            runner_file = tmp_path / "runner.py"

            code_file.write_text(code, encoding="utf-8")
            data_file.write_text(
                json.dumps({
                    "project_root": self.project_root,
                    "variables": state_payload,
                    "history_code": "\n".join(self.code_history),
                }),
                encoding="utf-8",
            )
            runner_file.write_text(runner_script, encoding="utf-8")

            cmd = [sys.executable, "-u", str(runner_file), str(code_file), str(data_file)]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return CodeModeResult(
                    error=f"TimeoutError: Code execution exceeded {timeout_seconds}s limit in isolated subprocess.",
                    duration_seconds=timeout_seconds,
                )

            if data_file.exists():
                try:
                    result_data = json.loads(data_file.read_text(encoding="utf-8"))
                    # Update local state variables
                    for k, v in result_data.get("variable_values", {}).items():
                        self.globals[k] = v
                    if result_data.get("error") is None:
                        self.code_history.append(code)
                    return CodeModeResult(
                        stdout=result_data.get("stdout", ""),
                        stderr=result_data.get("stderr", ""),
                        result=result_data.get("raw_result"),
                        variables=result_data.get("variables", []),
                        error=result_data.get("error"),
                    )
                except Exception as e:
                    return CodeModeResult(
                        stdout=out_b.decode(errors="replace") if out_b else "",
                        stderr=err_b.decode(errors="replace") if err_b else "",
                        error=f"RuntimeIPCError: Failed to parse subprocess results: {e}",
                    )

            return CodeModeResult(
                stdout=out_b.decode(errors="replace") if out_b else "",
                stderr=err_b.decode(errors="replace") if err_b else "",
                error=f"Subprocess terminated with code {proc.returncode} without writing output payload.",
            )

    async def _execute_in_process(
        self, code: str, timeout_seconds: float, start_time: float
    ) -> CodeModeResult:
        """In-process execution fallback."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        def _run_sync() -> tuple[Any, str | None]:
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
