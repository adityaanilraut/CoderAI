"""Coverage for coderAI/tui/rendering.py panel renderers."""

from rich.console import Console

from coderAI.tui import rendering as r
from coderAI.tui.state import AgentInfo, SessionState


def _to_text(renderable) -> str:
    console = Console(record=True, force_terminal=False, width=100)
    console.print(renderable)
    return console.export_text()


def test_render_session_header_rich_state():
    s = SessionState(
        model="claude-opus-4-8",
        provider="anthropic",
        streaming=True,
        ctx_used=12_000,
        ctx_limit=200_000,
        cost_usd=0.1234,
        budget_usd=5.0,
        iteration=3,
        max_iterations=50,
        elapsed_s=125.0,
        auto_approve=True,
        reasoning="high",
        active_persona="planner",
        progress={"label": "Indexing", "current": 2, "total": 10},
    )
    s.agents["a1"] = AgentInfo(id="a1", name="main", status="thinking")
    out = r.render_session_header(s)
    assert "claude-opus-4-8" in out
    assert "anthropic" in out
    assert "2m 5s" in out  # elapsed formatting
    assert "agents" in out  # active agent chip
    assert "yolo" in out
    assert "planner" in out
    assert "Indexing 2/10" in out


def test_render_session_header_minimal_state():
    s = SessionState()
    out = r.render_session_header(s)
    assert "…" in out  # model placeholder
    # No budget, no elapsed, no agents, no persona — still renders chips.
    assert "yolo" in out


def test_render_session_header_progress_without_totals():
    s = SessionState(progress={"label": "Working"})
    out = r.render_session_header(s)
    assert "Working" in out


def test_render_agent_tree_empty():
    out = r.render_agent_tree(SessionState())
    assert isinstance(out, str)
    assert "no agents yet" in out


def test_render_agent_tree_with_hierarchy_and_statuses():
    s = SessionState()
    s.agents = {
        "root": AgentInfo(id="root", name="root", status="tool_call"),
        "c1": AgentInfo(id="c1", name="child-think", parent_id="root", status="thinking"),
        "c2": AgentInfo(id="c2", name="child-wait", parent_id="root", status="waiting_for_user"),
        "c3": AgentInfo(id="c3", name="child-done", parent_id="root", status="done"),
        "c4": AgentInfo(id="c4", name="child-err", parent_id="root", status="error"),
        "c5": AgentInfo(id="c5", name="child-idle", parent_id="root", status="idle", task="x" * 40),
    }
    tree = r.render_agent_tree(s)
    text = _to_text(tree)
    assert "root" in text
    assert "child-think" in text
    assert "child-done" in text


def test_render_plan_empty_and_populated():
    empty = r.render_plan(SessionState())
    assert isinstance(empty, str)
    assert "no active plan" in empty

    s = SessionState(
        current_plan={
            "title": "Build feature",
            "completed": 1,
            "total": 3,
            "currentIdx": 0,
            "steps": [
                {"index": 1, "status": "done", "description": "scaffold"},
                {"index": 2, "status": "pending", "description": "implement"},
                {"index": 3, "status": "pending", "description": "test"},
            ],
        }
    )
    text = _to_text(r.render_plan(s))
    assert "Build feature" in text
    assert "scaffold" in text
    assert "implement" in text


def test_render_tasks_empty_and_populated():
    empty = r.render_tasks(SessionState())
    assert "no tasks" in empty

    s = SessionState(
        current_tasks={
            "summary": "2 of 4 done",
            "inProgress": [{"id": 1, "title": "writing", "priority": "high"}],
            "pending": [{"id": 2, "title": "later", "priority": "low"}],
            "completed": [{"id": 3, "title": "done thing", "priority": "medium"}],
        }
    )
    out = r.render_tasks(s)
    assert "writing" in out
    assert "later" in out
    assert "done thing" in out
    assert "In progress" in out
    assert "Pending" in out
    assert "Completed" in out


def test_render_tasks_all_buckets_empty():
    s = SessionState(current_tasks={"summary": "", "inProgress": [], "pending": [], "completed": []})
    out = r.render_tasks(s)
    assert "empty list" in out


def test_composer_footer_markup_states():
    not_ready = r.composer_footer_markup(SessionState(ready=False))
    assert "Waiting for agent" in not_ready

    ready = r.composer_footer_markup(SessionState(ready=True, reasoning="medium"))
    assert "reasoning:" in ready
    assert "medium" in ready

    with_progress = r.composer_footer_markup(
        SessionState(ready=True, progress={"label": "Compiling", "current": 1, "total": 2})
    )
    assert "Compiling" in with_progress
    assert "1/2" in with_progress

    progress_no_total = r.composer_footer_markup(
        SessionState(ready=True, progress={"label": "Spinning"})
    )
    assert "Spinning" in progress_no_total
