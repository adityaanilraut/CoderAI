"""CPython Isolated Code Execution Runtime.

Implements the DeepSeek Harness dsh-code-runtime specification with lossless data validation,
bounded execution deadlines, stream redirection, and host function bindings.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import pathlib
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_CODE_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_CHARS = 50_000
TRUNCATION_MARKER = "\n... [Output truncated after reaching length limit] ..."


@dataclass
class CodeExecutionOutcome:
    """Standardized output from code runtime execution."""

    stdout: str = ""
    stderr: str = ""
    result: Any = None
    success: bool = True
    error: str | None = None
    duration_ms: float = 0.0
    variables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "result": self.result,
            "success": self.success,
            "error": self.error,
            "durationMs": self.duration_ms,
            "variables": self.variables,
        }


def validate_lossless_json_value(val: Any) -> bool:
    """Verify that a value contains only lossless JSON-serializable types."""
    if val is None or isinstance(val, (bool, str)):
        return True
    if isinstance(val, int):
        return True
    if isinstance(val, float):
        return not (math.isnan(val) or math.isinf(val))
    if isinstance(val, (list, tuple)):
        return all(validate_lossless_json_value(x) for x in val)
    if isinstance(val, dict):
        return all(isinstance(k, str) and validate_lossless_json_value(v) for k, v in val.items())
    return False


class PythonCodeRuntime:
    """CPython execution engine with variable retention, host bindings, and stdout/stderr capture."""

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.globals: dict[str, Any] = {}
        self.reset()

    def reset(self) -> None:
        self.globals = {
            "__name__": "__coderai_runtime__",
            "__doc__": None,
            "project_root": self.project_root,
        }

    def register_binding(self, name: str, func: Callable[..., Any]) -> None:
        """Register a host callable function exposed directly into the runtime global scope."""
        self.globals[name] = func

    async def execute(
        self,
        code: str,
        timeout_seconds: float = DEFAULT_CODE_TIMEOUT_SECONDS,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> CodeExecutionOutcome:
        """Execute a Python snippet asynchronously in worker thread with timeout enforcement."""
        loop = asyncio.get_running_loop()
        start_time = time.time()

        def _run_in_thread() -> CodeExecutionOutcome:
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            eval_result = None
            err_msg = None
            success = True

            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                try:
                    # Compile to support evaluating the last expression if present
                    import ast
                    parsed = ast.parse(code)
                    if parsed.body and isinstance(parsed.body[-1], ast.Expr):
                        last_expr = parsed.body.pop()
                        if parsed.body:
                            compiled_stmts = compile(parsed, "<code_mode>", "exec")
                            exec(compiled_stmts, self.globals)
                        compiled_expr = compile(ast.Expression(last_expr.value), "<code_mode>", "eval")
                        eval_result = eval(compiled_expr, self.globals)
                    else:
                        compiled = compile(code, "<code_mode>", "exec")
                        exec(compiled, self.globals)
                except Exception as exc:
                    success = False
                    err_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

            raw_stdout = stdout_buf.getvalue()
            raw_stderr = stderr_buf.getvalue()

            if len(raw_stdout) > max_output_chars:
                raw_stdout = raw_stdout[:max_output_chars] + TRUNCATION_MARKER
            if len(raw_stderr) > max_output_chars:
                raw_stderr = raw_stderr[:max_output_chars] + TRUNCATION_MARKER

            active_vars = [
                k
                for k in self.globals.keys()
                if not k.startswith("__") and not callable(self.globals[k])
            ]

            elapsed_ms = (time.time() - start_time) * 1000.0
            return CodeExecutionOutcome(
                stdout=raw_stdout,
                stderr=raw_stderr,
                result=eval_result,
                success=success,
                error=err_msg,
                duration_ms=elapsed_ms,
                variables=active_vars,
            )

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _run_in_thread),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            elapsed_ms = (time.time() - start_time) * 1000.0
            return CodeExecutionOutcome(
                success=False,
                error=f"TimeoutError: Code execution exceeded {timeout_seconds}s limit.",
                duration_ms=elapsed_ms,
            )
