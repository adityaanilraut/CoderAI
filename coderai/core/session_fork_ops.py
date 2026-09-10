"""Session branching, forking, and checkpoint-based undo operations."""

from __future__ import annotations

import json
import uuid
from typing import Any

from coderai.core.session_models import _now
from coderai.core.state import clear_session_state, rebuild_session_state_from_history

MAX_SESSION_ENTRIES = 50


def fork_session(
    manager: Any,
    source_session_id: str,
    at_message_id_or_seq: str | int | None = None,
) -> str | None:
    """Fork an existing session into a new independent session branch with cloned message history, event logs, and file checkpoint."""
    target_src_id = manager.resolve_session_id(source_session_id) or source_session_id
    src_entry = manager._get_entry(target_src_id)
    if not src_entry:
        return None

    forked_id = f"ses_{uuid.uuid4().hex[:12]}"
    now = _now()

    raw_lines = manager.session_store.read_raw_lines(target_src_id)

    sliced_lines: list[str] = []
    checkpoint_hash: str | None = None

    if at_message_id_or_seq is None:
        sliced_lines = list(raw_lines)
    elif isinstance(at_message_id_or_seq, int):
        # Check if matching by seq number or message index
        matched_by_seq = False
        for line in raw_lines:
            try:
                data = json.loads(line)
                if "seq" in data:
                    if int(data["seq"]) <= at_message_id_or_seq:
                        sliced_lines.append(line)
                        matched_by_seq = True
                elif not matched_by_seq and len(sliced_lines) <= at_message_id_or_seq:
                    sliced_lines.append(line)
            except Exception:
                continue
        if not matched_by_seq and not sliced_lines and raw_lines:
            # Fallback to index-based slice
            sliced_lines = raw_lines[: at_message_id_or_seq + 1]
    elif isinstance(at_message_id_or_seq, str):
        for line in raw_lines:
            sliced_lines.append(line)
            try:
                data = json.loads(line)
                if data.get("id") == at_message_id_or_seq:
                    break
            except Exception:
                continue

    # Find latest checkpoint hash in the sliced messages/events
    for line in reversed(sliced_lines):
        try:
            data = json.loads(line)
            meta = data.get("meta") or data.get("data", {}).get("meta") or {}
            if isinstance(meta, dict) and meta.get("checkpointHash"):
                checkpoint_hash = meta["checkpointHash"]
                break
        except Exception:
            continue

    # Write cloned lines to the new session's file, rewriting sessionId
    forked_lines: list[str] = []
    for line in sliced_lines:
        try:
            data = json.loads(line)
            if "sessionId" in data:
                data["sessionId"] = forked_id
            elif "session_id" in data:
                data["session_id"] = forked_id
            forked_lines.append(json.dumps(data, ensure_ascii=False))
        except Exception:
            forked_lines.append(line)

    manager.session_store.write_raw_lines(forked_id, forked_lines)

    # Fork file history branch
    manager.file_history.ensure_session(forked_id)
    manager.file_history.fork_session(target_src_id, forked_id, checkpoint_hash=checkpoint_hash)

    # Phase 2: copy persisted state (Kimi: fork titles "Fork: <title>").
    try:
        from coderai.core.session_state import SessionState

        src_state = manager.get_session_state(target_src_id)
        forked_state = SessionState.model_validate(
            src_state.model_dump(mode="json"),
        )
        forked_state.custom_title = f"Fork: {src_entry.get('summary', '')}"[:200]
        forked_state.title_generated = True
        manager._session_states[forked_id] = forked_state
        manager._save_session_state(forked_id)
    except Exception:
        pass

    # Update sessions index
    index = manager._load_index()
    forked_entry = {
        "id": forked_id,
        "summary": f"[Fork] {src_entry.get('summary', '')}",
        "assistantReply": src_entry.get("assistantReply"),
        "assistantThinking": src_entry.get("assistantThinking"),
        "assistantRefusal": None,
        "toolCalls": src_entry.get("toolCalls"),
        "status": "completed",
        "failReason": None,
        "askPermissions": None,
        "usage": None,
        "usagePerModel": None,
        "activeTokens": src_entry.get("activeTokens", 0),
        "createTime": now,
        "updateTime": now,
        "planMode": src_entry.get("planMode", False),
        "forkOf": target_src_id,
        "parentSessionId": target_src_id,
        "forkPoint": at_message_id_or_seq,
    }
    index["entries"].insert(0, forked_entry)
    index["entries"] = index["entries"][:MAX_SESSION_ENTRIES]
    manager._save_index(index)
    try:  # Kimi metadata.py parity: forked session becomes the latest.
        from coderai.cli.metadata import record_last_session

        record_last_session(manager.project_root, forked_id)
    except Exception:
        pass

    return forked_id


