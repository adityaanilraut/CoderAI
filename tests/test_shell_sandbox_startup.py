"""A failed sandbox launch is infrastructure, not an executed shell command."""

import sys

from coderai.tools.shell import handle_bash_tool


def test_seatbelt_initialization_failure_is_reported_without_retry(tmp_path, monkeypatch):
    calls = []

    def failed_wrap(*args):
        calls.append(args)
        return (
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('sandbox-exec: sandbox_apply: Operation not permitted\\n'); sys.exit(71)",
            ],
            {
                "sandboxApplied": True,
                "sandboxBackend": "seatbelt",
                "sandboxMode": "workspace-write",
            },
        )

    monkeypatch.setattr("coderai.tools.shell._sandbox_wrap", failed_wrap)
    result = handle_bash_tool(
        {"command": "echo should-not-run"},
        {
            "project_root": str(tmp_path),
            "session_id": "startup-probe",
            "sandbox_mode": "workspace-write",
        },
    )
    assert not result.ok and len(calls) == 1
    assert "OS sandbox initialization failed" in result.error
    assert result.metadata["sandbox"]["sandboxApplied"] is False
    assert "should-not-run" not in result.output
