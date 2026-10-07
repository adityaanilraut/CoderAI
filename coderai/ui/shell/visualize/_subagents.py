"""Bounded, collapsible terminal activity tree for delegated agents."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.tree import Tree

from coderai.wire.types import (
    StepBegin,
    SubagentEvent,
    TextPart,
    ThinkPart,
    ToolCallPart,
    ToolResultPart,
)


@dataclass
class _Activity:
    label: str
    parent_id: str | None = None
    status: str = "running"
    step: int = 0
    tool: str = ""
    text: str = ""
    output: str = ""


class SubagentActivityBlock:
    """Store bounded child output; collapsed rows show activity and status."""

    def __init__(self, max_agents: int = 64, output_limit: int = 12000) -> None:
        self.activities: OrderedDict[str, _Activity] = OrderedDict()
        self.max_agents = max_agents
        self.output_limit = output_limit

    @property
    def has_running(self) -> bool:
        return any(item.status == "running" for item in self.activities.values())

    @property
    def has_expandable_content(self) -> bool:
        return any(item.text or item.output for item in self.activities.values())

    def update(self, name: str, payload: dict[str, Any], handle: Any = None) -> None:
        agent_id = str(getattr(handle, "id", None) or payload.get("id") or "")
        if not agent_id:
            return
        item = self.activities.get(agent_id)
        if item is None:
            item = _Activity(
                label=str(
                    getattr(handle, "description", None) or payload.get("subagentType") or agent_id
                ),
                parent_id=getattr(handle, "parent_agent_id", None),
            )
            self.activities[agent_id] = item
            while len(self.activities) > self.max_agents:
                self.activities.popitem(last=False)
        if name == "subagent/start":
            item.status = "running"
        elif name == "subagent/end":
            reason = str(payload.get("stopReason") or "completed")
            item.status = "completed" if reason == "completed" else reason
        else:
            event = payload.get("event")
            if isinstance(event, SubagentEvent) and isinstance(event.event, dict):
                if event.event.get("type") == "subagent.settled":
                    item.status = str(event.event.get("status") or "completed")
                    item.text = str(event.event.get("summary") or item.text)[-self.output_limit :]
            if isinstance(event, StepBegin):
                item.status = "running"
                item.step = event.n
            elif isinstance(event, TextPart):
                item.text = (item.text + event.text)[-self.output_limit :]
            elif isinstance(event, ThinkPart):
                item.tool = "thinking"
            elif isinstance(event, ToolCallPart):
                item.tool = event.name
            elif isinstance(event, ToolResultPart):
                item.output = (item.output + "\n" + event.output)[-self.output_limit :]

    def render(self, expanded: bool = False) -> Panel:
        tree = Tree(Text("Subagents", style="bold magenta"))
        nodes: dict[str, Any] = {}
        for agent_id, item in self.activities.items():
            detail = f" · step {item.step}" if item.step else ""
            if item.tool:
                detail += f" · {item.tool}"
            label = Text(f"{item.label} [{item.status}]{detail}")
            label.stylize("cyan" if item.status == "running" else "dim")
            row: Any = Spinner("dots", text=label) if item.status == "running" else label
            parent = nodes.get(item.parent_id, tree) if item.parent_id else tree
            node = parent.add(row)
            nodes[agent_id] = node
            if expanded:
                if item.text:
                    node.add(Text(item.text))
                if item.output:
                    node.add(Text(item.output, style="dim"))
        parts: list[Any] = [tree]
        if self.has_expandable_content and not expanded:
            parts.append(Text("Ctrl-E: expand child output", style="dim"))
        return Panel(Group(*parts), border_style="dim magenta", padding=(0, 1))

    def __rich_console__(self, console: Any, options: Any):
        yield self.render()
