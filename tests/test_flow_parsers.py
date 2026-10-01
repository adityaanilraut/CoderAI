"""Shared flow inference must preserve both syntax adapters and validation."""

from __future__ import annotations

import pytest

from coderai.skill.flow import FlowValidationError
from coderai.skill.flow.d2 import parse_d2_flowchart
from coderai.skill.flow.mermaid import parse_mermaid_flowchart


DIAGRAMS = [
    (parse_d2_flowchart, "begin: BEGIN\nwork: Review\nend: END\nbegin -> work\nwork -> end"),
    (parse_mermaid_flowchart, "flowchart TD\nbegin[BEGIN] --> work[Review]\nwork --> end[END]"),
]


@pytest.mark.parametrize(("parse", "diagram"), DIAGRAMS)
def test_single_edge_task_is_not_a_decision(parse, diagram):
    flow = parse(diagram)
    assert (flow.begin_id, flow.end_id) == ("begin", "end")
    assert {key: node.kind for key, node in flow.nodes.items()} == {
        "begin": "begin",
        "work": "task",
        "end": "end",
    }
    assert [(e.src, e.dst, e.label) for e in flow.outgoing["work"]] == [("work", "end", None)]


@pytest.mark.parametrize(
    ("parse", "diagram"),
    [
        (
            parse_d2_flowchart,
            "begin: BEGIN\nwork: Review\nend: END\nbegin -> work\nwork -> work: retry\nwork -> end: done",
        ),
        (
            parse_mermaid_flowchart,
            "flowchart TD\nbegin[BEGIN] --> work[Review]\nwork -->|retry| work\nwork -->|done| end[END]",
        ),
    ],
)
def test_multiple_edges_infer_decision_and_preserve_order(parse, diagram):
    flow = parse(diagram)
    assert flow.nodes["work"].kind == "decision"
    assert flow.nodes["work"].label == "Review"
    assert [(e.dst, e.label) for e in flow.outgoing["work"]] == [("work", "retry"), ("end", "done")]


@pytest.mark.parametrize(("parse", "diagram"), DIAGRAMS)
@pytest.mark.parametrize("invalid", ["unlabeled", "duplicate"])
def test_branch_validation_keeps_errors(parse, diagram, invalid):
    extra = "\nwork -> work" if parse is parse_d2_flowchart else "\nwork --> work"
    if invalid == "duplicate":
        diagram = diagram.replace("work -> end", "work -> end: same").replace(
            "work --> end", "work -->|same| end"
        )
        extra += ": same" if parse is parse_d2_flowchart else ""
        if parse is parse_mermaid_flowchart:
            extra = "\nwork -->|same| work"
    with pytest.raises(
        FlowValidationError,
        match=f'Node "work" has {"an unlabeled edge" if invalid == "unlabeled" else "duplicate edge labels"}',
    ):
        parse(diagram + extra)
