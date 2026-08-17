"""AskUserQuestion tool — pauses for user clarification (deepcode ask-user-question-handler.ts)."""

from __future__ import annotations

from typing import Any

from coderai.core.tools.types import ToolResult


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


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_ask_user_question_tool(args, context)


def handle_ask_user_question_tool(args: dict[str, Any], context: Any) -> ToolResult:
    ok, questions, err = _parse_questions(args.get("questions"))
    if not ok:
        return ToolResult(
            ok=False,
            name="AskUserQuestion",
            error=err or "Invalid questions payload.",
        )

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
