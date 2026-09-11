"""Consolidated background/wire: jobs, flows, notifications, wire server, browser, telemetry, hooks, atomic IO."""

from __future__ import annotations

import asyncio
import io
import json
import pathlib
import stat
import sys
import time
from typing import Any

import pytest

import coderai.core.session  # noqa: F401  (engine first: background.* before core.* is order-fragile)
from coderai.background.manager import get_job_store, reset_job_store
from coderai.background.worker import run_background_task_worker
from coderai.core.common.file_utils import with_file_lock, write_file_atomic
from coderai.skill.flow import (
    Flow,
    FlowEdge,
    FlowNode,
    FlowValidationError,
    parse_choice,
    validate_flow,
)
from coderai.skill.flow.mermaid import parse_mermaid_flowchart
from coderai.skill.flow.runner import FlowRunner, maybe_run_ralph, resolve_max_ralph_iterations
from coderai.core.hooks import (
    HookOutput,
    HookPoint,
    load_hook_config,
    matches_hook_pattern,
    merge_hook_outputs,
    run_hook_point,
    run_pre_tool_use,
)
from coderai.core.notifications import NotificationEvent, NotificationManager
from coderai.core.telemetry import ExecutionSpan, TelemetryCollector
from coderai.core.tools.browser import (
    DOMExtractor,
    HeadlessBrowserDriver,
    handle_browser_click_tool,
    handle_browser_close_tool,
    handle_browser_navigate_tool,
    handle_browser_snapshot_tool,
    handle_browser_type_tool,
)
from coderai.core.wire.serde import deserialize_wire_message, serialize_wire_message
from coderai.core.wire.server import WireServer
from coderai.core.wire.types import TurnBegin


@pytest.fixture(autouse=True)
def _clean_store():
    reset_job_store()
    yield
    reset_job_store()


def _start_job(store: Any, job_id: str, tmp_path: pathlib.Path) -> Any:
    """Start a background shell job writing to a per-test log file."""
    return store.start(
        job_id=job_id,
        session_id="sess-1",
        kind="bash",
        label="wire test",
        output_path=str(tmp_path / f"{job_id}.log"),
    )


def _run_worker(job_id: str, tmp_path: pathlib.Path, command: str, **kw: Any) -> Any:
    """Run one background worker task to completion against the shared store."""
    return asyncio.run(
        run_background_task_worker(
            job_id=job_id,
            session_id="sess-1",
            command=command,
            shell_path="/bin/sh",
            cwd=str(tmp_path),
            output_path=str(tmp_path / f"{job_id}.log"),
            **kw,
        )
    )


def test_background_worker_completes_and_logs_output(tmp_path: pathlib.Path):
    """Successful shell tasks complete with exit zero and captured output."""
    _start_job(get_job_store(), "job-ok", tmp_path)
    job = _run_worker("job-ok", tmp_path, "echo hello-worker", timeout_s=10.0)
    assert job is not None and job.status == "completed" and job.exit_code == 0
    assert "hello-worker" in (tmp_path / "job-ok.log").read_text()


def test_background_worker_fails_on_timeout(tmp_path: pathlib.Path):
    """Overrunning shell tasks fail with a timeout detail."""
    _start_job(get_job_store(), "job-timeout", tmp_path)
    job = _run_worker("job-timeout", tmp_path, "sleep 30", timeout_s=0.3, kill_grace_period_s=0.2)
    assert job is not None and job.status == "failed"
    assert job.detail is not None and "timed out" in job.detail


def test_background_worker_kills_on_cancel_request(tmp_path: pathlib.Path):
    """Cancelled shell tasks stop as killed after heartbeats fire."""
    _start_job(get_job_store(), "job-cancel", tmp_path)
    beats: list[bool] = []
    job = _run_worker(
        "job-cancel",
        tmp_path,
        "sleep 30",
        timeout_s=30.0,
        control_poll_interval_s=0.05,
        kill_grace_period_s=0.2,
        heartbeat_interval_s=0.05,
        is_cancelled=lambda: True,
        on_heartbeat=lambda: beats.append(True),
    )
    assert job is not None and job.status == "killed" and beats


