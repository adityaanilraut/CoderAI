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
policy for both streaming paths. The former no-op `CODERAI_MARKDOWN_LEAK`
environment switch has been removed. Approval, question, and background-chat
overlays remain separate modules under `visualize`.

## Maintenance and verification

The five review remediation phases are implemented. Completed implementation
plans, dated design clips, and duplicate run logs have been removed. Four
unused internal modules were removed: `soul.toolset`, `utils.diff`,
`utils.message`, and `tools.file.plan_mode`. They were not registered tools or
SDK exports. Active CLI entry points, packaged skill scripts, and build helpers
remain available. The unused 4 MB macOS-only vendored `rg` executable
was removed; search uses a validated explicit/PATH executable or the tested
Python fallback.

The [verification contract](verification.md) is authoritative for local and
release checks. Its dependency audit still blocks release on the previously
identified AsyncSSH advisories. Remaining typing exclusions are measured by
`docs/type-suppression-budget.json`; renderer extraction adds no exclusions.
