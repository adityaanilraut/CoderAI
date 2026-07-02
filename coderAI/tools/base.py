"""Base tool interface and registry."""

import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from coderAI.core.provenance import Provenance
from coderAI.core.tool_error_codes import ToolErrorCode  # noqa: F401 — re-export

__all__ = ["Tool", "ToolRegistry", "ToolClassificationError", "ToolErrorCode"]


class ToolClassificationError(RuntimeError):
    """Raised when a registered tool declares no safety classification.

    Every tool must declare at least one of ``is_read_only``,
    ``requires_confirmation``, ``is_egress`` or ``safe`` so the confirmation
    gate can reason about it. A tool that declares none is ambiguous and, under
    the Phase 4 fail-closed policy, is refused at registry-build time.
    """


class Tool(ABC):
    """Abstract base class for MCP tools."""

    name: str = ""
    description: str = ""
    parameters_model: Optional[Type[BaseModel]] = None

    # Safety: if True, the agent will ask the user to confirm before executing.
    requires_confirmation: bool = False

    # Parallelism: read-only tools can be executed concurrently.
    is_read_only: bool = False

    # Confirmation opt-out (Phase 4.1). A mutating tool (``is_read_only=False``)
    # that only touches internal, low-risk state (agent notepad / plan / task
    # list / memory) sets ``safe = True`` to run without confirmation. This is
    # the *explicit* escape hatch: any mutating tool that sets neither
    # ``requires_confirmation`` nor ``safe`` is treated as requiring
    # confirmation (fail-closed) — see ``permissions.tool_requires_confirmation``.
    safe: bool = False

    # Provenance (Phase 3): taint label applied to this tool's results. Tools
    # that ingest data from outside the user's own input (web fetch, MCP output)
    # set this to ``Provenance.UNTRUSTED_EXTERNAL`` so the result is rendered in a
    # non-authoritative ``<untrusted_tool_output>`` block and marks the turn as
    # having ingested untrusted content (which arms the egress gate below).
    result_provenance: str = Provenance.TRUSTED

    # Egress axis (Phase 3.4): True for tools that perform network egress (and so
    # can exfiltrate via URL/query strings). Separate from ``is_read_only`` — a
    # tool can be parallel-safe yet still require confirmation once the turn has
    # ingested untrusted content. Gated in ToolExecutor's confirmation path.
    is_egress: bool = False

    # Per-tool timeout in seconds. None = use ToolExecutor's default.
    timeout: Optional[float] = None

    # UI grouping. Used by the Textual UI to categorize tools (filesystem,
    # search, git, terminal, web, memory, agent, mcp, other). Subclasses
    # override to set their category; unset means "other".
    category: str = "other"

    # If >0, multiple invocations of this tool in one LLM turn may run
    # concurrently, at most this many at a time (extra calls are queued in
    # additional batches). Used for delegate_task so several sub-agents can
    # run in parallel (e.g. web research vs codebase reads). Standard
    # read-only tools use is_read_only=True with max_parallel_invocations=0
    # (unlimited concurrency among themselves).
    max_parallel_invocations: int = 0

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            Dictionary with execution results
        """
        pass

    def get_schema(self) -> Dict[str, Any]:
        """Get the JSON schema for this tool.

        Returns:
            OpenAI function calling schema
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_parameters(),
            },
        }

    def get_parameters(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool.

        Returns:
            JSON Schema for parameters
        """
        if self.parameters_model:
            # Generate JSON schema and simplify for LLMs
            schema = self.parameters_model.model_json_schema()
            # Pydantic puts things in $defs sometimes, but LLMs handle flat mostly.
            # model_json_schema is usually fine.
            return schema

        return {"type": "object", "properties": {}}

    @property
    def is_classified(self) -> bool:
        """True if this tool declares any safety class.

        A tool is *classified* when it sets at least one of ``is_read_only``,
        ``requires_confirmation``, ``is_egress`` or ``safe``. Unclassified tools
        are rejected by :meth:`ToolRegistry.validate_classifications`.
        """
        return bool(
            self.is_read_only or self.requires_confirmation or self.is_egress or self.safe
        )


class ToolRegistry:
    """Registry for managing available tools."""

    def __init__(self):
        """Initialize the tool registry."""
        self.tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool.

        Args:
            tool: Tool instance to register
        """
        self.tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name.

        Args:
            name: Tool name

        Returns:
            Tool instance or None if not found
        """
        return self.tools.get(name)

    def get_all(self) -> List[Tool]:
        """Get all registered tools.

        Returns:
            List of all tools
        """
        return list(self.tools.values())

    def find_unclassified(self) -> List[str]:
        """Names of registered tools that declare no safety class (Phase 4.1)."""
        return [name for name, tool in self.tools.items() if not tool.is_classified]

    def validate_classifications(self) -> None:
        """Fail-closed guard: refuse to run if any tool is unclassified.

        Raises:
            ToolClassificationError: listing every unclassified tool.
        """
        unclassified = self.find_unclassified()
        if unclassified:
            raise ToolClassificationError(
                "Every tool must declare a safety class "
                "(is_read_only / requires_confirmation / is_egress / safe=True). "
                "Unclassified: " + ", ".join(sorted(unclassified))
            )

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Get schemas for all tools.

        Returns:
            List of tool schemas for OpenAI function calling
        """
        return [tool.get_schema() for tool in self.tools.values()]

    async def execute(
        self,
        name: str,
        confirmation_callback: Optional[Callable] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute a tool by name.

        Args:
            name: Tool name
            confirmation_callback: Optional async/sync callable that receives
                (tool_name, arguments) and returns True to allow execution.
                When None, all tools execute without confirmation.
            **kwargs: Tool parameters

        Returns:
            Execution results

        Raises:
            ValueError: If tool not found
        """
        tool = self.get(name)
        if tool is None:
            raise ValueError(f"Tool not found: {name}")

        # Confirmation gate — supports both sync and async callbacks
        if tool.requires_confirmation and confirmation_callback is not None:
            if inspect.iscoroutinefunction(confirmation_callback) or (
                hasattr(confirmation_callback, "__call__")
                and inspect.iscoroutinefunction(confirmation_callback.__call__)
            ):
                approved = await confirmation_callback(name, kwargs)
            else:
                approved = confirmation_callback(name, kwargs)
            if not approved:
                return {
                    "success": False,
                    "error": f"Tool '{name}' was denied by the user.",
                    "error_code": ToolErrorCode.DENIED,
                }

        if tool.parameters_model:
            try:
                # Validate and parse arguments using Pydantic
                parsed_args = tool.parameters_model(**kwargs)
                return await tool.execute(**parsed_args.model_dump())
            except ValidationError as e:
                # Return validation errors as friendly tool response
                return {
                    "success": False,
                    "error": f"Validation error for tool '{name}':\n{str(e)}",
                    "error_code": ToolErrorCode.VALIDATION,
                }

        return await tool.execute(**kwargs)