def test_flow_validate_rejects_malformed_graphs():
    """Flows require exactly one begin/end node and unique non-empty branch labels."""
    begin = {"A": FlowNode(id="A", label="BEGIN", kind="begin")}
    with pytest.raises(FlowValidationError):
        validate_flow(begin, {"A": []})
    with pytest.raises(FlowValidationError):
        validate_flow(
            begin | {"B": FlowNode(id="B", label="END", kind="begin")}, {"A": [], "B": []}
        )
    nodes = begin | {
        "B": FlowNode(id="B", label="?", kind="task"),
        "C": FlowNode(id="C", label="END", kind="end"),
    }
    with pytest.raises(FlowValidationError):
        validate_flow(
            nodes,
            {
                "A": [FlowEdge("A", "B", None)],
                "B": [FlowEdge("B", "C", None), FlowEdge("B", "C", "go")],
                "C": [],
            },
        )
    with pytest.raises(FlowValidationError):
        validate_flow(
            nodes,
            {
                "A": [FlowEdge("A", "B", None)],
                "B": [FlowEdge("B", "C", "go"), FlowEdge("B", "C", "go")],
                "C": [],
            },
        )


def test_flow_mermaid_parses_decision_graph():
    """Mermaid flowcharts infer begin/task/decision/end nodes and edge labels."""
    flow = parse_mermaid_flowchart(
        "\n".join(
            [
                "flowchart TD",
                "A([BEGIN]) --> B[Search stdrc]",
                "B --> C{Enough?}",
                "C -->|yes| D([END])",
                "C -->|no| B",
            ]
        )
    )
    assert (flow.begin_id, flow.end_id) == ("A", "D") and flow.nodes["C"].kind == "decision"
    assert [(e.dst, e.label) for e in flow.outgoing["C"]] == [("D", "yes"), ("B", "no")]
    with pytest.raises(FlowValidationError):
        parse_mermaid_flowchart(
            "\n".join(["A([BEGIN]) --> B[Pick]", "B --> C([END])", "B --> D([END])"])
        )


class _FakeMessage:
    def __init__(self, role: str, content: str) -> None:
        self.role, self.content, self.meta = role, content, {}


class _FakeFileHistory:
    def ensure_session(self, session_id: str) -> None:
        pass

    def record_tracked_files_checkpoint(self, session_id: str, message: str) -> Any:
        return type("Ckpt", (), {"checkpoint_hash": "abc"})()


class _FakeMgr:
    """Minimal SessionManager surface used by FlowRunner."""

    def __init__(self, replies: list[str], final_status: str = "completed") -> None:
        self._messages: list[_FakeMessage] = []
        self._replies, self._status = list(replies), final_status
        self.project_root, self.file_history, self.activations = ".", _FakeFileHistory(), 0

    def list_session_messages(self, session_id: str) -> list[_FakeMessage]:
        return list(self._messages)

    def get_session(self, session_id: str) -> Any:
        return type("E", (), {"status": self._status, "fail_reason": "", "ask_permissions": []})()

    def get_active_model(self) -> str:
        return "fake-model"

    def get_resolved_settings(self) -> dict[str, Any]:
        return {}

    def _append_message(self, message: _FakeMessage) -> None:
        self._messages.append(message)

    def _build_message(self, session_id: str, role: str, content: str, **kw: Any) -> _FakeMessage:
        return _FakeMessage(role, content)

    async def _activate(self, session_id: str) -> None:
        self.activations += 1
        self._messages.append(
            _FakeMessage("assistant", self._replies.pop(0) if self._replies else "done")
        )


def _linear_flow() -> Flow:
    """Three-node BEGIN -> task -> END flow."""
    nodes = {
        "BEGIN": FlowNode(id="BEGIN", label="BEGIN", kind="begin"),
        "T": FlowNode(id="T", label="do work", kind="task"),
        "END": FlowNode(id="END", label="END", kind="end"),
    }
    return Flow(
        nodes=nodes,
        outgoing={
            "BEGIN": [FlowEdge("BEGIN", "T", None)],
            "T": [FlowEdge("T", "END", None)],
            "END": [],
        },
        begin_id="BEGIN",
        end_id="END",
    )


