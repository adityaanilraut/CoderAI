"""Tests for External Subagent Backends (Claude Code, Codex) and ACP Protocol."""

from __future__ import annotations

import json
import pytest
from coderai.core.acp.protocol import AcpMessage, AcpNdjsonParser
from coderai.core.subagent_backends.claude_code import ClaudeCodeDriver, ClaudeCodeConfig
from coderai.core.subagent_backends.codex import CodexDriver, CodexConfig
from coderai.core.subagent import SubAgentSpec


def test_acp_ndjson_parser():
    parser = AcpNdjsonParser()
    m1 = AcpMessage(id=1, method="initialize", params={"version": "0.25.1"})
    m2 = AcpMessage(id=2, result={"sessionId": "sess_123"})

    encoded = m1.encode_ndjson() + m2.encode_ndjson()
    parsed = parser.feed(encoded)

    assert len(parsed) == 2
    assert parsed[0].id == 1
    assert parsed[0].method == "initialize"
    assert parsed[0].params["version"] == "0.25.1"
    assert parsed[1].id == 2
    assert parsed[1].result["sessionId"] == "sess_123"


def test_claude_code_driver_config():
    config = ClaudeCodeConfig(
        permission_mode="acceptEdits",
        timeout_seconds=60.0,
        claude_bin="/fake/bin/claude",
    )
    driver = ClaudeCodeDriver(config)
    assert driver.config.permission_mode == "acceptEdits"
    assert driver.config.timeout_seconds == 60.0


def test_codex_driver_config():
    config = CodexConfig(
        approval_policy="approve-for-me",
        timeout_seconds=90.0,
        codex_bin="/fake/bin/codex",
    )
    driver = CodexDriver(config)
    assert driver.config.approval_policy == "approve-for-me"
    assert driver.config.timeout_seconds == 90.0


@pytest.mark.asyncio
async def test_subagent_spec_provider_field(tmp_path):
    spec = SubAgentSpec(
        description="Inspect files",
        prompt="Find todos",
        provider="claude_code",
    )
    assert spec.provider == "claude_code"
    assert spec.mode == "read_only"


@pytest.mark.asyncio
async def test_claude_code_execution_handling(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"result": "Found 3 files with TODO comments"})
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    driver = ClaudeCodeDriver(ClaudeCodeConfig(claude_bin="claude"))
    res = await driver.execute("Find all todos")
    assert res["ok"] is True
    assert res["status"] == "completed"
    assert "Found 3 files" in res["summary"]


@pytest.mark.asyncio
async def test_codex_execution_handling(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"output": "Refactored module successfully"})
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    driver = CodexDriver(CodexConfig(codex_bin="codex"))
    res = await driver.execute("Refactor module")
    assert res["ok"] is True
    assert res["status"] == "completed"
    assert "Refactored module successfully" in res["summary"]
