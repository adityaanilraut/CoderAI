# CoderAI

`coderai` is a terminal coding assistant built on the Kimi Code TypeScript engine. It streams Markdown answers, reads and searches code, writes and edits files, runs commands and tests, asks for approval, and saves conversations for later use. There is no browser application or local agent server.

## Install and run

For a source install, requires Node.js **24.15.0 or newer**, pnpm **10.33.0**, and `rg` (ripgrep) on PATH for file searches. Bash is used for commands; Windows requires Git Bash. Automated terminal tests additionally require Python 3 and a POSIX PTY. Development and verification here used Node 26.9.0 on macOS.

```sh
cd /Users/aditya/Desktop/coderai
npx --yes pnpm@10.33.0 install --frozen-lockfile
npx --yes pnpm@10.33.0 build
node dist/coderai.mjs --help
export CODERAI_MODEL_NAME='YOUR_MODEL_ID'
export OPENAI_API_KEY='YOUR_API_KEY'
node dist/coderai.mjs
```

To use the name `coderai` from any directory, install a local executable link with `npm link` from this directory, or add this shell alias:

```sh
alias coderai='/Users/aditya/Desktop/coderai/dist/coderai.mjs'
```

The built executable is executable directly and resolves its assets and dependencies relative to the installation, independently of the current directory. Keep `dist/` beside the installed `node_modules/`. No part of the original Kimi checkout is required.

## Standalone executable

CoderAI can be built as a single executable with an embedded Node.js runtime. The user running the executable does not need Node.js or `node_modules`; ripgrep (`rg`) is still required for file searches, and Windows requires Git Bash.

To build it on the target machine, install Node.js **26 or newer** and pnpm 10.33.0, then run:

```sh
npx --yes pnpm@10.33.0 install --frozen-lockfile
npx --yes pnpm@10.33.0 build:native
```

The result is `dist-native/<platform>-<architecture>/coderai` (`coderai.exe` on Windows). The build also produces a ZIP containing the executable and license notices, plus a SHA-256 checksum. You can copy the executable to a directory on your PATH. For example, on macOS or Linux:

```sh
mkdir -p ~/.local/bin
cp dist-native/$(node -p 'process.platform + "-" + process.arch')/coderai ~/.local/bin/coderai
~/.local/bin/coderai --help
```

Add `~/.local/bin` to your PATH if needed, then configure a model and API key as described below. Local macOS builds use an ad-hoc signature; distributing outside your machine may require Apple signing and notarization. The native build workflow creates platform-specific artifacts, but this repository does not yet publish them or host an installer. Linux and Windows native builds have not been verified locally.

## Authentication and models

No Kimi account or browser login is required. Configure your chosen provider's API key and model. The old `coderai login` command now only prints local setup guidance; it makes no authentication requests.

Run `coderai --setup` in a terminal for a guided provider and model setup. Use **↑/↓** to move, **Enter** to select, type to search, **Esc** to go back, and **Ctrl+C** to exit. Menus also accept a number followed by Enter. The dashboard shows your providers, model count, and current default. Presets are available for OpenAI, Anthropic, DeepSeek, Google, and Meta-compatible services; custom providers have an API protocol selector. Meta models require an endpoint from a compatible hosting provider.

After saving a provider, choose **Select and add a model now**, then browse the provider's live model list or enter a model ID manually. Discovery uses the saved credentials to request model metadata; it does not generate a completion. Context limits and capabilities are prefilled when supplied by the provider; confirm them or enter the provider's documented values. Context limits accept whole token counts, `128k`, or `1m`. Capabilities use toggles. Providers without models appear in **Choose default model** with an option to finish their setup, so a newly added provider does not disappear from selection.

API key entry is hidden. Setup can save the key in the local configuration file (unencrypted) or save an environment variable name instead. If you choose an environment variable, set it before browsing models or starting CoderAI; manual model entry remains available. Invalid fields can be corrected in place. Esc cancels an unfinished step while completed provider/model saves remain. Existing entries are preserved, and new aliases receive unique name suggestions. Model environment overrides still take precedence over the saved default.

For API-key use, configure environment variables. Obtain the model identifier and endpoint from your provider:

