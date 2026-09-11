---
name: worktree-status
description: Audit all git worktrees in the current project. Use when the user asks about worktree status, which branches are merged, which have uncommitted changes, or which worktrees can be safely cleaned up.
---

# Worktree Status

## Procedure

### 1. Pull latest main (MANDATORY)

```sh
cd "$(git rev-parse --show-toplevel)" && git pull origin main
```

Without this, merge detection produces stale results.

### 2. Collect worktree info

```sh
PROJECT_DIR="$(git rev-parse --show-toplevel)"

for wt in $(git worktree list --porcelain | grep "^worktree " | sed 's/^worktree //' | grep -v "$PROJECT_DIR$"); do
  branch=$(git -C "$wt" branch --show-current 2>/dev/null)
  [ -z "$branch" ] && branch="(detached)"
  name=$(basename "$wt")

  if [ -z "$(git -C "$wt" status --short 2>/dev/null)" ]; then
    dirty="clean"
  else
    dirty="DIRTY"
  fi

  if [ "$branch" != "(detached)" ]; then
    if git merge-base --is-ancestor "$branch" origin/main 2>/dev/null; then
      merged="merged"
    else
      merged="not merged (verify with content diff)"
    fi
  else
    merged="n/a"
  fi

  echo ""
  echo "[$name]  branch=$branch  $dirty  $merged"
  if [ "$dirty" = "DIRTY" ]; then
    git -C "$wt" status --short 2>/dev/null | sed 's/^/  /'
  fi
done
```

### 3. Detect squash-merged branches (content diff)

For branches showing "not merged", check if changes are already in main:

```sh
BRANCH="<branch>"
BASE=$(git merge-base origin/main "$BRANCH")
FILES=$(git diff --name-only "$BASE" "$BRANCH")

for f in $FILES; do
  d=$(git diff "$BRANCH" origin/main -- "$f" | wc -l)
  if [ "$d" != "0" ]; then
    echo "DIVERGES $f"
  else
    echo "IDENTICAL $f"
  fi
done
```

All `IDENTICAL` = squash-merged.

### 4. Present results as a Markdown table

| Worktree | Branch | Dirty | Merged | Can clean? |
|---|---|---|---|---|
| `example-wt` | `feat-foo` | clean | squash-merged | yes |
| `another-wt` | `fix-bar` | 3 files | not merged | no |

### 5. Cleanup (only when asked)

Only clean worktrees the user explicitly approves:

```sh
git worktree remove "/path/to/<worktree-name>"
git branch -D "<branch>"
```
