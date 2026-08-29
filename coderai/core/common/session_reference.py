"""Workspace cross-session reference parser and projection resolver (@session:<id>)."""

from __future__ import annotations

import base64
import json
import logging
import pathlib
import re
from typing import Any

from coderai.core.session_store import JsonlSessionStore

logger = logging.getLogger(__name__)

SESSION_MENTION_PATTERN = re.compile(
    r"@\[(?P<md_label>[^\]]+)\]\((?P<md_uri>dsh-session:[^\s)]+)\)"
    r"|(?P<dsh_uri>dsh-session:[a-zA-Z0-9_\-]+)"
    r"|@session[:/](?P<id>[a-zA-Z0-9_\-]+)"
    r"|session[:/](?P<raw_id>[a-zA-Z0-9_\-]{8,})"
)
DEFAULT_MAX_SESSION_REFERENCES = 3
DEFAULT_MAX_BYTES_PER_REFERENCE = 4096


def decode_session_uri(uri: str) -> str | None:
    """Decode a canonical dsh-session: URI or return the raw payload if alphanumeric."""
    if not uri or not uri.startswith("dsh-session:"):
        return None
    payload = uri[len("dsh-session:") :]
    try:
        padded = payload + "=" * ((4 - len(payload) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        try:
            val = json.loads(raw)
            if isinstance(val, str):
                return val
        except Exception:
            return raw
    except Exception:
        pass
    if re.match(r"^[a-zA-Z0-9_\-]+$", payload):
        return payload
    return None


def extract_session_reference_ids(text: str) -> list[str]:
    """Extract unique session reference IDs mentioned in text (supports @session:<id>, @session/<id>, dsh-session: URIs)."""
    if not text:
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for match in SESSION_MENTION_PATTERN.finditer(text):
        sid: str | None = None
        if match.group("md_uri"):
            sid = decode_session_uri(match.group("md_uri"))
        elif match.group("dsh_uri"):
            sid = decode_session_uri(match.group("dsh_uri"))
        else:
            sid = match.group("id") or match.group("raw_id")
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids



def render_session_snapshot(
    session_id: str,
    project_root: str,
    max_bytes: int = DEFAULT_MAX_BYTES_PER_REFERENCE,
) -> str | None:
    """Load and render a bounded snapshot projection of a historical session."""
    store = JsonlSessionStore(project_root)
    lines = store.read_raw_lines(session_id)
    if not lines:
        return None

    # Parse entries and events
    user_prompts: list[str] = []
    assistant_replies: list[str] = []
    tool_calls_summary: list[str] = []
    session_summary: str | None = None
    created_time: str | None = None
    model: str | None = None

    for line in lines:
        try:
            data = json.loads(line)
            role = data.get("role")
            content = data.get("content") or ""
            ev_type = data.get("type")

            if not created_time and data.get("createTime"):
                created_time = data.get("createTime")
            if not model and data.get("meta", {}).get("model"):
                model = data.get("meta", {}).get("model")
            if data.get("summary"):
                session_summary = data.get("summary")

            if role == "user" and content and not data.get("compacted"):
                user_prompts.append(content.strip())
            elif role == "assistant":
                if content and not data.get("compacted"):
                    assistant_replies.append(content.strip())
                tcs = data.get("tool_calls") or data.get("toolCalls")
                if tcs and isinstance(tcs, list):
                    for tc in tcs:
                        fn_name = tc.get("function", {}).get("name") if isinstance(tc, dict) else "tool"
                        if fn_name:
                            tool_calls_summary.append(fn_name)
            elif ev_type == "session/created":
                created_time = data.get("timestamp")
        except Exception:
            continue

    if not user_prompts and not assistant_replies:
        return None

    output_lines: list[str] = [
        f"### Referenced Historical Session: `{session_id}`",
    ]
    meta_parts: list[str] = []
    if session_summary:
        meta_parts.append(f"**Topic**: {session_summary}")
    if model:
        meta_parts.append(f"**Model**: `{model}`")
    if created_time:
        meta_parts.append(f"**Date**: `{created_time[:19]}`")
    if meta_parts:
        output_lines.append(" | ".join(meta_parts))

    if user_prompts:
        initial_goal = user_prompts[0]
        if len(initial_goal) > 500:
            initial_goal = initial_goal[:497] + "..."
        output_lines.append(f"\n**Initial Objective**:\n```\n{initial_goal}\n```")

    if assistant_replies:
        final_reply = assistant_replies[-1]
        if len(final_reply) > 1500:
            final_reply = final_reply[:1497] + "..."
        output_lines.append(f"\n**Final Outcome / Conclusions**:\n{final_reply}")

    if tool_calls_summary:
        unique_tools = sorted(set(tool_calls_summary))
        output_lines.append(f"\n*Tools executed in session*: `{', '.join(unique_tools)}` ({len(tool_calls_summary)} total calls)")

    rendered = "\n".join(output_lines)
    if len(rendered.encode("utf-8")) > max_bytes:
        rendered = rendered[: max_bytes - 30] + "\n... [snapshot truncated]"

    return rendered


def resolve_session_references(
    project_root: str,
    text: str,
    max_references: int = DEFAULT_MAX_SESSION_REFERENCES,
    max_bytes_per_reference: int = DEFAULT_MAX_BYTES_PER_REFERENCE,
) -> tuple[str, list[dict[str, Any]], str]:
    """Extract and resolve @session references from user input.

    Returns:
        tuple of (cleaned_text, list_of_reference_metadata, formatted_snapshot_context)
    """
    if not text:
        return text, [], ""

    target_ids = extract_session_reference_ids(text)[:max_references]
    if not target_ids:
        return text, [], ""

    snapshots: list[str] = []
    refs: list[dict[str, Any]] = []

    for sid in target_ids:
        snapshot = render_session_snapshot(
            session_id=sid,
            project_root=project_root,
            max_bytes=max_bytes_per_reference,
        )
        if snapshot:
            snapshots.append(snapshot)
            refs.append({"sessionId": sid, "resolved": True})
        else:
            refs.append({"sessionId": sid, "resolved": False})

    context_block = ""
    if snapshots:
        context_block = (
            "\n\n## Context from Referenced Prior Sessions\n"
            + "\n\n---\n\n".join(snapshots)
        )

    return text, refs, context_block
