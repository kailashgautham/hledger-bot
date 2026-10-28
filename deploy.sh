#!/usr/bin/env bash
# Deploy script: bring the checkout to the commit CI tested (or the latest
# main when none is given) and rebuild/restart the containers. Run on the
# host via SSH from GitHub Actions: bash ~/hledger-bot/deploy.sh <commit>
#
# Everything is inside main(), so bash has read the whole script before the
# git reset below rewrites this file.
set -euo pipefail

main() {
  cd "$HOME/hledger-bot"
  git fetch origin main
  git reset --hard "${1:-origin/main}"
  docker compose up -d --build
}

main "$@"
