<!-- Source: review agent e0a91a11-7a40-498a-b199-a378231a743a · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# CoderAI infrastructure review (read-only)

The biggest problem is that any cloned repository can run code and capture API keys as soon as `coderai` starts in it. Nothing asks the user whether to trust the project. There are also two other high-severity issues:
- API keys are stored in world-readable files.
- The `--wire` server uses asyncio features that only exist in Python 3.13. The project supports 3.12, and CI only tests 3.12.

I checked every finding with `rg` or by reading the code. Where I say "verified", I also confirmed it on this machine: file permissions with `ls`, and the log-redaction behaviour by running it in Python. I didn't modify any files or run the test suite.

---

## (A) Bugs and security issues

**A1. High: files in a project can run commands and steal keys, with no trust prompt.** Confidence: high.
- **What happens:** nothing in `coderai/` implements workspace trust. When `coderai` starts in a cloned repo:
  - **Arbitrary commands at startup.** `.coderai/settings.json` → `mcpServers.<x>.command` is merged in `config.py:702-749` and started automatically (`soul/session/manager.py:2333` calls `mcp_manager.initialize(...)`).
  - **API key sent to an attacker's host.** A project-level `baseURL` beats the user's (`config.py:409-415`), while `apiKey` still comes from user settings or `OPENAI_API_KEY` (`config.py:417-427`).
  - **Any environment variable can be set.** `load_dotenv` (`config.py:111-140`) copies every key from `<project>/.env` into `os.environ`. That includes `CODERAI_OAUTH_HOST` (`auth/oauth.py:68-74`), which sends the refresh token to an attacker, `CODERAI_TELEMETRY_ENDPOINT`, and variables that every child process inherits.
  - **Other command sources.** A workspace `.coderai/config.toml` (`config.py:1444-1447`) and statusline `command` providers can also supply commands.
- **Impact:** running `coderai` in a malicious repo gives the repo code execution and your key.
- **Fix:** add a per-project trust prompt, stored in the user directory. Until a project is trusted, ignore project-level `command`, `url`, `baseURL`, hooks, statusline providers and `.env`. Never pair a user-scope `apiKey` with a project-scope `baseURL` without asking.

**A2. High: secrets are written world-readable and not atomically.** Confidence: high, verified.
- **Where:** `config.py:97-100` (`_write_settings_file`) and `config.py:1536-1552` (`save_typed_config`) both use `Path.write_text`, which gives mode 0644.
- **What gets written:**
  - `save_provider_api_key` puts keys into `apiKey` and `env.*_API_KEY` (`config.py:981-1024`).
  - `LLMProvider.dump_secret` writes `api_key` to `config.toml` in plain text.
- **Verified here:** `~/.coderai/settings.json` is `-rw-r--r--` and contains 5 API-key fields. `config.toml`, `logs/coderai.log` and `logs/error.log` are also 0644.
- **Crash risk:** the writes aren't atomic. A crash mid-write leaves a torn file, which is then silently read back as `None` (see A13), so every setting is lost.
- **Fix:** use `utils/io.atomic_json_write`. It uses `mkstemp`, which already creates files as 0600. Also set 0700 on `~/.coderai`, and create the log files as 0600.

**A3. High: the wire server breaks on Python 3.12.** Confidence: high. This is based on the API's history; I couldn't run 3.12 here.
- **Where:** `wire/server.py:78, 118, 151, 181` use `asyncio.Queue().shutdown()` and `asyncio.QueueShutDown`, which were added in Python 3.13.
- **Why it matters:** `pyproject` declares `requires-python >=3.12`, and every CI job pins `"3.12"`.
- **What happens:** when stdin closes, the `finally` block in `serve()` raises `AttributeError`. `writer_task` is never awaited, `_active_wire_server` is never cleared, and queued responses can be lost.
- **Fix:** use the repo's own backport, `coderai.utils.aioqueue.Queue` / `QueueShutDown`, which `wire/__init__.py` already uses.

