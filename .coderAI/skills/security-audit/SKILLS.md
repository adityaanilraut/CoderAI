---
name: security-audit
description: Security audit workflow
---

# Security Audit Workflow

This skill defines the steps for performing a rigorous security review on code changes. It should be used before finalizing new features or reviewing pull requests.

## Step 1: Core Vulnerability Check
- **Credentials:** Scan the code for hardcoded passwords, tokens, API keys, or standard cryptographic material. Ensure these are loaded from environment variables or secure vaults.
- **Injection:** Look for dynamic construction of SQL queries, shell commands, or HTML templates. Ensure parameterized queries, safe subprocess wrappers, and proper escaping are used.
- **Path Traversal:** Ensure any user-supplied input used in file paths is rigorously sanitized or restricted to a specific directory.

## Step 2: Authentication and Authorization
- Verify that access control checks are present on all sensitive operations.
- Ensure that users can only access data belonging to them or data they have explicit permission to read/modify.

## Step 3: Dependency Review
- Check if the code introduces new third-party dependencies.
- Evaluate if the dependency is necessary or if the functionality can be securely implemented using the standard library.

## Step 4: Logging and Monitoring
- Ensure that errors and critical actions are logged appropriately.
- CRITICAL: Verify that sensitive information (e.g., passwords, credit card numbers, PII) is NOT included in log messages or stack traces.

## Step 5: Report Findings
- Document any identified issues categorized by severity (Critical, High, Medium, Low).
- Provide actionable recommendations and preferred code patterns to remediate the vulnerabilties.
