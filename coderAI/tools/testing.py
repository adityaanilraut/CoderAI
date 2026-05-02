"""Test-running tool — auto-detects test framework, runs tests, and parses results."""

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

TEST_FRAMEWORKS: Dict[str, Dict[str, Any]] = {
    "pytest": {
        "cmd": "pytest",
        "args": ["-v", "--tb=short"],
        "results_patterns": ["passed", "failed", "error", "skipped", "xfailed", "xpassed"],
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt", "Pipfile"},
        "test_dirs": ["tests", "test"],
        "test_suffixes": ["test_*.py", "*_test.py"],
        "extensions": {".py"},
        "timeout": 120,
    },
    "jest": {
        "cmd": "npx",
        "args": ["jest", "--verbose", "--no-coverage"],
        "results_patterns": ["PASS", "FAIL", "Tests:", "Suites:", "Snapshots:"],
        "detect_files": {"package.json"},
        "test_dirs": ["__tests__", "tests", "test", "spec"],
        "test_suffixes": ["*.test.{js,ts,jsx,tsx}", "*.spec.{js,ts,jsx,tsx}"],
        "extensions": {".js", ".ts", ".jsx", ".tsx"},
        "timeout": 120,
    },
    "vitest": {
        "cmd": "npx",
        "args": ["vitest", "run", "--reporter=verbose"],
        "results_patterns": ["✓", "×", "PASS", "FAIL", "Tests", "passed", "failed"],
        "detect_files": {"vitest.config.ts", "vitest.config.js", "vitest.config.mjs"},
        "test_dirs": ["__tests__", "tests", "test", "spec"],
        "test_suffixes": ["*.test.{js,ts,jsx,tsx}", "*.spec.{js,ts,jsx,tsx}"],
        "extensions": {".js", ".ts", ".jsx", ".tsx"},
        "timeout": 120,
    },
    "go_test": {
        "cmd": "go",
        "args": ["test", "-v", "-count=1", "./..."],
        "results_patterns": ["--- PASS:", "--- FAIL:", "PASS", "FAIL", "ok", "?"],
        "detect_files": {"go.mod"},
        "test_dirs": [],
        "test_suffixes": ["*_test.go"],
        "extensions": {".go"},
        "timeout": 180,
    },
    "cargo_test": {
        "cmd": "cargo",
        "args": ["test", "--", "--color=always"],
        "results_patterns": ["test result:", "running", "ok.", "FAILED"],
        "detect_files": {"Cargo.toml"},
        "test_dirs": ["tests"],
        "test_suffixes": [],
        "extensions": {".rs"},
        "timeout": 300,
    },
    "unittest": {
        "cmd": "python",
        "args": ["-m", "unittest", "discover", "-v"],
        "results_patterns": ["OK", "FAILED", "ERROR", "Ran", "tests in"],
        "detect_files": {"pyproject.toml", "setup.py", "requirements.txt"},
        "test_dirs": ["tests", "test"],
        "test_suffixes": ["test_*.py", "*_test.py"],
        "extensions": {".py"},
        "timeout": 120,
    },
}

_FRAMEWORK_PREFERENCE = ["pytest", "jest", "vitest", "go_test", "cargo_test", "unittest"]


def detect_test_framework(project_root: str = ".") -> Optional[str]:
    """Auto-detect the appropriate test framework for the project."""
    start_path = Path(project_root).resolve()
    if start_path.is_file():
        start_path = start_path.parent

    for current_dir in [start_path] + list(start_path.parents):
        for name in _FRAMEWORK_PREFERENCE:
            config = TEST_FRAMEWORKS[name]
            for detect_file in config["detect_files"]:
                if (current_dir / detect_file).exists():
                    # For jest/vitest, also check for npx
                    if name in ("jest", "vitest"):
                        if shutil.which("npx"):
                            return name
                        continue
                    # For python frameworks, check test dirs with appropriate files
                    if name in ("pytest", "unittest"):
                        if shutil.which("pytest") and name == "pytest":
                            return "pytest"
                        if not shutil.which("pytest") and name == "unittest":
                            # Check if test files exist
                            for td in config["test_dirs"]:
                                if (current_dir / td).exists():
                                    return "unittest"
                        continue
                    # For go/rust, check binary availability
                    if shutil.which(config["cmd"]):
                        return name
        if (current_dir / ".git").exists():
            break

    # Fallback: check for test directories and files
    for name in _FRAMEWORK_PREFERENCE:
        config = TEST_FRAMEWORKS[name]
        for td in config["test_dirs"]:
            if (start_path / td).exists():
                if name in ("jest", "vitest") and shutil.which("npx"):
                    # Check for jest/vitest config
                    for jf in ("jest.config.js", "jest.config.ts", "vitest.config.ts", "vitest.config.js"):
                        if (start_path / jf).exists():
                            return "vitest" if "vitest" in jf else "jest"
                    return "jest"
                if name == "pytest" and shutil.which("pytest"):
                    return "pytest"
                if name == "unittest":
                    return "unittest"
                if name == "go_test" and shutil.which("go"):
                    return "go_test"
                if name == "cargo_test" and shutil.which("cargo"):
                    return "cargo_test"
        break

    return None


