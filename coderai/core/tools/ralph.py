"""Ralph Automated Verification Engine — multi-round adversarial verification.

Mirrors packages/workflow/tool-ralph from deepseek-harness: a foreground
fresh-agent loop toward one immutable objective. Each round opens a fresh
child with no conversation seed; only a bounded structured handoff crosses
rounds. Run statuses: ``complete | blocked | budget-limited | round-failed``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.orchestration import resolve_ralph_max_rounds
from coderai.core.subagent import SubAgentManager, SubAgentResult, SubAgentSpec
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROUNDS = 256  # deployment default + ceiling (harness parity)
DEFAULT_TIMEOUT_PER_ROUND = 90.0
MAX_HANDOFF_CHARS = 16_384
MAX_RESULT_CHARS = 16_384
TRUNCATION_NOTICE = "\n… [truncated]"

ROUND_STATUSES = ("continue", "complete", "blocked")
RUN_STATUSES = ("complete", "blocked", "budget-limited", "round-failed")
# Legacy statuses retained for backward-compatible metadata consumers.
LEGACY_RUN_ALIASES = {"max_rounds_reached": "budget-limited"}


@dataclass
class RalphHandoff:
    """Structured handoff payload produced at the end of a verification round.

    ``evidence``/``next_steps`` are kept as strings for backward compatibility
    with the pinned parser; the harness report uses arrays and this tool
    normalizes to arrays for validation.
    """

    status: str  # "continue" | "complete" | "blocked"
    summary: str
    evidence: str = ""
    next_steps: str = ""
    blocker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence,
            "nextSteps": self.next_steps,
            "next_steps": self.next_steps,
            "blocker": self.blocker,
        }


@dataclass
class RalphRound:
    """Record of a single verification round."""

    round_number: int
    task_id: str
    handoff: RalphHandoff
    raw_summary: str
    tokens: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_number": self.round_number,
            "task_id": self.task_id,
            "handoff": self.handoff.to_dict(),
            "tokens": self.tokens,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


@dataclass
class RalphResult:
    """Aggregated result of the Ralph verification run."""

    objective: str
    status: str  # "complete" | "blocked" | "budget-limited" | "round-failed"
    total_rounds: int
    rounds: list[RalphRound] = field(default_factory=list)
    total_tokens: int = 0
    duration_seconds: float = 0.0
    final_verdict: str = ""

    @property
    def rounds_started(self) -> int:
        return self.total_rounds

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "status": self.status,
            "roundsStarted": self.total_rounds,
            "total_rounds": self.total_rounds,
            "rounds": [r.to_dict() for r in self.rounds],
            "total_tokens": self.total_tokens,
            "duration_seconds": self.duration_seconds,
            "final_verdict": self.final_verdict,
        }

    def format_markdown(self) -> str:
        status_badge = {
            "complete": "✅ VERIFIED COMPLETE",
            "blocked": "🚫 BLOCKED",
            "budget-limited": "⚠️ MAX ROUNDS REACHED",
            "round-failed": "❌ FAILED",
        }.get(self.status, f"⚠️ {self.status.upper()}")

        lines = [
            f"### Ralph Verification Report — {status_badge}",
            f"**Objective**: {self.objective}",
            f"**Rounds**: `{self.total_rounds}` | **Duration**: `{self.duration_seconds:.2f}s` | **Tokens**: `{self.total_tokens}`",
            f"\n**Final Verdict**:\n{self.final_verdict or 'No verdict provided.'}\n",
        ]

        if self.rounds:
            lines.append("#### Round Audit Trail")
            for r in self.rounds:
                h = r.handoff
                badge = (
                    "✅ Complete"
                    if h.status == "complete"
                    else ("🚫 Blocked" if h.status == "blocked" else "🔄 Continue")
                )
                lines.append(
                    f"\n<details><summary><b>Round {r.round_number}</b> [{badge}] ({r.duration_seconds:.1f}s, {r.tokens} tokens)</summary>\n"
                )
                lines.append(f"- **Summary**: {h.summary}")
                if h.evidence:
                    lines.append(f"- **Evidence**: {h.evidence}")
                if h.next_steps:
                    lines.append(f"- **Next Steps**: {h.next_steps}")
                if h.blocker:
                    lines.append(f"- **Blocker**: {h.blocker}")
                if r.error:
                    lines.append(f"- **Error**: {r.error}")
                lines.append("\n</details>")

        return "\n".join(lines)


def _bound_result(text: str, max_chars: int = MAX_RESULT_CHARS) -> str:
    """Bound terminal text, including the truncation marker (harness rule)."""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_NOTICE):
        return TRUNCATION_NOTICE[:max_chars]
    return f"{text[: max_chars - len(TRUNCATION_NOTICE)]}{TRUNCATION_NOTICE}"


def _parse_handoff(raw_text: str) -> RalphHandoff:
    """Parse structured Ralph handoff from JSON or markdown fallback."""
    raw_text = raw_text.strip()

    # 1. Try parsing JSON from code blocks or raw string
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
    candidate_json = m.group(1).strip() if m else raw_text

    try:
        data = json.loads(candidate_json)
        if isinstance(data, dict):
            status = str(data.get("status", "continue")).strip().lower()
            if status not in ("continue", "complete", "blocked"):
                status = "continue"
            evidence = data.get("evidence") or data.get("evidence_text") or ""
            next_steps = data.get("nextSteps") or data.get("next_steps") or ""
            if isinstance(evidence, list):
                evidence = "\n".join(str(e) for e in evidence)
            if isinstance(next_steps, list):
                next_steps = "\n".join(str(n) for n in next_steps)
            return RalphHandoff(
                status=status,
                summary=str(data.get("summary", "")).strip(),
                evidence=str(evidence).strip(),
                next_steps=str(next_steps).strip(),
                blocker=str(data.get("blocker", "")).strip(),
            )
    except Exception:
        pass

    # 2. Try regex extraction of JSON object
    first_brace = raw_text.find("{")
    last_brace = raw_text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            data = json.loads(raw_text[first_brace : last_brace + 1])
            if isinstance(data, dict):
                status = str(data.get("status", "continue")).strip().lower()
                if status not in ("continue", "complete", "blocked"):
                    status = "continue"
                evidence = data.get("evidence") or ""
                next_steps = data.get("nextSteps") or data.get("next_steps") or ""
                if isinstance(evidence, list):
                    evidence = "\n".join(str(e) for e in evidence)
                if isinstance(next_steps, list):
                    next_steps = "\n".join(str(n) for n in next_steps)
                return RalphHandoff(
                    status=status,
                    summary=str(data.get("summary", "")).strip(),
                    evidence=str(evidence).strip(),
                    next_steps=str(next_steps).strip(),
                    blocker=str(data.get("blocker", "")).strip(),
                )
        except Exception:
            pass

    # 3. Fallback: Parse section headers or keyword heuristics
    status = "continue"
    summary = raw_text
    evidence = ""
    next_steps = ""
    blocker = ""

    # Status detection
    lower = raw_text.lower()
    if (
        "status: complete" in lower
        or "status: completed" in lower
        or '"status": "complete"' in lower
    ):
        status = "complete"
    elif "status: blocked" in lower or '"status": "blocked"' in lower:
        status = "blocked"
    elif "status: continue" in lower or '"status": "continue"' in lower:
        status = "continue"
    elif "all tests pass" in lower and "verified" in lower:
        status = "complete"

    # Extract sections if available
    summary_match = re.search(
        r"(?:##\s*Summary|Summary:)\s*([\s\S]*?)(?=(?:##|$))", raw_text, re.IGNORECASE
    )
    if summary_match:
        summary = summary_match.group(1).strip()

    evidence_match = re.search(
        r"(?:##\s*Evidence|Evidence:)\s*([\s\S]*?)(?=(?:##|$))", raw_text, re.IGNORECASE
    )
    if evidence_match:
        evidence = evidence_match.group(1).strip()

    next_match = re.search(
        r"(?:##\s*Next Steps|Next Steps:)\s*([\s\S]*?)(?=(?:##|$))", raw_text, re.IGNORECASE
    )
    if next_match:
        next_steps = next_match.group(1).strip()

    blocker_match = re.search(
        r"(?:##\s*Blocker|Blocker:)\s*([\s\S]*?)(?=(?:##|$))", raw_text, re.IGNORECASE
    )
    if blocker_match:
        blocker = blocker_match.group(1).strip()

    return RalphHandoff(
        status=status,
        summary=summary or raw_text[:200],
        evidence=evidence,
        next_steps=next_steps,
        blocker=blocker,
    )


def _validate_report(handoff: RalphHandoff) -> str | None:
    """Harness per-status report validation; returns a failure message or None.

    Rules: a continuing report needs next steps and an empty blocker; a
    complete report needs evidence, no next steps, and an empty blocker; a
    blocked report needs a concrete blocker. The serialized handoff must stay
    under ``MAX_HANDOFF_CHARS``.
    """
    if not handoff.summary or handoff.summary != handoff.summary.strip():
        return "Ralph round report summary must be non-empty and normalized"
    if handoff.status == "continue":
        if not handoff.next_steps or handoff.blocker:
            return "a continuing Ralph report needs nextSteps and an empty blocker"
    elif handoff.status == "complete":
        if not handoff.evidence or handoff.next_steps or handoff.blocker:
            return "a complete Ralph report needs evidence, no nextSteps, and an empty blocker"
    elif handoff.status == "blocked":
        if not handoff.blocker:
            return "a blocked Ralph report needs a concrete blocker"
    else:
        return "Ralph round report status is invalid"
    serialized = json.dumps(handoff.to_dict())
    if len(serialized) > MAX_HANDOFF_CHARS:
        return (
            f"Ralph round report exceeds maxHandoffChars ({len(serialized)} > {MAX_HANDOFF_CHARS})"
        )
    return None


def _render_round_failure(rounds_started: int, last_handoff: RalphHandoff | None) -> str:
    header = f"Ralph round {rounds_started} child failed before producing a structured report."
    text = (
        f"{header}\nNo previous handoff was available."
        if last_handoff is None
        else f"{header}\nLast successful handoff:\n{json.dumps(last_handoff.to_dict(), indent=2)}"
    )
    return _bound_result(text)


def _render_run_result(result: RalphResult) -> str:
    rounds = f"{result.rounds_started} round{'' if result.rounds_started == 1 else 's'}"
    last = result.rounds[-1].handoff if result.rounds else None
    report_json = json.dumps(last.to_dict(), indent=2) if last else "null"
    if result.status == "complete":
        text = f"Ralph worker reported completion after {rounds}.\nFinal report:\n{report_json}"
    elif result.status == "blocked":
        text = f"Ralph worker reported a blocker after {rounds}.\nFinal report:\n{report_json}"
    else:
        text = f"Ralph reached its {rounds} limit; the worker reported work remaining.\nFinal report:\n{report_json}"
    return _bound_result(text)


async def handle_ralph_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute multi-round adversarial verification on an immutable objective."""
    objective = as_str(args.get("objective") or args.get("prompt", "")).strip()
    if not objective:
        return ToolResult(
            ok=False,
            name="ralph",
            error="Missing required argument 'objective' (or 'prompt').",
        )

    ceiling = resolve_ralph_max_rounds()
    try:
        raw_rounds = args.get("max_rounds") or args.get("maxRounds") or args.get("max_iterations")
        max_rounds = int(raw_rounds) if raw_rounds is not None else ceiling
        if max_rounds <= 0:
            max_rounds = ceiling
    except (ValueError, TypeError):
        max_rounds = ceiling
    if max_rounds > ceiling:
        return ToolResult(
            ok=False,
            name="ralph",
            error=f"Ralph maxRounds {max_rounds} exceeds the deployment ceiling {ceiling}",
        )

    try:
        timeout_per_round = float(args.get("timeout_per_round", DEFAULT_TIMEOUT_PER_ROUND))
    except (ValueError, TypeError):
        timeout_per_round = DEFAULT_TIMEOUT_PER_ROUND

    mode = str(args.get("mode") or "general").strip().lower()
    if mode not in ("read_only", "general"):
        mode = "general"

    initial_context = as_str(args.get("context", "")).strip()

    if not context.create_openai_client:
        return ToolResult(
            ok=False,
            name="ralph",
            error="OpenAI client factory not available for Ralph verification.",
        )

    manager = SubAgentManager(
        project_root=context.project_root,
        create_openai_client=context.create_openai_client,
    )

    start_time = time.time()
    rounds: list[RalphRound] = []
    total_tokens = 0
    final_status = "budget-limited"
    final_verdict = ""

    handoff_history: list[dict[str, Any]] = []

    for round_num in range(1, max_rounds + 1):
        round_start = time.time()

        # Build prompt with immutable objective + accumulated handoff history
        prompt_sections = [
            f"You are executing Round {round_num}/{max_rounds} of Ralph Automated Verification.",
            f"\n### IMMUTABLE OBJECTIVE:\n{objective}\n",
        ]

        if initial_context and round_num == 1:
            prompt_sections.append(f"### Initial Context:\n{initial_context}\n")

        if handoff_history:
            prompt_sections.append("### Cumulative Prior Round Findings:")
            for prev in handoff_history:
                prompt_sections.append(
                    f"- **Round {prev['round']}** [{prev['status'].upper()}]: {prev['summary']}"
                )
                if prev.get("evidence"):
                    prompt_sections.append(f"  * Evidence: {prev['evidence']}")
                if prev.get("next_steps"):
                    prompt_sections.append(f"  * Recommended Next Steps: {prev['next_steps']}")
                if prev.get("blocker"):
                    prompt_sections.append(f"  * Blocker: {prev['blocker']}")
            prompt_sections.append("")

        prompt_sections.append(
            "### Verification Protocol & Requirements:\n"
            "1. Read the code, run relevant tests or shell commands, inspect git status/diff, and check all assumptions.\n"
            "2. Verify whether the immutable objective is completely satisfied with zero regressions.\n"
            "3. Conclude by providing your structured verification handoff in valid JSON format:\n"
            "```json\n"
            "{\n"
            '  "status": "continue" | "complete" | "blocked",\n'
            '  "summary": "Clear summary of findings in this round",\n'
            '  "evidence": "Concrete verification evidence (test outputs, commands run, inspected files)",\n'
            '  "nextSteps": "Specific guidance for the next round if continuing",\n'
            '  "blocker": "Details of blocker if blocked"\n'
            "}\n"
            "```"
        )

        full_prompt = "\n".join(prompt_sections)
        task_id = f"ralph_r{round_num}_{uuid.uuid4().hex[:4]}"

        spec = SubAgentSpec(
            description=f"Ralph Round {round_num}: {objective[:40]}",
            prompt=full_prompt,
            task_id=task_id,
            mode=mode,
            timeout_seconds=timeout_per_round,
            depth=1,
            parent_session_id=context.session_id,
        )

        round_result: SubAgentResult = await manager.spawn_subagent(spec)
        round_duration = max(0.0, time.time() - round_start)
        total_tokens += round_result.total_tokens

        # A child that failed before producing a structured report ends the
        # loop as round-failed, carrying the most recent durable handoff.
        if round_result.status not in ("completed",):
            final_status = "round-failed"
            last_handoff = rounds[-1].handoff if rounds else None
            final_verdict = _render_round_failure(round_num, last_handoff)
            rounds.append(
                RalphRound(
                    round_number=round_num,
                    task_id=task_id,
                    handoff=RalphHandoff(
                        status="blocked",
                        summary=round_result.summary,
                        blocker=str(round_result.error or ""),
                    ),
                    raw_summary=round_result.summary,
                    tokens=round_result.total_tokens,
                    duration_seconds=round_duration,
                    error=round_result.error,
                )
            )
            break

        handoff = _parse_handoff(round_result.summary)
        validation_error = _validate_report(handoff)
        if validation_error:
            final_status = "round-failed"
            last_handoff = rounds[-1].handoff if rounds else None
            final_verdict = _bound_result(
                f"Ralph round {round_num} returned an invalid structured report: {validation_error}."
                + (
                    f"\nLast successful handoff:\n{json.dumps(last_handoff.to_dict(), indent=2)}"
                    if last_handoff
                    else "\nNo previous handoff was available."
                )
            )
            rounds.append(
                RalphRound(
                    round_number=round_num,
                    task_id=task_id,
                    handoff=handoff,
                    raw_summary=round_result.summary,
                    tokens=round_result.total_tokens,
                    duration_seconds=round_duration,
                    error=validation_error,
                )
            )
            break

        round_record = RalphRound(
            round_number=round_num,
            task_id=task_id,
            handoff=handoff,
            raw_summary=round_result.summary,
            tokens=round_result.total_tokens,
            duration_seconds=round_duration,
            error=round_result.error,
        )
        rounds.append(round_record)

        handoff_history.append(
            {
                "round": round_num,
                "status": handoff.status,
                "summary": handoff.summary,
                "evidence": handoff.evidence,
                "next_steps": handoff.next_steps,
                "blocker": handoff.blocker,
            }
        )

        if handoff.status == "complete":
            final_status = "complete"
            break
        elif handoff.status == "blocked":
            final_status = "blocked"
            break

    total_duration = max(0.0, time.time() - start_time)
    ralph_res = RalphResult(
        objective=objective,
        status=final_status,
        total_rounds=len(rounds),
        rounds=rounds,
        total_tokens=total_tokens,
        duration_seconds=total_duration,
        final_verdict=final_verdict,
    )

    # Final verdicts for terminal statuses not set inside the loop.
    if final_status == "budget-limited":
        last_round = rounds[-1] if rounds else None
        last_summary = (
            last_round.handoff.summary if last_round else "Maximum verification rounds reached."
        )
        ralph_res.final_verdict = (
            f"Reached maximum round limit ({max_rounds}). Latest findings: {last_summary}"
        )
    elif final_status == "complete" and not ralph_res.final_verdict and rounds:
        h = rounds[-1].handoff
        ralph_res.final_verdict = h.summary + (
            f"\n\n**Evidence**:\n{h.evidence}" if h.evidence else ""
        )
    elif final_status == "blocked" and not ralph_res.final_verdict and rounds:
        h = rounds[-1].handoff
        ralph_res.final_verdict = (
            f"Verification blocked in round {rounds[-1].round_number}: {h.blocker or h.summary}"
        )

    run_id = f"ralph_{uuid.uuid4().hex[:8]}"
    meta_dict = {
        "runId": run_id,
        "agentsStarted": len(rounds),
        "result": ralph_res.to_dict(),
        **ralph_res.to_dict(),
    }

    is_ok = final_status in ("complete", "budget-limited")
    output = (
        ralph_res.format_markdown() if final_status != "round-failed" else ralph_res.final_verdict
    )
    return ToolResult(
        ok=is_ok,
        name="ralph",
        output=output,
        metadata=meta_dict,
        error=None if is_ok else f"Ralph verification status: {final_status}",
    )
