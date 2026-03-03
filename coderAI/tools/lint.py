"""Linter integration tool for auto-detecting and running project linters."""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

# Linter configurations: (command, check_args, fix_args, file_extensions)
LINTERS = {
    "ruff": {
        "cmd": "ruff",
        "check_args": ["check", "--output-format=json"],
        "fix_args": ["check", "--fix"],
        "extensions": {".py"},
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt", "ruff.toml", ".ruff.toml"},
    },
    "eslint": {
        "cmd": "npx",
        "check_args": ["eslint", "--format=json"],
        "fix_args": ["eslint", "--fix"],
        "extensions": {".js", ".jsx", ".ts", ".tsx"},
        "detect_files": {"package.json", ".eslintrc.json", ".eslintrc.js", ".eslintrc.yml"},
    },
    "clippy": {
        "cmd": "cargo",
        "check_args": ["clippy", "--message-format=json"],
        "fix_args": ["clippy", "--fix", "--allow-dirty"],
        "extensions": {".rs"},
        "detect_files": {"Cargo.toml"},
    },
    "golangci-lint": {
        "cmd": "golangci-lint",
        "check_args": ["run", "--out-format=json"],
        "fix_args": ["run", "--fix"],
        "extensions": {".go"},
        "detect_files": {"go.mod"},
    },
}


def detect_linter(project_root: str = ".") -> Optional[str]:
    """Auto-detect the appropriate linter for the project.

    Returns:
        Name of the detected linter, or None
    """
    root = Path(project_root).resolve()

    for linter_name, config in LINTERS.items():
        # Check if project indicator files exist
        for detect_file in config["detect_files"]:
            if (root / detect_file).exists():
                # Check if the linter binary is available
                if shutil.which(config["cmd"]):
                    return linter_name
    return None


class LintParams(BaseModel):
    path: str = Field(".", description="File or directory path to lint (default: current directory)")
    fix: bool = Field(False, description="Attempt to auto-fix lint issues (default: false)")
    linter: Optional[str] = Field(None, description="Linter to use (auto-detected if omitted)")


class LintTool(Tool):
    """Tool for running linters on code."""

    name = "lint"
    description = (
        "Run a linter on code files to check for errors, style issues, and potential bugs. "
        "Auto-detects the linter (ruff, eslint, clippy, golangci-lint) based on the project type."
    )
    parameters_model = LintParams
    is_read_only = True  # check mode is read-only; fix mode mutates but that's opt-in

    async def execute(
        self,
        path: str = ".",
        fix: bool = False,
        linter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run linter on the given path."""
        try:
            # Detect linter
            linter_name = linter or detect_linter(path)
            if not linter_name:
                return {
                    "success": False,
                    "error": "No supported linter detected. Supported: ruff, eslint, clippy, golangci-lint.",
                }

            if linter_name not in LINTERS:
                return {
                    "success": False,
                    "error": f"Unknown linter: {linter_name}. Supported: {', '.join(LINTERS)}",
                }

            config = LINTERS[linter_name]
            cmd_binary = config["cmd"]

            if not shutil.which(cmd_binary):
                return {
                    "success": False,
                    "error": f"Linter binary '{cmd_binary}' not found on PATH. Please install {linter_name}.",
                }

            # Build command
            args = config["fix_args"] if fix else config["check_args"]
            cmd = [cmd_binary] + args

            # For file-level linters, append the path
            if linter_name in ("ruff", "eslint"):
                cmd.append(path)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=path if Path(path).is_dir() else ".",
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=60
                )
            except asyncio.TimeoutError:
                process.kill()
                return {"success": False, "error": "Linter timed out after 60 seconds."}

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Truncate very large output
            max_output = 8000
            if len(stdout_str) > max_output:
                stdout_str = stdout_str[:max_output] + "\n... [truncated]"

            # Parse results
            has_issues = process.returncode != 0

            result = {
                "success": True,
                "linter": linter_name,
                "mode": "fix" if fix else "check",
                "has_issues": has_issues,
                "output": stdout_str or stderr_str,
                "returncode": process.returncode,
            }

            if fix:
                result["message"] = (
                    "Auto-fix applied. Some issues may remain."
                    if has_issues
                    else "No issues found after fix."
                )
            else:
                result["message"] = (
                    f"Found lint issues ({linter_name})."
                    if has_issues
                    else f"No lint issues found ({linter_name})."
                )

            return result

        except Exception as e:
            return {"success": False, "error": str(e)}