def _parse_pytest_output(stdout: str) -> Dict[str, Any]:
    passed = len(re.findall(r"\s+PASSED\b", stdout))
    failed = len(re.findall(r"\s+FAILED\b", stdout))
    errors = len(re.findall(r"\s+ERROR\b", stdout))
    skipped = len(re.findall(r"\s+SKIPPED\b", stdout))
    xfailed = len(re.findall(r"\s+XFAIL\b", stdout))
    xpassed = len(re.findall(r"\s+XPASS\b", stdout))

    m = re.search(r"=+\s+(\d+)\s+passed.*in\s+([\d.]+)s", stdout)
    total = passed + failed + errors
    try:
        duration = float(m.group(2)) if m else None
    except (ValueError, TypeError):
        duration = None

    failures = []
    for line in stdout.splitlines():
        if line.strip().startswith("FAILED"):
            parts = line.strip().split()
            if len(parts) >= 2:
                failures.append({"test": parts[1], "output": line.strip()[:2000]})

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "duration_seconds": duration,
        "failures": failures,
        "passed_clean": failed == 0 and errors == 0,
    }


def _parse_go_test_output(stdout: str) -> Dict[str, Any]:
    pass_lines = [line for line in stdout.splitlines() if line.strip().startswith("--- PASS:")]
    fail_lines = [line for line in stdout.splitlines() if line.strip().startswith("--- FAIL:")]
    return {
        "total": len(pass_lines) + len(fail_lines),
        "passed": len(pass_lines),
        "failed": len(fail_lines),
        "errors": 0,
        "skipped": len([line for line in stdout.splitlines() if "--- SKIP:" in line]),
        "duration_seconds": None,
        "failures": [{"test": line.split("--- FAIL:")[1].split()[0], "output": line} for line in fail_lines[:20]],
        "passed_clean": len(fail_lines) == 0,
    }


def _parse_cargo_test_output(stdout: str) -> Dict[str, Any]:
    test_result = re.findall(r"test result: (ok|FAILED)\.\s+(\d+)\s+passed;\s+(\d+)\s+failed", stdout)
    passed = sum(int(m[1]) for m in test_result)
    failed = sum(int(m[2]) for m in test_result)
    return {
        "total": passed + failed,
        "passed": passed,
        "failed": failed,
        "errors": 0,
        "skipped": len(re.findall(r"ignored", stdout)),
        "duration_seconds": None,
        "failures": [],
        "passed_clean": failed == 0,
    }


def _parse_jest_vitest_output(stdout: str, framework: str) -> Dict[str, Any]:
    m_total = re.search(r"Tests:\s+(\d+)\s+(passed|total)", stdout)
    m_failed = re.search(r"(\d+)\s+failed", stdout)
    m_passed = re.search(r"(\d+)\s+passed", stdout)

    total = int(m_total.group(1)) if m_total else 0
    failed_count = int(m_failed.group(1)) if m_failed else 0
    passed_count = int(m_passed.group(1)) if m_passed else total - failed_count

    failures = []
    for match in re.finditer(r"(FAIL|×)\s+(.+?)(?=\n\s{4,})", stdout, re.DOTALL | re.MULTILINE):
        failures.append({"test": match.group(2).strip().split("\n")[0], "output": match.group(2).strip()[:2000]})

    return {
        "total": total,
        "passed": passed_count,
        "failed": failed_count,
        "errors": 0,
        "skipped": len(re.findall(r"○|⊙|skipped", stdout)),
        "duration_seconds": None,
        "failures": failures[:20],
        "passed_clean": failed_count == 0,
    }


def _parse_unittest_output(stdout: str, stderr: str) -> Dict[str, Any]:
    combined = stdout + "\n" + stderr
    m_ran = re.search(r"Ran\s+(\d+)\s+tests?", combined)
    m_failures = re.search(r"FAILED\s+\((\w+)=(\d+)\)", combined)
    m_errors = re.search(r"errors=(\d+)", combined)

    total = int(m_ran.group(1)) if m_ran else 0
    failed = int(m_failures.group(2)) if m_failures else 0
    errors_count = int(m_errors.group(1)) if m_errors else 0

    failures = []
    for match in re.finditer(r"FAIL:\s+(\S+).*?\n-+\n(.*?)(?=\n-+\n|$)", combined, re.DOTALL):
        failures.append({"test": match.group(1), "output": match.group(2).strip()[:2000]})

    return {
        "total": total,
        "passed": total - failed - errors_count,
        "failed": failed,
        "errors": errors_count,
        "skipped": 0,
        "duration_seconds": None,
        "failures": failures,
        "passed_clean": failed == 0 and errors_count == 0,
    }


class RunTestsParams(BaseModel):
    path: str = Field(".", description="File, directory, or test pattern to run (default: all tests)")
    framework: Optional[str] = Field(None, description="Test framework to use (pytest, jest, vitest, go_test, cargo_test, unittest). Auto-detected if omitted.")
    filter: Optional[str] = Field(None, description="Run only tests matching this substring or pattern (e.g., 'test_login' or path to test file)")
    verbose: bool = Field(True, description="Show verbose test output (default: true)")
    timeout: Optional[int] = Field(None, description="Override default timeout in seconds")


