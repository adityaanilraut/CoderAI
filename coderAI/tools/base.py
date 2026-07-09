"""Base tool interface and registry."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, ValidationError

from coderAI.core.provenance import Provenance
from coderAI.core.tool_error_codes import ToolErrorCode  # noqa: F401 — re-export

__all__ = [
    "SUBPROCESS_TIMEOUT_MARGIN_SECONDS",
    "Tool",
    "ToolPreview",
    "ToolRegistry",
    "ToolClassificationError",
    "ToolErrorCode",
]

# Head-room added on top of a tool's own (clamped) subprocess timeout when it
# derives the executor's outer wall-clock cap via ``Tool.resolve_timeout``.
# Covers ``run_scrubbed``'s 2s SIGTERM grace, the SIGKILL escalation, and the
# partial-output read, so the inner timeout path — which returns the tool's
# richer partial-output dict — always fires before the outer ``wait_for``.
SUBPROCESS_TIMEOUT_MARGIN_SECONDS = 10.0


@dataclass
class ToolPreview:
    """Result of :meth:`Tool.preview` — the approval-diff for a mutating call.

    A tool returns exactly one of:

    * ``new_content`` — the file's full resulting text; the executor renders the
      unified diff against the current file (keeping caching/truncation central).
    * ``rendered_diff`` — pre-rendered diff text shown verbatim (e.g. ``apply_diff``
      surfaces the model's own patch); the executor only truncates it.
    """

    new_content: Optional[str] = None
    rendered_diff: Optional[str] = None


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

    # True for *local* tools whose results are relayed from a third-party MCP
    # server (the static ``mcp_call_tool`` / ``mcp_read_resource`` /
    # ``mcp_get_prompt`` family). An ``mcp__server__tool`` *proxy* call has no
    # local Tool object and is detected by name; these static tools do, so they
    # must self-declare to arm the confused-deputy (MCP-mutation) gate the same
    # way a proxy call does. Kept distinct from ``is_egress`` because a listing
    # tool ingests untrusted MCP content without an outbound payload channel.
    mcp_source: bool = False

    # Per-tool timeout in seconds. None = use ToolExecutor's default.
    timeout: Optional[float] = None

    # Transient-failure retry opt-in. The executor re-attempts a tool call
    # (bounded by ``config.tool_retry_max_attempts``) only when this is True,
    # the call needed no confirmation, and the failure looks transient
    # (429/5xx/connection-reset…). Explicit opt-in only — read-only does NOT
    # imply idempotent/cheap, so the default is fail-closed. Reserved for
    # read-only network fetch tools whose errors are HTTP-shaped.
    retryable: bool = False

    # Background-job opt-in: ``start_job`` may run this tool detached from the
    # turn (JobManager). Reserved for long-running tools whose effects don't
    # race live foreground edits (test runs, package installs, downloads).
    backgroundable: bool = False

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

    # ── Approval-scope metadata (Phase 4.2) ──────────────────────────────
    # A blanket, name-level "always allow" is forbidden for this tool: it can
    # escalate to arbitrary local effect (code/command execution, file clobber)
    # depending on its arguments, so approval must be per-call or scoped to a
    # reviewed prefix/path. Consumed by ``permissions.ApprovalRules``.
    high_risk_no_blanket: bool = False

    # How a scoped "always allow" rule is matched for this tool:
    #   "command" — by shell command-prefix (run_command / run_background),
    #   "path"    — by file path/subtree (write_file / delete_file / move_file),
    #   None      — no safe scope abstraction; the tool is approved per-call.
    approval_scope: Optional[Literal["command", "path"]] = None

    # ── Registry-gating metadata (Phase 4.2) ─────────────────────────────
    # ``sys.platform`` values this tool is available on (None = all). Tools
    # whose platform doesn't match the host are dropped from the registry —
    # e.g. ``frozenset({"darwin"})`` for the macOS desktop-automation tools.
    platforms: Optional[frozenset[str]] = None

    # Optional third-party package this tool needs. When it can't be imported
    # the tool is dropped from the registry (``"playwright"`` for browser tools).
    requires_package: Optional[str] = None

    # Network-egress tool removed whenever ``config.web_tools_in_main`` is False.
    # Phase 5.1 made this transitive: sub-agents no longer keep web tools, so a
    # delegated child can't regain a capability the parent gave up. Distinct from
    # ``is_egress`` — that arms the untrusted-content egress gate; this controls
    # availability under ``web_tools_in_main``.
    network_gate: bool = False

    # File-editing tool whose batch scheduling serializes by target path so two
    # writes to the same file in one turn can't race (Phase 4.2; replaces the
    # executor's hardcoded ``safe_file_tools`` set).
    batch_serialize_by_path: bool = False

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

    def resolve_timeout(self, arguments: Dict[str, Any]) -> Optional[float]:
        """Argument-derived wall-clock cap for one call, or ``None`` to defer.

        Tools whose ``execute`` accepts its own ``timeout`` argument (e.g.
        ``run_command``) override this to return that inner timeout — clamped
        exactly as ``execute`` clamps it — plus
        :data:`SUBPROCESS_TIMEOUT_MARGIN_SECONDS`, so the executor's outer
        ``wait_for`` can never fire first and bypass the tool's own
        process-group cleanup. Returning ``None`` (the default) defers to the
        ``timeout`` class attribute / config / executor default chain.
        """
        return None

    def preview(
        self, arguments: Dict[str, Any], original: Optional[str]
    ) -> Optional["ToolPreview"]:
        """Approval-diff preview for a mutating call (Phase 4.3).

        File-editing tools override this so the approval diff is computed by the
        *same* semantics as :meth:`execute` (one implementation, no drift). The
        executor supplies the file's current text as ``original`` (``None`` when
        the file doesn't exist) — it has already resolved and project-scope
        checked the path and read the content through its mtime cache — and the
        tool returns a :class:`ToolPreview` (or ``None`` when no preview applies).

        Default: no preview.
        """
        return None

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
        return bool(self.is_read_only or self.requires_confirmation or self.is_egress or self.safe)


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
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute a tool by name.

        Confirmation/approval is NOT gated here: the live gate is
        :class:`~coderAI.core.tool_executor.ToolExecutor`'s permissions-based,
        fail-closed check. This registry just validates arguments and dispatches.

        Args:
            name: Tool name
            **kwargs: Tool parameters

        Returns:
            Execution results

        Raises:
            ValueError: If tool not found
        """
        tool = self.get(name)
        if tool is None:
            raise ValueError(f"Tool not found: {name}")

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
