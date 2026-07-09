"""Python REPL tool for interactive code execution."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, Field

from coderAI.core.services import get_services
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.proc import kill_process_group, new_session_kwargs, scrub_env
from coderAI.system.safeguards import truncate_output
from coderAI.tools.base import Tool
from coderAI.tools.terminal import _resolve_working_dir

logger = logging.getLogger(__name__)


class PythonREPLParams(BaseModel):
    code: str = Field(
        ...,
        description=(
            "Python code to execute. Can be a single expression or a multi-line script. "
            "Use print() to see output. The code runs in a fresh subprocess."
        ),
    )
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds (default: 30).",
    )
    working_dir: str = Field(
        ".",
        description="Working directory for the script (default: current directory).",
    )


class PythonREPLTool(Tool):
    """Execute Python code in an isolated subprocess and return the output."""

    name = "python_repl"
    description = (
        "Execute Python code in an isolated subprocess and return stdout/stderr. "
        "Useful for quick calculations, data exploration, testing snippets, "
        "parsing files, or running one-off scripts. The code runs in a fresh "
        "Python process each time, so state is not preserved between calls."
    )
    category = "repl"
    parameters_model = PythonREPLParams
    requires_confirmation = True
    # Arbitrary code execution — no blanket allow, and no safe scope to bind to.
    high_risk_no_blanket = True

    async def execute(  # type: ignore[override]
        self,
        code: str,
        timeout: int = 30,
        working_dir: str = ".",
    ) -> Dict[str, Any]:
        """Execute Python code in a subprocess."""
        try:
            timeout = max(1, min(timeout, 3600))

            resolved_cwd, cwd_err = _resolve_working_dir(working_dir)
            if cwd_err:
                return {"success": False, "error": cwd_err, "error_code": ToolErrorCode.SCOPE}

            # Write code to a temp file to avoid shell escaping issues
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                prefix="coderai_repl_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(code)
                script_path = f.name

            try:
                import shutil
                import sys

                # ``sys.executable`` is the interpreter currently running
                # CoderAI — the most reliable choice and the only one that
                # works out of the box on Windows, where a bare ``python3`` is
                # often missing or a non-functional Store stub.
                python_cmd = (
                    sys.executable or shutil.which("python3") or shutil.which("python") or "python3"
                )

                # ``python_repl`` runs unsandboxed model-authored code. Scrub
                # secret-bearing env vars so an injected snippet cannot read
                # ``$OPENAI_API_KEY`` etc., and isolate the process group so a
                # timeout can reap any children it spawned.
                process = await asyncio.create_subprocess_exec(
                    python_cmd,
                    script_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(resolved_cwd),
                    env=scrub_env(),
                    **new_session_kwargs(),
                )

                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    # Kill the whole group and reap it — a bare ``process.kill()``
                    # without ``wait()`` leaves a zombie and orphans children.
                    kill_process_group(process)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        pass
                    return {
                        "success": False,
                        "error": f"Execution timed out after {timeout} seconds",
                        "error_code": ToolErrorCode.TIMEOUT,
                        "hint": "Increase the timeout or simplify the code.",
                    }

                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")

                # Truncate large output — head+tail for stdout (keep the tail,
                # where tracebacks/results land), head-only for stderr.
                max_output = get_services().config.max_command_output
                stdout_str, _ = truncate_output(stdout_str, max_chars=max_output, mode="head_tail")
                stderr_str, _ = truncate_output(stderr_str, max_chars=max_output, mode="head")

                return {
                    "success": process.returncode == 0,
                    "returncode": process.returncode,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                }
            finally:
                # Clean up temp file
                try:
                    Path(script_path).unlink()
                except Exception:
                    pass

        except Exception as e:
            return {"success": False, "error": str(e)}
