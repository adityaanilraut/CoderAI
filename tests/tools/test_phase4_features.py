"""Unit tests for Phase 4 features:
- Session export (HTML & Markdown)
- Context statistics tool (token layer accounting)
- Workspace status tool (git & recent file tracking)
"""

from pathlib import Path
import pytest

from coderAI.core.services import services_scope
from coderAI.system.history import Session
from coderAI.system.session_render import session_to_html, session_to_markdown
from coderAI.tools.session_export import ExportSessionTool
from coderAI.tools.context_stats import ContextStatsTool
from coderAI.tools.filesystem.manage import WorkspaceStatusTool


def test_session_export_to_markdown_and_html(tmp_path: Path):
    s = Session(session_id="test_export_sess", model="claude-sonnet-5")
    s.name = "Test Export Feature"
    s.add_message("user", "Hello CoderAI")
    s.messages[-1].reasoning_content = "Thinking about greeting..."
    s.add_message("assistant", "Hello! How can I help you today?")
    s.total_tokens = 1500
    s.prompt_tokens = 1000
    s.completion_tokens = 500
    s.total_cost_usd = 0.0045

    md_out = session_to_markdown(s)
    assert "# Test Export Feature" in md_out
    assert "**Session ID**: `test_export_sess`" in md_out
    assert "Thinking about greeting..." in md_out
    assert "Hello! How can I help you today?" in md_out

    html_out = session_to_html(s)
    assert "<!DOCTYPE html>" in html_out
    assert "Test Export Feature" in html_out
    assert "test_export_sess" in html_out
    assert "Thinking about greeting..." in html_out
    assert "badge-user" in html_out
    assert "badge-assistant" in html_out


@pytest.mark.asyncio
async def test_export_session_tool(tmp_path: Path):
    from coderAI.system.history import HistoryManager

    tool = ExportSessionTool()
    assert tool.requires_confirmation is True
    assert tool.safe is False
    assert tool.approval_scope == "path"

    history_mgr = HistoryManager(history_dir=tmp_path / "history")
    session = history_mgr.create_session(model="test-model")
    session.name = "Tool Export Test"
    session.add_message("user", "Please export this session")
    session.add_message("assistant", "Exporting now.")
    history_mgr.save_session(session)

    with services_scope(inherit=True, history=history_mgr):
        target_html = tmp_path / "report.html"
        res_html = await tool.execute(
            session_id=session.session_id, format="html", output_path=str(target_html)
        )
        assert res_html["success"] is True
        assert target_html.exists()
        assert "<!DOCTYPE html>" in target_html.read_text(encoding="utf-8")

        target_md = tmp_path / "report.md"
        res_md = await tool.execute(
            session_id=session.session_id, format="md", output_path=str(target_md)
        )
        assert res_md["success"] is True
        assert target_md.exists()
        assert "# Tool Export Test" in target_md.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_context_stats_tool():
    tool = ContextStatsTool()
    res = await tool.execute(include_layers=True, include_pinned_files=True)
    assert res["success"] is True
    assert res["live_controller"] is False
    assert "total_tokens" in res
    assert "context_limit" in res
    assert "utilization_pct" in res
    assert "layers" in res
    assert "recommendation" in res
    assert res["pinned_files"] == []


@pytest.mark.asyncio
async def test_context_stats_uses_live_controller_pins(tmp_path: Path):
    class _StubProvider:
        MODEL_CONTEXT_WINDOWS: dict[str, int] = {}

        def count_tokens(self, text: str) -> int:
            return max(1, len(text or "") // 4)

    from coderAI.context.context_controller import ContextController
    from coderAI.system.config import config_manager

    pinned = tmp_path / "app.py"
    pinned.write_text("print('hello')\n", encoding="utf-8")
    controller = ContextController(provider=_StubProvider(), config=config_manager.load())
    controller.pinned_files[str(pinned)] = pinned.read_text(encoding="utf-8")
    controller._pinned_mtimes[str(pinned)] = pinned.stat().st_mtime
    tool = ContextStatsTool()
    with services_scope(inherit=True, context_controller=controller):
        res = await tool.execute(include_pinned_files=True)
    assert res["success"] is True
    assert res["live_controller"] is True
    assert res["pinned_files"] == [
        {"path": str(pinned), "size_chars": len("print('hello')\n")}
    ]


@pytest.mark.asyncio
async def test_workspace_status_tool(tmp_path: Path):
    project_dir = tmp_path / "ws_test"
    project_dir.mkdir()
    (project_dir / "file1.txt").write_text("content 1")
    (project_dir / "file2.py").write_text("print(2)")

    tool = WorkspaceStatusTool()
    res = await tool.execute(path=str(project_dir), include_recent_minutes=10)
    assert res["success"] is True
    assert res["recent_modified_files_count"] >= 2
    paths = {f["path"] for f in res["recent_modified_files"]}
    assert "file1.txt" in paths
    assert "file2.py" in paths
    assert res["scan_truncated"] is False


def test_session_export_tool_does_not_import_tui():
    import coderAI.tools.session_export as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "coderAI.tui" not in source
