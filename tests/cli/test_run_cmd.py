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


def _ndjson_events(result):
    """Parse every non-empty stdout line, failing on any mixed text."""
    return [json.loads(line) for line in result.stdout.splitlines() if line]


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


def test_run_output_json_matches_json_flag(runner):
    agent = FakeAgent(content="4")
    result = _invoke(runner, ["--output", "json", "what is 2+2"], agent)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["response"] == "4"


def test_run_ndjson_success_emits_ordered_events_and_one_terminal(runner):
    class EventAgent(FakeAgent):
        async def process_message(self, prompt):
            from coderAI.system.events import event_emitter

            event_emitter.emit(
                "tool_call", tool_name="read_file", arguments={"path": "README.md"}, tool_id="t1"
            )
            event_emitter.emit(
                "tool_result",
                tool_name="read_file",
                result={"success": True, "content": "ok"},
                tool_id="t1",
            )
            return {"content": self._content}

    result = _invoke(runner, ["--output", "ndjson", "read README"], EventAgent(content="done"))
    events = _ndjson_events(result)

    assert result.exit_code == 0
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert {event["schema_version"] for event in events} == {1}
    assert [event["type"] for event in events] == [
        "run.started",
        "tool.started",
        "tool.completed",
        "result",
    ]
    terminal = [event for event in events if event["terminal"]]
    assert len(terminal) == 1
    assert terminal[0]["data"]["response"] == "done"


def test_run_ndjson_emits_actual_provider_deltas_when_streaming(runner):
    class StreamingAgent(FakeAgent):
        def __init__(self):
            super().__init__(content="")
            self.streaming = True
            self.streaming_handler = None

        async def process_message(self, prompt):
            async def chunks():
                yield {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]}
                yield {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}

            return await self.streaming_handler.handle_stream(chunks())

    result = _invoke(runner, ["--output", "ndjson", "say hello"], StreamingAgent())
    events = _ndjson_events(result)

    assert result.exit_code == 0
    assert [event["type"] for event in events] == [
        "run.started",
        "assistant.started",
        "assistant.delta",
        "assistant.completed",
        "result",
    ]
    assert events[2]["data"]["delta"] == "hello"
    assert events[-1]["data"]["response"] == "hello"


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


def test_run_blocked_mutation_ndjson_has_result_terminal_and_clean_stdout(runner):
    agent = FakeAgent(content="nope", mutate=True, auto_approve=False)
    result = _invoke(runner, ["--output", "ndjson", "delete README.md"], agent)
    events = _ndjson_events(result)

    assert result.exit_code == 1
    assert events[-1]["type"] == "result"
    assert events[-1]["terminal"] is True
    assert events[-1]["data"]["success"] is False
    assert events[-1]["data"]["blocked_tools"] == ["delete_file"]
    assert sum(event["terminal"] for event in events) == 1
    assert "Blocked mutating tool" in result.stderr


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


def test_run_provider_exception_ndjson_has_error_terminal_and_no_mixed_stdout(runner):
    class BoomAgent(FakeAgent):
        async def process_message(self, prompt):
            raise RuntimeError("provider exploded")

    agent = BoomAgent()
    result = _invoke(runner, ["--output", "ndjson", "hi"], agent)
    events = _ndjson_events(result)

    assert result.exit_code == 1
    assert events[-1]["type"] == "error"
    assert events[-1]["terminal"] is True
    assert events[-1]["data"]["success"] is False
    assert "provider exploded" in events[-1]["data"]["error"]
    assert sum(event["terminal"] for event in events) == 1
    assert "provider exploded" in result.stderr
    assert agent.closed is True
