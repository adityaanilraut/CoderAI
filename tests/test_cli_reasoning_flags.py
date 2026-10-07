"""Headless reviews must honor explicitly requested reasoning settings."""

import pytest


@pytest.mark.parametrize("flag,expected", [("--thinking", True), ("--no-thinking", False)])
def test_exec_resolves_reasoning_flags(tmp_path, monkeypatch, flag, expected):
    from coderai.config import resolve_current_settings
    from coderai.ui.shell.app import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODERAI_CONFIG_FILE", raising=False)
    monkeypatch.delenv("CODERAI_CONFIG_JSON", raising=False)
    monkeypatch.setenv("CODERAI_THINKING_ENABLED", "0" if expected else "1")
    monkeypatch.setenv("CODERAI_REASONING_EFFORT", "low")
    captured = {}

    async def fake_exec(prompt, **kwargs):
        captured.update(resolve_current_settings(kwargs["project_root"]))
        return 0

    monkeypatch.setattr("coderai.ui.print.run_exec_session", fake_exec)
    assert main(["--quiet", flag, "--reasoning-effort", "high", "--exec", "Review code"]) == 0
    assert captured["thinkingEnabled"] is expected
    assert captured["reasoningEffort"] == "high"


@pytest.mark.parametrize("configured,expected", [("1", True), ("0", False), (None, False)])
def test_model_override_preserves_explicit_thinking_setting(
    tmp_path, monkeypatch, configured, expected
):
    from coderai.llm import create_openai_client

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODERAI_CONFIG_FILE", raising=False)
    monkeypatch.delenv("CODERAI_CONFIG_JSON", raising=False)
    monkeypatch.setenv("CODERAI_API_KEY", "test-no-network")
    monkeypatch.setenv("CODERAI_MODEL", "o3")
    if configured is None:
        monkeypatch.delenv("CODERAI_THINKING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CODERAI_THINKING_ENABLED", configured)
    client = create_openai_client(str(tmp_path), model_override="gpt-6-luna")
    assert client["thinkingEnabled"] is expected


def test_luna_maximum_uses_supported_effort():
    from coderai.utils.common.openai_thinking import build_thinking_request_options

    assert build_thinking_request_options(True, reasoning_effort="max", model="gpt-6-luna") == {
        "reasoning_effort": "xhigh"
    }
    assert build_thinking_request_options(True, reasoning_effort="high", model="gpt-6-luna") == {
        "reasoning_effort": "high"
    }
