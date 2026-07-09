"""Code formatter tool — auto-detects and runs formatters (ruff format, black, prettier, gofmt)."""

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.config import config_manager
from coderAI.system.proc import run_scrubbed, subprocess_timeout
from coderAI.system.safeguards import truncate_output
from coderAI.tools._detect import walk_up_detect
from coderAI.tools.base import Tool

logger = logging.getLogger(__name__)

# Formatter configurations keyed by name
FORMATTERS: Dict[str, Dict[str, Any]] = {
    "ruff": {
        "cmd": "ruff",
        "args": ["format"],
        "check_args": ["format", "--check", "--diff"],
        "extensions": {".py"},
        "detect_files": {
            "pyproject.toml",
            "setup.py",
            "requirements.txt",
            "ruff.toml",
            ".ruff.toml",
        },
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
        "extensions": {
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".css",
            ".html",
            ".json",
            ".md",
            ".yaml",
            ".yml",
        },
        "detect_files": {
            "package.json",
            ".prettierrc",
            ".prettierrc.json",
            ".prettierrc.js",
            ".prettierrc.yml",
        },
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

    def _available(name: str, _dir: Path) -> Optional[str]:
        # For prettier the binary probed is npx.
        return name if shutil.which(FORMATTERS[name]["cmd"]) else None

    return walk_up_detect(project_root, FORMATTERS, _FORMATTER_PREFERENCE, _available)


class FormatParams(BaseModel):
    path: str = Field(
        ".", description="File or directory path to format (default: current directory)"
    )
    check: bool = Field(
        False, description="Check formatting without writing changes (default: false)"
    )
    formatter: Optional[str] = Field(
        None,
        description="Formatter to use: ruff, black, prettier, gofmt (auto-detected if omitted)",
    )


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
    category = "code_quality"

    async def execute(  # type: ignore[override]
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

            # Resolve the target against the project root and run under it: the
            # formatter finds project config (e.g. prettier's .prettierrc), and
            # run_scrubbed scrubs secrets from the child env with a bounded
            # timeout + process-group kill — the raw exec did neither (finding 3).
            cfg = config_manager.load_project_config(".")
            project_root = Path(getattr(cfg, "project_root", ".") or ".").resolve()
            target = str((project_root / path).resolve())

            # Same shape for every formatter, incl. prettier: <binary> <args> <path>
            cmd: List[str] = [cmd_binary] + extra + [target]

            fmt_timeout = subprocess_timeout()
            returncode, stdout, stderr, timed_out = await run_scrubbed(
                cmd,
                cwd=str(project_root),
                timeout=fmt_timeout,
                shell=False,
            )
            if timed_out:
                return {
                    "success": False,
                    "error": f"Formatter timed out after {fmt_timeout:.0f} seconds.",
                }

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Truncate large diffs
            output, _ = truncate_output(stdout_str or stderr_str, max_chars=8000)

            if check:
                needs_formatting = returncode != 0
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

            formatted = returncode == 0
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
            return {"success": False, "error": str(e), "error_code": ToolErrorCode.TOOL_ERROR}