def test_flow_runner_completes_linear_tasks():
    """Linear flows activate once per task node and complete."""
    mgr = _FakeMgr(["did the work"])
    outcome = asyncio.run(FlowRunner(_linear_flow()).run(mgr, "s1"))
    assert outcome.status == "completed" and outcome.moves == 1 and mgr.activations == 1


def test_flow_runner_routes_decisions_on_choice():
    """Decision nodes follow the last <choice> tag; choice parsing takes the last match."""
    assert parse_choice("no choice here") is None
    assert parse_choice("a <choice>CONTINUE</choice> b <choice>STOP</choice>") == "STOP"
    nodes = {
        "BEGIN": FlowNode(id="BEGIN", label="BEGIN", kind="begin"),
        "D": FlowNode(id="D", label="ready?", kind="decision"),
        "END": FlowNode(id="END", label="END", kind="end"),
    }
    flow = Flow(
        nodes=nodes,
        outgoing={
            "BEGIN": [FlowEdge("BEGIN", "D", None)],
            "D": [FlowEdge("D", "D", "CONTINUE"), FlowEdge("D", "END", "STOP")],
            "END": [],
        },
        begin_id="BEGIN",
        end_id="END",
    )
    mgr = _FakeMgr(["<choice>CONTINUE</choice>", "<choice>STOP</choice>"])
    outcome = asyncio.run(FlowRunner(flow).run(mgr, "s1"))
    assert outcome.status == "completed" and outcome.moves == 2


def test_flow_runner_stops_without_completion_on_limits():
    """Exhausted move budgets and rejected tools stop the run without completing."""
    assert (
        asyncio.run(FlowRunner(_linear_flow(), max_moves=0).run(_FakeMgr(["x"] * 10), "s1")).status
        == "budget_exceeded"
    )
    assert (
        asyncio.run(
            FlowRunner(_linear_flow()).run(_FakeMgr(["whatever"], "ask_permission"), "s1")
        ).status
        == "tool_rejected"
    )


def test_flow_ralph_loop_builds_bounded_retries(monkeypatch: pytest.MonkeyPatch):
    """Ralph loops bound retries, honor settings/env precedence, and stay off by default."""
    runner = FlowRunner.ralph_loop("fix the bug", 2)
    assert runner._max_moves == 3
    assert [e.label for e in runner._flow.outgoing["R2"]] == ["CONTINUE", "STOP"]
    assert FlowRunner.ralph_loop("fix the bug", -1)._max_moves > 10**9
    monkeypatch.delenv("CODERAI_MAX_RALPH_ITERATIONS", raising=False)
    assert resolve_max_ralph_iterations({}) == 0
    assert resolve_max_ralph_iterations({"maxRalphIterations": 3}) == 3
    idle = _FakeMgr(["done"])
    assert asyncio.run(maybe_run_ralph(idle, "s1", "do stuff")) is False and idle.activations == 0
    monkeypatch.setenv("CODERAI_MAX_RALPH_ITERATIONS", "7")
    assert resolve_max_ralph_iterations({"maxRalphIterations": 3}) == 7
    monkeypatch.setenv("CODERAI_MAX_RALPH_ITERATIONS", "2")
    mgr = _FakeMgr(["working", "<choice>CONTINUE</choice>", "<choice>STOP</choice>"])
    assert asyncio.run(maybe_run_ralph(mgr, "s1", "do stuff")) is True and mgr.activations == 3


def test_notifications_publish_claim_acknowledges(tmp_path: pathlib.Path):
    """Published events claim exactly once per sink and acknowledge delivery."""
    manager = NotificationManager(tmp_path)
    view = manager.publish(
        NotificationEvent(
            id=manager.new_id(),
            category="task",
            type="background_task_completed",
            source_kind="background_task",
            source_id="t1",
            title="done",
            body="ok",
        )
    )
    assert manager.has_pending_for_sink("llm")
    assert [v.event.id for v in manager.claim_for_sink("llm", limit=4)] == [view.event.id]
    assert not manager.has_pending_for_sink("llm")
    manager.ack("llm", view.event.id)
    assert manager.store.read_delivery(view.event.id).sinks["llm"].status == "acked"


