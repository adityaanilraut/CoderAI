"""Modular, type-safe tool registry with scoped layers and strict JSON Schema."""

from __future__ import annotations

from typing import Any
from collections.abc import Callable, Sequence


from coderai.tools.legacy.types import (
    ToolDefinition,
    ValidationError,
    canonicalize_tool_schema,
)

BASH_SCOPE_ENUM = [
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "unknown",
]


class ToolLayer:
    """Scoped tool registration layer supporting dynamic overrides, filters, and guards."""

    def __init__(self, scope: str | None = None) -> None:
        self.scope = scope
        self.tools: dict[str, ToolDefinition] = {}
        self.aliases: dict[str, str] = {}
        self.restrictions: list[dict[str, set[str]]] = []
        self.guards: list[Callable[[ToolDefinition, dict[str, Any], Any], str | None]] = []

    def insert(self, tool_def: ToolDefinition) -> None:
        self.tools[tool_def.name] = tool_def
        for alias in tool_def.aliases:
            self.aliases[alias] = tool_def.name

    def remove(self, name: str) -> bool:
        canonical = self.aliases.get(name, name)
        removed = self.tools.pop(canonical, None)
        if removed:
            # Clean up aliases
            self.aliases = {k: v for k, v in self.aliases.items() if v != canonical}
            return True
        return False

    def admits(self, name: str) -> bool:
        """Check if a tool name passes all compiled restrictions in this layer."""
        name = self.aliases.get(name, name)
        for r in self.restrictions:
            allow_set = r.get("allow")
            deny_set = r.get("deny")
            if allow_set is not None and name not in allow_set:
                return False
            if deny_set is not None and name in deny_set:
                return False
        return True


