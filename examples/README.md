# Examples

Minimal, runnable CLI recipes ported from the Kimi CLI reference.

| Example | Shows |
| ------- | ----- |
| `custom-tools` | Custom `kosong` tool registered on the agent toolset |
| `coderai-cli-stream-json` | Drive `coderai --print` via stream-JSON over stdio |
| `coderai-cli-wire-messages` | Headless `CoderAICLI.run` with raw Wire messages |
| `sample-plugin` | Language-agnostic plugin (`plugin.json` + `SKILL.md`) |

Skipped from the reference (not lazy to port): `kimi-psql` (separate
PTY TUI app + live PostgreSQL), `custom-echo-soul` / `custom-kimi-soul`
(need a `Shell` UI class + `run_soul`, which CoderAI doesn't have;
`coderai-cli-wire-messages` covers headless usage).
