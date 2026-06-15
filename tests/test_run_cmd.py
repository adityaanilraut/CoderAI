"""Coverage for the headless one-shot command (coderAI/cli/run_cmd.py)."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coderAI.cli.run_cmd import _resolve_prompt, run


class FakeAgent:
    """Minimal stand-in for Agent used by the run command."""

    def __init__(self, content="done", *, mutate=False, auto_approve=False):
        self.auto_approve = auto_approve
        self.confirmation_override = None
        self.model = "test-model"
        self.session = SimpleNamespace(session_id="session_abc123")
        self.cost_tracker = SimpleNamespace(get_total_cost=lambda: 0.0123)
        self._content = content
        self._mutate = mutate
        self.closed = False
        self.saw_override = False

    async def process_message(self, prompt):
        # Simulate a mutating tool call so the deny-on-mutate guard fires.
        if self._mutate and self.confirmation_override is not None:
            self.saw_override = True
            await self.confirmation_override("delete_file", {"path": "README.md"})
        return {"content": self._content}

    async def close(self):
        self.closed = True


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, args, agent, *, input=None):
    """Invoke `run` with `_build_agent` patched to return `agent`."""
    with (
        patch("coderAI.cli.run_cmd._build_agent", return_value=agent),
        patch("coderAI.cli.run_cmd.missing_api_key_message", return_value=None),
    ):
        return runner.invoke(run, args, input=input)


# ── prompt resolution ───────────────────────────────────────────────────


def test_resolve_prompt_from_arg():
    assert _resolve_prompt("hello") == "hello"


def test_resolve_prompt_dash_reads_stdin():
    with patch("sys.stdin") as stdin:
        stdin.read.return_value = "piped text\n"
        assert _resolve_prompt("-") == "piped text"


def test_resolve_prompt_none_with_tty_returns_none():
    with patch("sys.stdin") as stdin:
        stdin.isatty.return_value = True
        assert _resolve_prompt(None) is None


def test_resolve_prompt_none_with_pipe_reads_stdin():
    with patch("sys.stdin") as stdin:
        stdin.isatty.return_value = False
        stdin.read.return_value = "from pipe"
        assert _resolve_prompt(None) == "from pipe"


# ── happy path ──────────────────────────────────────────────────────────


def test_run_prints_response(runner):
    agent = FakeAgent(content="the answer is 4")
    result = _invoke(runner, ["what is 2+2"], agent)
    assert result.exit_code == 0
    assert "the answer is 4" in result.output
    assert agent.closed is True


def test_run_json_output(runner):
    agent = FakeAgent(content="4")
    result = _invoke(runner, ["--json", "what is 2+2"], agent)
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["response"] == "4"
    assert payload["session_id"] == "session_abc123"
    assert payload["model"] == "test-model"
    assert payload["cost_usd"] == pytest.approx(0.0123)
    assert payload["blocked_tools"] == []


def test_run_reads_prompt_from_stdin(runner):
    agent = FakeAgent(content="listed")
    result = _invoke(runner, [], agent, input="list files in cwd\n")
    assert result.exit_code == 0
    assert "listed" in result.output


# ── usage errors ────────────────────────────────────────────────────────


def test_run_no_prompt_exits_2(runner):
    agent = FakeAgent()
    # Empty stdin (CliRunner stdin is not a tty) → no prompt available.
    result = _invoke(runner, [], agent, input="")
    assert result.exit_code == 2
    assert "No prompt" in result.output


def test_run_resume_and_continue_conflict(runner):
    agent = FakeAgent()
    result = _invoke(runner, ["--resume", "x", "--continue", "hi"], agent)
    assert result.exit_code == 2
    assert "either --resume or --continue" in result.output.lower()


def test_run_missing_api_key_exits_1(runner):
    agent = FakeAgent()
    with (
        patch("coderAI.cli.run_cmd._build_agent", return_value=agent),
        patch(
            "coderAI.cli.run_cmd.missing_api_key_message",
            return_value="No API key configured.",
        ),
    ):
        result = runner.invoke(run, ["hi"])
    assert result.exit_code == 1
    assert "No API key" in result.output


# ── deny-on-mutate safety default ───────────────────────────────────────


def test_run_blocks_mutation_by_default(runner):
    agent = FakeAgent(content="I tried to delete it", mutate=True, auto_approve=False)
    result = _invoke(runner, ["delete README.md"], agent)
    assert result.exit_code == 1
    assert agent.saw_override is True
    assert "Blocked mutating tool call(s): delete_file" in result.stderr
    assert "--yolo" in result.stderr


def test_run_blocked_mutation_json(runner):
    agent = FakeAgent(content="nope", mutate=True, auto_approve=False)
    result = _invoke(runner, ["--json", "delete README.md"], agent)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["blocked_tools"] == ["delete_file"]


def test_run_yolo_allows_mutation(runner):
    # With auto_approve, _run_agent must NOT install the deny override.
    agent = FakeAgent(content="deleted", mutate=True, auto_approve=True)
    result = _invoke(runner, ["--yolo", "delete README.md"], agent)
    assert result.exit_code == 0
    assert agent.confirmation_override is None
    assert "deleted" in result.output


# ── runtime errors ──────────────────────────────────────────────────────


def test_run_agent_exception_exits_1(runner):
    class BoomAgent(FakeAgent):
        async def process_message(self, prompt):
            raise RuntimeError("provider exploded")

    agent = BoomAgent()
    result = _invoke(runner, ["hi"], agent)
    assert result.exit_code == 1
    assert "provider exploded" in result.output
    assert agent.closed is True
