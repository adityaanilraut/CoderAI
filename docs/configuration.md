# Configuration

Legacy settings combine user settings and trusted project settings, with process
`CODERAI_*` overrides taking precedence. Global MCP seed, user/project server
entries, and repeatable CLI MCP overlays merge in that order. Untrusted projects
cannot supply executable hooks, MCP commands, or provider endpoints paired with
user credentials. Project `notify` is ignored even when trusted; use user settings
or `CODERAI_NOTIFY` for notification executables.

Typed provider/model config coexists with legacy settings; it is not a single
unconditional overlay. An explicit `--config-file` / `CODERAI_CONFIG_FILE` selects
that file; otherwise discovery checks project `.coderai/config.toml`,
`~/.config/coderai/config.toml`, then the share-directory `config.toml`.
`--config` / `CODERAI_CONFIG_STRING` supplies inline JSON. Model selection and
whether the typed config is project-scoped determine its legacy overlay;
[configuration regressions](../tests/test_hierarchical_config.py) cover this boundary.

User `.env` loads before trusted project `.env`; neither replaces values already
in the process environment. It supplies missing environment values rather than
forming a higher-precedence configuration layer. CLI workspace/skill directories
and MCP overlays have their own merging rules.

Share directory: `CODERAI_SHARE_DIR` overrides, otherwise `~/.coderai`.
Device identity and telemetry spillover use the share dir. Session storage
normally lives in `<project>/.coderai/sessions/`, with a legacy global fallback;
see the [session persistence reference](../coderai/skills/coderai-self-refer/references/session-persistence.md).

---

## Providers

Provider credentials are managed with the `--setup` family of flags:

```bash
coderai --setup                                   # interactive wizard
coderai --setup --provider openai --key sk-...    # save a key non-interactively
coderai --setup --provider ollama --base-url http://localhost:11434/v1
coderai --setup --provider anthropic --key sk-ant-... --setup-model claude-3-7-sonnet
coderai --setup --status                          # show credential/config table
coderai --setup --test                            # test connection + auth
coderai --setup --project                         # save to project instead of user global
coderai --setup --global                          # save to ~/.coderai explicitly
```

Supported provider names include `openai`, `deepseek`, `gemini`, `anthropic`,
`openrouter`, and `jev`; local presets include `ollama` (see setup help). Under the hood each entry
carries a `type` selecting the wire client:

| `type` | Protocol |
|---|---|
| `openai_legacy` | OpenAI Chat Completions API (default) |
| `openai_responses` | OpenAI Responses API |
| `anthropic` | Anthropic Claude API (`provider_type "anthropic"` in `coderai/llm.py`) |
| `gemini` / `google_genai` | Google Gemini API |
| `kimi` | Kimi coding API |
| `vertexai` | Google Vertex AI |

Per-invocation overrides:

```bash
coderai --model gpt-4o
coderai --model claude-3-7-sonnet
CODERAI_MODEL=gpt-4o CODERAI_API_KEY=sk-... coderai -p "hello"
```

`CODERAI_BASE_URL` redirects the endpoint (local proxies, gateways);
`CODERAI_PROVIDER_<TYPE>_BASE_URL` / `CODERAI_PROVIDER_<TYPE>_API_KEY`-style
overrides exist per provider type (see `coderai/llm.py`).

### Legacy Kimi / Moonshot routing

For Kimi/Moonshot model-name routing, credential precedence remains dedicated
`KIMI_API_KEY`, dedicated `MOONSHOT_API_KEY`, Kimi OAuth, then a non-project
explicit key. Within each dedicated key, the supplied environment mapping wins
over the process environment. `sk-proj-` explicit keys are rejected and ambient
`OPENAI_API_KEY` is never a fallback to the Kimi endpoint. Configure a dedicated
key or OAuth instead. Typed providers retain their explicit credential policy.

### Reasoning effort

Reasoning models expose `thinking_effort` (kosong `ThinkingEffort`):

```bash
coderai --thinking -p "design this carefully"     # enable thinking this run
coderai --no-thinking -p "quick answer"           # disable thinking this run
```

---

## API keys

Keys live in the settings store (user-global by default, `--project` for
per-repo). Prefer the wizard over hand-editing JSON:

```bash
coderai --setup --provider deepseek --key $DEEPSEEK_KEY
coderai --setup --status   # confirm what is configured
coderai --setup --test     # confirm the key actually authenticates
```

Settings and OAuth token JSON writers pass mode `0600` when creating their
temporary files, before replacement; they also retain the final private mode.
This applies to new files and replacement of an older public file.

