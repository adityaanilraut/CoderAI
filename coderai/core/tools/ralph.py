"""Ralph Automated Verification Engine — multi-round adversarial verification.

Mirrors packages/workflow/tool-ralph from deepseek-harness.
Executes multi-round verification by presenting an immutable objective to a sequence
of fresh child agents with a structured handoff protocol.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.subagent import SubAgentManager, SubAgentResult, SubAgentSpec
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROUNDS = 5
DEFAULT_TIMEOUT_PER_ROUND = 90.0


@dataclass
class RalphHandoff:
    """Structured handoff payload produced at the end of a verification round."""

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
    status: str  # "complete" | "blocked" | "max_rounds_reached" | "failed"
    total_rounds: int
    rounds: list[RalphRound] = field(default_factory=list)
    total_tokens: int = 0
    duration_seconds: float = 0.0
    final_verdict: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "status": self.status,
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
            "max_rounds_reached": "⚠️ MAX ROUNDS REACHED",
            "failed": "❌ FAILED",
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
            return RalphHandoff(
                status=status,
                summary=str(data.get("summary", "")).strip(),
                evidence=str(data.get("evidence", "")).strip(),
                next_steps=str(data.get("next_steps", "")).strip(),
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
                return RalphHandoff(
                    status=status,
                    summary=str(data.get("summary", "")).strip(),
                    evidence=str(data.get("evidence", "")).strip(),
                    next_steps=str(data.get("next_steps", "")).strip(),
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


async def handle_ralph_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute multi-round adversarial verification on an immutable objective."""
    objective = as_str(args.get("objective") or args.get("prompt", "")).strip()
    if not objective:
        return ToolResult(
            ok=False,
            name="ralph",
            error="Missing required argument 'objective' (or 'prompt').",
        )

    try:
        raw_rounds = args.get("max_rounds") or args.get("max_iterations")
        max_rounds = int(raw_rounds) if raw_rounds is not None else DEFAULT_MAX_ROUNDS
        if max_rounds <= 0:
            max_rounds = DEFAULT_MAX_ROUNDS
    except (ValueError, TypeError):
        max_rounds = DEFAULT_MAX_ROUNDS

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
    final_status = "max_rounds_reached"
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
            '  "next_steps": "Specific guidance for the next round if continuing",\n'
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

        handoff = _parse_handoff(round_result.summary)

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
            final_verdict = handoff.summary + (
                f"\n\n**Evidence**:\n{handoff.evidence}" if handoff.evidence else ""
            )
            break
        elif handoff.status == "blocked":
            final_status = "blocked"
            final_verdict = (
                f"Verification blocked in round {round_num}: {handoff.blocker or handoff.summary}"
            )
            break
        elif round_result.status in ("failed", "timeout", "interrupted"):
            if round_num == max_rounds:
                final_status = "failed"
                final_verdict = (
                    f"Round {round_num} failed: {round_result.error or round_result.summary}"
                )
                break

    if final_status == "max_rounds_reached":
        last_round = rounds[-1] if rounds else None
        last_summary = (
            last_round.handoff.summary if last_round else "Maximum verification rounds reached."
        )
        final_verdict = (
            f"Reached maximum round limit ({max_rounds}). Latest findings: {last_summary}"
        )

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

    run_id = f"ralph_{uuid.uuid4().hex[:8]}"
    meta_dict = {
        "runId": run_id,
        "agentsStarted": len(rounds),
        "result": ralph_res.to_dict(),
        **ralph_res.to_dict(),
    }

    is_ok = final_status == "complete"
    return ToolResult(
        ok=is_ok,
        name="ralph",
        output=ralph_res.format_markdown(),
        metadata=meta_dict,
        error=None if is_ok else f"Ralph verification status: {final_status}",
    )
