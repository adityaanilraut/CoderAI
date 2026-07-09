"""Tests for coderAI.core.jobs — background JobManager lifecycle."""

import asyncio

from coderAI.core.jobs import (
    MAX_FINISHED_JOBS,
    JobManager,
    JobRecord,
    JobStatus,
)
from coderAI.core.services import services_scope
from coderAI.system.config import Config


class FakeRegistry:
    """Stands in for ToolRegistry: routes execute() to a coroutine factory."""

    def __init__(self, fn):
        self._fn = fn

    async def execute(self, tool_name, **kwargs):
        return await self._fn(tool_name, **kwargs)


def _registry(result=None, *, delay=0.0, exc=None):
    async def fn(tool_name, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return result if result is not None else {"success": True, "output": "ok"}

    return FakeRegistry(fn)


class TestJobLifecycle:
    async def test_successful_job_reaches_done_with_result(self):
        with services_scope():
            manager = JobManager()
            job_id = manager.submit(
                "fake_tool", {"x": 1}, registry=_registry({"success": True, "n": 7}), timeout=5.0
            )
            assert job_id.startswith("job_")
            record = await manager.wait(job_id, timeout=5.0)
            assert record.status is JobStatus.DONE
            assert record.result == {"success": True, "n": 7}
            assert record.is_finished
            assert record.started_at is not None
            assert record.finished_at >= record.started_at
            assert record.arguments == {"x": 1}

    async def test_non_dict_result_is_wrapped(self):
        with services_scope():
            manager = JobManager()

            class OddRegistry:
                async def execute(self, tool_name, **kwargs):
                    return "plain string"

            job_id = manager.submit("odd", {}, registry=OddRegistry(), timeout=5.0)
            record = await manager.wait(job_id, timeout=5.0)
            assert record.status is JobStatus.DONE
            assert record.result == {"success": True, "result": "plain string"}

    async def test_raising_tool_reaches_failed(self):
        with services_scope():
            manager = JobManager()
            job_id = manager.submit(
                "boom", {}, registry=_registry(exc=ValueError("kaput")), timeout=5.0
            )
            record = await manager.wait(job_id, timeout=5.0)
            assert record.status is JobStatus.FAILED
            assert "kaput" in record.error
            assert record.result is None

    async def test_per_job_timeout_reaches_timeout_state(self):
        with services_scope():
            manager = JobManager()
            job_id = manager.submit(
                "slow", {}, registry=_registry(delay=5.0), timeout=0.05
            )
            record = await manager.wait(job_id, timeout=5.0)
            assert record.status is JobStatus.TIMEOUT
            assert "timeout" in record.error.lower()

    async def test_get_and_list(self):
        with services_scope():
            manager = JobManager()
            assert manager.get("job_nope") is None
            assert manager.list() == []
            job_id = manager.submit("fake", {}, registry=_registry(), timeout=5.0)
            assert manager.get(job_id) is not None
            assert [r.job_id for r in manager.list()] == [job_id]
            await manager.wait(job_id, timeout=5.0)


class TestJobConcurrencyCap:
    async def test_peak_concurrency_equals_max_background_jobs(self):
        running = 0
        peak = 0

        async def fn(tool_name, **kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return {"success": True}

        with services_scope(config=Config(max_background_jobs=2)):
            manager = JobManager()
            job_ids = [
                manager.submit(f"tool_{i}", {}, registry=FakeRegistry(fn), timeout=10.0)
                for i in range(4)
            ]
            for job_id in job_ids:
                record = await manager.wait(job_id, timeout=10.0)
                assert record.status is JobStatus.DONE
        assert peak == 2


class TestJobCancel:
    async def test_cancel_mid_run(self):
        with services_scope():
            manager = JobManager()
            job_id = manager.submit("slow", {}, registry=_registry(delay=10.0), timeout=30.0)
            await asyncio.sleep(0.05)  # let it start
            record = await manager.cancel(job_id)
            assert record.status is JobStatus.CANCELLED
            assert record.is_finished

    async def test_cancel_finished_job_is_noop(self):
        with services_scope():
            manager = JobManager()
            job_id = manager.submit("fast", {}, registry=_registry(), timeout=5.0)
            await manager.wait(job_id, timeout=5.0)
            record = await manager.cancel(job_id)
            assert record.status is JobStatus.DONE

    async def test_cancel_unknown_returns_none(self):
        with services_scope():
            manager = JobManager()
            assert await manager.cancel("job_nope") is None


class TestJobWait:
    async def test_wait_timeout_leaves_job_running(self):
        with services_scope():
            manager = JobManager()
            job_id = manager.submit("slow", {}, registry=_registry(delay=10.0), timeout=30.0)
            record = await manager.wait(job_id, timeout=0.05)
            assert record is not None
            assert not record.is_finished
            assert record.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            await manager.cancel(job_id)

    async def test_wait_unknown_returns_none(self):
        with services_scope():
            manager = JobManager()
            assert await manager.wait("job_nope", timeout=0.05) is None


class TestFinishedJobLRU:
    def test_prune_drops_oldest_beyond_cap(self):
        manager = JobManager()
        for i in range(MAX_FINISHED_JOBS + 5):
            record = JobRecord(
                job_id=f"job_{i:04d}", tool_name="t", arguments={}, status=JobStatus.DONE
            )
            record.finished_at = float(i)
            manager._jobs[record.job_id] = record
        removed = manager.prune_finished()
        assert removed == 5
        assert len(manager._jobs) == MAX_FINISHED_JOBS
        # Oldest five dropped, newest kept.
        assert "job_0004" not in manager._jobs
        assert "job_0005" in manager._jobs

    def test_live_jobs_never_pruned(self):
        manager = JobManager()
        live = JobRecord(job_id="job_live", tool_name="t", arguments={})
        live.status = JobStatus.RUNNING
        manager._jobs[live.job_id] = live
        for i in range(MAX_FINISHED_JOBS + 5):
            record = JobRecord(
                job_id=f"job_{i:04d}", tool_name="t", arguments={}, status=JobStatus.DONE
            )
            record.finished_at = float(i)
            manager._jobs[record.job_id] = record
        manager.prune_finished()
        assert "job_live" in manager._jobs


class TestJobShutdown:
    async def test_shutdown_cancels_all_live_jobs(self):
        with services_scope():
            manager = JobManager()
            job_ids = [
                manager.submit(f"slow_{i}", {}, registry=_registry(delay=30.0), timeout=60.0)
                for i in range(3)
            ]
            await asyncio.sleep(0.05)  # let them start
            await manager.shutdown()
            for job_id in job_ids:
                record = manager.get(job_id)
                assert record is not None
                assert record.is_finished
                assert record.status is JobStatus.CANCELLED
