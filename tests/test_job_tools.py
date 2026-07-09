"""Tests for coderAI.tools.jobs — start_job / job_status / job_result /
wait_job / cancel_job contracts."""

import asyncio
from types import SimpleNamespace

from coderAI.core.provenance import Provenance
from coderAI.core.services import get_services, services_scope
from coderAI.tools.base import Tool, ToolRegistry
from coderAI.tools.jobs import (
    CancelJobTool,
    JobResultTool,
    JobStatusTool,
    StartJobTool,
    WaitJobTool,
)
from pydantic import BaseModel, Field


class EchoParams(BaseModel):
    text: str = Field(..., description="Text to echo")


class BackgroundableEchoTool(Tool):
    name = "bg_echo"
    description = "Echo test tool"
    parameters_model = EchoParams
    is_read_only = True
    backgroundable = True

    async def execute(self, text):  # type: ignore[override]
        return {"success": True, "echo": text}


class BackgroundableSlowTool(Tool):
    name = "bg_slow"
    description = "Slow test tool"
    is_read_only = True
    backgroundable = True

    async def execute(self, **kwargs):  # type: ignore[override]
        await asyncio.sleep(10.0)
        return {"success": True}


class BackgroundableGatedTool(Tool):
    name = "bg_gated"
    description = "Confirmation-gated test tool"
    requires_confirmation = True
    backgroundable = True

    def __init__(self):
        self.calls = 0

    async def execute(self, **kwargs):  # type: ignore[override]
        self.calls += 1
        return {"success": True, "output": "installed"}


class ForegroundOnlyTool(Tool):
    name = "fg_only"
    description = "Not backgroundable"
    is_read_only = True

    async def execute(self, **kwargs):  # type: ignore[override]
        return {"success": True}


