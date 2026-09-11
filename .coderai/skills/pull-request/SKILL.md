---
name: pull-request
description: Create and submit a GitHub Pull Request for the current branch. Use when the user wants to push changes and open a PR, or asks to create a pull request.
---

# Pull Request Workflow

## Pre-flight Check

1. Verify no dirty changes on the current branch:

```sh
git status --short
```

If there are uncommitted changes, stop and ask the user to commit or stash first.

2. Verify the current branch is different from `main`:

```sh
git branch --show-current
```

If on `main`, stop — a PR needs a feature branch.

## Create the PR

1. Push the branch:

```sh
git push -u origin HEAD
```

2. Open a PR using `gh`:

```sh
gh pr create --fill
```

3. Verify the PR was created:

```sh
gh pr view --web
```

## PR Title Convention

PR title becomes the squash-merge commit message. Use imperative mood, no period, under 72 characters.

Examples:
- `Add file-level permission checks to tool sandbox`
- `Fix session history replay on fork`

## PR Description

Include:
- What changed and why
- How to test the changes
- Any breaking changes or migration notes