```sh
export CODERAI_MODEL_NAME='YOUR_MODEL_ID'
export CODERAI_MODEL_PROVIDER_TYPE='openai'
export OPENAI_API_KEY='YOUR_API_KEY'
# For a custom OpenAI-compatible service, also set its API base URL:
export CODERAI_MODEL_BASE_URL='https://YOUR_PROVIDER/v1'
coderai -p 'Explain this project'
```

Avoid putting actual keys into source files or shell history; load them from your preferred secret manager. Compatible retained provider types are `kimi`, `openai`, `openai_responses`, `anthropic`, `google-genai`, and `vertexai`. Provider-specific services and model capabilities still depend on the provider. Deterministic tests cover Kimi and OpenAI tool paths and text streaming for Anthropic, Google GenAI and OpenAI Responses.

Alternatively create **`~/.coderai/config.toml`**:

```toml
default_model = "coding"
telemetry = false

[providers.api]
type = "openai"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[models.coding]
provider = "api"
model = "YOUR_MODEL_ID"
max_context_size = 262144
capabilities = ["thinking", "image_in"]

[loop_control]
max_steps_per_turn = 100
```

Set `OPENAI_API_KEY` externally for that example. The configuration retains Kimi's TOML schema. Use the provider's actual context limit and capabilities. `-m coding` selects a configured alias. `CODERAI_MODEL_*` overrides are mapped into the retained engine's `KIMI_MODEL_*` overlay and select the temporary alias `__kimi_env_model__`; these overrides are not written to config. CoderAI model overrides default to provider type `openai`; set `CODERAI_MODEL_PROVIDER_TYPE` to select another provider. Useful suffixes include `NAME`, `API_KEY`, `PROVIDER_TYPE`, `BASE_URL`, `MAX_CONTEXT_SIZE`, `MAX_OUTPUT_SIZE`, `CAPABILITIES`, and `THINKING_EFFORT`. The upstream `KIMI_MODEL_*` variables remain supported. Standard provider key variables are also supported by the engine. Missing credentials, unknown models/providers, invalid config and provider failures produce actionable errors and nonzero headless exits.

In an interactive session, use `/model` to choose a configured provider and then one of its models. Providers such as OpenAI, Anthropic, DeepSeek, Meta, and Google appear when configured with models; custom providers are supported too. Use `/model <alias>` to select a model directly. The selection applies to the current conversation.

## Interactive use

```sh
coderai -C /path/to/project
coderai --resume SESSION_ID
coderai --continue
coderai --plan --agent explore --add-dir /path/to/other/project
coderai --skills-dir /path/to/skills --agent-file /path/to/agent.yaml
```

Enter sends a prompt; Shift+Enter or Alt+Enter inserts a newline (terminal support varies). Bracketed multiline paste and input history are supported by the upstream editor. Slash commands, supported command arguments, and file paths have completion; command suggestions show argument hints. Assistant text streams through pi-tui's Markdown renderer, with highlighted code and visible tool activity. Input submitted while work is running, including `/btw`, is queued; A queue area above the input shows pending work; `/queue` inspects or removes queued input and `/steer TEXT` redirects the current turn. The `/undo` selector only offers prompts in the active context after the most recent compaction; undo does not revert file changes. Shift+Tab toggles plan mode when idle. Ctrl+G opens the current draft in `$VISUAL` or `$EDITOR`, or in the command selected with `/editor`.

Prefix a command with `!` to run it directly in the session's shell, for example `!git status`. Esc or Ctrl+C cancels active work.

The approval window shows the action and a scrollable preview. Use ↑/↓ and Enter, or type a number from 1–5 and Enter, to approve once, remember matching actions for the session, enable YOLO, reject with feedback, or reject. PgUp/PgDn scrolls long previews; Esc rejects. Rejection is selected by default. Typed `yes`, `always`, and `no: <feedback>` replies also work. Start with `coderai --yolo` to auto-approve routine actions, or use `/yolo` to toggle it during a conversation. `/yolo on`, `/yolo off`, and `/yolo status` explicitly enable, disable, or inspect it. Explicit denials and guarded checks still apply in YOLO mode. Plan reviews and single-choice questions use a keyboard selector. Ctrl+C cancels active work; when idle it exits. Ctrl+D on empty input and `/quit` save and exit. SIGTERM cancels and shuts down. Shutdown stops owned processes, drains session state and releases locks, with an eight-second emergency shutdown ceiling.

