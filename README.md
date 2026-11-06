# CoderAI

CoderAI is a terminal coding assistant built with TypeScript and Bun on the Kimi Code engine. It can explore and edit a codebase, run commands and tests with your approval, and resume conversations across terminal sessions.

## Install and run

Starting with **0.6.0**, CoderAI is distributed as standalone executables through [GitHub Releases](https://github.com/adityaanilraut/CoderAI/releases). The Python edition and its `pip` installation instructions are available on the [legacy branch](https://github.com/adityaanilraut/CoderAI/tree/old-python-stable-legacy).

### Standalone download

1. Download the `coderai-bun-<platform>-<architecture>.zip` archive for your machine from the 0.6.0 or later release. Each ZIP has a matching `.sha256` checksum file.
2. Extract the archive and put `coderai` (`coderai.exe` on Windows) in a directory on your `PATH`. The executable embeds Bun.
3. Install `rg` (ripgrep) for file searches. CoderAI uses Bash to run shell commands. On Windows, install Git for Windows for Git Bash.
4. Run `coderai --version` to check the installation, then run `coderai --setup` to configure a model provider.

If you also use the Python edition, set `CODERAI_HOME` to a separate directory such as `~/.coderai-bun` before setting up the Bun edition. Both editions use `~/.coderai` by default, with different settings and session formats.

### Run from source

Install Bun **1.4.4**, ripgrep and Bash (Git Bash on Windows). Python 3 is used by the POSIX terminal tests.

```sh
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI
bun install --frozen-lockfile
bun run build
bun run start --version
bun run start --setup
bun run start
```

### Build a standalone executable

Build a single executable with the Bun runtime embedded on the target machine:

```sh
bun install --frozen-lockfile
bun run build:native
```

The executable is written to `dist-bun/<platform>-<architecture>/coderai` (`coderai.exe` on Windows). The ZIP and its SHA-256 checksum are in `dist-bun/`. Use `bun run test:native` to build and exercise the standalone CLI on the current host.

The standalone workflow builds and tests each platform on GitHub Actions. Pushing a version tag that matches `package.json` (for example, `v0.6.0`) prepares a draft GitHub Release with the tested ZIPs and their checksums. Publish the draft from GitHub when the release is ready.

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

The compact welcome panel shows your project, current model, and useful commands. Long paths keep their final directory visible. If no usable model is configured, the terminal stays open: use `/setup` to connect one and start chatting without restarting.

Enter sends a prompt; Shift+Enter or Alt+Enter inserts a newline (terminal support varies). Bracketed multiline paste and input history are supported by the upstream editor. Slash commands, supported command arguments, and file paths have completion; command suggestions show argument hints. Assistant text streams through pi-tui's Markdown renderer, with highlighted code and visible tool activity. Input submitted while work is running is queued; `/btw QUESTION` opens a read-only side panel immediately, including during the main turn. Follow-up text goes to the side conversation until Esc or `/btw close`. A queue area above the input shows pending work; `/queue` inspects or removes queued input and `/steer TEXT` redirects the current turn. The `/undo` selector offers prompts after the most recent compaction, then lets you withdraw prompts alone or restore their tracked file snapshots as well. `/diff [turn]` shows recorded file changes using zero-based engine turn IDs. Shift+Tab toggles plan mode when idle. Ctrl+G opens the current draft in `$VISUAL` or `$EDITOR`, or in the command selected with `/editor`.

Prefix a command with `!` to run it directly in the session's shell, for example `!git status`. Esc or Ctrl+C cancels active work.

Help, selections, questions, and plan reviews open in framed dialogs. Use ↑/↓ and Enter to choose, PgUp/PgDn to page or scroll a long preview, and Esc to return to your draft. `/help` groups commands by purpose. `/model` searches providers and models, marks the current model, and shows context limits and supported capabilities; **Add provider or model…** opens guided setup and returns to the picker afterward. `/setup` opens that same setup directly.

The `/sessions` picker starts in the current workspace. **Ctrl+A** switches between that workspace and all workspaces, **Ctrl+R** renames the selected conversation, and **Ctrl+X** offers deletion with confirmation. The open conversation cannot be deleted. Rows show relative update times and a current-session marker. Submitted prompts are recalled across restarts with ↑/↓; history is stored separately for each workspace under `~/.coderai/input-history/`, with up to 100 entries of at most 32 KiB each. Slash commands and dialog answers are not persisted in prompt history.

Live and resumed conversations share the same message layout. Tool cards update in place with progress and completion; consecutive file reads collapse into a group. Successful output stays collapsed, while failed actions show an error preview. Thinking shows up to three live rows and collapses after completion. **Ctrl+O** expands or collapses tools and thinking. Diff previews include separate change hunks, old/new line numbers, context, and colored additions/deletions. Very expensive comparisons show an explicit omission notice.

The footer keeps the model, workspace, active modes, and context percentage in two lines; `/status` shows exact counts and settings. In Git workspaces it also shows the branch (or detached commit) and `clean`/`dirty` state, including staged and untracked files. Git status refreshes asynchronously every five seconds and when session status refreshes, with a two-second probe timeout; non-Git workspaces omit the badge. Live activity appears above the editor with the current action and elapsed time. Provider retries show the attempt count, remaining delay, and provider message. Errors offer recovery guidance, with diagnostic details available through Ctrl+O. Todo updates use cards with active, pending, and completed markers; the current list stays above the editor, long cards expand with Ctrl+O, and `/todos` prints the full list. Completed lists shrink to a summary, and clearing the list removes it from the pinned area.

Staged attachments appear above the composer with numbered filenames and detected media types. Ctrl+T opens a removal picker while preserving your draft; `/attach remove NUMBER` and `/attach clear` also work. Compact terminals show two filenames and a count of additional files; `/attach list` lists every path. The first supported image gets a bounded preview on Kitty/iTerm2-compatible terminals. Unsupported terminals, formats, unavailable files, and images above the 4 MiB preview limit retain readable text labels. Kitty previews use PNG; iTerm2 previews support PNG, JPEG, and GIF. Preview limits do not change the media accepted for submission.

Ctrl+B or `/tasks` opens background tasks and retained agent activity. Agent and AgentSwarm cards show child text, thinking, tool activity, status, model and context usage as they arrive. Select an agent to inspect its recent steps and tool results. Activity stays in memory and clears when switching sessions. For process tasks, `/output TASK_ID` opens captured output directly. The dedicated output dialog polls through the SDK while open, keeping large logs out of the conversation. Use ↑/↓ or PgUp/PgDn to read, Home for the beginning of captured output, and End or F to follow new output. Scrolling up freezes the reading snapshot until you resume following, including when the SDK output buffer rotates. Esc, Q, or Enter returns to the conversation and restores your draft. Task browsing and output remain available during an agent turn. The viewer requests at most the last 1 MiB of characters available from the SDK; older output may already have been discarded by the engine.

The approval window shows the action and a scrollable preview. Use ↑/↓ and Enter, or type a number from 1–5 and Enter, to approve once, remember matching actions for the session, enable YOLO, reject with feedback, or reject. PgUp/PgDn scrolls long previews; Esc rejects. Rejection is selected by default. Typed `yes`, `always`, and `no: <feedback>` replies also work. Start with `coderai --yolo` to auto-approve routine actions, or use `/yolo` to toggle it during a conversation. `/yolo on`, `/yolo off`, and `/yolo status` explicitly enable, disable, or inspect it. Explicit denials and guarded checks still apply in YOLO mode. Plan reviews and single-choice questions use a keyboard selector. Ctrl+C cancels active work; when idle it exits. Ctrl+D on empty input and `/quit` save and exit. SIGTERM cancels and shuts down. Shutdown stops owned processes, drains session state and releases locks, with an eight-second emergency shutdown ceiling.

| Command | Behavior |
| --- | --- |
| `/help` | Browse grouped commands and shortcuts |
| `/new` | Create a saved conversation |
| `/sessions`, `/resume`, `/resume ID`, `/resume latest` | List or reopen conversations; `/resume` opens a picker |
| `/title [TITLE]`, `/status` | Show or change the title; inspect session status |
| `/model`, `/model ALIAS` | Choose or select a configured model |
| `/setup` | Add providers and models with guided setup, then return to the conversation |
| `/yolo [on\|off\|status]` | Toggle, enable, disable, or inspect YOLO automatic approval |
| `/permission` | Choose approval mode for this session, optionally saving it as the default for new sessions |
| `/permissions`, `/permissions manual`, `/permissions yolo`, `/permissions auto` | Inspect or change approval mode |
| `/context`, `/compact`, `/clear` | Inspect usage, summarize context, or clear history |
| `/plan`, `/plan show` | Toggle planning mode or show the current plan |
| `/swarm [on\|off\|status]`, `/swarm TASK` | Toggle Swarm, inspect it, or run a one-shot task; manual mode offers a permission choice |
| `/tower [status\|on [BASE]\|off\|teardown\|BASE]` | Control Tower; teardown asks the agent to invoke the engine’s guarded teardown tool |
| `/secondary-model [ALIAS]`, `/subagent-model` | Save the default model for newly spawned subagents |
| `/settings`, `/config`, `/reload-tui` | Configure or reload terminal preferences |
| `/experiments`, `/experimental` | Inspect engine flags; Space toggles, Enter applies, Esc discards |
| `/cron [list\|delete ID]`, `/agents`, `/hooks`, `/doctor` | Inspect scheduled work, agent profiles, configured hooks, and local diagnostics |
| `/goal OBJECTIVE`, `/goal status`, `/goal pause`, `/goal resume`, `/goal cancel`, `/goal next` | Run or manage an autonomous goal and its queue |
| `/tasks`, `/stop TASK_ID` | Browse live agent activity and background commands |
| `/output TASK_ID` | Open captured output with paging and live following |
| `/effort LEVEL`, `/mcp`, `/skills`, `/add-dir PATH` | Configure thinking or inspect and extend the session; choose whether to remember added directories. Quote paths with spaces |
| `/plugins`, `/plugins info ID`, `/plugins mcp enable\|disable ID SERVER` | Browse plugins, inspect diagnostics and toggle plugin MCP servers |
| `/reload`, `/todos`, `/version` | Refresh session configuration, inspect todos or show the version |
| `/diff [turn]` | View unified diffs from engine snapshots for a recent turn |
| `/fork`, `/undo [count]`, `/export-md PATH` | Fork, withdraw prompts with optional file restoration, or export the persisted conversation |
| `/export-debug [PATH]` | Export a diagnostic archive for the current session |
| `/theme [auto\|dark\|light\|name]` | Choose and save terminal colors |
| `/attach PATH`, `/attach list`, `/attach clear` | Stage media for the next prompt |
| `/btw [QUESTION\|close]` | Ask an in-session side question, send follow-ups, or close the panel |
| `/auto-title [on\|off]` | Configure automatic session title generation |
| `/copy` | Request that the terminal copy the last assistant message using OSC 52 |
| `/editor COMMAND` | Save the external editor command; `default` restores `$VISUAL` / `$EDITOR` |
| `/steer TEXT` | Redirect active work through the engine's steering queue |
| `/quit` | Save and exit |

`coderai sessions` (or `coderai sessions list`) lists the current workspace without entering the terminal UI. Add `--all` for every workspace, `--archived` to include archived conversations, `--limit N` to cap results, or `--json` for session summaries. `sessions rename ID TITLE` and `sessions delete ID` manage saved conversations. `sessions fork ID --turn N --title TITLE` keeps history through a zero-based turn; `/fork [turn]` uses the same indexing. `sessions export ID [PATH] --zip` creates a diagnostic archive; Markdown export requires a path. Use `coderai import-sessions` or `/import-sessions` (alias `/import`) to discover local Claude Code and Codex sessions from the last 15 or 30 days, importing up to the newest 100 per run. Completed imports are skipped on reruns; Esc stops after the current session and keeps completed imports. Use `coderai doctor`, `coderai providers --help`, `coderai models --help`, and `coderai permission-default --help` for local diagnostics and configuration. Resume rehydrates engine state and replays recorded messages and tool activity. `/clear` clears the current model context while preserving the session identity. Project skills and plugin commands are discovered as slash commands; project skills use `/skill:NAME`. CoderAI holds a process lock for each open session, in addition to the engine's persistence locks; concurrent CoderAI writers are rejected, and abandoned locks expire after ten seconds.

`--continue` selects the most recently updated session in the invocation workspace, or starts a new session if none exists. `/resume latest` selects from the current session's workspace.

## Terminal themes

Place JSON files in `~/.coderai/themes/` (or `themes/` under `CODERAI_HOME`). The filename is the name used by `/theme`; the picker rescans the directory each time. For example, `ember.json`:

```json
{
  "name": "ember",
  "base": "dark",
  "colors": { "primary": "#FE8019", "textMuted": "#A89984" }
}
```

The optional `base` is `dark` or `light` (default: `dark`). Supported color tokens are `primary`, `textMuted`, `success`, `error`, and `warning`; omitted or invalid colors use the base palette. Setup menus, conversation chrome, and syntax highlighting share these tokens. Changes to an active custom file take effect when it is selected again.

The default theme is `auto`. The conversation interface and standalone setup query the terminal background through OSC 11 without blocking startup, with a 200 ms deadline. A valid reply selects the light or dark palette; unsupported, malformed, or late replies leave the fallback palette in place. The fallback uses `COLORFGBG` when valid, otherwise dark. Explicit `dark`, `light`, and custom themes are respected and skip detection. Setup uses the same saved theme and palette as the conversation; its first menu also checks the background when automatic detection is enabled. Terminal color replies are kept out of the draft, including delayed fragments.

Rendering and terminal tests cover 50×16 layouts, longer paths, light/dark palettes, resizing, setup cancellation and first-run setup, and draft preservation. The standard suite also verifies background output navigation, Git status, attachment previews/fallbacks, and terminal escape sanitization.

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

`--attach` can be repeated in headless or interactive mode for supported image, video and audio formats; interactive attachments are staged for the next prompt and can be inspected with `/attach list`; each video can be up to 100 MiB. Media is staged in the engine file store and preserved with the session; Kimi uploads videos to the provider when the model request is made. The selected model and provider must support the input type and format. In the interactive terminal, Ctrl+V (Alt+V on Windows) stages clipboard images or copied media files for the next prompt; The optional native clipboard binding is included in standalone builds; Linux also supports `wl-paste`, `xclip` and a Windows clipboard fallback on WSL. MCP add and update accept a JSON object for one server; project servers are shown only after workspace trust and are read-only in this command. HTTP servers may use `bearerTokenEnvVar` or headers; Use `coderai --trust mcp auth NAME` or `/mcp auth NAME` for browser-based OAuth. `mcp auth NAME --no-browser` prints the URL for manual opening. `/mcp logout NAME` or `coderai --trust mcp logout NAME` removes the saved grant. Closing the sign-in dialog cancels authorization. Session import accepts `codex` or `claude-code` JSONL files, preserves user/assistant turns and source metadata, copies source data into the saved session’s `attachments/` directory under `CODERAI_HOME`, and never modifies the source. Discovery honors `CLAUDE_CONFIG_DIR` and `CODEX_HOME`; Codex imports retain database thread titles and copies of descendant subagent rollouts. Imported context may be truncated to fit the model; conversion issues are reported. Exports include persisted conversation and tool activity, and do not overwrite existing files. `--agent-file` accepts one explicit agent profile for a new session and cannot be combined with `--agent`. Explicit additional directories grant session access and do not change workspace trust.

When both are supplied, the explicit prompt comes first, followed by a blank line and trimmed piped input. Headless input must contain a prompt with at least one non-whitespace character. Standard input is capped at 8 MiB. Headless text output contains assistant text on stdout; tool activity, session hints and diagnostics go to stderr.

`--json` emits one JSON object per line: `coderai.session`, the SDK's streaming events (`turn.started`, `assistant.delta`, `tool.call.started`, `tool.result`, usage and errors), and a final `coderai.result` with `reason` and `exitCode` after a streamed turn. Approval denials additionally emit `coderai.approval`. Startup diagnostics use stderr and may occur before any JSON event. The headless goal status, pause, and cancel commands emit a single `goal.summary` on success. Consumers should tolerate additional engine event types. No terminal rendering or progress text is mixed into JSON stdout.

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

Excluded: Kimi's browser bundles and application host, extension, kap-server, ACP server, remote-control package, standalone visual inspectors, migration applications, telemetry transport package, klient IPC transport, MiniDb's standalone server, and upstream packaging/release machinery. The separate `transcript` package is unnecessary for this SDK event/context rendering path. No product HTTP/WebSocket/ACP interface is started. Optional configured MCP programs remain an engine feature, subject to workspace trust.

```sh
bun run dev          # rebuild and run interactive CLI
bun run typecheck    # all retained source + host
bun run build        # executable, embedded prompts, worker, licenses
bun run test         # build and test the Bun CLI, including POSIX PTYs
bun run test:native # build and test the standalone Bun executable
bun run start --help
```

Tests run real engine/provider adapters against a local deterministic test endpoint and use temporary workspaces/homes. They exercise file/search/edit/shell results, permissions, trust, session persistence, concurrent writers, cancellation and terminal restoration. No model credentials are needed. The test endpoint is test infrastructure, not part of CoderAI's runtime architecture.

Known limits: live-provider inference requires credentials and is not covered by the deterministic suite. Provider tool/media paths beyond Kimi and OpenAI and remote MCP authentication have not all been exercised end to end. Inline previews depend on terminal and format support, as described above. Automatic updates and remote application integrations are outside this terminal host. Background commands stop when CoderAI exits; detached process retention and live reattachment are not supported by this host. Automated PTY checks require a POSIX host. Windows CI checks the built CLI and runs standalone headless smoke tests in the native build workflow. Native executable checks run on the build host.

Provider configuration can be managed with CLI commands, edited in TOML, or supplied by environment variables; there is no interactive OAuth provider manager. The JSON event stream is CoderAI's own format, not Kimi's `stream-json` protocol. Task output is text-based. Prompts submitted during a turn wait in the queue; `/btw` runs in a separate agent in the current session; task browsing, output viewing, and stopping tasks remain available immediately. Use `/attach PATH` or clipboard paste to stage media for the next interactive prompt; `/attach list`, `/attach clear`, and `/attach remove NUMBER` manage staged files. Use `coderai providers`, `coderai models`, `coderai mcp`, `coderai permission-default`, and `coderai doctor` for local configuration and diagnostics; other settings can be edited in TOML. Imported session context is a textual conversion and may omit unsupported source events. Use `--wait-background` in headless mode to wait for background tasks for up to five minutes; completion does not start another model turn.

## License

Based on [Kimi Code](https://github.com/MoonshotAI/kimi-code), copyright © 2026 Moonshot AI, MIT licensed. The pi-tui fork includes [pi](https://github.com/earendil-works/pi), copyright © 2025 Mario Zechner, MIT licensed. The embedded WebP decoder includes jSquash and libwebp. See [LICENSE](LICENSE), [NOTICE](NOTICE), [pi-tui's license](packages/pi-tui/LICENSE), and [third-party licenses](third-party-licenses). Notices are included in the built output. Other npm dependencies retain their own licenses.

## Provider management and media commands

Setup now includes **Manage providers**, **Edit a model**, registry import, and registry refresh. Provider updates preserve model aliases and defaults. Removing a provider from setup requires confirmation and removes its model aliases. Registry-managed providers are reconfigured by reimporting their source.

```sh
coderai providers update example openai https://example.test/v1 EXAMPLE_API_KEY
coderai models update coding example MODEL_ID 128000 image_in
coderai providers import https://example.test/api.json REGISTRY_API_KEY
coderai providers refresh
```

`/provider` opens setup; `/provider refresh` refreshes registry models. `/usage` includes token totals by model. `doctor` explicitly reports that provider connectivity is not tested.

Staged media works with goals, project skills, and plugin commands, including queued submissions. Skills and goals receive media prompt parts; plugin commands receive references to copies stored with the session, matching Kimi's text argument channel. Failed media validation retains staged attachments. Cancelling the session picker retains attachments; successfully switching conversations clears them.

Local configuration diagnostics do not make live provider connection tests. Generic monetary spending limits and cross-process task reattachment remain excluded. Kimi's monetary usage display depends on its account billing service.

Automatic session titles are enabled by default after a completed main turn. Set `auto_session_title = false` in `~/.coderai/tui.toml`, or use `/auto-title off`, to disable them. The runtime also honors `auto_session_title` in the main configuration. Title generation uses the configured SDK title service when available; custom titles are preserved. The UI retries unavailable generation at most three times per session.


## Terminal preferences

`~/.coderai/tui.toml` (or `tui.toml` under `CODERAI_HOME`) owns client preferences. `/settings` and `/config` open the same searchable menu; `/reload-tui` applies external edits without reopening the session. `/reload` refreshes both the engine session and TUI preferences. Reloads retain the transcript, input draft, staged media, and automatic-title preference; a session reload closes stale side panels and clears transient child activity. Failed reloads report an error and can be retried with `/reload` once active work finishes.

```toml
theme = "auto"
tui_mode = "regular"
render_latex = true
disable_paste_burst = false
auto_session_title = true

[editor]
command = "" # Falls back to VISUAL / EDITOR; /editor default clears the override

[notifications]
enabled = true
notification_condition = "unfocused" # or "always"; terminal bell on completed work

[markdown]
mermaid = "final" # Draw completed diagrams; "off" displays source

[status_line]
items = ["mode", "goal", "model", "tasks", "cwd", "git", "tips"]
# command = "~/.coderai/statusline.sh"
```

Set `tui_mode = "fullscreen"` for a scrollable transcript with a docked editor and status area. Restart CoderAI after changing terminal modes; `/reload-tui` applies other preferences live. Fullscreen supports PgUp/PgDn, Home/End, mouse wheel scrolling, text selection and clickable HTTP(S) links. Ctrl+Shift+F opens transcript search; `/` also opens it while scrolled away from the bottom with empty input. Enter and Shift+Enter move through matches; Esc closes search and restores the draft. Mermaid diagrams render after a reply finishes; unsupported, invalid or overly wide diagrams retain their source with a reason. LaTeX, Mermaid, paste detection, editor, notifications, titles, theme, and status slots apply live. A custom status command receives a JSON snapshot on stdin; its first output line replaces the first footer line. Commands run at most once per second with a 300 ms deadline and bounded output; failures retain the last successful line, or the built-in slots if there is none. Unknown preferences are preserved when saving. Invalid TOML is reported and is never silently overwritten. Existing `~/.coderai/theme` files are a migration fallback; `tui.toml` takes precedence, and `/theme` also updates the legacy file for compatibility.

Experiments are resolved by the engine, including environment overrides. Tool Select uses the upstream ID `tool-select` (search also accepts `tool_select`). `full_compaction` defaults on to preserve existing behavior; disabling it stops new automatic/manual compactions and context-overflow recovery without changing saved context. Environment-locked flags are visible but cannot be changed in the selector. Changes persist in `config.toml` and reload the session when needed. Tower’s tool assembly requires restarting CoderAI after its flag changes. Enabling `notify_user` can require a new session; successful `NotifyUser` deliveries appear in the Updates panel in interactive mode. Headless runs do not advertise interactive UI capabilities.

Headless prompts support `/skill:NAME [arguments]`, `/<plugin>:<command> [arguments]`, and `/goal status|pause|resume|cancel`, in addition to goal creation and replacement. Skills and plugin commands activate through the engine, including staged attachments. Goal status, pause, and cancel are deterministic operations with exit code 0 on success; resume runs the goal and retains the goal completion exit codes. Without `--wait-background`, main-turn completion follows the engine print policy (`exit`, `drain`, or bounded `steer`, which is the upstream default); the explicit flag requests a bounded drain. The host bounds idle continuation waits to `task.print_wait_ceiling_s` (or its legacy `background` setting), with a five-minute fallback. Approval-requiring actions are denied in headless mode unless an automatic permission mode was explicitly selected.

## Plugins, MCP and file history

`/plugins` opens a searchable installed-plugin panel with information, enable/disable, removal, per-server MCP toggles and capability readiness/installation. `/plugins capabilities [list|info ID|install ID]` also provides direct access. Capability installs run in the background while the terminal stays open; closing their progress panel retains the install and reopening it follows the same work. Plugin mutations report diagnostics and require `/reload` or `/new` to refresh an existing conversation. CLI management works before configuring a model:

```sh
coderai plugins list
coderai plugins info ID
coderai plugins install /path/to/plugin
coderai plugins enable ID
coderai plugins disable ID
coderai plugins mcp enable ID SERVER
coderai plugins mcp disable ID SERVER
coderai plugins reload
coderai plugins remove ID
coderai plugins capabilities list
coderai plugins capabilities info ID
coderai plugins capabilities install ID
coderai --trust skills list
```

`/mcp add NAME JSON-FILE` validates and saves one user server, connects it and registers its tools in the active conversation. `/mcp remove NAME` removes user configuration and disconnects the live server; `/mcp test NAME` probes a configured server. Sign-in and reconnect remain available. Project and plugin configuration keep their existing ownership rules. SDK `createSession` and `resumeSession` accept `mcpServers` for ephemeral session overlays; explicit reconnect and live additions work with overlays, and `/reload` retains those in-memory overrides. They are not saved to disk and must be supplied again after closing the session. Sessions without an ephemeral overlay retain upstream's shared workspace connection manager: an unpersisted live add can appear in sibling connection lists, though each existing session keeps its own authorized tool inventory.

File history tracks edits made through Write/Edit (including child agents) and records final bytes at turn end. `/diff` uses the latest turn, or accepts an explicit zero-based turn ID. Snapshots retain the last five captured turns; binary changes and oversized/unavailable snapshots are identified explicitly. Undo with file restoration restores the earliest pre-turn bytes for every changed path in the withdrawn suffix, removes files created there, and recreates deleted tracked files. It overwrites subsequent edits to those paths; the selector explains this before you choose. Missing, oversized or expired backups stop restoration before files or prompts change. Shell-only edits are not tracked. Restoration does not run while a turn, queued work or compaction is active.

File mentions use the engine's fuzzy file/folder suggestions with a filesystem fallback when unavailable. Inline `/skill:NAME` completions activate the skill through the engine, including queued prompts, media and `/btw`. Searchable selectors accept fuzzy queries and retain keyboard navigation and Esc cancellation. Multi-question dialogs retain answers, multiple selections and custom-answer drafts when using Esc/left to go back. Multi-question batches end with a review step; Ctrl+C cancels the batch. `/copy` emits OSC 52 for remote terminals and also tries the native clipboard or platform clipboard command, reporting whether local delivery was verified.
