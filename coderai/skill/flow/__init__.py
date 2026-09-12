"""Prompt-flow graphs.

A flow skill (``SKILL.md`` frontmatter ``type: flow`` + one fenced
``mermaid``/``d2`` block) compiles to a ``Flow``: BEGIN → task/decision
nodes → END. Decision nodes branch on the model's ``<choice>`` reply.
``FlowRunner`` (see :mod:`coderai.skill.flow.runner`) executes the graph
turn-by-turn in the current session.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

FlowNodeKind = Literal["begin", "end", "task", "decision"]

SKILL_COMMAND_PREFIX = "skill:"
FLOW_COMMAND_PREFIX = "flow:"


class FlowError(ValueError):
    """Base error for flow parsing/validation."""


class FlowParseError(FlowError):
    """Raised when prompt flow parsing fails."""


class FlowValidationError(FlowError):
    """Raised when a flowchart fails validation."""


@dataclass(frozen=True, slots=True)
class FlowNode:
    id: str
    label: str
    kind: FlowNodeKind


@dataclass(frozen=True, slots=True)
class FlowEdge:
    src: str
    dst: str
    label: str | None


@dataclass(slots=True)
class Flow:
    nodes: dict[str, FlowNode]
    outgoing: dict[str, list[FlowEdge]]
    begin_id: str
    end_id: str


_CHOICE_RE = re.compile(r"<choice>([^<]*)</choice>")


def parse_choice(text: str) -> str | None:
    matches = _CHOICE_RE.findall(text or "")
    if not matches:
        return None
    return matches[-1].strip()


def validate_flow(
    nodes: dict[str, FlowNode],
    outgoing: dict[str, list[FlowEdge]],
) -> tuple[str, str]:
    begin_ids = [node.id for node in nodes.values() if node.kind == "begin"]
    end_ids = [node.id for node in nodes.values() if node.kind == "end"]

    if len(begin_ids) != 1:
        raise FlowValidationError(f"Expected exactly one BEGIN node, found {len(begin_ids)}")
    if len(end_ids) != 1:
        raise FlowValidationError(f"Expected exactly one END node, found {len(end_ids)}")

    begin_id = begin_ids[0]
    end_id = end_ids[0]

    reachable: set[str] = set()
    queue: list[str] = [begin_id]
    while queue:
        node_id = queue.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        for edge in outgoing.get(node_id, []):
            if edge.dst not in reachable:
                queue.append(edge.dst)

    for node in nodes.values():
        if node.id not in reachable:
            continue
        edges = outgoing.get(node.id, [])
        if len(edges) <= 1:
            continue
        labels: list[str] = []
        for edge in edges:
            if edge.label is None or not edge.label.strip():
                raise FlowValidationError(f'Node "{node.id}" has an unlabeled edge')
            labels.append(edge.label)
        if len(set(labels)) != len(labels):
            raise FlowValidationError(f'Node "{node.id}" has duplicate edge labels')

    if end_id not in reachable:
        raise FlowValidationError("END node is not reachable from BEGIN")

    return begin_id, end_id


def parse_flow_from_skill_content(content: str) -> Flow:
    """Compile the first mermaid/d2 fenced block in skill markdown to a Flow."""
    from coderai.skill.flow.d2 import parse_d2_flowchart
    from coderai.skill.flow.mermaid import parse_mermaid_flowchart

    for lang, code in _iter_fenced_codeblocks(content):
        if lang == "mermaid":
            return _parse_flow_block(parse_mermaid_flowchart, code)
        if lang == "d2":
            return _parse_flow_block(parse_d2_flowchart, code)
    raise ValueError("Flow skills require a mermaid or d2 code block in SKILL.md.")


def _parse_flow_block(parser: Callable[[str], Flow], code: str) -> Flow:
    try:
        return parser(code)
    except FlowError as exc:
        raise ValueError(f"Invalid flow diagram: {exc}") from exc


def _iter_fenced_codeblocks(content: str) -> Iterator[tuple[str, str]]:
    fence = ""
    fence_char = ""
    lang = ""
    buf: list[str] = []
    in_block = False

    for line in content.splitlines():
        stripped = line.lstrip()
        if not in_block:
            match = _parse_fence_open(stripped)
            if match:
                fence, fence_char, info = match
                lang = _normalize_code_lang(info)
                in_block = True
                buf = []
            continue

        if _is_fence_close(stripped, fence_char, len(fence)):
            yield lang, "\n".join(buf).strip("\n")
            in_block = False
            fence = ""
            fence_char = ""
            lang = ""
            buf = []
            continue

        buf.append(line)


def _normalize_code_lang(info: str) -> str:
    if not info:
        return ""
    lang = info.split()[0].strip().lower()
    if lang.startswith("{") and lang.endswith("}"):
        lang = lang[1:-1].strip()
    return lang


def _parse_fence_open(line: str) -> tuple[str, str, str] | None:
    if not line or line[0] not in ("`", "~"):
        return None
    fence_char = line[0]
    count = 0
    for ch in line:
        if ch == fence_char:
            count += 1
        else:
            break
    if count < 3:
        return None
    fence = fence_char * count
    info = line[count:].strip()
    return fence, fence_char, info


def _is_fence_close(line: str, fence_char: str, fence_len: int) -> bool:
    if not fence_char or not line or line[0] != fence_char:
        return False
    count = 0
    for ch in line:
        if ch == fence_char:
            count += 1
        else:
            break
    if count < fence_len:
        return False
    return not line[count:].strip()