**A4. Medium-High: an SSE MCP server can make CoderAI send its bearer token to another host.** Confidence: high.
- **Where:** in `mcp/transport.py:328-338`, the server's `endpoint` event can name any absolute URL.
- **Why that leaks the token:** `_post_session` sends the configured headers, including `Authorization: Bearer …` from `apply_bearer_auth` (`mcp/client.py:63`).
- **The SSRF check is bypassed:** `check_outbound_url` only checks `self.url` (line 246). `network/security.is_same_origin` exists but isn't used here.
- **Fix:** reject cross-origin endpoints, or re-check them and strip auth headers.

**A5. Medium: MCP servers inherit every secret in the environment.** Confidence: high.
- **Where:** `mcp/transport.py:125-127` passes `dict(os.environ)` to every stdio MCP server.
- **Why it's worse than it looks:** `config.py:1019-1022` and `1099-1103` actively copy the user's key into `OPENAI_API_KEY` / `CODERAI_API_KEY`.
- **Inconsistency:** plugins (`plugin/tool.py:24-27`) and shell tools strip secrets before starting a subprocess; MCP servers don't.
- **Fix:** use `scrub_subprocess_env`, plus the per-server `env` from config.

**A6. Medium: MCP processes are orphaned when a server's config changes.** Confidence: high.
- **Where:** `mcp/manager.py:472` removes the old client for the same server name from `self.clients` without disconnecting it. `sync_servers` (lines 276-283) calls `_connect_server` directly when a config changes.
- **Impact:** the old stdio process is orphaned. Because it was started with `start_new_session=True` (transport line 144), it also outlives the CLI.
- **Also:** `disconnect()` (lines 748-752) stops at the first exception, so any servers after it leak too.
- **Fix:** await `disconnect()` on the old client before replacing it, and wrap each disconnect in `try`.

**A7. Medium: blocking writes and silent failures in the MCP transports.** Confidence: medium-high.
- **Stdio:**
  - `StdioMcpTransport.send` (lines 184-191) writes to the pipe synchronously on the event loop. A server that stops reading freezes the whole event loop.
  - The broken-pipe error is swallowed, so the request only fails after its full timeout.
- **Streamable HTTP:**
  - It ignores the `Mcp-Session-Id` header.
  - It never sets `last_http_status`, so the 401 → "unauthorized" path (`manager.py:564`) can never trigger.
  - It swallows every exception in its POST thread (lines 462-463).
  - It calls `on_message` from a worker thread.
- **Fix:** move writes to a thread or an asyncio subprocess, and pass errors on to the pending futures.

**A8. Medium: the OAuth device-flow polling can hang or loop forever.** Confidence: high.
- **Where:** `auth/oauth.py:383-401` only stops on success or `expired_token`.
- **What's missing:**
  - `access_denied` makes it loop forever inside `to_thread`.
  - `slow_down` is ignored (RFC 8628 §3.5 says to add 5 seconds to the interval).
  - `expires_in` is ignored.
  - `login_device_flow` restarts itself recursively with no limit (lines 638-642).

**A9. Medium: one rejected OAuth token stops refresh for all the others.** Confidence: high.
- **Where:** `auth/oauth.py:573` uses `return` inside the `for key in self._keys` loop in `ensure_fresh`.
- **Fix:** change it to `continue`.

**A10. Medium: the wire server resolves some requests as "allow", fail-open.** Confidence: medium, because the paths are currently latent.
- **Where:** `wire/server.py:464-468` resolves every pending request that isn't an approval or question as `"allow"` and ignores what the client answered. `_shutdown_requests` (lines 719-720) does the same when the client disconnects.
- **HookRequest:** a client's "block" answer, or a disconnect, becomes "allow".
- **ToolCallRequest:** `resolve("allow","")` passes two arguments to a one-argument method (`wire/types.py:350`). The `TypeError` is swallowed, so the tool call hangs forever.
- **Fix:** handle each request type explicitly, and fail closed.

**A11. Medium: `doctor` always reports failed MCP servers as passing.** Confidence: high.
- **Where:** `cli/doctor.py:173` looks for `status == "error"`, but the manager only ever sets `"failed"` or `"unauthorized"` (`manager.py:569, 581`).
- **Also:**
  - `doctor` creates `<project>/.coderai/` as a side effect (line 221).
  - It points users to `.coderai/mcp.json`. The actual config lives in `settings.json` `mcpServers` and `~/.coderai/mcp.json`.

