#!/usr/bin/env bash
# Update Trade Assistant to the newest version on GitHub.
# Your .env file and data folder are not in the repository, so they are never touched.
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Files in this folder have been changed by hand, so the update stopped rather than overwrite them:"
  git status --short --untracked-files=no
  echo "Undo those changes (git checkout -- .) or keep them on a branch, then run this again."
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
