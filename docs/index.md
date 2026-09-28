# CoderAI Documentation

**CoderAI** is an autonomous terminal AI software engineering platform, agentic execution engine, and multi-agent swarm designed for high reliability, deterministic tool execution, token efficiency, and developer velocity.

CoderAI couples a headless core runtime (`coderai.core`), a rich interactive terminal interface (`coderai.cli`), and a hybrid **Kahneman System 1 (Jev / TypeSafe) + System 2 (CoderAI Deep Review)** compound reasoning harness for ultra-fast, high-precision code review and autonomous software engineering.

---

## What is CoderAI?

CoderAI bridges the gap between conversational AI and deterministic, production-grade software development. Rather than treating code generation as simple text completion, CoderAI functions as a disciplined software engineer directly in your terminal:

1. **Terminal-Native Purity**: Built exclusively for developers' natural habitat—the terminal. CoderAI provides an interactive REPL (`coderai` or `cai`), slash commands (`/review`, `/plan`, `/undo`, `/agents`), and headless execution modes without the overhead of browser or web-based GUIs.
2. **Compound Reasoning (System 1 + System 2)**: CoderAI introduces Kahneman-style dual-process cognition to code review and development. By pairing sub-second non-autoregressive diff screening and confidence calibration (**Jev System-One**) with deep autoregressive multi-turn reasoning (**System 2** frontier models), CoderAI achieves state-of-the-art precision while eliminating developer alert fatigue.
3. **Decentralized Multi-Agent Swarms**: Orchestrate teams of specialized AI engineers (`spawn_teammate`, `team_task_*`, `wait_agent`) across shared DAG task boards and priority actor mailboxes.
4. **Deterministic Tool Execution**: File edits are anchored to cryptographic snippet hashes (`snippet_id`) with real-time staleness detection, avoiding hallucinated file overwrites.
5. **Defense-in-Depth Security**: 10 fine-grained permission scopes, native OS sandboxing (macOS Seatbelt, Linux Bubblewrap), and git working tree checkpoints (`.git_history`) providing instant rollback (`/undo`).
6. **Agent Control Protocol (ACP) & MCP**: Native in-process JSON-RPC server implementing the Agent Control Protocol for editor integration, along with full Model Context Protocol (MCP) server support.

---

## 🏆 Benchmark Validation

In the independent **Martian Code Review Benchmark** across 50 real-world enterprise pull requests (`cal.com`, `keycloak`, `grafana`, `sentry`, `discourse`), CoderAI placed **Rank 5 in the World** out of 30+ leading commercial and open-source AI code review tools:

- **Precision**: **58.2%** (Top 3 globally)
- **Recall**: **45.1%**
- **F1 Score**: **50.8%**
- **False Positives**: **56** (only 1.1 per PR, compared to 130–260+ for peer bots)
- **Turnaround Latency**: **6.63 seconds** average per PR (instantaneous developer feedback)

For full methodology, comparative tables, and per-repository breakdown, see the [Martian Benchmark Report](benchmarks.md).

---

## Documentation Index

Explore the CoderAI documentation suite:

| Guide / Reference | Description |
|---|---|
| [**CLI & Slash Commands**](cli.md) | Complete CLI flags, options, keyboard shortcuts, and interactive `/slash` commands. |
| [**Configuration**](configuration.md) | Configuration hierarchy, provider credentials, LLM endpoints, permission presets, and JEV settings. |
| [**Agent Roles & Discovery**](agent-roles.md) | Bundled specs (`default`, `okabe`) and dynamic Markdown discovery (`architect`, `tdd-guide`, etc.). |
| [**JEV System-One Implementation**](jev-system-one.md) | Architectural specification of the 3-tier compound review engine, non-autoregressive triage, and calibration gate. |
| [**Martian Benchmark Results**](benchmarks.md) | 50-PR evaluation data, 30-tool leaderboard, precision/recall curves, and latency analysis. |
| [**Model Context Protocol (MCP)**](mcp.md) | Connecting local and remote MCP servers, tools, and resource providers. |
| [**Workspace Skills**](skills.md) | Creating and managing reusable workflow instructions (`SKILL.md`) and domain knowledge. |
| [**Troubleshooting & Diagnostics**](troubleshooting.md) | Diagnostic routines (`/doctor`), permission resets, sandbox debugging, and session recovery. |

---

## Quickstart

Install CoderAI via `uv`, `pipx`, or `pip`:

```bash
# Using pipx
pipx install coderai-agent

# Using uv
uv tool install coderai-agent
```

Launch the interactive terminal shell in any repository:

```bash
coderai
# or short alias
cai
```

Configure API keys and providers interactively:

```bash
coderai --setup
```

Launch with a specialized role or plan mode:

```bash
coderai --agent architect
coderai --plan
```

