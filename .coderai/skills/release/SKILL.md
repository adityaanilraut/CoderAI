---
name: release
description: Execute the CoderAI release workflow. Use when bumping versions, updating changelogs, preparing release branches, or coordinating the release process.
---

# Release Workflow

## Steps

1. **Understand the automation.** Read `AGENTS.md` and any CI/release workflow files before changing versions.

2. **Detect changed packages.** Check for changes since the last release tag:

```sh
git log --oneline <last-tag>..HEAD
```

If nothing changed, stop — there is nothing to release.

3. **Confirm versions with the user.** Propose new versions:
   - Patch is always `0`
   - Bump the minor version for any change
   - Major only by explicit user decision

4. **Create the release branch.** Name it `bump-<package>-<new-version>`.

5. **Update version metadata and changelogs:**
   - Update `pyproject.toml` version
   - Update `CHANGELOG.md`: keep `## Unreleased` header empty, add new dated section below
   - Update `breaking-changes.md` if breaking changes exist

6. **Run `uv sync`** to refresh the lockfile.

7. **Confirm with the user before opening the PR.** Summarize staged changes and wait for explicit approval.

8. **Open the PR** with `gh pr create`. PR description must include:
   - Summary of version bump and notable changes
   - Validation commands run
   - Post-merge tag command

9. **After merge**, switch to `main`, pull latest, and tell the user the exact `git tag` command.

## Stop Conditions

Fail fast — never paper over errors:
- Nothing changed since last tag
- `uv sync` fails or produces unexpected churn
- PR checks go red — investigate, do not force-merge
