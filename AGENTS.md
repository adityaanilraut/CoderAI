# AGENTS.md — Contributor & Agent Guide for CoderAI

This document serves as the operational guide and architectural specification for AI agents and human contributors working on the **CoderAI** codebase.

---

## 1. Architectural Overview

CoderAI is an enterprise-grade AI software engineering CLI and agentic execution platform.

### Core Systems
- **Terminal CLI (`coderai/ui/shell/`, `coderai/cli/`)**: Pure CLI interface (`coderai` and `cai`) with rich terminal rendering, interactive REPL, slash command dispatcher (`coderai/ui/shell/slash.py`), and multi-provider configuration.
- **Session Manager (`coderai/soul/session/manager.py`)**: Stateful session orchestrator managing conversation histories, event streams, turn life cycles, compaction, and file history checkpoints.
- **Agent Roles & Discovery (`coderai/subagents/` & `.coderai/agents/`)**: Dynamic discovery of specialized markdown agent specifications (`.coderai/agents/*.md`, `.agents/agents/*.md`, `~/.agents/agents/*.md`). Discovered roles include `architect`, `build-error-resolver`, `code-reviewer`, `planner`, `security-reviewer`, and `tdd-guide`.
- **Autonomous Swarms (`coderai/teams/`)**: Decentralized multi-agent swarm coordination featuring `TeamManager`, DAG-validated `TeamTaskBoard`, priority actor mailboxes (`ActorChannel` / `AsyncMailbox`), and synchronization barriers (`wait_agent`).
- **Tool Platform (`coderai/tools/`, `coderai/tools/legacy/`)**: Sandboxed tool execution with dry-run verification, approval workflows (`YOLO` and `AFK` modes), AST-based code transforms, ripgrep search, and git working tree isolation.
- **Agent Control Protocol (`coderai/acp/`)**: In-process JSON-RPC server implementing the ACP protocol with session creation, resume, fork (`fork_session`), and extensible method dispatch (`ext_method`).
- **JEV System-One AI Model (`coderai/jev/`, `coderai/triage/`)**: Non-autoregressive "System One" AI model providing ultra-fast single-shot triage, diff screening, confidence calibration, LRU caching, and fail-safe fallback to System-Two autoregressive reasoning.

---

## 2. Agent Roles & Specifications

CoderAI provides both bundled and dynamically discovered agent roles:

| Role / Spec | Type | Mode | Description |
|---|---|---|---|
| `default` | Bundled | Primary | Full software engineering tool suite. |
| `okabe` | Bundled | Extended | Experimental mad-scientist persona with advanced toolsets. |
| `architect` | Discovered | General | Systems architect designing components, interfaces, and boundary layers. |
| `build-error-resolver` | Discovered | General | Pinpoints root causes of compiler, build, and typecheck errors. |
| `code-reviewer` | Discovered | Read-Only | Read-only security, correctness, and architecture review. |
| `planner` | Discovered | Read-Only | Generates actionable implementation plans with phased milestones. |
| `security-reviewer` | Discovered | Read-Only | Security auditor auditing OWASP vulnerabilities, authorization, and sanitization. |
| `tdd-guide` | Discovered | General | Test-driven development specialist writing failing reproduction tests first. |

### CLI Usage
```bash
# Launch interactive session with a specific role
coderai --agent architect
coderai --agent-file .coderai/agents/code-reviewer.md

# Inspect available roles in the REPL
/agents roles
```

---

## 3. Development Guidelines & Constraints

### Python Version Support
- **Minimum Python Version**: Python 3.12+
- **Typing Rule**: Do **not** use PEP 695 syntax (`type Alias = ...` or `def func[T](...)`). Use standard `typing.TypeVar`, `Union`, and `typing_extensions` where applicable to preserve broad compatibility.

### Terminal Purity
- CoderAI is strictly a terminal CLI and daemon application. Do not reintroduce web frontends, browser viewers, or HTTP HTML renderers.

### Testing Conventions
- Never run the entire test suite in a single process due to test memory isolation.
- Run tests on individual test files using:
  ```bash
  python3 -m pytest tests/<test_file>.py -p no:cacheprovider --benchmark-disable -q
  ```
