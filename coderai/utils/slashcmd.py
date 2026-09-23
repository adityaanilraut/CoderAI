from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar, overload

F = TypeVar("F", bound=Callable[..., None | Awaitable[None]])


class _DisplayName(str):
    _canonical: str
    _aliases: set[str]

    def __new__(cls, value: str, canonical: str, aliases: Sequence[str] = ()):
        obj = super().__new__(cls, value)
        obj._canonical = canonical
        obj._aliases = set(aliases)
        return obj

    def __call__(self, trigger: str | None = None) -> str:
        if trigger is not None and trigger != self._canonical and trigger in self._aliases:
            return f"/{self._canonical} ({trigger})"
        return f"/{self._canonical}"


class SlashCommand(Generic[F]):
    __slots__ = ("name", "description", "category", "aliases", "subcommands", "func")

    def __init__(
        self,
        name: str,
        description: str = "",
        category: str = "General",
        aliases: Sequence[str] | None = None,
        subcommands: Sequence[str] | None = None,
        func: F | None = None,
        *,
        summary: str | None = None,
    ) -> None:
        self.name = name
        self.description = summary if summary is not None else description
        self.category = category
        self.aliases = list(aliases) if aliases else []
        self.subcommands = tuple(subcommands) if subcommands else ()
        self.func = func

    @property
    def summary(self) -> str:
        return self.description

    @property
    def display_name(self) -> _DisplayName:
        canonical = f"/{self.name}"
        alias_str = ", ".join(f"/{a}" for a in self.aliases)
        full_str = canonical + (f", {alias_str}" if alias_str else "")
        return _DisplayName(full_str, self.name, tuple(self.aliases))

    def slash_name(self) -> str:
        """/name (aliases)"""
        if self.aliases:
            aliases_part = ", ".join(self.aliases)
            return f"/{self.name} ({aliases_part})"
        return f"/{self.name}"

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SlashCommand):
            return NotImplemented
        return self.name == other.name

    def __repr__(self) -> str:
        return f"SlashCommand(name={self.name!r}, category={self.category!r}, aliases={self.aliases!r})"


class SlashCommandRegistry(Generic[F]):
    """Registry for slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand[F]] = {}
        """Primary name -> SlashCommand"""
        self._command_aliases: dict[str, SlashCommand[F]] = {}
        """Primary name or alias -> SlashCommand"""

    @overload
    def command(self, func: F, /) -> F: ...

    @overload
    def command(
        self,
        *,
        name: str | None = None,
        aliases: Sequence[str] | None = None,
        category: str = "General",
        subcommands: Sequence[str] | None = None,
        description: str | None = None,
    ) -> Callable[[F], F]: ...

    def command(
        self,
        func: F | None = None,
        *,
        name: str | None = None,
        aliases: Sequence[str] | None = None,
        category: str = "General",
        subcommands: Sequence[str] | None = None,
        description: str | None = None,
    ) -> F | Callable[[F], F]:
        """Decorator to register a slash command with optional custom name and aliases."""

        def _register(f: F) -> F:
            func_name = f.__name__
            # Convention: `cmd_<name>` functions register as `/<name>`.
            # e.g. `cmd_model` -> `/model`, `cmd_help` -> `/help`.
            if name is not None:
                primary = name
            elif func_name.startswith("cmd_"):
                primary = func_name[4:] or func_name
            else:
                primary = func_name
            alias_list = list(aliases) if aliases else []

            desc = description if description is not None else (f.__doc__ or "").strip()
            # Create the primary command with aliases
            cmd = SlashCommand[F](
                name=primary,
                description=desc,
                category=category,
                aliases=alias_list,
                subcommands=subcommands,
                func=f,
            )

            # Register primary command
            self._commands[primary] = cmd
            self._command_aliases[primary] = cmd

            # Register aliases pointing to the same command
            for alias in alias_list:
                self._command_aliases[alias] = cmd

            return f

        if func is not None:
            return _register(func)
        return _register

    def find_command(self, name: str) -> SlashCommand[F] | None:
        return self._command_aliases.get(name)

    def list_commands(self) -> list[SlashCommand[F]]:
        """Get all unique primary slash commands (without duplicating aliases)."""
        return list(self._commands.values())

    def iter_command_entries(self) -> list[tuple[str, SlashCommand[F]]]:
        """Get canonical and alias entries as (trigger, command) pairs."""
        entries: list[tuple[str, SlashCommand[F]]] = []
        for cmd in self._commands.values():
            entries.append((cmd.name, cmd))
            seen = {cmd.name}
            for alias in cmd.aliases:
                if alias in seen:
                    continue
                if self._command_aliases.get(alias) is not cmd:
                    continue
                entries.append((alias, cmd))
                seen.add(alias)
        return entries


@dataclass(frozen=True, slots=True, kw_only=True)
class SlashCommandCall:
    name: str
    args: str
    raw_input: str


def parse_slash_command_call(user_input: str) -> SlashCommandCall | None:
    """Parse a slash command call from user input.

    Returns:
        SlashCommandCall if a slash command is found, else None.
    """
    user_input = user_input.strip()
    if not user_input or not user_input.startswith("/"):
        return None

    name_match = re.match(r"^\/([a-zA-Z0-9_-]+(?::[a-zA-Z0-9_-]+)*)", user_input)

    if not name_match:
        return None

    command_name = name_match.group(1)
    if len(user_input) > name_match.end() and not user_input[name_match.end()].isspace():
        return None
    raw_args = user_input[name_match.end() :].lstrip()
    return SlashCommandCall(name=command_name, args=raw_args, raw_input=user_input)
