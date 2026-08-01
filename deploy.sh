#!/usr/bin/env bash
# Deploy script: pull the latest pushed code and rebuild/restart the container.
# Run on the host (via SSH from GitHub Actions) from the bot repo checkout.
set -euo pipefail

cd "$HOME/hledger-bot"
git fetch origin
git reset --hard origin/main
docker compose up -d --build
