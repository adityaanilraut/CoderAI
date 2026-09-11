# CoderAI Documentation

CoderAI is an autonomous software engineering AI platform built for the terminal. It pairs developer workflows with intelligent subagents, multi-agent swarms, dynamic tool execution, and local-first session management.

---

## Key Features

- **Decisive Turn-Based Execution**: Turn-efficient engineering loops with parallel tool batching, unified diff patches, and verification runs.
- **Autonomous Swarms & Teammates**: Coordinate teams of AI engineers (`spawn_teammate`, `team_task_*`, `wait_agent`) with DAG-validated task dependencies and actor channels.
- **Specialized Markdown Agents**: Define custom agent personas in markdown with YAML frontmatter under `.coderai/agents/`. Built-in roles include `architect`, `build-error-resolver`, `code-reviewer`, `planner`, `security-reviewer`, and `tdd-guide`.
- **Safety & Approvals**: Flexible execution policies including Full Approval, Read-Only, `AFK` auto-pilot, and `YOLO` unrestricted execution.
- **Extensible Tooling & MCP**: Model Context Protocol (MCP) server support, sandboxed bash execution, ripgrep code search, AST-based file editing, and git history checkpoints.
- **Agent Control Protocol (ACP)**: Native in-process server for editor extensions and headless daemon automation.

---

## Getting Started

Install CoderAI using `uv`, `pipx`, or `pip`:
```bash
pipx install coderai-agent
# or
uv tool install coderai-agent
```

Launch the interactive terminal shell in any git repository:
```bash
coderai
# or short alias
cai
```

Configure your provider API keys:
```bash
coderai --setup
```

Launch with a specialized role:
```bash
coderai --agent architect
```
