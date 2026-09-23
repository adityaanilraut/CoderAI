"""Dynamic per-step prompt injections.

Providers are consulted before each LLM step and may contribute
``<system-reminder>`` payloads. Each provider owns its throttling. The loop
passes plain message dicts (``role``/``content``) so this module stays
independent of any chat-provider SDK.

The afk + plan-mode providers live in :mod:`coderai.soul.dynamic_injections`
and are re-exported here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from kosong.message import Message


if TYPE_CHECKING:
    from coderai.soul.dynamic_injections.afk_mode import (
        AFK_DISABLED_REMINDER,
        AFK_INJECTION_TYPE,
        AFK_PROMPT_ROOT,
        AfkModeInjectionProvider,
    )
    from coderai.soul.dynamic_injections.plan_mode import (
        PlanModeInjectionProvider,
        plan_full_reminder,
        plan_reentry_reminder,
        plan_sparse_reminder,
    )


@dataclass(frozen=True, slots=True)
class DynamicInjection:
    """A dynamic prompt content to be injected before an LLM step."""

    type: str
    """Identifier, e.g. ``plan_mode``."""
    content: str
    """Text content (the loop wraps it in ``<system-reminder>`` tags)."""


class SoulView(Protocol):
    """Minimal soul surface injection providers may read."""

    @property
    def is_afk(self) -> bool: ...
    @property
    def is_subagent(self) -> bool: ...
    @property
    def plan_mode(self) -> bool: ...
    def get_plan_file_path(self) -> str | None: ...
    def consume_pending_plan_activation_injection(self) -> bool: ...


class DynamicInjectionProvider(ABC):
    """Base class for dynamic injection providers (own throttling)."""

    @abstractmethod
    async def get_injections(
        self, history: list[dict[str, Any]], soul: SoulView
    ) -> list[DynamicInjection]: ...

    async def on_context_compacted(self) -> None:
        """Reset throttling after compaction rewrites history."""
        return None

    async def on_afk_changed(self, enabled: bool) -> None:
        """Re-arm mode-specific reminders after a runtime toggle."""
        _ = enabled
        return None


# -- re-exports -------------------------------------------------------------
# Provider modules import the base classes above, so they are imported lazily
# here (PEP 562) to avoid a module-level import cycle.

_AFK_NAMES = frozenset(
    {
        "AFK_INJECTION_TYPE",
        "AFK_PROMPT_ROOT",
        "AFK_DISABLED_REMINDER",
        "AfkModeInjectionProvider",
    }
)

_PLAN_NAMES = frozenset(
    {
        "plan_full_reminder",
        "plan_sparse_reminder",
        "plan_reentry_reminder",
        "PlanModeInjectionProvider",
    }
)


def __getattr__(name: str) -> Any:
    if name in _AFK_NAMES:
        from coderai.soul.dynamic_injections import afk_mode

        return getattr(afk_mode, name)
    if name in _PLAN_NAMES:
        from coderai.soul.dynamic_injections import plan_mode

        return getattr(plan_mode, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DynamicInjection",
    "SoulView",
    "DynamicInjectionProvider",
    "AFK_INJECTION_TYPE",
    "AFK_PROMPT_ROOT",
    "AFK_DISABLED_REMINDER",
    "AfkModeInjectionProvider",
    "plan_full_reminder",
    "plan_sparse_reminder",
    "plan_reentry_reminder",
    "PlanModeInjectionProvider",
    "InjectionRegistry",
    "default_registry",
    "wrap_as_reminder",
    "normalize_history",
]


@dataclass
class InjectionRegistry:
    """Ordered provider set; collects injections before each LLM step."""

    providers: list[DynamicInjectionProvider] = field(default_factory=list)

    def add(self, provider: DynamicInjectionProvider) -> None:
        self.providers.append(provider)

    async def collect(
        self, history: list[dict[str, Any]], soul: SoulView
    ) -> list[DynamicInjection]:
        collected: list[DynamicInjection] = []
        for provider in self.providers:
            try:
                collected.extend(await provider.get_injections(history, soul))
            except Exception:
                continue
        return collected

    async def notify_compacted(self) -> None:
        for provider in self.providers:
            try:
                await provider.on_context_compacted()
            except Exception:
                continue

    async def notify_afk_changed(self, enabled: bool) -> None:
        for provider in self.providers:
            try:
                await provider.on_afk_changed(enabled)
            except Exception:
                continue


def default_registry() -> InjectionRegistry:
    """Standard provider set: afk + plan-mode reminders."""
    from coderai.soul.dynamic_injections.afk_mode import AfkModeInjectionProvider
    from coderai.soul.dynamic_injections.plan_mode import PlanModeInjectionProvider

    registry = InjectionRegistry()
    registry.add(AfkModeInjectionProvider())
    registry.add(PlanModeInjectionProvider())
    return registry


def wrap_as_reminder(content: str) -> str:
    """Wrap injection content in authoritative ``<system-reminder>`` tags."""
    return f"<system-reminder>\n{content}\n</system-reminder>"


def normalize_history(history: Sequence[Message]) -> list[Message]:
    """Merge adjacent user messages to produce a clean API input sequence.

    Dynamic injections are stored as standalone user messages in history;
    normalization merges them into the adjacent user message.

    Only ``user`` role messages are merged. Assistant and tool messages
    are never merged because their ``tool_calls`` / ``tool_call_id``
    fields form linked pairs that must stay intact.
    """
    if not history:
        return []

    from coderai.notifications import is_notification_message

    result: list[Message] = []
    for msg in history:
        if (
            result
            and result[-1].role == msg.role
            and msg.role == "user"
            and not is_notification_message(result[-1])
            and not is_notification_message(msg)
        ):
            merged_content = list(result[-1].content) + list(msg.content)
            result[-1] = Message(role="user", content=merged_content)
        else:
            result.append(msg)
    return result
