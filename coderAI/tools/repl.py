"""Python REPL tool for interactive code execution."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, Field

from .base import Tool
from ..config import config_manager

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
    parameters_model = PythonREPLParams
    requires_confirmation = True

    async def execute(
        self,
        code: str,
        timeout: int = 30,
        working_dir: str = ".",
    ) -> Dict[str, Any]:
        """Execute Python code in a subprocess."""
        try:
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

                python_cmd = shutil.which("python3") or shutil.which("python") or "python3"

                process = await asyncio.create_subprocess_exec(
                    python_cmd,
                    script_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=working_dir,
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    return {
                        "success": False,
                        "error": f"Execution timed out after {timeout} seconds",
                        "error_code": "timeout",
                        "hint": "Increase the timeout or simplify the code.",
                    }

                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")

                # Truncate large output
                max_output = config_manager.load().max_command_output
                if len(stdout_str) > max_output:
                    stdout_str = (
                        stdout_str[: max_output // 2]
                        + f"\n\n... [truncated {len(stdout_str) - max_output} chars] ...\n\n"
                        + stdout_str[-max_output // 2 :]
                    )
                if len(stderr_str) > max_output:
                    stderr_str = stderr_str[:max_output]

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