| Command | Behavior |
| --- | --- |
| `/help` | List commands |
| `/new` | Create a saved conversation |
| `/sessions`, `/resume`, `/resume ID`, `/resume latest` | List or reopen conversations; `/resume` opens a picker |
| `/title [TITLE]`, `/status` | Show or change the title; inspect session status |
| `/model`, `/model ALIAS` | Choose or select a configured model |
| `/yolo [on|off|status]` | Toggle, enable, disable, or inspect YOLO automatic approval |
| `/permission` | Choose approval mode, including YOLO |
| `/permissions`, `/permissions manual`, `/permissions yolo`, `/permissions auto` | Inspect or change approval mode |
| `/context`, `/compact`, `/clear` | Inspect usage, summarize context, or clear history |
| `/plan`, `/plan show` | Toggle planning mode or show the current plan |
| `/swarm`, `/tower` | Inspect or change supported engine modes |
| `/goal OBJECTIVE`, `/goal status`, `/goal pause`, `/goal resume`, `/goal cancel`, `/goal next` | Run or manage an autonomous goal and its queue |
| `/tasks`, `/stop TASK_ID` | Browse, inspect and stop background commands |
| `/output TASK_ID` | Show captured background task output |
| `/effort LEVEL`, `/mcp`, `/skills`, `/add-dir PATH` | Configure thinking or inspect and extend the session; choose whether to remember added directories |
| `/plugins`, `/reload`, `/todos`, `/version` | Manage plugins, reload configuration, inspect todos or show the version |
| `/fork`, `/undo`, `/export-md PATH` | Fork the session, withdraw the last prompt, or export the persisted conversation |
| `/export-debug [PATH]` | Export a diagnostic archive for the current session |
| `/theme [auto|dark|light|name]` | Choose and save terminal colors |
| `/attach PATH`, `/attach list`, `/attach clear` | Stage media for the next prompt |
| `/btw [QUESTION]` | Ask a read-only side question in a temporary session |
| `/copy` | Request that the terminal copy the last assistant message using OSC 52 |
| `/editor COMMAND` | Set the external editor for the current process |
| `/steer TEXT` | Redirect active work through the engine's steering queue |
| `/quit` | Save and exit |

`coderai sessions` also lists sessions without entering the terminal UI. Use `coderai doctor`, `coderai providers --help`, `coderai models --help`, and `coderai permission-default --help` for local diagnostics and configuration. Resume rehydrates engine state and replays recorded messages and tool activity. `/clear` clears the current model context while preserving the session identity. Project skills and plugin commands are discovered as slash commands; project skills use `/skill:NAME`. CoderAI holds a process lock for each open session, in addition to the engine's persistence locks; concurrent CoderAI writers are rejected, and abandoned locks expire after ten seconds.

`--continue` selects the most recently updated session in the invocation workspace, or starts a new session if none exists. `/resume latest` selects from the current session's workspace.

## Headless use

```sh
coderai -p 'Explain this project'
coderai -p 'Run tests and fix the failure' --yolo
cat task.txt | coderai
cat context.txt | coderai -p 'Explain the following context'
coderai --resume SESSION_ID -p 'Continue'
coderai --json -p 'Explain this project' > events.jsonl
coderai --attach screenshot.png -p 'Explain this image'
coderai sessions export SESSION_ID conversation.md
coderai sessions fork SESSION_ID
coderai --trust sessions import codex /path/to/session.jsonl
coderai mcp add local-server /path/to/server-config.json
coderai --trust mcp list
```

`--attach` can be repeated in headless mode for supported image and video formats; each video can be up to 100 MiB. Media is staged in the engine file store and preserved with the session; Kimi uploads videos to the provider when the model request is made. The selected model and provider must support the input type and format. In the interactive terminal, Ctrl+V (Alt+V on Windows) stages clipboard images or copied media files for the next prompt; The optional native clipboard binding is included in standalone builds; Linux also supports `wl-paste`, `xclip` and a Windows clipboard fallback on WSL. MCP add and update accept a JSON object for one server; project servers are shown only after workspace trust and are read-only in this command. HTTP servers may use `bearerTokenEnvVar` or headers; browser-based MCP OAuth is not exposed. Session import accepts `codex` or `claude-code` JSONL files, copies source data under `CODERAI_HOME`, and never modifies the source. Imported context may be truncated to fit the model; conversion issues are reported. Exports include persisted conversation and tool activity, and do not overwrite existing files. `--agent-file` accepts one explicit agent profile for a new session and cannot be combined with `--agent`. Explicit additional directories grant session access and do not change workspace trust.

