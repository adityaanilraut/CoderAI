# Ported from coderai/core/tools/ask_user_question.py - kimi structure (kimi_cli/tools/ask_user/__init__.py).
"""AskUserQuestion tool — pauses for user clarification."""

from __future__ import annotations

import json
import uuid
from typing import Any

from coderai.tools.legacy.types import ToolResult


def _parse_questions(raw: Any) -> tuple[bool, list[dict[str, Any]], str | None]:
    if not isinstance(raw, list) or not raw:
        return False, [], '"questions" must be a non-empty array.'

    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return False, [], f"Question at index {index} must be an object."

        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            return False, [], f'Question at index {index} is missing a non-empty "question" string.'

        raw_options = item.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            return False, [], f'Question at index {index} must include a non-empty "options" array.'

        options: list[dict[str, Any]] = []
        for opt_index, option in enumerate(raw_options):
            if not isinstance(option, dict):
                return False, [], f"Option {opt_index} for question {index} must be an object."

            label = option.get("label")
            if not isinstance(label, str) or not label.strip():
                return (
                    False,
                    [],
                    f'Option {opt_index} for question {index} is missing a non-empty "label" string.',
                )

            desc = option.get("description")
            opt_entry: dict[str, str] = {"label": label.strip()}
            if isinstance(desc, str) and desc.strip():
                opt_entry["description"] = desc.strip()
            options.append(opt_entry)

        multi_select = (
            bool(item.get("multiSelect", False)) if item.get("multiSelect") is not None else None
        )

        q_dict: dict[str, Any] = {
            "question": question.strip(),
            "options": options,
        }
        if multi_select is not None:
            q_dict["multiSelect"] = multi_select

        questions.append(q_dict)

    return True, questions, None


def _build_question_summary(questions: list[dict[str, Any]]) -> str:
    lines = ["Waiting for user input."]

    for index, item in enumerate(questions, 1):
        lines.append("")
        lines.append(f"{index}. {item['question']}")
        mode = "multi-select" if item.get("multiSelect") else "single-select"
        lines.append(f"   Mode: {mode}")
        for option in item.get("options", []):
            lines.append(f"   - {option['label']}")
            if option.get("description"):
                lines.append(f"     {option['description']}")
        lines.append("   - Other")

    return "\n".join(lines)


async def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return await handle_ask_user_question_tool(args, context)


