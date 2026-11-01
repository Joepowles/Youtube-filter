#!/usr/bin/env bash
# Pulls new commits from GitHub and reinstalls the app when something changed.
# Run every few minutes by yt-filter-update.timer (set up by install.sh).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
git fetch --quiet origin main
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
  exit 0
fi
echo "Updating $(git rev-parse --short HEAD) -> $(git rev-parse --short origin/main)"
git log --oneline HEAD..origin/main
git merge --ff-only --quiet origin/main
bash "$REPO/deploy/install.sh"
