# Contributing to CoderAI

Thank you for your interest in contributing to CoderAI! This document outlines our development process, design philosophies, and submission guidelines.

---

## 1. Development Setup

### Requirements
- Python 3.12, 3.13, or 3.14
- Git 2.30+

### Clone & Install
```bash
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## 2. Code Standards

- **Python Compatibility**: Code must run on Python >= 3.12. Avoid PEP 695 syntax (`type X = ...` or `def func[T](...)`). Use `TypeVar` and `Union`.
- **Pure Terminal Architecture**: CoderAI is a terminal CLI and daemon application. Do not introduce web GUIs or browser servers.
- **Asynchronous I/O**: Use `asyncio` for network, subagent runners, and long-running subprocesses. Avoid blocking operations in the event loop.

---

## 3. Testing

Run tests per file using pytest:
```bash
python3 -m pytest tests/test_session_engine.py -p no:cacheprovider --benchmark-disable -q
python3 -m pytest tests/test_orchestration.py -p no:cacheprovider --benchmark-disable -q
python3 -m pytest tests/test_subagents.py -p no:cacheprovider --benchmark-disable -q
```

---

## 4. Creating Custom Agent Roles

To add a new specialized agent role to your workspace:
1. Create a markdown file in `.coderai/agents/<role-name>.md`:
```markdown
---
name: database-optimizer
description: Analyzes SQL queries, indexes, and schema definitions.
tools: read, grep, glob, bash
mode: read_only
---

# Database Optimizer Persona
You are an expert database performance engineer. Analyze indexing strategies, explain plans, and query performance.
```
2. The role is automatically discovered and can be inspected via `/agents roles` or launched via `coderai --agent database-optimizer`.

---

## 5. Pull Request Workflow

1. Fork the repository and create a feature branch (`git checkout -b feat/my-feature`).
2. Implement your changes with corresponding unit and integration tests.
3. Ensure all test suites pass.
4. Open a pull request with a descriptive title and summary of changes.
