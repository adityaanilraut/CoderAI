"""Package management tool — safe package installation and dependency management."""

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

PACKAGE_MANAGERS: Dict[str, Dict[str, Any]] = {
    "pip": {
        "cmd": "pip",
        "install_cmd": ["install"],
        "uninstall_cmd": ["uninstall", "-y"],
        "list_cmd": ["list", "--format=json"],
        "list_outdated_cmd": ["list", "--outdated", "--format=json"],
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt", "Pipfile"},
        "lock_file": None,
        "timeout": 180,
    },
    "pip3": {
        "cmd": "pip3",
        "install_cmd": ["install"],
        "uninstall_cmd": ["uninstall", "-y"],
        "list_cmd": ["list", "--format=json"],
        "list_outdated_cmd": ["list", "--outdated", "--format=json"],
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt", "Pipfile"},
        "lock_file": None,
        "timeout": 180,
    },
    "npm": {
        "cmd": "npm",
        "install_cmd": ["install", "--no-audit", "--no-fund"],
        "uninstall_cmd": ["uninstall"],
        "list_cmd": ["ls", "--depth=0", "--json"],
        "list_outdated_cmd": ["outdated", "--json"],
        "detect_files": {"package.json"},
        "lock_file": "package-lock.json",
        "timeout": 300,
    },
    "yarn": {
        "cmd": "yarn",
        "install_cmd": ["add"],
        "uninstall_cmd": ["remove"],
        "list_cmd": ["list", "--depth=0", "--json"],
        "list_outdated_cmd": ["outdated", "--json"],
        "detect_files": {"yarn.lock"},
        "lock_file": "yarn.lock",
        "timeout": 300,
    },
    "pnpm": {
        "cmd": "pnpm",
        "install_cmd": ["add"],
        "uninstall_cmd": ["remove"],
        "list_cmd": ["list", "--depth=0", "--json"],
        "list_outdated_cmd": ["outdated", "--json"],
        "detect_files": {"pnpm-lock.yaml"},
        "lock_file": "pnpm-lock.yaml",
        "timeout": 300,
    },
    "bun": {
        "cmd": "bun",
        "install_cmd": ["add"],
        "uninstall_cmd": ["remove"],
        "list_cmd": ["pm", "ls"],
        "list_outdated_cmd": [],
        "detect_files": {"bun.lock", "bun.lockb"},
        "lock_file": "bun.lock",
        "timeout": 300,
    },
    "cargo": {
        "cmd": "cargo",
        "install_cmd": ["add"],
        "uninstall_cmd": ["remove"],
        "list_cmd": ["tree", "--depth=0"],
        "list_outdated_cmd": ["outdated", "-R"],
        "detect_files": {"Cargo.toml"},
        "lock_file": "Cargo.lock",
        "timeout": 300,
    },
    "go": {
        "cmd": "go",
        "install_cmd": ["get"],
        "uninstall_cmd": [],  # go get with @none pattern
        "list_cmd": ["list", "-m", "-json", "all"],
        "list_outdated_cmd": [],
        "detect_files": {"go.mod"},
        "lock_file": "go.sum",
        "timeout": 300,
    },
}

_DETECTION_ORDER = ["pip3", "pip", "npm", "yarn", "pnpm", "bun", "cargo", "go"]


def detect_package_manager(project_root: str = ".") -> Optional[str]:
    """Auto-detect the package manager used by the project."""
    start_path = Path(project_root).resolve()
    if start_path.is_file():
        start_path = start_path.parent

    for current_dir in [start_path] + list(start_path.parents):
        for name in _DETECTION_ORDER:
            config = PACKAGE_MANAGERS[name]
            for detect_file in config["detect_files"]:
                if (current_dir / detect_file).exists():
                    if name == "pip3" and shutil.which("pip3"):
                        return "pip3"
                    if name == "pip" and not shutil.which("pip3") and shutil.which("pip"):
                        return "pip"
                    if name in ("pip", "pip3"):
                        continue
                    if shutil.which(name):
                        return name
        if (current_dir / ".git").exists():
            break

    return None


