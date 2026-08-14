"""Tool for exporting session history transcripts to shareable HTML or Markdown reports."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from coderAI.core.services import get_services
from coderAI.system.session_render import session_to_html, session_to_markdown
from coderAI.tools.base import Tool
from coderAI.tools.filesystem import ProjectPathError, resolve_under_project
from coderAI.types.tool_error_codes import ToolErrorCode


class ExportSessionParams(BaseModel):
    session_id: Optional[str] = Field(
        None, description="Specific session ID to export (default: current active session)"
    )
    format: Literal["html", "markdown", "md"] = Field(
        "html",
        description="Output format: 'html' for self-contained styled report, 'markdown' for GFM.",
    )
    output_path: Optional[str] = Field(
        None, description="Output destination file path (default: './session_<id>.<format>')"
    )


class ExportSessionTool(Tool):
    """Export conversation session transcripts into formatted HTML or Markdown files."""

    name = "export_session"
    description = (
        "Export a conversation session into a standalone, styled HTML report or Markdown document. "
        "Useful for generating shareable audit logs, code reviews, and project documentation."
    )
    parameters_model = ExportSessionParams
    is_read_only = False
    requires_confirmation = True
    approval_scope = "path"
    category = "utilities"

    async def execute(  # type: ignore[override]
        self,
        session_id: Optional[str] = None,
        format: str = "html",
        output_path: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            history_mgr = get_services().history
            session = None
            if session_id:
                session = history_mgr.load_session(session_id)
                if not session:
                    return {
                        "success": False,
                        "error": f"Session '{session_id}' not found.",
                        "error_code": ToolErrorCode.NOT_FOUND,
                    }
            else:
                session = history_mgr.current_session
                if not session:
                    return {
                        "success": False,
                        "error": "No active session to export.",
                        "error_code": ToolErrorCode.NOT_FOUND,
                    }

            fmt = format.lower()
            ext = "md" if fmt in ("markdown", "md") else "html"
            content = session_to_markdown(session) if ext == "md" else session_to_html(session)

            default_filename = f"{session.session_id}.{ext}"
            target_path_str = output_path or default_filename

            resolved_path = resolve_under_project(
                target_path_str,
                operation="export",
                check_protected=True,
                reject_symlink=True,
            )

            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(content, encoding="utf-8")

            return {
                "success": True,
                "session_id": session.session_id,
                "format": ext,
                "path": str(resolved_path),
                "messages_exported": len(session.messages),
                "bytes_written": len(content.encode("utf-8")),
                "message": f"Successfully exported session to {resolved_path.name}",
            }
        except ProjectPathError as e:
            return e.as_result()
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