**A12. Medium: the log redactor misses common secret formats.** Confidence: high, verified by running it.
- **Where:** `log.redact_secrets` (`log.py:31-44`).
- **Not masked:**
  - `{"Authorization": "Bearer sk-…"}` when written as JSON.
  - `access_token` and `refresh_token`.
  - Bare `sk-…` keys.
- **Why it matters:** `log_api_error` writes up to 2000 characters of `str(error)` to a 0644 file.
- **Related:** a separate redactor, `utils/common/llm_error.mask_sensitive`, does catch `sk-` keys (see D3).

**A13. Medium: config precedence and loading bugs.** Confidence: high.
- **`project_root` is ignored.** `config.py:162`: `resolve_typed_config_overlay(project_root)` calls `load_typed_config()` without it. The workspace `config.toml` is therefore looked up from the current directory, which is wrong for ACP sessions whose root differs from the cwd.
- **Project config loses to user config.** `config.py:401-423`: `config.toml`, including a project-level one, ranks below the user's `settings.json` for `model`, `baseURL` and `apiKey`. That contradicts the module docstring ("Project wins over user"). Because the setup wizard always writes `model` to `settings.json`, a project's `default_model` never takes effect.
- **Values can come from different layers.** For example, `model` from `settings.json` combined with `baseURL`/`apiKey` from `config.toml`, which gives a mismatched provider.
- **Stale cache.** The cache key at line 1481 only covers the local file, but the loaded data is merged with the global file (lines 1492-1502). Edits to the global file are missed.

**A14. Medium-Low: config parse errors are silent.** Confidence: high.
- **Where:**
  - `_read_settings_file` (`config.py:78-86`) returns `None` on a JSON error. One typo in `settings.json` silently drops all user settings, including permission `deny` lists.
  - The typed-config overlay swallows every `Exception` (line 163).
- **Fix:** print a warning once to stderr.

**A15. Low: loading the typed config writes a file.** Confidence: high.
- **Where:** `load_typed_config` (`config.py:1472-1477`) writes a config file when none exists, including at an explicit `CODERAI_CONFIG_FILE` path.
- **Also:** it isn't cached when the file is missing, so it rewrites on every settings resolution.

**A16. Low: API keys on the command line.** Confidence: high.
- **What happens:** `--key` (`ui/shell/startup.py`) puts the API key in `argv` and shell history.
- **With `--project`:** the key is written into `<repo>/.coderai/settings.json`. This repo's `.gitignore` covers that path, but users' projects usually won't.

**A17. Low: failing to connect to an ACP terminal leaks a pending request, plus a B904 hit.**
- **Where:** `acp/runner.py:158-162` only pops the pending request on timeout, not on cancellation. It also raises inside `except` without `from` (B904).
- **B904 elsewhere:** the hits at `acp/server.py:485` and `mcp/transport.py:313` are cosmetic.
- **B023:** there are no hits in my scope.

---

## (B) Inconsistencies: config, dependencies, docs, entry points

**B1. Medium: `requirements.lock` is stale compared with `pyproject.toml`.** Confidence: high.
- **Wrong direct dependencies.** The lock lists `certifi` and `fastmcp` as coming "via coderai-agent (pyproject.toml)". Neither is declared; pyproject declares `fastmcp-slim[client]`.
- **Imported but not declared:**
  - `certifi`, used directly at `utils/aiohttp.py:312`.
  - `tenacity`, imported without a guard at `soul/coderaisoul.py:21`. It's only available through google-genai.
- **Also:** `loguru` is described as "optional", but kosong always pulls it in.
- **Fix:** declare these, then run `uv pip compile` again.

**B2. Medium: the entry points behave differently.** Confidence: high.
- **Without crash handlers or `set_phase("shutdown")`:** the pip scripts `coderai`/`cai` → `coderai.main:main`, and `python -m coderai`. Both call `ui.shell.app.main` directly.
- **With them:** `python -m coderai.cli` and the PyInstaller binary (`coderai.spec:12` → `cli/__main__.py`).
- **Duplication:** the same call is spread across four shims (`main.py`, `__main__.py`, `cli/__main__.py`, and `cli/__init__.__getattr__`).
- **Fix:** have every path call `cli.__main__:main`.