def _validate_package_name(package: str, manager: str) -> Optional[str]:
    """Simple package-name validation to reject shell injection attempts."""
    if not package or not package.strip():
        return "Package name cannot be empty."
    dangerous_chars = [";", "|", "&", "$", "`", "(", ")", "{", "}", "<", ">", "\n", "\r", "'", '"']
    for ch in dangerous_chars:
        if ch in package:
            return f"Package name contains unsafe character: {ch!r}. Use a simple package name only."
    if len(package) > 256:
        return "Package name too long (max 256 characters)."
    return None


class PackageManagerParams(BaseModel):
    action: str = Field(..., description="Action: install, uninstall, list, outdated, info")
    package: Optional[str] = Field(None, description="Package name (required for install/uninstall/info)")
    version: Optional[str] = Field(None, description="Package version constraint (e.g., '>=2.0', '@latest', '@1.2.3')")
    manager: Optional[str] = Field(None, description="Package manager (pip, pip3, npm, yarn, pnpm, bun, cargo, go). Auto-detected if omitted.")
    dev: bool = Field(False, description="Install as a dev dependency (default: false)")
    max_results: int = Field(20, description="Maximum packages to list (default: 20)")


class PackageManagerTool(Tool):
    """Tool for safe package installation and dependency management."""

    name = "package_manager"
    description = (
        "Install, uninstall, list, or check outdated packages using the project's package manager. "
        "Auto-detects pip, npm, yarn, pnpm, bun, cargo, or go based on project files. "
        "Safe: validates package names to prevent shell injection. "
        "Use 'install' to add a new dependency, 'uninstall' to remove one, "
        "'list' to see installed packages, 'outdated' to check for updates."
    )
    parameters_model = PackageManagerParams
    is_read_only = False
    requires_confirmation = True
    timeout = None
    category = "other"

    async def execute(
        self,
        action: str,
        package: Optional[str] = None,
        version: Optional[str] = None,
        manager: Optional[str] = None,
        dev: bool = False,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        try:
            action = action.strip().lower()
            if action not in ("install", "uninstall", "list", "outdated", "info"):
                return {
                    "success": False,
                    "error": f"Unknown action: {action}. Supported: install, uninstall, list, outdated, info.",
                }

            if action in ("install", "uninstall", "info") and not package:
                return {
                    "success": False,
                    "error": f"Action '{action}' requires a package name.",
                }

            manager_name = manager or detect_package_manager(".")
            if not manager_name:
                return {
                    "success": False,
                    "error": (
                        "No supported package manager detected. "
                        "Supported: pip, npm, yarn, pnpm, bun, cargo, go. "
                        "Specify one with 'manager' parameter."
                    ),
                    "detected_files_checked": True,
                }

            if manager_name not in PACKAGE_MANAGERS:
                return {
                    "success": False,
                    "error": f"Unknown package manager: {manager_name}. Supported: {', '.join(PACKAGE_MANAGERS)}",
                }

            config = PACKAGE_MANAGERS[manager_name]
            cmd_binary = config["cmd"]

            if not shutil.which(cmd_binary):
                return {
                    "success": False,
                    "error": (
                        f"Package manager binary '{cmd_binary}' not found on PATH. "
                        f"Install {manager_name} or specify a different manager."
                    ),
                }

            if package:
                validation_error = _validate_package_name(package, manager_name)
                if validation_error:
                    return {"success": False, "error": validation_error}
                pkg_with_version = f"{package}@{version}" if version and manager_name in ("npm", "yarn", "pnpm", "bun") else package
                if version and manager_name not in ("npm", "yarn", "pnpm", "bun"):
                    if version.startswith(">=") or version.startswith("==") or version.startswith("~"):
                        pkg_with_version = f"{package}{version}"
                    else:
                        pkg_with_version = f"{package}>={version}"

            cmd: List[str] = [cmd_binary]

            if action == "install":
                cmd.extend(config["install_cmd"])
                if dev:
                    if manager_name in ("npm", "pnpm"):
                        cmd.append("--save-dev")
                    elif manager_name == "yarn":
                        cmd.append("--dev")
                    elif manager_name == "bun":
                        cmd.append("--dev")
                    elif manager_name == "pip":
                        pass  # dev deps via requirements-dev.txt, not CLI flag
                cmd.append(package)
                if version:
                    if manager_name == "pip":
                        cmd.append(pkg_with_version)
                    elif manager_name in ("npm", "yarn", "pnpm", "bun"):
                        # Already included in pkg_with_version
                        if pkg_with_version != package:
                            cmd[-1] = pkg_with_version
                    elif manager_name == "cargo":
                        pass

            elif action == "uninstall":
                if manager_name == "go":
                    if not package:
                        return {"success": False, "error": "go modules use 'go get package@none' for removal; specify a package."}
                    cmd = [cmd_binary, "get", f"{package}@none"]
                else:
                    uninstall_cmd = list(config["uninstall_cmd"])
                    if not uninstall_cmd:
                        return {
                            "success": False,
                            "error": f"{manager_name} does not have a dedicated uninstall command.",
                        }
                    cmd.extend(uninstall_cmd)
                    cmd.append(package)

            elif action == "list":
                cmd.extend(config["list_cmd"])

            elif action == "outdated":
                if not config["list_outdated_cmd"]:
                    return {
                        "success": False,
                        "error": f"Outdated checking is not supported for {manager_name}.",
                    }
                cmd.extend(config["list_outdated_cmd"])

            elif action == "info":
                if manager_name == "pip":
                    cmd = [cmd_binary, "show", package]
                elif manager_name == "npm":
                    cmd = [cmd_binary, "info", package, "--json"]
                elif manager_name in ("yarn", "pnpm"):
                    cmd = [cmd_binary, "info", package, "--json"]
                elif manager_name == "bun":
                    cmd = [cmd_binary, "pm", "info", package]
                elif manager_name == "cargo":
                    cmd = [cmd_binary, "search", package, "--limit", "1"]
                elif manager_name == "go":
                    cmd = [cmd_binary, "doc", package]
                else:
                    cmd.extend(config["list_cmd"])

            effective_timeout = config.get("timeout", 180)

            project_root = Path(".").resolve()

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_root),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return {
                    "success": False,
                    "error": f"Package manager operation timed out after {effective_timeout} seconds.",
                }

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            max_output = 8000
            if len(stdout_str) > max_output:
                stdout_str = stdout_str[:max_output] + "\n... [truncated]"

            result: Dict[str, Any] = {
                "success": process.returncode == 0,
                "action": action,
                "manager": manager_name,
                "returncode": process.returncode,
                "stdout": stdout_str,
            }

            if stderr_str.strip():
                truncated_stderr = stderr_str[:4000]
                result["stderr"] = truncated_stderr

            if package:
                result["package"] = package
            if version:
                result["version"] = version
            if dev:
                result["dev_dependency"] = True

            if action == "list" and stdout_str.strip():
                try:
                    parsed = json.loads(stdout_str)
                    if isinstance(parsed, dict):
                        deps = parsed.get("dependencies", parsed.get("packages", parsed))
                        if isinstance(deps, dict):
                            deps = list(deps.keys())
                        if isinstance(deps, list):
                            result["packages"] = deps[:max_results]
                            result["total_count"] = len(deps)
                except (json.JSONDecodeError, TypeError):
                    pass

            if action == "outdated":
                try:
                    parsed = json.loads(stdout_str)
                    if isinstance(parsed, (dict, list)):
                        result["outdated"] = parsed if isinstance(parsed, list) else list(parsed.keys())
                except (json.JSONDecodeError, TypeError):
                    pass

            result["message"] = self._format_message(action, manager_name, package, process.returncode == 0)
            return result

        except Exception as e:
            logger.exception("package_manager failed")
            return {"success": False, "error": str(e)}

    def _format_message(self, action: str, manager: str, package: Optional[str], success: bool) -> str:
        if action == "install":
            return (
                f"Successfully installed {package} with {manager}."
                if success
                else f"Failed to install {package} with {manager}."
            )
        elif action == "uninstall":
            return (
                f"Successfully uninstalled {package} with {manager}."
                if success
                else f"Failed to uninstall {package} with {manager}."
            )
        elif action == "list":
            return f"Listed installed packages with {manager}."
        elif action == "outdated":
            return f"Checked outdated packages with {manager}."
        elif action == "info":
            return (
                f"Retrieved info for {package} with {manager}."
                if success
                else f"Failed to get info for {package} with {manager}."
            )
        return f"Package manager {action} completed."
