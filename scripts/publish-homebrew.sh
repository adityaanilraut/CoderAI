#!/usr/bin/env bash
set -euo pipefail

release_tag=$1
default_branch=$2
formula=$3

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'

for attempt in 1 2 3; do
  git fetch --no-tags origin "refs/heads/$default_branch"
  base=$(git rev-parse FETCH_HEAD)
  git switch --detach "$base"
  install -m 644 "$formula" Formula/coderai.rb
  git add Formula/coderai.rb
  if git diff --cached --quiet; then exit 0; fi
  git commit -m "Update Homebrew formula to $release_tag"

  latest=$(gh release view --json tagName --jq .tagName)
  if [[ "$release_tag" != "$latest" ]]; then
    echo "Release $release_tag has been superseded. Skipped the tap update."
    exit 0
  fi
  if git push origin "HEAD:refs/heads/$default_branch"; then exit 0; fi

  git fetch --no-tags origin "refs/heads/$default_branch"
  if [[ "$base" == "$(git rev-parse FETCH_HEAD)" ]]; then
    echo 'The default branch rejected the formula update. Check its branch protection settings.' >&2
    exit 1
  fi
done

echo "The default branch changed during all $attempt formula update attempts." >&2
exit 1