Never commit keys: project settings (`<root>/.coderai/settings.json`) are
inside the repo — use `--global`/user settings for secrets, and keep
`.env` out of version control.

---

## Custom endpoints

```bash
# Local Ollama-compatible server
coderai --setup --provider ollama --base-url http://localhost:11434/v1

# One-shot override without persisting
CODERAI_BASE_URL=http://localhost:11434/v1 CODERAI_MODEL=qwen3:8b coderai -p "hi"
```

---

## Default models

```bash
coderai --setup --provider openai --setup-model gpt-4o   # persist default
coderai --model gpt-4o -p "..."                           # one-shot override
```

`--setup --status` shows the active default per provider.

---

## Reasoning effort levels

Thinking is a tri-state per invocation: `--thinking` forces it on,
`--no-thinking` forces it off, and omitting both keeps the configured
default. Inside a session, thinking-capable models stream their reasoning
through the terminal renderer like any other chunk.

---

## Permission presets

`--preset` selects the tool platform preset at launch:

```bash
coderai --preset full         # everything (default)
coderai --preset core         # core file/shell/search tools
coderai --preset shell_edit   # shell + editing focused
```

Related run-mode flags:

| Flag | Effect |
|---|---|
| `--yolo` (`--yes`, `-y`, `--auto-approve`) | Auto-approve all tool actions (still reachable via AskUserQuestion) |
| `--afk` | Auto-pilot: `AskUserQuestion` auto-dismissed, tool calls auto-approved |
| `--plan` | Start in Plan Mode (read-only architectural planning) |
| `--print` | Non-interactive single shot (implies AFK for the invocation) |
| `--quiet` / `-q` | `--print` with minimal output (final message only) |

In-session toggles: `/yolo`, `/afk`, `/plan`. In Plan Mode, mutating scopes
stay prompt-gated even with `--yolo`.

---

## JEV System-One Configuration

CoderAI incorporates a hybrid **Kahneman System 1 + System 2** compound review harness (`coderai/triage/`, `coderai/jev/`). Jev System-One provides sub-second non-autoregressive diff screening and confidence gating, cutting review false positives and delivering a **+13% precision lift** on the Martian Code Review Benchmark.

### Activation & Credentials

Jev activates automatically when `TYPESAFE_API_KEY` is present in the environment, `.env`, or configured via setup:

```bash
# In shell environment or .env
export TYPESAFE_API_KEY="ts-..."

# Or via interactive setup
coderai --setup
```

To install the optional TypeSafe SDK dependencies:
```bash
pip install -e '.[jev]'
# or
pip install 'coderai-agent[jev]'
```

### Environment Variables & Tunables

| Variable | Type | Default | Description |
|---|---|---|---|
| `TYPESAFE_API_KEY` | String | None | Authentication key for the TypeSafe Jev System-One API. |
| `CODERAI_JEV_GATE_THRESHOLD` | Float (`0.0`–`1.0`) | `0.40` | Minimum `is_actionable_bug` probability required to approve a review comment in Tier 3. |
| `CODERAI_JEV_SPECULATIVE_THRESHOLD` | Float (`0.0`–`1.0`) | `0.65` | Maximum ceiling for `is_speculative_or_nit`. Comments exceeding this are suppressed. |
| `CODERAI_JEV_ACCEPT_THRESHOLD` | Float (`0.0`–`1.0`) | `0.50` | Minimum `will_developer_accept` probability required to approve a comment. |
| `CODERAI_JEV_TRIAGE_THRESHOLD` | Float (`0.0`–`1.0`) | `0.35` | Minimum risk score to qualify a diff hunk for deep review in Tier 1. |
| `CODERAI_JEV_CACHE_SIZE` | Integer | `1024` | Capacity of the thread-safe LRU in-memory query cache. |
| `CODERAI_JEV_MAX_DIFF_CHARS` | Integer | `12000` | Maximum diff characters sent per file hunk (prevents oversized payloads). |
| `CODERAI_JEV_TIMEOUT_S` | Float | `3.0` | Timeout in seconds for Jev API calls before falling back to System 2 (minimum `0.5`). |

### Secret Path Sanitization

CoderAI enforces zero-egress protection for sensitive credentials. Files matching sensitive path patterns (`.env*`, `*.pem`, `*.key`, `*credential*`, `*secret*`, `*id_rsa*`) are **never** transmitted to the Jev API; they are automatically reviewed by the local System 2 model, and their comments are always shown.

For architectural details, calibration benchmarks, and workflow diagrams, see [docs/jev-system-one.md](jev-system-one.md).

