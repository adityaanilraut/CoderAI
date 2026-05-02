"""Code formatter tool — auto-detects and runs formatters (ruff format, black, prettier, gofmt)."""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

# Formatter configurations keyed by name
FORMATTERS: Dict[str, Dict[str, Any]] = {
    "ruff": {
        "cmd": "ruff",
        "args": ["format"],
        "check_args": ["format", "--check", "--diff"],
        "extensions": {".py"},
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt", "ruff.toml", ".ruff.toml"},
    },
    "black": {
        "cmd": "black",
        "args": [],
        "check_args": ["--check", "--diff"],
        "extensions": {".py"},
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt", ".flake8"},
    },
    "prettier": {
        "cmd": "npx",
        "args": ["prettier", "--write"],
        "check_args": ["prettier", "--check"],
        "extensions": {".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json", ".md", ".yaml", ".yml"},
        "detect_files": {"package.json", ".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.yml"},
    },
    "gofmt": {
        "cmd": "gofmt",
        "args": ["-w"],
        "check_args": ["-l"],
        "extensions": {".go"},
        "detect_files": {"go.mod"},
    },
}

# Preference order when multiple formatters could apply
_FORMATTER_PREFERENCE = ["ruff", "black", "prettier", "gofmt"]


def detect_formatter(project_root: str = ".") -> Optional[str]:
    """Auto-detect the appropriate formatter for the project.

    Walks up from *project_root* to the first .git boundary, checking each
    directory for formatter indicator files.  Returns the first formatter
    whose binary is available on PATH, in preference order.
    """
    start_path = Path(project_root).resolve()
    if start_path.is_file():
        start_path = start_path.parent

    for current_dir in [start_path] + list(start_path.parents):
        for name in _FORMATTER_PREFERENCE:
            config = FORMATTERS[name]
            for detect_file in config["detect_files"]:
                if (current_dir / detect_file).exists():
                    cmd = config["cmd"]
                    # For prettier we check npx availability
                    if shutil.which(cmd):
                        return name
        if (current_dir / ".git").exists():
            break

    return None


class FormatParams(BaseModel):
    path: str = Field(".", description="File or directory path to format (default: current directory)")
    check: bool = Field(False, description="Check formatting without writing changes (default: false)")
    formatter: Optional[str] = Field(None, description="Formatter to use: ruff, black, prettier, gofmt (auto-detected if omitted)")


class FormatTool(Tool):
    """Tool for running code formatters on source files."""

    name = "format"
    description = (
        "Run a code formatter on source files. "
        "Auto-detects the formatter (ruff format, black, prettier, gofmt) based on project type. "
        "Use check=true to preview changes without writing them."
    )
    parameters_model = FormatParams
    requires_confirmation = True

    async def execute(
        self,
        path: str = ".",
        check: bool = False,
        formatter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run formatter on the given path."""
        try:
            formatter_name = formatter or detect_formatter(path)
            if not formatter_name:
                return {
                    "success": False,
                    "error": (
                        "No supported formatter detected. "
                        "Supported: ruff, black, prettier, gofmt. "
                        "Install one and ensure it is on PATH."
                    ),
                }

            if formatter_name not in FORMATTERS:
                return {
                    "success": False,
                    "error": f"Unknown formatter: {formatter_name}. Supported: {', '.join(FORMATTERS)}",
                }

            config = FORMATTERS[formatter_name]
            cmd_binary = config["cmd"]

            if not shutil.which(cmd_binary):
                return {
                    "success": False,
                    "error": f"Formatter binary '{cmd_binary}' not found on PATH. Please install {formatter_name}.",
                }

            extra = config["check_args"] if check else config["args"]

            if formatter_name in ("ruff", "black", "gofmt"):
                cmd: List[str] = [cmd_binary] + extra + [path]
            else:
                # prettier: npx prettier [--write|--check] <path>
                cmd = [cmd_binary] + extra + [path]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=".",
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
            except asyncio.TimeoutError:
                process.kill()
                return {"success": False, "error": "Formatter timed out after 60 seconds."}

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Truncate large diffs
            max_output = 8000
            output = stdout_str or stderr_str
            if len(output) > max_output:
                output = output[:max_output] + "\n... [truncated]"

            if check:
                needs_formatting = process.returncode != 0
                return {
                    "success": True,
                    "formatter": formatter_name,
                    "mode": "check",
                    "needs_formatting": needs_formatting,
                    "output": output,
                    "message": (
                        f"Formatting required ({formatter_name})."
                        if needs_formatting
                        else f"Already formatted ({formatter_name})."
                    ),
                }

            formatted = process.returncode == 0
            return {
                "success": formatted,
                "formatter": formatter_name,
                "mode": "format",
                "output": output,
                "message": (
                    f"Formatting complete ({formatter_name})."
                    if formatted
                    else f"Formatter exited with errors ({formatter_name})."
                ),
            }

        except Exception as e:
            return {"success": False, "error": str(e)}
