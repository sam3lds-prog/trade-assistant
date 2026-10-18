#!/usr/bin/env bash
# Update Trade Assistant from GitHub and safely sync its Grok review worktree.
# .env and data are left alone provided they are untracked/ignored.
set -euo pipefail
cd "$(dirname "$0")"
repo_dir="$(pwd -P)"
review_dir="$(dirname "$repo_dir")/trade-assistant-review"

# Avoid overwriting tracked changes in the live application checkout.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Tracked files in the app folder have local changes; update stopped:"
  git status --short --untracked-files=no
  echo "Review these changes and commit, stash, or otherwise preserve them before updating."
  exit 1
fi

before=$(git rev-parse HEAD)
git pull --ff-only --quiet
after=$(git rev-parse HEAD)

[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

version=$(.venv/bin/python -c "from tradeassist import __version__; print(__version__)")
if [ "$before" = "$after" ]; then
  echo "Already up to date: Trade Assistant $version."
else
  echo "Updated to Trade Assistant $version. What changed:"
  git log --format="  - %s" "$before..$after"
  echo "Restart the app to use it: press Ctrl+C where it is running, then .venv/bin/python run.py"
fi

# Sync the review checkout ONLY when it is our registered worktree and clean.
# Exit Grok before running this script to avoid changing files mid-review.
if [ ! -d "$review_dir" ]; then
  echo "Grok review folder not found; skipped review sync."
elif ! git worktree list --porcelain | grep -Fx "worktree $review_dir" >/dev/null; then
  echo "Review folder exists but is not registered to this Git repository; skipped review sync."
elif [ -n "$(git -C "$review_dir" status --porcelain)" ]; then
  echo "Grok review folder has local or untracked files; skipped review sync to protect them:"
  git -C "$review_dir" status --short
else
  review_before=$(git -C "$review_dir" rev-parse HEAD)
  git -C "$review_dir" switch --detach --quiet main
  review_after=$(git -C "$review_dir" rev-parse HEAD)
  if [ "$review_before" = "$review_after" ]; then
    echo "Grok review folder already up to date ($(git -C "$review_dir" rev-parse --short HEAD))."
  else
    echo "Grok review folder synchronized to main ($(git -C "$review_dir" rev-parse --short HEAD))."
  fi
fi