def test_notifications_dedupe_collapses_repeats_and_recovers(tmp_path: pathlib.Path):
    """Repeat dedupe keys reuse one event; stale claims recover to pending."""
    manager = NotificationManager(tmp_path)
    first = manager.publish(
        NotificationEvent(id=manager.new_id(), title="t", body="b", dedupe_key="k1")
    )
    second = manager.publish(
        NotificationEvent(id=manager.new_id(), title="t", body="b", dedupe_key="k1")
    )
    assert first.event.id == second.event.id and len(manager.store.list_notification_ids()) == 1
    assert len(manager.claim_for_sink("shell")) == 1
    manager._claim_stale_after_s = -1.0
    manager.recover()
    assert manager.has_pending_for_sink("shell")
    with pytest.raises(ValueError):
        manager.store.write_delivery("../evil", type(first.delivery)())


def test_wire_serde_round_trips_turn_messages():
    """Wire messages serialize with a type tag and reject unknown types."""
    data = serialize_wire_message(TurnBegin(user_input="hello"))
    assert data["type"] == "TurnBegin"
    back = deserialize_wire_message(data)
    assert isinstance(back, TurnBegin) and back.user_input == "hello"
    with pytest.raises(ValueError):
        deserialize_wire_message({"type": "Nope", "payload": {}})


class _StubWireMgr:
    project_root = "."

    def list_available_skills(self, session_id: Any = None) -> list[dict[str, Any]]:
        return []


def _serve_scripted(lines: list[str]) -> tuple[int, list[dict[str, Any]]]:
    """Drive a WireServer over scripted stdin lines, capturing stdout frames."""
    server = WireServer(_StubWireMgr(), None)
    old_stdin, old_write = sys.stdin, sys.stdout.write
    sys.stdin = io.StringIO("".join(line + "\n" for line in lines))  # type: ignore[assignment]
    captured: list[str] = []
    sys.stdout.write = captured.append  # type: ignore[assignment]
    try:
        rc = asyncio.run(server.serve())
    finally:
        sys.stdin, sys.stdout.write = old_stdin, old_write  # type: ignore[assignment]
    return rc, [json.loads(line) for line in "".join(captured).splitlines() if line.strip()]


def _rpc(id: str, method: str, params: dict | None = None) -> str:
    """Encode one JSON-RPC request line."""
    return json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def test_wire_server_handles_initialize_replay_cancel():
    """Scripted sessions initialize, survive bad JSON, replay, and report cancel/method errors."""
    rc, messages = _serve_scripted(
        [
            _rpc("1", "initialize"),
            "not json at all",
            _rpc("2", "replay"),
            _rpc("3", "cancel"),
            _rpc("4", "bogus"),
        ]
    )
    assert rc == 0
    by_id = {m.get("id"): m for m in messages if "id" in m}
    assert (
        by_id["1"]["result"]["server"]["name"] == "coderai"
        and "slash_commands" in by_id["1"]["result"]
    )
    assert by_id["2"]["result"]["status"] == "finished"
    assert by_id["3"]["error"]["code"] == -32000 and by_id["4"]["error"]["code"] == -32601
    assert any(m.get("error", {}).get("code") == -32700 for m in messages)


def test_wire_server_rejects_external_tools():
    """Initialize calls with external tools report them as rejected."""
    rc, messages = _serve_scripted(
        [_rpc("1", "initialize", {"external_tools": [{"name": "x", "description": "d"}]})]
    )
    assert rc == 0
    result = next(m for m in messages if m.get("id") == "1")["result"]
    assert result["external_tools"]["rejected"][0]["name"] == "x"


def test_wire_server_maps_failures_to_errors():
    """Missing credentials and failed turns surface as JSON-RPC errors."""

    class _NoKeyMgr(_StubWireMgr):
        async def create_session(self, prompt: str, plan_mode: bool = False) -> str:
            raise RuntimeError("API key not found")

    resp = asyncio.run(WireServer(_NoKeyMgr(), None)._handle_prompt("9", {"user_input": "hi"}))
    assert resp["id"] == "9" and resp["error"]["code"] == -32001

    class _FailedEntry:
        status, fail_reason = "failed", "boom"

        def get(self, key: str, default: Any = None) -> Any:
            return {"status": "failed", "failReason": "boom"}.get(key, default)

    class _FailedMgr(_StubWireMgr):
        async def reply_session(self, session_id: str, prompt: Any = None) -> None:
            return None

        def get_session(self, session_id: str) -> Any:
            return _FailedEntry()

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(WireServer(_FailedMgr(), "s1")._run_turn(""))