class ToolRegistry:
    """Type-safe registry for built-in and dynamic agent tools with scoping, restrictions, and validation."""

    def __init__(self) -> None:
        self._global_layer = ToolLayer(scope=None)
        self._scoped_layers: dict[str, ToolLayer] = {}
        self._change_listeners: list[Callable[[], None]] = []
        self._register_builtins()

    def on_change(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe a listener to tool registration/restriction changes."""
        self._change_listeners.append(listener)

        def disposer() -> None:
            if listener in self._change_listeners:
                self._change_listeners.remove(listener)

        return disposer

    def _emit_change(self) -> None:
        for listener in list(self._change_listeners):
            try:
                listener()
            except Exception:
                pass

    def _get_layer(self, scope: str | None, create: bool = False) -> ToolLayer:
        if scope is None:
            return self._global_layer
        if scope not in self._scoped_layers:
            if create:
                self._scoped_layers[scope] = ToolLayer(scope=scope)
            else:
                return ToolLayer(scope=scope)
        return self._scoped_layers[scope]

    def register(self, tool_def: ToolDefinition, scope: str | None = None) -> Callable[[], None]:
        """Register a tool definition and return an unregister disposer function."""
        from coderai.tools.legacy.schema import assert_supported_json_schema

        assert_supported_json_schema(tool_def.runtime_schema(), tool_def.name)
        layer = self._get_layer(scope, create=True)
        layer.insert(tool_def)
        self._emit_change()

        def unregister_disposer() -> None:
            self.unregister(tool_def.name, scope=scope)

        return unregister_disposer

    def unregister(self, name: str, scope: str | None = None) -> bool:
        """Unregister a tool by name or alias from the specified scope or global layer."""
        layer = self._get_layer(scope, create=False)
        removed = layer.remove(name)
        if removed:
            self._emit_change()
        return removed

    def restrict(
        self,
        filter_spec: dict[str, Sequence[str]],
        scope: str | None = None,
    ) -> Callable[[], None]:
        """Restrict visible tools for a scope (e.g. allow only read tools for a read_only subagent)."""
        layer = self._get_layer(scope, create=True)
        compiled: dict[str, set[str]] = {}
        if "allow" in filter_spec:
            compiled["allow"] = {
                layer.aliases.get(n, self._global_layer.aliases.get(n, n))
                for n in filter_spec["allow"]
            }
        if "deny" in filter_spec:
            compiled["deny"] = {
                layer.aliases.get(n, self._global_layer.aliases.get(n, n))
                for n in filter_spec["deny"]
            }

        layer.restrictions.append(compiled)
        self._emit_change()

        def disposer() -> None:
            if compiled in layer.restrictions:
                layer.restrictions.remove(compiled)
                self._emit_change()

        return disposer

    def guard(
        self,
        guard_fn: Callable[[ToolDefinition, dict[str, Any], Any], str | None],
        scope: str | None = None,
    ) -> Callable[[], None]:
        """Register a monotonic execution guard for a scope or globally."""
        layer = self._get_layer(scope, create=True)
        layer.guards.append(guard_fn)

        def disposer() -> None:
            if guard_fn in layer.guards:
                layer.guards.remove(guard_fn)

        return disposer

    def get_guards(
        self, scope: str | None = None
    ) -> list[Callable[[ToolDefinition, dict[str, Any], Any], str | None]]:
        """Return active monotonic execution guards for scope and global layer."""
        guards = list(self._global_layer.guards)
        if scope and scope in self._scoped_layers:
            guards.extend(self._scoped_layers[scope].guards)
        return guards

    def admits(self, name: str, scope: str | None = None) -> bool:
        canonical = self._global_layer.aliases.get(name, name)
        layer = self._scoped_layers.get(scope) if scope else None
        if layer:
            canonical = layer.aliases.get(name, canonical)
        return self._global_layer.admits(canonical) and (layer is None or layer.admits(canonical))

    def get(self, name: str, scope: str | None = None) -> ToolDefinition | None:
        """Resolve a tool definition by name or alias, applying scoping and active restrictions."""
        canonical = self._global_layer.aliases.get(name, name)
        scoped = self._scoped_layers.get(scope) if scope else None
        if scoped:
            canonical = scoped.aliases.get(name, canonical)
        if not self._global_layer.admits(canonical):
            return None
        # 1. Check scoped layer first
        if scope and scope in self._scoped_layers:
            scoped_layer = self._scoped_layers[scope]
            canonical = scoped_layer.aliases.get(name, canonical)
            if canonical in scoped_layer.tools:
                if not scoped_layer.admits(canonical):
                    return None
                return scoped_layer.tools[canonical]

        # 2. Check global layer
        tool_def = self._global_layer.tools.get(canonical)
        if tool_def is None:
            return None

        # 3. Check restrictions on inherited tool
        if scope and scope in self._scoped_layers:
            if not self._scoped_layers[scope].admits(tool_def.name):
                return None

        return tool_def

    def get_tool(self, name: str, scope: str | None = None) -> ToolDefinition | None:
        """Alias for get(name, scope)."""
        return self.get(name, scope=scope)

    def has_tool(self, name: str, scope: str | None = None) -> bool:
        """Check if a tool exists and is permitted in the given scope."""
        return self.get(name, scope=scope) is not None

    def resolve_name(self, name: str) -> str | None:
        """Return the canonical registered name for a name or alias."""
        canonical = self._global_layer.aliases.get(name, name)
        return canonical if canonical in self._global_layer.tools else None

    def list_tools(self, scope: str | None = None) -> list[ToolDefinition]:
        """Return all registered and permitted tool definitions for the given scope."""
        tools_map: dict[str, ToolDefinition] = {}

        # 1. Global tools that pass scope restrictions
        for name, tool_def in self._global_layer.tools.items():
            if not self._global_layer.admits(name):
                continue
            if scope and scope in self._scoped_layers:
                if not self._scoped_layers[scope].admits(name):
                    continue
            tools_map[name] = tool_def

        # 2. Scoped tool additions / overrides
        if scope and scope in self._scoped_layers:
            for name, tool_def in self._scoped_layers[scope].tools.items():
                if not self._global_layer.admits(name):
                    continue
                if not self._scoped_layers[scope].admits(name):
                    continue
                tools_map[name] = tool_def

        return list(tools_map.values())

    def validate_arguments(
        self,
        name: str,
        args: dict[str, Any],
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Strictly validate input arguments against the tool's parameter schema.

        Raises:
            ValidationError: If required fields are missing, types mismatch, or constraints are violated.
        """
        tool_def = self.get(name, scope=scope)
        if not tool_def:
            raise ValidationError(
                f"Tool '{name}' is not registered or is restricted in the ToolRegistry."
            )

        if not isinstance(args, dict):
            raise ValidationError(f"Tool arguments for '{name}' must be a dictionary/object.")

        from coderai.tools.legacy.schema import validate_json_schema_value

        violations = validate_json_schema_value(tool_def.runtime_schema(), args, name)
        if violations:
            raise ValidationError("; ".join(violations))

        if name == "WebSearch" and (bool(args.get("query")) == bool(args.get("queries"))):
            raise ValidationError("WebSearch requires exactly one of query or queries.")

        return args

    def to_openai_schemas(
        self,
        scope: str | None = None,
        options: dict[str, Any] | None = None,
        external_tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Project registered tools onto formatted OpenAI Function tool call schemas."""
        options = options or {}
        from coderai.prompt.sections import get_preset_tools, order_tools
        from coderai.utils.common.model_capabilities import supports_multimodal

        tools_list: list[dict[str, Any]] = []
        is_non_interactive = options.get("nonInteractive") is True
        is_child_agent = options.get("childAgent") is True
        model = str(options.get("model", ""))
        multimodal_mode = str(options.get("multimodal", "default"))
        model_supports_vision = supports_multimodal(model, multimodal_mode)

        preset = options.get("preset") or options.get("toolsPreset") or options.get("tools_preset")

        for tool_def in self.list_tools(scope=scope):
            name = tool_def.name
            # Filter non-interactive tools
            if is_non_interactive and name == "AskUserQuestion":
                continue
            # Filter child agent specific tools
            if not is_child_agent and name == "report":
                continue
            # Filter multimodal tool if model has native vision
            if model_supports_vision and name == "UnderstandImage":
                continue

            schema = tool_def.to_openai_schema()
            tools_list.append(schema)

        if external_tools:
            layer = self._scoped_layers.get(scope) if scope else None
            tools_list.extend(
                canonicalize_tool_schema(t)
                for t in external_tools
                if self._global_layer.admits((t.get("function") or {}).get("name", ""))
                and (layer is None or layer.admits((t.get("function") or {}).get("name", "")))
            )

        preset_tools = get_preset_tools(preset)
        if preset_tools is not None:
            tools_list = [
                t for t in tools_list if (t.get("function") or {}).get("name") in preset_tools
            ]

        allowed_tools_opt = options.get("allowedTools")
        if allowed_tools_opt is None:
            allowed_tools_opt = options.get("allowed_tools")
        if allowed_tools_opt is not None:
            from coderai.subagents.registry import is_tool_allowed

            tools_list = [
                t
                for t in tools_list
                if is_tool_allowed(
                    (t.get("function") or {}).get("name", ""),
                    "allowlist",
                    tuple(allowed_tools_opt),
                )
            ]

        ordered = order_tools(tools_list)
        return [canonicalize_tool_schema(t) for t in ordered]

    def _register_builtins(self) -> None:
        """Register subsystem definitions through the public registry facade."""
        from coderai.tools.shell.definitions import register_tools as register_0

        register_0(self)
        from coderai.tools.background.definitions import register_tools as register_1

        register_1(self)
        from coderai.tools.file.definitions import register_tools as register_2

        register_2(self)
        from coderai.tools.ask_user.definitions import register_tools as register_3

        register_3(self)
        from coderai.tools.web.definitions import register_tools as register_4

        register_4(self)
        from coderai.tools.agent.definitions import register_tools as register_5

        register_5(self)
        from coderai.tools.legacy.terminal_definitions import register_tools as register_6

        register_6(self)
        from coderai.tools.legacy.skill_definitions import register_tools as register_7

        register_7(self)
        from coderai.tools.todo.definitions import register_tools as register_8

        register_8(self)
        from coderai.tools.think.definitions import register_tools as register_9

        register_9(self)
        from coderai.tools.dmail.definitions import register_tools as register_10

        register_10(self)
        from coderai.tools.plan.definitions import register_tools as register_11

        register_11(self)
        from coderai.tools.legacy.schedule_definitions import register_tools as register_12

        register_12(self)
        from coderai.goals.tool_definitions import register_tools as register_13

        register_13(self)
        from coderai.teams.tool_definitions import register_tools as register_14

        register_14(self)
        from coderai.tools.session.definitions import register_tools as register_15

        register_15(self)


# Global default tool registry
_default_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _default_tool_registry
