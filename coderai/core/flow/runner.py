"""Flow execution (Kimi ``soul/kimisoul.py:FlowRunner`` parity).

``FlowRunner`` walks a :class:`~coderai.core.flow.Flow` turn-by-turn in the
current session: task nodes run one agent turn each, decision nodes branch on
the model's ``<choice>`` reply. :meth:`FlowRunner.ralph_loop` builds the
automated repeat-prompt loop (``CONTINUE``/``STOP``) used when
``max_ralph_iterations != 0``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from coderai.core.flow import (
    FLOW_COMMAND_PREFIX,
    Flow,
    FlowEdge,
    FlowNode,
    parse_choice,
)

DEFAULT_MAX_FLOW_MOVES = 1000  # Kimi DEFAULT_MAX_FLOW_MOVES parity

# Sessions currently inside a flow/ralph run. Flow node turns call
# ``_activate`` directly so the ralph branch in ``reply_session`` skips them
# (no recursive loops).
_FLOW_ACTIVE: set[str] = set()


class FlowBudgetExceeded(Exception):
    """Raised when a flow exhausts its move budget."""


@dataclass
class FlowTurnResult:
    stop_reason: str  # "natural" | "tool_rejected" | "error" | "interrupted"
    final_message: str = ""
    steps_used: int = 0


@dataclass
class FlowOutcome:
    status: str  # "completed" | "tool_rejected" | "error" | "budget_exceeded"
    moves: int = 0
    detail: str = ""


class FlowRunner:
    def __init__(
        self,
        flow: Flow,
        *,
        name: str | None = None,
        max_moves: int = DEFAULT_MAX_FLOW_MOVES,
    ) -> None:
        self._flow = flow
        self._name = name
        self._max_moves = max_moves

    @staticmethod
    def ralph_loop(prompt_text: str, max_ralph_iterations: int) -> FlowRunner:
        """Build the automated repeat-prompt loop (Kimi parity).

        ``max_ralph_iterations`` extra runs after the first; ``-1`` repeats
        until the model chooses STOP.
        """
        text = (prompt_text or "").strip()
        total_runs = max_ralph_iterations + 1
        if max_ralph_iterations < 0:
            total_runs = 1000000000000000  # effectively infinite

        nodes: dict[str, FlowNode] = {
            "BEGIN": FlowNode(id="BEGIN", label="BEGIN", kind="begin"),
            "END": FlowNode(id="END", label="END", kind="end"),
        }
        outgoing: dict[str, list[FlowEdge]] = {"BEGIN": [], "END": []}

        nodes["R1"] = FlowNode(id="R1", label=text, kind="task")
        nodes["R2"] = FlowNode(
            id="R2",
            label=(
                f"{text}. (You are running in an automated loop where the same "
                "prompt is fed repeatedly. Only choose STOP when the task is fully complete. "
                "Including it will stop further iterations. If you are not 100% sure, "
                "choose CONTINUE.)"
            ).strip(),
            kind="decision",
        )
        outgoing["R1"] = []
        outgoing["R2"] = []

        outgoing["BEGIN"].append(FlowEdge(src="BEGIN", dst="R1", label=None))
        outgoing["R1"].append(FlowEdge(src="R1", dst="R2", label=None))
        outgoing["R2"].append(FlowEdge(src="R2", dst="R2", label="CONTINUE"))
        outgoing["R2"].append(FlowEdge(src="R2", dst="END", label="STOP"))

        flow = Flow(nodes=nodes, outgoing=outgoing, begin_id="BEGIN", end_id="END")
        return FlowRunner(flow, max_moves=total_runs)

    async def run(self, mgr: Any, session_id: str, args: str = "") -> FlowOutcome:
        if (args or "").strip():
            command = f"/{FLOW_COMMAND_PREFIX}{self._name}" if self._name else "/flow"
            return FlowOutcome(
                status="error", detail=f"Agent flow {command} ignores args."
            )
        _FLOW_ACTIVE.add(session_id)
        try:
            current_id = self._flow.begin_id
            moves = 0
            while True:
                node = self._flow.nodes[current_id]
                edges = self._flow.outgoing.get(current_id, [])

                if node.kind == "end":
                    return FlowOutcome(status="completed", moves=moves)
                if node.kind == "begin":
                    if not edges:
                        return FlowOutcome(status="error", detail="BEGIN has no outgoing edges.")
                    current_id = edges[0].dst
                    continue
                if moves >= self._max_moves:
                    raise FlowBudgetExceeded(
                        f"Agent flow exceeded {self._max_moves} moves."
                    )
                next_id, turn = await self._execute_flow_node(mgr, session_id, node, edges)
                if turn.stop_reason == "tool_rejected":
                    return FlowOutcome(
                        status="tool_rejected",
                        moves=moves,
                        detail="Agent flow stopped after tool rejection.",
                    )
                if turn.stop_reason in ("error", "interrupted"):
                    return FlowOutcome(
                        status=turn.stop_reason, moves=moves, detail=turn.final_message
                    )
                if next_id is None:
                    return FlowOutcome(status="completed", moves=moves)
                moves += 1
                current_id = next_id
        except FlowBudgetExceeded as exc:
            return FlowOutcome(status="budget_exceeded", detail=str(exc))
        finally:
            _FLOW_ACTIVE.discard(session_id)

    async def _execute_flow_node(
        self, mgr: Any, session_id: str, node: FlowNode, edges: list[FlowEdge]
    ) -> tuple[str | None, FlowTurnResult]:
        if not edges:
            return None, FlowTurnResult(
                stop_reason="error", final_message=f'Flow node "{node.id}" has no outgoing edges.'
            )
        base_prompt = self._build_flow_prompt(node, edges)
        prompt = base_prompt
        while True:
            result = await self._flow_turn(mgr, session_id, prompt)
            if result.stop_reason == "tool_rejected":
                return None, result
            if result.stop_reason in ("error", "interrupted"):
                return None, result
            if node.kind != "decision":
                return edges[0].dst, result
            choice = parse_choice(result.final_message) if result.final_message else None
            next_id = self._match_flow_edge(edges, choice)
            if next_id is not None:
                return next_id, result
            options = ", ".join(edge.label or "" for edge in edges)
            prompt = (
                f"{base_prompt}\n\n"
                "Your last response did not include a valid choice "
                f"(got: {choice or '<missing>'}; available: {options}). "
                "Reply with one of the choices using <choice>...</choice>."
            )

    @staticmethod
    def _build_flow_prompt(node: FlowNode, edges: list[FlowEdge]) -> str:
        if node.kind != "decision":
            return node.label
        choices = [edge.label for edge in edges if edge.label]
        lines = [
            node.label,
            "",
            "Available branches:",
            *(f"- {choice}" for choice in choices),
            "",
            "Reply with a choice using <choice>...</choice>.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _match_flow_edge(edges: list[FlowEdge], choice: str | None) -> str | None:
        if not choice:
            return None
        for edge in edges:
            if edge.label == choice:
                return edge.dst
        return None

    @staticmethod
    async def _flow_turn(mgr: Any, session_id: str, prompt: str) -> FlowTurnResult:
        """Run one flow node as a session turn (append + activate + read back)."""
        try:
            from coderai.core.wire.emitter import get_emitter

            emitter = get_emitter()
            emitter.turn_begin(prompt)
        except Exception:
            emitter = None  # type: ignore[assignment]
        try:
            before = len(mgr.list_session_messages(session_id))
        except Exception:
            before = 0
        prompt_text = prompt
        try:
            # First-turn parity with create_session: prepend the dynamic
            # workspace runtime context to the session's first user message.
            has_user = any(
                getattr(m, "role", "") == "user"
                for m in mgr.list_session_messages(session_id)
            )
        except Exception:
            has_user = True
        if not has_user:
            try:
                from coderai.core.prompt import get_runtime_context

                runtime_context = get_runtime_context(
                    mgr.project_root, mgr.get_active_model()
                )
                if runtime_context:
                    prompt_text = f"{runtime_context}\n\n---\n\n{prompt}"
            except Exception:
                pass
        try:
            mgr.file_history.ensure_session(session_id)
            checkpoint = mgr.file_history.record_tracked_files_checkpoint(
                session_id, "Flow node checkpoint"
            )
            checkpoint_hash = checkpoint.checkpoint_hash
        except Exception:
            checkpoint_hash = ""
        try:
            mgr._append_message(
                mgr._build_message(
                    session_id,
                    "user",
                    prompt_text,
                    meta={"checkpointHash": checkpoint_hash, "isFlowTurn": True},
                )
            )
        except Exception as exc:
            return FlowTurnResult(stop_reason="error", final_message=str(exc))
        _FLOW_ACTIVE.add(session_id)
        try:
            await mgr._activate(session_id)
        except Exception as exc:
            return FlowTurnResult(stop_reason="error", final_message=str(exc))
        try:
            if emitter is not None:
                emitter.flush()
                emitter.turn_end()
        except Exception:
            pass
        try:
            entry = mgr.get_session(session_id)
            status = (entry.status if entry else "") or ""
        except Exception:
            status = ""
        if status in ("ask_permission", "waiting_for_user"):
            return FlowTurnResult(stop_reason="tool_rejected")
        if status in ("interrupted",):
            return FlowTurnResult(stop_reason="interrupted", final_message=status)
        if status not in ("completed",):
            reason = ""
            try:
                reason = str((mgr.get_session(session_id).fail_reason if entry else "") or status)
            except Exception:
                reason = status
            return FlowTurnResult(stop_reason="error", final_message=reason)
        final = ""
        steps_used = 0
        try:
            messages = mgr.list_session_messages(session_id)[before:]
            for message in reversed(messages):
                if getattr(message, "role", "") == "assistant" and (
                    getattr(message, "content", "") or ""
                ).strip():
                    final = str(message.content).strip()
                    break
            steps_used = sum(
                1 for m in messages if getattr(m, "role", "") == "assistant"
            )
        except Exception:
            pass
        return FlowTurnResult(stop_reason="natural", final_message=final, steps_used=steps_used)


def is_flow_active(session_id: str) -> bool:
    """True while a flow/ralph run owns the session (reentry guard)."""
    return session_id in _FLOW_ACTIVE


def resolve_max_ralph_iterations(settings: dict[str, Any] | None = None) -> int:
    """Resolve the Kimi ``max_ralph_iterations`` knob (0 = off, -1 = unlimited).

    Precedence: ``CODERAI_MAX_RALPH_ITERATIONS`` env (what the CLI flag sets)
    → resolved ``maxRalphIterations`` → typed ``typedLoopControl`` overlay.
    """
    raw = os.environ.get("CODERAI_MAX_RALPH_ITERATIONS")
    if raw is not None:
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            pass
    settings = settings or {}
    for key in ("maxRalphIterations", "max_ralph_iterations"):
        if settings.get(key) is not None:
            try:
                return int(settings[key])
            except (ValueError, TypeError):
                pass
    try:
        overlay = settings.get("typedLoopControl") or {}
        if overlay.get("maxRalphIterations") is not None:
            return int(overlay["maxRalphIterations"])
    except (ValueError, TypeError, AttributeError):
        pass
    return 0


async def maybe_run_ralph(mgr: Any, session_id: str, prompt_text: str) -> bool:
    """Run the automated ralph loop instead of a normal turn when configured.

    Returns True when a loop ran (caller must skip the normal turn).
    Mirrors Kimi ``soul.run``: only for fresh non-slash prompts, never nested.
    """
    if is_flow_active(session_id):
        return False
    try:
        max_ralph = ralph_iterations_for_prompt(mgr.get_resolved_settings(), prompt_text)
    except Exception:
        max_ralph = 0
    if max_ralph == 0:
        return False
    text = prompt_text.strip()
    runner = FlowRunner.ralph_loop(text, max_ralph)
    await runner.run(mgr, session_id)
    return True


def ralph_iterations_for_prompt(settings: dict[str, Any] | None, prompt_text: Any) -> int:
    """Sync check: ralph iterations for this prompt, or 0 for a normal turn."""
    text = prompt_text if isinstance(prompt_text, str) else str(prompt_text or "")
    if not text.strip() or text.strip().startswith("/"):
        return 0
    try:
        return resolve_max_ralph_iterations(settings)
    except Exception:
        return 0


async def run_flow_skill(mgr: Any, session_id: str, name: str) -> FlowOutcome:
    """Run a ``type: flow`` skill by name in the current session."""
    from coderai.core.flow import parse_flow_from_skill_content
    from coderai.core.skill import load_skill

    skill = load_skill(name, getattr(mgr, "project_root", None))
    if not skill:
        return FlowOutcome(status="error", detail=f"Unknown skill: {name}.")
    meta_type = ""
    try:
        from coderai.core.skill.loader import extract_skill_frontmatter

        meta_type = str(
            (extract_skill_frontmatter(skill.get("content", "")) or {}).get("type", "")
        ).strip().lower()
    except Exception:
        meta_type = ""
    if meta_type != "flow":
        return FlowOutcome(status="error", detail=f"Skill '{name}' is not a flow skill.")
    try:
        flow = parse_flow_from_skill_content(skill.get("content", ""))
    except ValueError as exc:
        return FlowOutcome(status="error", detail=str(exc))
    runner = FlowRunner(flow, name=skill.get("name") or name)
    return await runner.run(mgr, session_id)