**B3. Low-Medium: the telemetry setting in config files does nothing.** Confidence: high.
- **Where:** `telemetry_requested` reads `telemetryEnabled`/`telemetry` from the dict passed by `session_factory.py:92`. That dict comes from `resolve_current_settings()`, which never contains those keys.
- **Also unused:** `TypedConfig.telemetry` (`config.py:1350`), and `llm.py:1187` is always `False`.
- **Impact:** only the `CODERAI_TELEMETRY` environment variable works. This fails safe (telemetry is opt-in and stays off), but the setting is broken.

**B4. Low-Medium: `CODERAI_SHARE_DIR` is only partly honoured.** Confidence: high.
- **Honoured by:** `share.py`, which is used for `config.toml`, credentials and the main logs.
- **Hard-coded `~/.coderai` instead:**
  - `config.py:70-71, 115` (`settings.json` and `.env`)
  - `utils/logging.py:39` (`error.log`)
  - `hooks/config.py:136`
  - `soul/session/store.py:56`
  - `tools/plan/heroes.py:8`
  - prompt history
  - `doctor.py:218`
- **Migration bug:** `_migrate_legacy_settings_once` reads `share_dir/settings.json`, while settings are actually read from `~/.coderai`. When the override is set, the migration never finds them.

**B5. Low: `docs/cli.md` doesn't match the actual CLI flags.** Confidence: high.
- **Documented but missing:** `--list-sessions` (lines 13 and 31) isn't in `startup.py`.
- **Implemented but undocumented:** about 45 flags, including `--print`, `--wire`, `--output-format`, `--input-format`, `--last`, `--continue`, `--mcp-config`/`--mcp-config-file`, `--config`/`--config-file`, `--work-dir`, `--add-dir`, `--thinking`/`--no-thinking` and `--reasoning-effort`.
- **Env var names:** I diffed `.env.example`, `config.py` and the docs. The names are consistent; `TOOLS_PRESET` is read through prefix stripping.

**B6. Low: copying `.env.example` quietly overrides settings.** Confidence: high.
- **What it ships:** non-empty `CODERAI_MODEL`, `CODERAI_BASE_URL`, `CODERAI_REASONING_EFFORT=max` and `CODERAI_TOOLS_PRESET=core`.
- **Impact:** copied to `.env`, these outrank `settings.json`. `/model` and setup-wizard choices then don't stick, and the tool preset silently changes to `core`.
- **Fix:** comment them out.

**B7. Low: two logging systems run side by side.** Confidence: high.
- **The split:** 27 modules use stdlib `logging.getLogger`; the rest use loguru through `coderai.log`.
- **The effect:** `enable_logging` only configures loguru, which is always installed. The stdlib loggers (MCP transport, telemetry, ACP) get no handler: DEBUG and INFO are dropped, and WARNING+ goes through Python's fallback handler to stderr and then into the redirector.
- **More writers:** `utils/logging.py` and `telemetry/crash.py` also write to `error.log` directly.

---

## (C) Verified dead code

Each item below has only its definition, and possibly an `__all__` entry, as a reference. I also found no quoted-name or `getattr` dispatch for any of them.

