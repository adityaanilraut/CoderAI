# 001: Common Principles

This rule applies universally to all agents operating within this project. Follow these principles at all times:

## 1. Test-Driven Development (TDD)
- **Always write tests first:** When implementing new features or fixing bugs, write a failing test before writing the implementation code.
- **Verify Coverage:** Ensure that all new core logic is covered by tests.
- **Independence:** Tests should not rely on shared state or external systems without proper mocking.

## 2. Security First
- **No Hardcoded Secrets:** Never hardcode API keys, tokens, passwords, or connection strings in the source code. Use environment variables (e.g., `os.environ.get()`).
- **Input Validation:** Always validate and sanitize user input at the boundaries of the application.
- **Defense in Depth:** Do not assume that internal components are safe from malicious input.

## 3. Tool Usage & Autonomy
- **Act Proactively:** Use your available tools (`read_file`, `grep`, `run_command`, etc.) to gather necessary context. Do not guess file paths or function names.
- **Verify Assumptions:** If you are unsure about how a component works, read the code or run a test script to understand its behavior before making changes.

## 4. Communication
- **Clarity and Precision:** When reporting findings or documenting code, be concise but factually complete.
- **Cite Sources:** Reference specific file paths and line numbers when discussing code changes.
