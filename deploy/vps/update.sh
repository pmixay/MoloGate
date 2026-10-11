#!/usr/bin/env bash
# Mologate: deploy a prebuilt image; older images stay available for rollback.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
exec 9>.update.lock
flock -n 9 || { echo "Another Mologate update is running" >&2; exit 1; }
docker compose config --quiet

# Reuse the GitHub account already registered on this VPS.
registry_user=$(gh api user --jq .login)
gh auth token | docker login ghcr.io -u "$registry_user" --password-stdin >/dev/null
# codex-lb carries the ChatGPT accounts; Mologate does not wait on it, so start it explicitly.
docker compose pull mologate codex-lb
docker compose up -d --wait --wait-timeout 180 mologate codex-lb