async def handle_ask_user_question_tool(args: dict[str, Any], context: Any) -> ToolResult:
    ok, questions, err = _parse_questions(args.get("questions"))
    if not ok:
        return ToolResult(
            ok=False,
            name="AskUserQuestion",
            error=err or "Invalid questions payload.",
        )

    # AFK parity: auto-dismiss AskUserQuestion when AFK/YOLO is enabled
    is_afk = False
    try:
        from coderai.soul.session.manager import _global_afk_check  # type: ignore

        if _global_afk_check():
            is_afk = True
    except Exception:
        pass
    if not is_afk:
        try:
            sid = getattr(context, "session_id", "")
            if sid:
                from coderai.soul.session.manager import _check_afk_for_session

                if _check_afk_for_session(sid):
                    is_afk = True
        except Exception:
            pass

    tc_id = ""
    tc = getattr(context, "tool_call", None)
    if isinstance(tc, dict):
        tc_id = str(tc.get("id") or "")
    elif tc is not None:
        tc_id = str(getattr(tc, "id", "") or "")

    wire_server = None
    try:
        from coderai.wire.server import get_active_wire_server

        wire_server = get_active_wire_server()
    except Exception:
        pass

    if is_afk:
        if wire_server is not None:
            from kosong.tooling import BriefDisplayBlock, ToolResult as WireToolResult, ToolReturnValue
            from coderai.wire.emitter import wire_send

            rv = ToolReturnValue(
                is_error=False,
                output=(
                    '{"answers": {}, "note": "Running in afk mode.'
                    ' No user is present. Make your own decision."}'
                ),
                message="Afk mode, auto-dismissed.",
                display=[BriefDisplayBlock(text="Auto-dismissed (afk)")],
            )
            wire_send(WireToolResult(tool_call_id=tc_id, return_value=rv))
        return ToolResult(
            ok=True,
            name="AskUserQuestion",
            output="Auto-dismissed (afk mode enabled). Proceed with best assumption.",
            metadata={
                "kind": "ask_user_question",
                "questions": questions,
                "afk_dismissed": True,
            },
        )

    if wire_server is not None:
        from coderai.wire.emitter import wire_send
        from coderai.wire.types import (
            QuestionItem,
            QuestionNotSupported,
            QuestionOption,
            QuestionRequest,
        )
        from kosong.tooling import BriefDisplayBlock, ToolError, ToolResult as WireToolResult, ToolReturnValue

        if not getattr(wire_server, "_client_supports_question", False):
            err = ToolError(
                message=(
                    "The connected client does not support interactive questions. "
                    "Do NOT call this tool again. "
                    "Ask the user directly in your text response instead."
                ),
                brief="Client unsupported",
                display=[BriefDisplayBlock(text="Client unsupported")],
            )
            wire_send(WireToolResult(tool_call_id=tc_id, return_value=err))
            return ToolResult(
                ok=False,
                name="AskUserQuestion",
                error=err.message,
            )

        q_items = [
            QuestionItem(
                question=q["question"],
                header=q.get("header", ""),
                options=[
                    QuestionOption(label=o["label"], description=o.get("description", ""))
                    for o in (q.get("options") or [])
                ],
                multi_select=bool(q.get("multiSelect")),
            )
            for q in questions
        ]
        request = QuestionRequest(
            id=str(uuid.uuid4()),
            tool_call_id=tc_id,
            questions=q_items,
        )
        wire_send(request)

        try:
            answers = await request.wait()
        except QuestionNotSupported:
            err = ToolError(
                message=(
                    "The connected client does not support interactive questions. "
                    "Do NOT call this tool again. "
                    "Ask the user directly in your text response instead."
                ),
                brief="Client unsupported",
                display=[BriefDisplayBlock(text="Client unsupported")],
            )
            wire_send(WireToolResult(tool_call_id=tc_id, return_value=err))
            return ToolResult(
                ok=False,
                name="AskUserQuestion",
                error=err.message,
            )
        except Exception:
            err = ToolError(
                message="Failed to get user response.",
                brief="Question failed",
                display=[BriefDisplayBlock(text="Question failed")],
            )
            wire_send(WireToolResult(tool_call_id=tc_id, return_value=err))
            return ToolResult(
                ok=False,
                name="AskUserQuestion",
                error=err.message,
            )

        if not answers:
            rv = ToolReturnValue(
                is_error=False,
                output='{"answers": {}, "note": "User dismissed the question without answering."}',
                message="User dismissed the question without answering.",
                display=[BriefDisplayBlock(text="User dismissed")],
            )
            wire_send(WireToolResult(tool_call_id=tc_id, return_value=rv))
            return ToolResult(
                ok=True,
                name="AskUserQuestion",
                output=rv.output,
            )

        formatted = json.dumps({"answers": answers}, ensure_ascii=False)
        rv = ToolReturnValue(
            is_error=False,
            output=formatted,
            message="User has answered.",
            display=[BriefDisplayBlock(text="User answered")],
        )
        wire_send(WireToolResult(tool_call_id=tc_id, return_value=rv))
        return ToolResult(
            ok=True,
            name="AskUserQuestion",
            output=formatted,
        )

    # CLI mode fallback
    metadata: dict[str, Any] = {
        "kind": "ask_user_question",
        "questions": questions,
    }

    return ToolResult(
        ok=True,
        name="AskUserQuestion",
        output=_build_question_summary(questions),
        metadata=metadata,
        await_user_response=True,
    )