| Location | Dead symbols | Note |
|---|---|---|
| `utils/environment.py` | Whole module | Never imported. All 13 names are duplicated in `utils/subprocess_env.py`. Delete it. |
| `wire/serde.py` | Whole module | Only `tests/test_background_wire.py:41` imports it, and it's just a re-export. |
| `wire/jsonrpc.py` | `success_response`, `error_response`, `event_notification`, `request_message`, `JSONRPCMessage` and its methods, `ClientInfo`, `ExternalTool`, `ClientCapabilities`, `JSONRPC_IN/OUT_METHODS`, `AUTH_EXPIRED` | `wire/server.py:36-57` redefines them privately. Only `ErrorCodes`/`Statuses` are used. |
| `mcp/manager.py` | `set/get/clear_session_tool_mask`, `eject_server`, `reload_server`, `list_active_servers`, `set_on_status_changed`, `auto_heal_servers`, `hot_reload_tools`, `get_mcp_prompt`, `read_mcp_resource` | All 11 confirmed. Nothing ever sets masks, so `is_tool_enabled_for_session` is always `True`. MCP prompts and resources are collected but never exposed. |
| `mcp_oauth.py` | `create_mcp_oauth`, `has_mcp_oauth_tokens` | The second one is also buggy: it passes a URL as the server name. |
| `acp/kaos.py` | `ACPProcess` (never instantiated), and `ACPKaos._supports_terminal` / `_output_byte_limit` / `_poll_interval` | Because of this, `exec()` always runs locally and never uses the ACP terminal. |
| `config.py` | `get_default_auto_compact_window` (inlined at line 444), `PROVIDER_REGISTRY`, `save_base_url_setting`, `clear_typed_config_cache` (its docstring says tests use it; none do), the `NotificationConfig`/`McpConfig`/`save_config` aliases, `PermissionScope` | |
| `config.py` fields | `TypedConfig.default_yolo`, `default_plan_mode`, `telemetry`, and all of `background.*` | Validated but never read. Users who set them get no effect. |
| `auth/oauth.py` | `save_tokens`, `OAuthManager.from_typed_config` | |
| `wire/emitter.py` | `attach/detach_session_wire`, `hook_resolved` | Because the attach methods are unused, the session-wire passthrough branch never runs. |
| `wire/file.py` | `append_record`, `replay_sync`, `dump_line` | |
| `telemetry/crash.py` | `install_asyncio_handler` | Never installed, so asyncio task crashes aren't recorded, despite what its docstring says. |
| Other | `cli/__init__.ExitCode`, `DoctorReport.has_errors/has_warnings`, `utils/logging._mask_sensitive` | |

**False positives to keep (reported by vulture but actually used):**
- pydantic `validate_model` and `dump_secret`.
- `ACPServer.authenticate` (called by the ACP protocol).
- The `_NullWritable` methods (they implement the `AsyncWritable` protocol).
- `ACPProcess._poll_task` (holds a strong reference so the task isn't garbage-collected).
- `utils/pyinstaller` `datas`/`excludes`/`binaries` (used by `coderai.spec:5`).
- The module-level `__getattr__` in `constant.py` and `cli/__init__.py` (PEP 562).

---

## (D) Design smells

**D1. Medium: `config.py` (1,599 lines) is two modules pasted together.**
- The marker "merged from coderai/core/typed_config.py" is at line 1158, followed by a second module docstring and imports in the middle of the file.
- This is why E402 is ignored in `pyproject.toml`.
- It also imports `coderai.llm`, which risks import cycles.
- There are two parallel config systems: `settings.json` plus env, and `config.toml`. They have separate precedence rules and are written back to each other (`save_active_model_setting` writes to both).
- **Fix:** split it into a `settings` module, a typed-config module and a provider-registry module, with one precedence resolver.

**D2. Medium: four different implementations strip secrets from the environment, and they disagree:**
- `utils/environment.SENSITIVE_ENV_PATTERN`
- `utils/subprocess_env.is_sensitive_env_var`
- `utils/shell_quoting.is_sensitive_env_var`, which is copy-pasted and identical
- the inline list in `plugin/tool.py:26`

**D3. Low: three secret redactors and two key-masking helpers.**
- Redactors: `log.redact_secrets`, `utils/logging._mask_sensitive` and `utils/common/llm_error.mask_sensitive`. They behave differently.
- Masking helpers: `config.mask_api_key` and `doctor.mask_secret`, which are identical.

**D4. Low: telemetry sends events from tasks nobody keeps a reference to.** `track_session_started_once` and `TransportSink.flush_sync` fire off tasks with `create_task` and don't keep them, so events can be lost when the process shuts down.

**D5. Low: blocking calls on the event loop.**
- `wire/server._write_loop` writes to `stdout` synchronously.
- `_handle_prompt` builds a whole OpenAI client on every prompt just to read its capabilities.
- `mcp_manager.eject_server` uses the deprecated `get_event_loop().create_task` without keeping the task.
