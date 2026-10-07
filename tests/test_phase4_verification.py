"""Release-gate, isolated test runner, audit policy and type-budget regressions."""

from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_release_requires_same_commit_source_security_and_jev_gates():
    release = workflow("release.yml")
    ci = workflow("ci.yml")
    verify = workflow("verify.yml")
    for caller in (ci, release):
        job = caller["jobs"]["verification"]
        assert job["uses"] == "./.github/workflows/verify.yml"
        assert job["with"]["source_sha"] == "${{ github.sha }}"
    assert "workflow_call" in verify["on"]
    assert {"source", "security", "jev"} <= verify["jobs"].keys()
    for name in ("source", "security", "jev"):
        job = verify["jobs"][name]
        assert job.get("continue-on-error", "false") == "false"
        checkout = next(s for s in job["steps"] if s.get("uses") == "actions/checkout@v4")
        assert checkout["with"]["ref"] == "${{ inputs.source_sha }}"
    for name in ("build", "publish-github-release", "publish-pypi"):
        assert "verification" in release["jobs"][name]["needs"]
        assert "always()" not in release["jobs"][name].get("if", "")
    build_steps = release["jobs"]["build"]["steps"]
    assert sum("python -m build" in s.get("run", "") for s in build_steps) == 1
    smoke = release["jobs"]["smoke-wheel"]
    assert "--audit" in next(
        s["run"] for s in smoke["steps"] if "verify_wheel.py" in s.get("run", "")
    )


@pytest.mark.asyncio
async def test_acp_rejects_invalid_config_option_parameters():
    import acp
    from coderai.acp.server import ACPServer

    with pytest.raises(acp.RequestError) as error:
        await ACPServer().set_config_option("unknown", "session", "value")
    assert error.value.code == -32602


def test_audited_wheel_requires_clean_dependency_installation():
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_wheel.py"),
            "missing.whl",
            "--audit",
            "--no-deps",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "--audit requires a clean installation" in proc.stderr


@pytest.mark.parametrize(
    "body,exit_code,expected",
    [
        ('<testcase name="passed"/>', 0, 0),
        ('<testcase name="skipped"><skipped/></testcase>', 0, 1),
        ('<testcase name="failed"><failure/></testcase>', 1, 1),
        ("", 0, 1),
    ],
)
def test_required_suite_cannot_skip_or_collect_nothing(
    tmp_path, monkeypatch, body, exit_code, expected
):
    verification = script("verification")
    file = tmp_path / "calibration.py"
    file.touch()
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    monkeypatch.setattr(verification, "test_files", lambda _: [file])
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["CODERAI_SDK_INTEGRATION"] == "1"
        assert "PYTEST_ADDOPTS" not in kwargs["env"]
        junit = Path(next(s.split("=", 1)[1] for s in command if s.startswith("--junitxml=")))
        junit.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
        return subprocess.CompletedProcess(command, exit_code, stdout="test output", stderr="")

    monkeypatch.setattr(verification.subprocess, "run", run)
    report = tmp_path / "report.json"
    assert verification.run_tests("jev", coverage=False, report=report) == expected
    assert len(commands) == 1
    assert commands[0][:4] == [sys.executable, "-m", "pytest", "calibration.py"]
    assert "--benchmark-disable" in commands[0]
    assert json.loads(report.read_text())["passed"] is (expected == 0)


def test_all_suite_requires_offline_sdk_and_protocol_coverage():
    verification = script("verification")
    files = {p.relative_to(ROOT).as_posix() for p in verification.test_files("all")}
    assert verification.REQUIRED_FILES <= files
    assert any(p.startswith("tests_e2e/") for p in files)
    assert len(files) == len(verification.test_files("all"))


def test_empty_scope_fails_instead_of_green_noop(tmp_path, monkeypatch):
    verification = script("verification")
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="Empty verification scope"):
        verification.test_files("jev")


def advisory_report():
    return {
        "dependencies": [
            {
                "name": "example",
                "version": "1.0",
                "vulns": [
                    {"id": "PYSEC-123", "aliases": ["CVE-123"]},
                ],
            }
        ]
    }


def test_dependency_findings_block_by_default_and_alias_exception_is_scoped():
    audit = script("audit_dependencies")
    report = advisory_report()
    today = date(2026, 9, 30)
    assert audit.blocking_findings(report, {"exceptions": []}, today)
    exception = {
        "package": "example",
        "id": "CVE-123",
        "reason": "Temporary mitigation",
        "expires": "2026-10-15",
    }
    assert not audit.blocking_findings(report, {"exceptions": [exception]}, today)
    exception["package"] = "another"
    assert audit.blocking_findings(report, {"exceptions": [exception]}, today)


@pytest.mark.parametrize("expiry", ["2026-09-30", "2026-09-29", "2026-12-01", "invalid"])
def test_expired_or_unbounded_audit_exception_fails(expiry):
    audit = script("audit_dependencies")
    exception = {"package": "example", "id": "PYSEC-123", "reason": "mitigation", "expires": expiry}
    with pytest.raises(ValueError):
        audit.blocking_findings(advisory_report(), {"exceptions": [exception]}, date(2026, 9, 30))


