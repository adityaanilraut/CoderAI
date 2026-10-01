# Runtime architecture

CoderAI is a terminal CLI and daemon. `coderai.main` delegates to
`coderai.ui.shell.app`; `coderai.cli` provides shared session construction,
diagnostics, export, and command helpers.

## Session and turn execution

`coderai.cli.session_factory.build_session_manager` constructs a
`coderai.soul.session.manager.SessionManager` with provider configuration,
workspace, callbacks, permission policy, and runtime ownership. The manager
owns conversation history, session state, turn cancellation, checkpoints,
and session-local event delivery. `coderai.soul.coderaisoul.AgentLoop` runs
provider requests and tool steps. Compaction lives in `coderai.soul.compaction`.

Tools execute through `coderai.tools.legacy.registry.ToolRegistry` and
`coderai.tools.legacy.executor.ToolExecutor`. Despite its directory name,
`legacy` is the active execution platform. Permissions and approvals live in
`coderai.soul.approval` and `coderai.approval_runtime`; hooks can deny, request
approval, or halt dispatch. Shell escalation requires approval bound to the
exact call. Terminal sessions carry their owner and enforced execution context.

`coderai.config` loads settings JSON and environment overrides.
`coderai.typed_config` validates the TOML/JSON provider and model vocabulary;
it can migrate an existing `settings.json` into `config.toml`. MCP overlays
are parsed by `coderai.mcp.files` and executed by `coderai.mcp` transports.

## Protocol adapters

ACP (`coderai.acp.server`) and the Python SDK
(`sdks/coderai-sdk/src/coderai_sdk/client.py`) share
`coderai.acp.engine.SessionManagerEngine`. It forwards real manager callbacks
through session-local wire events. `coderai.wire.server` serves JSON-RPC over
stdio; `coderai.wire.types` owns event types and envelope serialization.
`coderai.wire.root_hub` coordinates owner-filtered subscriptions and
`coderai.wire.file` supports file-backed event streams. Event histories and
subscription queues have bounded message counts.

## Roles and child execution

`coderai.agentspec` resolves bundled YAML specs and inheritance.
`coderai.subagents.registry` discovers Markdown roles, while
`coderai.subagents.builder` enforces role policies. The runner carries the
parent workspace, policy, ancestry, and depth into child execution.
See [Agent roles](agent-roles.md) for the resolved role modes and discovery order.

Teams use `coderai.teams.manager.TeamManager`, the DAG-validated task board,
and actor mailboxes. Team managers, background jobs, terminals, notifications,
and registries enforce runtime ownership. Scheduler stores are scoped to the
project. In-memory mailboxes and atomic JSON persistence do not establish
cross-process transactional coordination.

## Terminal rendering

`coderai.ui.shell.visualize._blocks` owns live content, thinking, and context
status. `_markdown_boundary` supplies the shared commitment parser;
`_markdown_stream` provides the fallback streaming renderer. `_tool_cards`
formats completed tool results and `_todos` renders checklists. Existing
shell imports are retained through narrow exports and a fallback constructor
adapter in `_blocks`.

`coderai.utils.rich.markdown.Markdown` supplies the heading and code background
policy for both streaming paths. Approval, question, and background-chat
overlays remain separate modules under `visualize`.

## Persistence and execution boundaries

`hooks.events` builds lifecycle payloads exposed through compatible `hooks.runner`
names. D2 and Mermaid share decision inference; wire adapters share JSON-RPC
framing while retaining their error-envelope policies. MCP overlays share merging;
settings normalization remains separate because its filtering policies differ.
Request, UI, and streaming token estimates retain distinct policies.

`soul.session.streaming` assembles provider deltas; `completion` negotiates clients,
retries, and synchronous fallback. `soul.session.tool_dispatch` prepares chunks and
persists results; `SessionManager` retains execution ownership, cancellation, and
settlement. `subagents.execution` applies child policy and constructs callbacks;
the runner calculates inherited approval and owns the loop. CLI drivers use an
asynchronous `ProcessFactory` boundary and shared process-group shutdown.

Session index/JSONL replacement share `soul.session.store` storage logic with
umask permissions and failed-write orphan recovery. The general atomic writer
uses exclusive temporary creation, mode preservation, fsync, replacement, and
failure cleanup. Its sync/async text API honors Python codecs and error handlers,
retains `utf16le` shorthand, and returns encoded byte counts. Settings and OAuth
callers explicitly require private temporary/replacement mode `0600`.

Approval IDs identify retained records for the lifetime of an `ApprovalRuntime`.
A reused ID raises `ValueError` before creating a record or publishing events,
including when the old record is resolved or cancelled. This prevents stale
responses and waiters from targeting a different owner.

## Local filesystem adapter

`coderai.kaos` bundles the local PyKAOS 0.9.0 path, filesystem, subprocess and
ContextVar interfaces with their Apache-2.0 license and upstream notice. Session
metadata and ACP fallbacks use this package. The unused SSH backend is excluded,
removing the external distribution's vulnerable exact AsyncSSH dependency pin.
Backend names and serialized workspace metadata retain their existing values.

## Maintenance and verification

The [verification contract](verification.md) defines local and release gates.
The [test-suite audit](test-suite-audit.md) inventories coverage, intentional
behavior corrections and removal decisions. Source/regression checks and dependency-audit
release readiness are separate results. Remaining typing exclusions are measured
by `type-suppression-budget.json`; the extracted execution modules have no new
exclusions.