def list_undo_targets(manager: Any, session_id: str) -> list[dict[str, Any]]:
    """Return all undoable user turns with checkpoint hashes and prompt previews in chronological order."""
    target_id = manager.resolve_session_id(session_id) or session_id
    messages = manager.list_session_messages(target_id)
    user_messages = [m for m in messages if m.role == "user" and not m.compacted and m.visible]
    if not user_messages:
        return []

    targets: list[dict[str, Any]] = []
    for idx, m in enumerate(user_messages, 1):
        ckpt_hash = (m.meta or {}).get("checkpointHash")
        if not ckpt_hash:
            ckpt_hash = manager.file_history.get_current_checkpoint_hash(target_id)

        can_restore_code = bool(
            ckpt_hash and manager.file_history.can_restore(target_id, ckpt_hash)
        )
        raw_p = (m.meta or {}).get("rawPrompt")
        if raw_p and isinstance(raw_p, str):
            prompt_preview = raw_p.strip().splitlines()[0] if raw_p else "(empty prompt)"
        else:
            prompt_text = m.content or ""
            if "\n\n---\n\n" in prompt_text:
                prompt_text = prompt_text.split("\n\n---\n\n", 1)[1]
            prompt_preview = (
                prompt_text.strip().splitlines()[0] if prompt_text else "(empty prompt)"
            )
        targets.append(
            {
                "index": idx,
                "turn_index": idx,
                "message_id": m.id,
                "prompt": prompt_preview,
                "full_prompt": m.content,
                "create_time": m.create_time,
                "checkpoint_hash": ckpt_hash,
                "can_restore_code": can_restore_code,
            }
        )
    return targets


def undo(
    manager: Any,
    session_id: str,
    target_message_id: str | None = None,
    mode: str = "restore_both",
) -> bool:
    """Revert files and/or message history back to a previous user prompt checkpoint.

    Modes:
      - "restore_both" (default): Reverts disk files and truncates message history.
      - "restore_conversation_only": Truncates message history without modifying disk files.
      - "restore_code_only": Reverts disk files without truncating message history.
    """
    target_id = manager.resolve_session_id(session_id) or session_id
    messages = manager.list_session_messages(target_id)
    user_messages = [m for m in messages if m.role == "user" and not m.compacted and m.visible]
    if not user_messages:
        return False

    if target_message_id:
        target_user = next((m for m in user_messages if m.id == target_message_id), None)
        if not target_user:
            return False
    else:
        target_user = user_messages[-1]

    checkpoint_hash = (target_user.meta or {}).get("checkpointHash")
    if not checkpoint_hash:
        checkpoint_hash = manager.file_history.get_current_checkpoint_hash(target_id)

    # Restore code if requested
    if mode in ("restore_both", "restore_code_only"):
        if not checkpoint_hash or not manager.file_history.can_restore(target_id, checkpoint_hash):
            if mode == "restore_code_only":
                return False
        else:
            manager.file_history.restore(target_id, checkpoint_hash)

    # Restore conversation if requested
    if mode in ("restore_both", "restore_conversation_only"):
        cutoff_idx = next((i for i, m in enumerate(messages) if m.id == target_user.id), -1)
        if cutoff_idx >= 0:
            retained_messages = messages[:cutoff_idx]
            manager._save_messages(target_id, retained_messages)
            clear_session_state(target_id)
            rebuild_session_state_from_history(
                target_id, [manager._serialize_message(m) for m in retained_messages]
            )

    manager._update_entry(
        target_id,
        lambda e: {
            **e,
            "status": "completed",
            "updateTime": _now(),
        },
    )
    return True
