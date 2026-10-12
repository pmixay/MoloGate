#!/usr/bin/env bash
# Mologate: deploy a prebuilt image together with the deploy files of the same commit.
# Older images stay available for rollback.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
exec 9>.update.lock
flock -n 9 || { echo "Another Mologate update is running" >&2; exit 1; }

# The checkout of the commit being deployed (the one IMAGE in .env was built from).
checkout=${MOLOGATE_SOURCE:-/root/MoloGate}
source_dir=$checkout/deploy/vps
if [[ -d $source_dir ]]; then
  saved=$(mktemp -d /root/mologate-config-XXXXXXXX)
  cp -a compose.yaml Caddyfile update.sh "$saved/"
  install -m 644 "$source_dir/compose.yaml" "$source_dir/Caddyfile" .
  # The running script keeps its own copy; the new one applies from the next update.
  install -m 700 "$source_dir/update.sh" .
  if ! docker compose config --quiet; then
    cp -a "$saved/compose.yaml" "$saved/Caddyfile" "$saved/update.sh" .
    echo "The deploy files in $source_dir are invalid; the previous ones are back." >&2
    exit 1
  fi
  echo "Deploy files applied from $source_dir; the previous ones are in $saved"
else
  echo "No checkout at $checkout; keeping the current deploy files." >&2
fi
docker network inspect mologate_edge >/dev/null 2>&1 || docker network create mologate_edge

# Reuse the GitHub account already registered on this VPS.
registry_user=$(gh api user --jq .login)
gh auth token | docker login ghcr.io -u "$registry_user" --password-stdin >/dev/null
docker compose pull mologate codex-lb
if [[ -d $checkout/.git ]]; then
  image=$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["mologate"]["image"])')
  built_from=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" 2>/dev/null || true)
  checked_out=$(git -C "$checkout" rev-parse HEAD)
  if [[ -n $built_from && $built_from != "$checked_out" ]]; then
    echo "Warning: $image was built from $built_from, but $checkout is at $checked_out." >&2
  fi
fi
# Every service, so new ones start (codex-lb, for example) and changed settings apply.
docker compose up -d --wait --wait-timeout 180
# Caddy reads its file at start; a reload applies a changed Caddyfile without downtime.
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