def _start_job_tool(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    agent = SimpleNamespace(tools=registry, tracker_info=None)
    return StartJobTool(agent)


class TestStartJobGating:
    async def test_unknown_tool_refused(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            result = await start.execute(tool_name="no_such_tool", arguments={})
        assert result["success"] is False
        assert "Unknown tool" in result["error"]

    async def test_non_backgroundable_tool_refused_with_allowlist(self):
        start = _start_job_tool(BackgroundableEchoTool(), ForegroundOnlyTool())
        with services_scope():
            result = await start.execute(tool_name="fg_only", arguments={})
        assert result["success"] is False
        assert "not backgroundable" in result["error"]
        assert "bg_echo" in result["error"]

    async def test_job_machinery_refused_as_target(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            for name in ("start_job", "job_status", "job_result", "wait_job", "cancel_job"):
                result = await start.execute(tool_name=name, arguments={})
                assert result["success"] is False
                assert "job machinery" in result["error"]

    async def test_delegate_task_refused_as_target(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            result = await start.execute(tool_name="delegate_task", arguments={})
        assert result["success"] is False
        assert "delegate_task" in result["error"]

    async def test_invalid_arguments_fail_foreground_and_submit_nothing(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            result = await start.execute(tool_name="bg_echo", arguments={"wrong": 1})
            assert result["success"] is False
            assert result["error_code"] == "validation_error"
            assert get_services().jobs.list() == []

    def test_classifications_fail_closed(self):
        # A single approval covers a whole unattended job: per-call gate,
        # never blanket, fail-closed on tainted turns.
        assert StartJobTool.requires_confirmation is True
        assert StartJobTool.high_risk_no_blanket is True
        assert StartJobTool.is_egress is True


class TestStartJobRoundTrip:
    async def test_job_round_trip_start_wait_result(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            started = await start.execute(
                tool_name="bg_echo", arguments={"text": "hello"}
            )
            assert started["success"] is True
            job_id = started["job_id"]
            assert job_id.startswith("job_")
            assert started["timeout_seconds"] > 0

            waited = await WaitJobTool().execute(job_id=job_id, timeout=10)
            assert waited["success"] is True
            assert waited["finished"] is True
            assert waited["job"]["status"] == "done"

            collected = await JobResultTool().execute(job_id=job_id)
            assert collected["success"] is True
            assert collected["result"] == {"success": True, "echo": "hello"}

    async def test_single_start_job_approval_covers_gated_target(self):
        """start_job's own confirmation is the approval for the whole job —
        the gated target must run detached without a second prompt."""
        gated = BackgroundableGatedTool()
        start = _start_job_tool(gated)
        with services_scope():
            started = await start.execute(tool_name="bg_gated", arguments={})
            assert started["success"] is True
            record = await get_services().jobs.wait(started["job_id"], timeout=10.0)
            assert record.status.value == "done"
            assert gated.calls == 1


class TestJobStatusTool:
    async def test_unknown_job_id_errors(self):
        with services_scope():
            result = await JobStatusTool().execute(job_id="job_nope")
        assert result["success"] is False
        assert "job_nope" in result["error"]

    async def test_single_and_list_all(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            started = await start.execute(tool_name="bg_echo", arguments={"text": "x"})
            job_id = started["job_id"]
            await get_services().jobs.wait(job_id, timeout=10.0)

            single = await JobStatusTool().execute(job_id=job_id)
            assert single["success"] is True
            assert single["job"]["job_id"] == job_id
            assert single["job"]["tool_name"] == "bg_echo"

            listing = await JobStatusTool().execute()
            assert listing["success"] is True
            assert listing["count"] == 1
            assert listing["jobs"][0]["job_id"] == job_id


class TestJobResultTool:
    def test_result_provenance_taints_turn(self):
        # A background download's payload relayed via job_result must taint
        # the turn exactly as the foreground tool would have.
        assert JobResultTool.result_provenance is Provenance.UNTRUSTED_EXTERNAL

    async def test_unknown_job_id_errors(self):
        with services_scope():
            result = await JobResultTool().execute(job_id="job_nope")
        assert result["success"] is False

    async def test_running_job_result_not_ready(self):
        start = _start_job_tool(BackgroundableSlowTool())
        with services_scope():
            started = await start.execute(tool_name="bg_slow", arguments={})
            job_id = started["job_id"]
            await asyncio.sleep(0.05)  # let it start
            result = await JobResultTool().execute(job_id=job_id)
            assert result["success"] is False
            assert "not ready" in result["error"]
            await get_services().jobs.cancel(job_id)


class TestWaitJobTool:
    def test_resolve_timeout_adds_margin(self):
        assert WaitJobTool().resolve_timeout({"timeout": 120}) == 130.0

    def test_resolve_timeout_clamps(self):
        tool = WaitJobTool()
        assert tool.resolve_timeout({"timeout": 9999}) == 610.0
        assert tool.resolve_timeout({"timeout": -3}) == 11.0
        assert tool.resolve_timeout({"timeout": "garbage"}) == 70.0
        assert tool.resolve_timeout({}) == 70.0

    async def test_wait_on_running_job_reports_unfinished(self):
        start = _start_job_tool(BackgroundableSlowTool())
        with services_scope():
            started = await start.execute(tool_name="bg_slow", arguments={})
            job_id = started["job_id"]
            result = await WaitJobTool().execute(job_id=job_id, timeout=1)
            assert result["success"] is True
            assert result["finished"] is False
            assert "still" in result["message"]
            await get_services().jobs.cancel(job_id)

    async def test_wait_unknown_job_errors(self):
        with services_scope():
            result = await WaitJobTool().execute(job_id="job_nope")
        assert result["success"] is False


class TestCancelJobTool:
    async def test_cancel_running_job(self):
        start = _start_job_tool(BackgroundableSlowTool())
        with services_scope():
            started = await start.execute(tool_name="bg_slow", arguments={})
            job_id = started["job_id"]
            await asyncio.sleep(0.05)  # let it start
            result = await CancelJobTool().execute(job_id=job_id)
            assert result["success"] is True
            assert result["job"]["status"] == "cancelled"
            assert "cancelled" in result["message"]

    async def test_cancel_finished_job_reports_already_finished(self):
        start = _start_job_tool(BackgroundableEchoTool())
        with services_scope():
            started = await start.execute(tool_name="bg_echo", arguments={"text": "x"})
            job_id = started["job_id"]
            await get_services().jobs.wait(job_id, timeout=10.0)
            result = await CancelJobTool().execute(job_id=job_id)
            assert result["success"] is True
            assert "already finished" in result["message"]
            assert result["job"]["status"] == "done"

    async def test_cancel_unknown_job_errors(self):
        with services_scope():
            result = await CancelJobTool().execute(job_id="job_nope")
        assert result["success"] is False