SAMPLE_HTML = """<!DOCTYPE html><html><head><title>CoderAI Test Dashboard</title></head><body>
<h1>Welcome to Dashboard</h1><a href="/docs">Documentation</a><a href="/settings">Settings</a>
<form><input type="text" name="query" value="" /><button type="submit">Search</button></form>
</body></html>"""


def test_browser_dom_extracts_interactive_elements():
    """DOM extraction indexes links, inputs, and buttons with sequential refs."""
    extractor = DOMExtractor()
    extractor.feed(SAMPLE_HTML)
    assert extractor.title == "CoderAI Test Dashboard" and len(extractor.elements) >= 4
    assert {"a", "input", "button"} <= {e.tag for e in extractor.elements}
    assert [e.ref_id for e in extractor.elements] == list(range(1, len(extractor.elements) + 1))
    state = HeadlessBrowserDriver().navigate("http://localhost:3000/app", html_override=SAMPLE_HTML)
    assert (
        state.title == "CoderAI Test Dashboard"
        and "Interactive Elements:" in state.format_summary()
    )


def test_browser_tools_drive_navigate_type_click_close():
    """Browser tool handlers navigate, snapshot, type, click, and close cleanly."""
    ctx = {"session_id": "test_browser_sess"}
    res_nav = handle_browser_navigate_tool(
        {"url": "https://dashboard.local", "html_override": SAMPLE_HTML}, ctx
    )
    assert res_nav.ok is True and "CoderAI Test Dashboard" in (res_nav.output or "")
    assert res_nav.metadata.get("element_count", 0) >= 4
    assert "Interactive Elements:" in (
        handle_browser_snapshot_tool({"extract_dom": True}, ctx).output or ""
    )
    assert (
        handle_browser_type_tool({"element_ref": 3, "text": "query", "clear_first": True}, ctx).ok
        is True
    )
    assert handle_browser_click_tool({"element_ref": 1}, ctx).ok is True
    res_close = handle_browser_close_tool({}, ctx)
    assert res_close.ok is True and "Browser session closed" in (res_close.output or "")


def test_telemetry_span_tracks_duration_and_exports_otel():
    """Execution spans record duration, events, attributes, and OTel export shape."""
    span = ExecutionSpan(
        span_id="span_123",
        trace_id="trace_456",
        name="tool:edit",
        kind="tool",
        attributes={"file_path": "src/main.py"},
    )
    assert span.status == "running" and span.duration_ms == 0.0
    time.sleep(0.01)
    span.add_event("validation_passed", {"rules": 3})
    span.finish(status="ok", extra_attributes={"lines_changed": 15})
    assert (
        span.status == "ok" and span.duration_ms >= 5.0 and span.attributes["lines_changed"] == 15
    )
    assert span.events[0]["name"] == "validation_passed"
    otel_span = span.to_otel_span()
    assert (otel_span["traceId"], otel_span["spanId"], otel_span["name"]) == (
        "trace_456",
        "span_123",
        "tool:edit",
    )
    assert otel_span["status"]["code"] == 1


def test_telemetry_collector_aggregates_counters_and_spans():
    """Collectors summarize span outcomes, counters, and per-trace exports."""
    collector = TelemetryCollector()
    collector.set_active_trace_id("trace_main_sess")
    collector.end_span(collector.start_span("llm:gpt-5", kind="llm").span_id, status="ok")
    collector.end_span(
        collector.start_span("tool:bash", kind="tool").span_id, status="error", error="denied"
    )
    collector.increment_counter("tool_calls", 2.0)
    collector.increment_counter("llm_tokens", 520.0)
    summary = collector.get_metrics_summary()
    assert (summary["total_spans"], summary["ok_spans"], summary["error_spans"]) == (2, 1, 1)
    assert summary["counters"] == {"tool_calls": 2.0, "llm_tokens": 520.0}
    spans = collector.export_spans()
    assert len(spans) == 2 and spans[0]["trace_id"] == "trace_main_sess"


