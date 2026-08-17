"""UpdatePlan tool — updates the task plan (deepcode update-plan-handler.ts)."""

from __future__ import annotations

from typing import Any

from coderai.core.common.validate import execute_validated_tool
from coderai.core.tools.types import ToolResult


def _validate_update_plan_schema(args: dict[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
    plan = args.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return False, {}, "plan must be a non-empty string."

    explanation = args.get("explanation")
    if explanation is not None and not isinstance(explanation, str):
        return False, {}, "explanation must be a string."

    validated = {"plan": plan}
    if isinstance(explanation, str) and explanation.strip():
        validated["explanation"] = explanation.strip()

    return True, validated, None


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_update_plan_tool(args, context)


def handle_update_plan_tool(args: dict[str, Any], context: Any) -> ToolResult:
    def run(validated_args: dict[str, Any], _ctx: Any) -> ToolResult:
        metadata: dict[str, Any] = {"plan": validated_args["plan"]}
        if "explanation" in validated_args:
            metadata["explanation"] = validated_args["explanation"]

        return ToolResult(
            ok=True,
            name="UpdatePlan",
            output="Plan updated.",
            metadata=metadata,
        )

    return execute_validated_tool(
        "UpdatePlan",
        args,
        context,
        run,
        validator=_validate_update_plan_schema,
    )