class RunTestsTool(Tool):
    """Tool for auto-detecting and running project tests with result parsing."""

    name = "run_tests"
    description = (
        "Auto-detect test framework (pytest, jest, vitest, go test, cargo test, unittest) "
        "and run project tests. Parses results into pass/fail/skip counts with failure details. "
        "Use 'filter' to run a specific test file or test name. "
        "Use this after making code changes to verify correctness."
    )
    parameters_model = RunTestsParams
    is_read_only = False
    requires_confirmation = False
    timeout = None

    async def execute(
        self,
        path: str = ".",
        framework: Optional[str] = None,
        filter: Optional[str] = None,
        verbose: bool = True,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            framework_name = framework or detect_test_framework(path)
            if not framework_name:
                return {
                    "success": False,
                    "error": (
                        "No supported test framework detected. "
                        "Supported: pytest, jest, vitest, go_test, cargo_test, unittest. "
                        "Ensure a test framework is configured for this project."
                    ),
                    "frameworks_checked": list(TEST_FRAMEWORKS.keys()),
                }

            if framework_name not in TEST_FRAMEWORKS:
                return {
                    "success": False,
                    "error": f"Unknown test framework: {framework_name}. Supported: {', '.join(TEST_FRAMEWORKS)}",
                }

            config = TEST_FRAMEWORKS[framework_name]
            cmd_binary = config["cmd"]

            if not shutil.which(cmd_binary):
                if framework_name in ("jest", "vitest") and cmd_binary == "npx":
                    return {
                        "success": False,
                        "error": f"npx not found on PATH. Install Node.js to run {framework_name} tests.",
                    }
                return {
                    "success": False,
                    "error": f"Test binary '{cmd_binary}' not found on PATH. Install {framework_name}.",
                }

            cmd: List[str] = [cmd_binary] + list(config["args"])

            if filter:
                if framework_name == "pytest":
                    cmd.append("-k")
                    cmd.append(filter)
                elif framework_name in ("jest", "vitest"):
                    cmd.append("--testNamePattern")
                    cmd.append(filter)
                elif framework_name == "go_test":
                    cmd = [cmd_binary, "test", "-v", "-count=1", "-run", filter, "./..."]
                elif framework_name == "cargo_test":
                    cmd = [cmd_binary, "test", filter, "--", "--color=always"]
                elif framework_name == "unittest":
                    cmd = [cmd_binary, "-m", "unittest", filter, "-v"]
                else:
                    cmd.append(filter)
            elif framework_name == "pytest":
                cmd.append(path)
            elif framework_name in ("jest", "vitest") and path != ".":
                cmd.append(path)
            elif framework_name == "unittest" and path != ".":
                cmd = [cmd_binary, "-m", "unittest", "discover", "-s", path, "-v"]

            if verbose and framework_name in ("jest", "vitest"):
                pass
            elif verbose and framework_name in ("pytest", "unittest"):
                pass

            effective_timeout = timeout or config.get("timeout", 120)

            project_root = Path(path).resolve()
            if project_root.is_file():
                project_root = project_root.parent

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
                return {
                    "success": False,
                    "error": f"Tests timed out after {effective_timeout} seconds.",
                    "framework": framework_name,
                }

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            max_output = 16000
            if len(stdout_str) > max_output:
                stdout_str = stdout_str[:max_output] + "\n... [output truncated]"
            if len(stderr_str) > max_output:
                stderr_str = stderr_str[:max_output] + "\n... [stderr truncated]"

            if framework_name == "pytest":
                parsed = _parse_pytest_output(stdout_str)
            elif framework_name in ("jest", "vitest"):
                parsed = _parse_jest_vitest_output(stdout_str, framework_name)
            elif framework_name == "go_test":
                parsed = _parse_go_test_output(stdout_str)
            elif framework_name == "cargo_test":
                parsed = _parse_cargo_test_output(stdout_str)
            elif framework_name == "unittest":
                parsed = _parse_unittest_output(stdout_str, stderr_str)
            else:
                parsed = {
                    "total": 0,
                    "passed": 0,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "duration_seconds": None,
                    "failures": [],
                    "passed_clean": process.returncode == 0,
                }

            return {
                "success": True,
                "framework": framework_name,
                "returncode": process.returncode,
                "results": parsed,
                "stdout": stdout_str if verbose else (stdout_str[-4000:] if len(stdout_str) > 4000 else stdout_str),
                "stderr": stderr_str if stderr_str.strip() else None,
                "summary": (
                    f"{parsed['total']} tests: {parsed['passed']} passed, "
                    f"{parsed['failed']} failed, {parsed['errors']} errors, "
                    f"{parsed['skipped']} skipped"
                    + (" — ALL PASSED" if parsed["passed_clean"] else " — FAILURES EXIST")
                ),
            }

        except Exception as e:
            logger.exception("run_tests failed")
            return {"success": False, "error": str(e)}