def test_incomplete_dependency_audit_fails():
    audit = script("audit_dependencies")
    report = {"dependencies": [{"name": "example", "skip_reason": "not found", "vulns": []}]}
    assert audit.blocking_findings(report, {"exceptions": []}, date(2026, 9, 30))
    with pytest.raises(ValueError):
        audit.blocking_findings({"dependencies": []}, {"exceptions": []}, date(2026, 9, 30))


def test_audit_covers_exact_installed_versions_and_all_transitive_packages():
    audit = script("audit_dependencies")
    inventory = [
        {"name": "Example", "version": "2.0"},
        {"name": "coderai-agent", "version": "0.4.0"},
    ]
    assert audit.validate_inventory(advisory_report(), inventory)
    report = {"dependencies": [{"name": "example", "version": "2.0", "vulns": []}]}
    assert not audit.validate_inventory(report, inventory)
    inventory.append({"name": "missing-transitive", "version": "1.0"})
    assert audit.validate_inventory(report, inventory)


def test_local_tool_version_drift_fails(monkeypatch):
    verification = script("verification")
    monkeypatch.setattr(verification.metadata, "version", lambda _: "0.0.0")
    assert verification.check_tool_versions() == 1


def test_type_budget_guards_wildcards_and_growth():
    mod = script("check_type_budget")
    budget = json.loads((ROOT / "docs/type-suppression-budget.json").read_text())
    current = mod.measure(ROOT, tomllib.loads((ROOT / "pyproject.toml").read_text()))
    assert not mod.check_budget(current, budget)
    current["patterns"] = ["coderai.soul.*"]
    assert any("Wildcard" in p for p in mod.check_budget(current, budget))
    current["patterns"] = []
    current["lines"] = budget["max_lines"] + 1
    assert any("lines" in p for p in mod.check_budget(current, budget))


def test_typed_boundaries_reject_invalid_context_and_sdk_engine(tmp_path):
    file = tmp_path / "bad_boundary.py"
    file.write_text(
        "from coderai.tools.legacy.types import ToolExecutionContext\n"
        "from coderai_sdk.client import CoderAIClient\n"
        "ToolExecutionContext(session_id=42, project_root='.')\n"
        "CoderAIClient(engine=42)\n"
        "from kosong.tooling import ToolResult, ToolReturnValue\n"
        "from coderai.wire.emitter import wire_send\n"
        "wire_send(ToolResult(tool_call_id='t', return_value=ToolReturnValue(is_error=False, output='ok', message='', display=[])))\n"
        "wire_send(42)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", str(file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert 'Argument "session_id"' in proc.stdout
    assert 'Argument "engine"' in proc.stdout
    assert 'Argument 1 to "wire_send"' in proc.stdout
    assert "Found 3 errors" in proc.stdout


def test_precommit_and_make_use_shared_project_checks():
    precommit = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = precommit["repos"][0]["hooks"]
    assert {h["entry"].split()[-1] for h in hooks} == {"lint", "format-check", "typecheck"}
    assert all(h["entry"].startswith(".venv/bin/python scripts/verification.py") for h in hooks)
    assert all(h["pass_filenames"] is False for h in hooks)
    assert "PYTHON ?= .venv/bin/python" in (ROOT / "Makefile").read_text()


def test_offline_runner_isolates_each_home_and_scrubs_credentials(tmp_path, monkeypatch):
    verification = script("verification")
    files = [tmp_path / "one.py", tmp_path / "two.py"]
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    monkeypatch.setattr(verification, "test_files", lambda _: files)
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward")
    monkeypatch.setenv("TYPESAFE_API_KEY", "never-forward")
    monkeypatch.setenv("GITHUB_TOKEN", "never-forward")
    monkeypatch.setenv("CODERAI_CONFIG_FILE", "host-settings")
    monkeypatch.setenv("CODERAI_SDK_INTEGRATION", "0")
    monkeypatch.setenv("KIMI_BASE_URL", "https://invalid.test")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests")
    homes = []

    def run(command, **kwargs):
        env = kwargs["env"]
        assert (
            not {
                "OPENAI_API_KEY",
                "TYPESAFE_API_KEY",
                "GITHUB_TOKEN",
                "CODERAI_CONFIG_FILE",
                "KIMI_BASE_URL",
                "PYTEST_ADDOPTS",
            }
            & env.keys()
        )
        assert env["CODERAI_SDK_INTEGRATION"] == "1"
        assert env["HOME"] == env["USERPROFILE"]
        home = Path(env["HOME"])
        assert home.is_dir() and not list(home.iterdir())
        homes.append(home)
        (home / "settings-from-test").touch()
        junit = Path(next(s.split("=", 1)[1] for s in command if s.startswith("--junitxml=")))
        junit.write_text('<testsuites><testsuite><testcase name="pass"/></testsuite></testsuites>')
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(verification.subprocess, "run", run)
    assert verification.run_tests("all", coverage=False, report=None) == 0
    assert len(set(homes)) == 2 and all(not home.exists() for home in homes)
