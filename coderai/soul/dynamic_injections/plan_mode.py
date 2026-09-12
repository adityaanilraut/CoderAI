# Moved from coderai/soul/dynamic_injection.py - coderai structure
# (coderai/soul/dynamic_injections/plan_mode.py).
"""Plan-mode injection provider (CoderAI ``plan_mode`` parity).

Re-exported from :mod:`coderai.soul.dynamic_injection` so the old path
keeps working.
"""

from __future__ import annotations

from typing import Any

from coderai.soul.dynamic_injection import (
    DynamicInjection,
    DynamicInjectionProvider,
    SoulView,
)

_PLAN_SPARSE_KEY = "Plan mode still active"
_PLAN_FULL_KEY = "Plan mode is active."

# Inject a reminder every N assistant turns; every Nth reminder is full.
_TURN_INTERVAL = 5
_FULL_EVERY_N = 5


def plan_full_reminder(plan_file: str | None, plan_exists: bool = False) -> str:
    lines = [
        "Plan mode is active. You MUST NOT make any edits "
        "(with the exception of the plan file below), run non-readonly tools, "
        "or otherwise make changes to the system. "
        "This supersedes any other instructions you have received.",
    ]
    if plan_file:
        if plan_exists:
            lines += [
                "",
                f"Plan file: {plan_file} (exists — read first, then update it with edit/write)",
            ]
        else:
            lines += [
                "",
                f"Plan file: {plan_file} (create it with write; then modify with edit/write)",
            ]
        lines.append("This is the only file you are allowed to edit.")
    lines += [
        "",
        "Workflow:",
        "1. Understand — explore the codebase with glob, grep, read",
        "2. Design — converge on the best approach with trade-offs",
        "3. Review — re-read key files to verify understanding",
        "4. Write Plan — write the plan file with write/edit",
        "5. Exit — call exit_plan_mode for user approval",
        "",
        "Your turn must end with either AskUserQuestion (clarifications) "
        "or exit_plan_mode (plan approval). Never ask about plan approval via text.",
    ]
    return "\n".join(lines)


def plan_sparse_reminder(plan_file: str | None = None) -> str:
    parts = ["Plan mode still active (see full instructions earlier)."]
    parts.append(
        f"Read-only except plan file ({plan_file})." if plan_file else "Read-only."
    )
    parts.append(
        "Modify the plan file with write/edit. "
        "End turns with AskUserQuestion (clarifications) or exit_plan_mode (approval)."
    )
    return " ".join(parts)


def plan_reentry_reminder(plan_file: str | None = None) -> str:
    lines = [
        "Plan mode is active. You MUST NOT make any edits "
        "(with the exception of the plan file below), run non-readonly tools, "
        "or otherwise make changes to the system.",
        "",
        "## Re-entering Plan Mode",
        (
            f"A plan file exists at {plan_file} from a previous planning session."
            if plan_file
            else "A plan file from a previous planning session already exists."
        ),
        "Before proceeding:",
        "1. Read the existing plan file",
        "2. If different task: replace it. If same task: update it.",
        "3. Always edit the plan file before calling exit_plan_mode.",
    ]
    return "\n".join(lines)


class PlanModeInjectionProvider(DynamicInjectionProvider):
    """Periodic read-only reminders while plan mode is active (root only)."""

    def __init__(self) -> None:
        self._inject_count = 0

    @staticmethod
    def _has_plan_reminder(history: list[dict[str, Any]]) -> bool:
        for msg in history:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            text = content if isinstance(content, str) else ""
            if _PLAN_SPARSE_KEY in text or _PLAN_FULL_KEY in text:
                return True
        return False

    @staticmethod
    def _turns_since_reminder(history: list[dict[str, Any]]) -> tuple[int, bool]:
        turns = 0
        for msg in reversed(history):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user":
                content = msg.get("content")
                text = content if isinstance(content, str) else ""
                if _PLAN_SPARSE_KEY in text or _PLAN_FULL_KEY in text:
                    return turns, True
            elif msg.get("role") == "assistant":
                turns += 1
        return turns, False

    async def get_injections(
        self, history: list[dict[str, Any]], soul: SoulView
    ) -> list[DynamicInjection]:
        if soul.is_subagent or not soul.plan_mode:
            if not soul.plan_mode:
                self._inject_count = 0
            return []

        import pathlib as _pl

        plan_file = soul.get_plan_file_path()
        plan_exists = bool(plan_file and _pl.Path(plan_file).is_file())

        if soul.consume_pending_plan_activation_injection():
            self._inject_count = 1
            if plan_exists:
                return [
                    DynamicInjection(
                        type="plan_mode_reentry",
                        content=plan_reentry_reminder(plan_file),
                    )
                ]
            return [
                DynamicInjection(type="plan_mode", content=plan_full_reminder(plan_file))
            ]

        turns, found = self._turns_since_reminder(history)
        if not found:
            self._inject_count = 1
            return [
                DynamicInjection(
                    type="plan_mode",
                    content=plan_full_reminder(plan_file, plan_exists),
                )
            ]
        if turns < _TURN_INTERVAL:
            return []
        self._inject_count += 1
        is_full = self._inject_count % _FULL_EVERY_N == 1
        content = (
            plan_full_reminder(plan_file, plan_exists)
            if is_full
            else plan_sparse_reminder(plan_file)
        )
        return [DynamicInjection(type="plan_mode", content=content)]

    async def on_context_compacted(self) -> None:
        self._inject_count = 0


__all__ = [
    "plan_full_reminder",
    "plan_sparse_reminder",
    "plan_reentry_reminder",
    "PlanModeInjectionProvider",
]