def _hook_cmd(payload: dict) -> str:
    """Wrap a hook JSON payload in a python3 -c command."""
    return f'python3 -c "import json; print(json.dumps({payload}))"'


def test_hooks_match_patterns_across_wildcards():
    """Hook matchers support wildcards, lists, globs, and regexes."""
    assert matches_hook_pattern("*", "bash") is True
    assert matches_hook_pattern("bash, edit, write", "edit") is True
    assert matches_hook_pattern("bash, edit, write", "read") is False
    assert (
        matches_hook_pattern("web_*", "web_fetch") is True
        and matches_hook_pattern("web_*", "bash") is False
    )
    assert matches_hook_pattern(r"^tool_\d+$", "tool_42") is True
    assert matches_hook_pattern(r"^tool_\d+$", "tool_abc") is False


def test_hooks_merge_prefers_deny_and_stops():
    """Merged hook outcomes resolve deny over ask over allow and propagate stops."""
    merged = merge_hook_outputs(
        [
            HookOutput(decision="allow", additional_context=["Context A"]),
            HookOutput(decision="ask", reason="Needs confirmation"),
            HookOutput(decision="deny", reason="Security policy violation"),
        ]
    )
    assert merged.decision == "deny" and "Security policy violation" in (merged.reason or "")
    assert "Context A" in merged.additional_context
    assert (
        merge_hook_outputs([HookOutput(decision="allow"), HookOutput(decision="ask")]).decision
        == "ask"
    )
    stopped = merge_hook_outputs(
        [
            HookOutput(decision="allow", continue_run=True),
            HookOutput(decision="allow", continue_run=False, stop_reason="Milestone"),
        ]
    )
    assert stopped.stop is True and stopped.stop_reason == "Milestone"


def test_hooks_load_config_and_run_points(tmp_path: pathlib.Path):
    """Hook configs load from settings and pre/post points execute commands."""
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "bash",
                    "hooks": [
                        {
                            "command": _hook_cmd(
                                "{'decision': 'allow', 'additionalContext': ['Safe command']}"
                            )
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "bash",
                    "hooks": [{"command": _hook_cmd("{'systemMessages': ['Tool finished']}")}],
                }
            ],
        }
    }
    assert len(load_hook_config(str(tmp_path), settings=settings)["PreToolUse"]) == 1
    res_pre = run_hook_point(
        HookPoint.PRE_TOOL_USE,
        payload={"tool_name": "bash", "tool_input": {"command": "ls"}},
        project_root=str(tmp_path),
        settings=settings,
    )
    assert res_pre.decision == "allow" and "Safe command" in res_pre.additional_context
    ctx = type("Ctx", (), {"project_root": str(tmp_path), "session_id": "sess_1"})()
    assert run_pre_tool_use("bash", {"command": "ls"}, ctx, settings=settings) == "allow"
    res_post = run_hook_point(
        HookPoint.POST_TOOL_USE,
        payload={"tool_name": "bash", "tool_result": "output"},
        project_root=str(tmp_path),
        settings=settings,
    )
    assert "Tool finished" in res_post.system_messages


def test_atomic_write_creates_and_preserves_mode(tmp_path: pathlib.Path):
    """Atomic writes create parents, return bytes, and preserve existing modes."""
    target = tmp_path / "subdir" / "test.txt"
    assert write_file_atomic(target, "Hello World\nLine 2", mode=0o644) > 0
    assert target.read_text(encoding="utf-8") == "Hello World\nLine 2"
    restricted = tmp_path / "restricted.txt"
    write_file_atomic(restricted, "initial content", mode=0o600)
    write_file_atomic(restricted, "updated content")
    assert (
        stat.S_IMODE(restricted.stat().st_mode) == 0o600
        and restricted.read_text() == "updated content"
    )


def test_file_lock_serializes_exclusive_access(tmp_path: pathlib.Path):
    """File locks run operations sequentially and return their results."""
    target, executed = tmp_path / "locked_file.txt", []

    def op1() -> str:
        executed.append("op1_start")
        time.sleep(0.05)
        executed.append("op1_end")
        return "res1"

    assert with_file_lock(target, op1) == "res1"
    assert with_file_lock(target, lambda: (executed.append("op2"), "res2")[1]) == "res2"
    assert executed == ["op1_start", "op1_end", "op2"]