When both are supplied, the explicit prompt comes first, followed by a blank line and trimmed piped input. Empty piped input without an explicit prompt is an error; stdin is capped at 8 MiB. Headless text output contains assistant text on stdout; tool activity, session hints and diagnostics go to stderr.

`--json` emits one JSON object per line: `coderai.session`, the SDK's streaming events (`turn.started`, `assistant.delta`, `tool.call.started`, `tool.result`, usage and errors), and a final `coderai.result` with `reason` and `exitCode`. Approval denials additionally emit `coderai.approval`. Startup diagnostics use stderr and may occur before any JSON event. Consumers should tolerate additional engine event types. No terminal rendering or progress text is mixed into JSON stdout.

Exit codes: **0** completed, **1** failure/configuration error/required approval denied or background wait timed out, **3** goal blocked, **6** goal paused, **130** cancellation. For a goal, the final JSON result also includes `goalStatus`. A denied tool can be followed by an assistant explanation, but the invocation still exits 1. Headless approvals are rejected immediately; headless user questions are dismissed. `--yolo` explicitly opts into broad automatic approval. User-configured denials and upstream dangerous-command/sensitive-file checks retain precedence, so a guarded action can still be denied in headless mode.

## Permissions, trust and storage

Workspace trust and individual tool approval are separate. CoderAI requires a trusted workspace before opening a conversation: at interactive startup, the workspace trust window shows the directory and what trusting it allows. Choose **Trust and continue** with ↑/↓ and Enter, type `1` then Enter, or type `yes` then Enter. **Exit without trusting** is selected by default; Enter or Esc exits. Long details scroll with PgUp/PgDn. Declining trust while switching workspaces keeps the current conversation open. Headless mode preserves existing trust and requires explicit `--trust` for a new workspace. Project instructions, skills, agent profiles and project MCP configuration can affect the agent; only trust projects you control. Resuming another workspace checks its trust too.

Default `manual` mode follows Kimi's policy: ordinary reads and searches can be approved automatically, including outside the workspace and through symlinks. In a Git worktree on POSIX systems, eligible writes and edits within the workspace or additional directories can also be approved automatically; other writes and shell commands generally require approval unless another rule authorizes them. Sensitive file filtering, argument validation, execution limits, output truncation and tool error reporting remain in the engine. Bash has normal host access once approved; this is not an OS sandbox. Prompt instructions cannot override application permission checks. Startup does not run the user's login shell to enrich PATH.

All application-owned data defaults to **`~/.coderai`**: `config.toml`, `oauth/`, `logs/`, `locks/`, engine `sessions/`, workspace/session indexes, `bin/` caches, and trust/plugin settings. `CODERAI_HOME` selects an isolated alternative home and rejects the usual Kimi data directories. Project configuration uses `.coderai/` (for example `.coderai/mcp.json`, `.coderai/agents/`, `.coderai/skills/`). Applicable `AGENTS.md` instructions, environment context, tools, conversation history, reminders and compaction are assembled by Kimi's retained prompt pipeline. Tool descriptions and prompt templates are embedded during build.

CoderAI sets the engine's home explicitly; it does not read, migrate or write `~/.kimi` or `~/.kimi-code` application data. No cloud telemetry appender is installed, engine telemetry defaults off, and `telemetry = true` is rejected. Provider requests still include required Kimi client identity/device headers for authentication compatibility.

## Architecture and development

```text
CLI / pi-tui presentation
  → adapted TypeScript node-sdk
  → klient memory transport
  → CoderAI agent-core-v2 (DI scopes, state machines, tools and journals)
  → model providers and host file/process APIs
```

The CLI never calls a provider directly. The retained agent cycle, validated tool results, retry logic, steering, context management, compaction, cancellation and persistence remain intact. The CLI adds terminal presentation, headless output, configuration validation and session ownership locks.

