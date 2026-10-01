"""Shared local, pre-commit and CI verification contract.

Run with the project interpreter: .venv/bin/python scripts/verification.py check
Each test file gets its own pytest process, including SDK and wire coverage.
"""

from __future__ import annotations

import argparse
import json
from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("coderai", "tests", "tests_e2e", "scripts", "sdks/coderai-sdk", "examples")
SUITES = {
    "main": ("tests/test_*.py",),
    "wire": ("tests_e2e/test_*.py",),
    "sdk": ("sdks/coderai-sdk/tests/test_*.py",),
    "jev": ("tests/test_jev_calibration.py",),
    # Windows probes portable boundaries; the full suite includes POSIX PTYs,
    # chmod/umask and signal contracts covered by required Linux/macOS jobs.
    "windows": (
        "tests/test_cli.py",
        "tests/test_approval_identity.py",
        "tests/test_completion_streaming.py",
        "tests/test_compaction_conversion.py",
        "tests/test_flow_parsers.py",
        "tests/test_hook_payloads.py",
        "tests/test_jev_calibration.py",
        "tests/test_local_kaos.py",
        "tests/test_mcp_overlays.py",
        "tests/test_token_estimation.py",
        "tests/test_wire_framing.py",
        "sdks/coderai-sdk/tests/test_sdk*.py",
    ),
    "security": (
        "tests/test_security*.py",
        "tests/test_phase1_*.py",
        "tests/test_phase2_runtime_ownership.py",
        "tests/test_phase3_session_approvals.py",
        "tests/test_review_phase4.py",
        "tests/test_approval_identity.py",
        "tests/test_credential_boundaries.py",
    ),
}
REQUIRED_FILES = {
    "tests/test_jev_calibration.py",
    "tests/test_local_kaos.py",
    "tests/test_approval_identity.py",
    "tests/test_credential_boundaries.py",
    "tests/test_atomic_file_ops.py",
    "tests/test_phase2_protocol_events.py",
    "tests/test_phase3_sdk_oauth.py",
    "sdks/coderai-sdk/tests/test_sdk.py",
    "sdks/coderai-sdk/tests/test_sdk_integration.py",
}


def test_files(suite: str) -> list[Path]:
    patterns = SUITES[suite] if suite != "all" else SUITES["main"] + SUITES["wire"] + SUITES["sdk"]
    files: set[Path] = set()
    for pattern in patterns:
        matched = set(ROOT.glob(pattern))
        if not matched:
            raise ValueError(f"Empty verification scope: {pattern}")
        files.update(matched)
    if suite == "all":
        missing = REQUIRED_FILES - {p.relative_to(ROOT).as_posix() for p in files}
        if missing:
            raise ValueError(f"Missing required integration files: {sorted(missing)}")
    return sorted(files)


def junit_counts(path: Path) -> dict[str, int]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case in cases:
        kind = (
            "failed"
            if case.find("failure") is not None
            else "errors"
            if case.find("error") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        counts[kind] += 1
    return counts


def run_tests(suite: str, *, coverage: bool, report: Path | None) -> int:
    files = test_files(suite)
    from coderai.utils.subprocess_env import scrub_subprocess_env

    env = scrub_subprocess_env(dict(os.environ))
    for key in list(env):
        if key.startswith("CODERAI_") or any(part in key for part in ("BASE_URL", "MODEL")):
            env.pop(key)
    env["CODERAI_SDK_INTEGRATION"] = "1"
    # Prevent developer pytest options from silently altering the gate/coverage.
    env.pop("PYTEST_ADDOPTS", None)
    rows = []
    failed = False
    if coverage:
        subprocess.run([sys.executable, "-m", "coverage", "erase"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="coderai-verification-") as temporary:
        for index, file in enumerate(files):
            home = Path(temporary) / f"home-{index}"
            home.mkdir()
            env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"))
            name = file.relative_to(ROOT).as_posix()
            junit = Path(temporary) / f"{index}.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                name,
                "-p",
                "no:cacheprovider",
                "--benchmark-disable",
                "-q",
                f"--junitxml={junit}",
            ]
            if coverage and name.startswith("tests/"):
                command += ["--cov=coderai", "--cov-append", "--cov-report="]
            start = time.monotonic()
            proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
            counts = junit_counts(junit) if junit.exists() else {}
            required = name in REQUIRED_FILES or suite in {"jev", "security", "sdk"}
            skip_failure = bool(counts.get("skipped"))
            empty = (
                not counts
                or sum(counts.values()) == 0
                or (required and counts.get("passed", 0) == 0)
            )
            bad = proc.returncode != 0 or skip_failure or empty
            failed |= bad
            row = {
                "file": name,
                "command": command,
                "exit_code": proc.returncode,
                "seconds": round(time.monotonic() - start, 3),
                **counts,
                "gate_passed": not bad,
            }
            rows.append(row)
            print(f"{name}: {'FAIL' if bad else 'PASS'} {counts}", flush=True)
            if bad:
                print(proc.stdout + proc.stderr, flush=True)

    if coverage:
        failed |= (
            subprocess.run(
                [sys.executable, "-m", "coverage", "report", "--fail-under=28"], cwd=ROOT
            ).returncode
            != 0
        )
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"suite": suite, "files": rows, "passed": not failed}, indent=2) + "\n"
        )
    return int(failed)


def run(*args: str) -> int:
    return subprocess.run([sys.executable, *args], cwd=ROOT).returncode


def check_tool_versions() -> int:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"][
        "dev"
    ]
    for requirement in dev:
        if "==" not in requirement:
            continue
        name, expected = requirement.split("==", 1)
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            print(
                f"Tool mismatch: {name} {actual}, expected {expected}; run make dev",
                file=sys.stderr,
            )
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        choices=("check", "lint", "format", "format-check", "typecheck", "deps", "test", "audit"),
    )
    parser.add_argument("--suite", choices=("all", *SUITES), default="all")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.target != "test" and check_tool_versions():
        return 1
    commands = {
        "lint": ("-m", "ruff", "check", *SCOPES),
        "format": ("-m", "ruff", "format", *SCOPES),
        "format-check": ("-m", "ruff", "format", "--check", *SCOPES),
        "typecheck": ("-m", "mypy", "coderai", "sdks/coderai-sdk/src"),
        "deps": ("scripts/check_dependency_versions.py", "--strict", "--extras", "dev", "jev"),
        "audit": ("scripts/audit_dependencies.py",),
    }
    targets = (
        ("format-check", "lint", "typecheck", "deps", "test", "audit")
        if args.target == "check"
        else (args.target,)
    )
    for target in targets:
        if target == "test":
            code = run_tests(args.suite, coverage=args.coverage, report=args.report)
        elif target == "typecheck":
            code = run("scripts/check_type_budget.py") or run(*commands[target])
        elif target == "deps":
            code = run(*commands[target]) or run("-m", "pip", "check")
        else:
            code = run(*commands[target])
        if code:
            return code
    if args.target in {"check", "test"}:
        return run("-m", "coderai", "--version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