Retained packages: `node-sdk`, `agent-core-v2`, `klient` (memory facade), `kosong`, `oauth`, `product` (SDK rating/config types), `minidb` (persistence plus its packaged text-index worker), `tree-sitter-bash`, and `pi-tui`. Workspace packages use the `@coderai` scope. Serialized engine identifiers retain upstream names for compatibility, and the SDK retains aliases for its original harness exports. The host is a focused replacement for `apps/kimi-code`; it reuses pi-tui's editor, Markdown rendering, terminal lifecycle and input patterns.

Excluded: Kimi's browser bundles and application host, extension, kap-server, ACP server, remote-control package, standalone visual inspectors, migration applications, telemetry transport package, klient IPC transport, native clipboard binaries, MiniDb's standalone server, and upstream packaging/release machinery. The separate `transcript` package is unnecessary for this SDK event/context rendering path. No product HTTP/WebSocket/ACP interface is started. Optional configured MCP programs remain an engine feature, subject to workspace trust.

```sh
npx --yes pnpm@10.33.0 dev          # rebuild and run interactive CLI
npx --yes pnpm@10.33.0 typecheck    # all retained source + host
npx --yes pnpm@10.33.0 build        # executable, embedded prompts, worker, licenses
npx --yes pnpm@10.33.0 test         # build, deterministic engine/tool tests and PTY tests
npx --yes pnpm@10.33.0 start --help
```

Tests run real engine/provider adapters against a local deterministic test endpoint and use temporary workspaces/homes. They exercise file/search/edit/shell results, permissions, trust, session persistence, concurrent writers, cancellation and terminal restoration. No model credentials are needed. The test endpoint is test infrastructure, not part of CoderAI's runtime architecture.

Known limits: live-provider inference was not verified because no credentials were available. Provider tool and media paths beyond Kimi and OpenAI and remote MCP authentication have not all been exercised end to end here. A deterministic test covers automatic compaction before a resumed turn. Clipboard media is staged for the next prompt; the terminal does not yet reproduce Kimi’s inline media previews and editing controls. The compact terminal host omits many of Kimi's richer dialogs, mouse panes, updates, remote integrations and slash commands. Background commands belong to the current process and stop when it exits. Windows/Linux runtime support is inherited but unverified; automated PTY tests target POSIX.

Provider configuration can be managed with CLI commands, edited in TOML, or supplied by environment variables; there is no interactive OAuth provider manager. The JSON event stream is CoderAI's own format, not Kimi's `stream-json` protocol. Task output is text-based, and commands submitted during a turn wait in the queue. Use `/attach PATH` or clipboard paste to stage media for the next interactive prompt; `/attach list`, `/attach clear`, and `/attach remove NUMBER` manage staged files. Use `coderai providers`, `coderai models`, `coderai mcp`, `coderai permission-default`, and `coderai doctor` for local configuration and diagnostics; other settings can be edited in TOML. Imported session context is a textual conversion and may omit unsupported source events. Use `--wait-background` in headless mode to wait for background tasks for up to five minutes; completion does not start another model turn.

## License

Based on [Kimi Code](https://github.com/MoonshotAI/kimi-code), copyright © 2026 Moonshot AI, MIT licensed. The pi-tui fork includes [pi](https://github.com/earendil-works/pi), copyright © 2025 Mario Zechner, MIT licensed. The embedded WebP decoder includes jSquash and libwebp. See [LICENSE](LICENSE), [NOTICE](NOTICE), [pi-tui's license](packages/pi-tui/LICENSE), and [third-party licenses](third-party-licenses). Notices are included in the built output. Other npm dependencies retain their own licenses.

### Custom terminal themes

Place JSON files in `~/.coderai/themes/` (or `themes/` under `CODERAI_HOME`). The filename is the name used by `/theme`; the picker rescans the directory each time. For example, `ember.json`:

```json
{
  "name": "ember",
  "base": "dark",
  "colors": { "primary": "#FE8019", "textMuted": "#A89984" }
}
```

The optional `base` is `dark` or `light` (default: `dark`). Supported color tokens are `primary`, `textMuted`, `success`, `error`, and `warning`; omitted or invalid colors use the base palette. `auto` uses the terminal's `COLORFGBG` setting when available, and otherwise uses dark colors. Changes to an active custom file take effect when it is selected again.
