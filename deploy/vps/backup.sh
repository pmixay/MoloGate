#!/usr/bin/env bash
# Mologate: complete, private backup sets with a seven-day retention period.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
stack_dir=$PWD
backup_dir="$stack_dir/backups"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

exec 9>"$stack_dir/.backup.lock"
flock -n 9 || { echo "Another Mologate backup is running" >&2; exit 1; }

stamp=$(date -u +%Y-%m-%dT%H-%M-%S-%NZ)
partial=$(mktemp -d "$backup_dir/.partial-$stamp.XXXXXX")
trap 'rm -rf -- "$partial"' EXIT

docker compose exec -T postgres sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$partial/db.dump"
test -s "$partial/db.dump"
docker compose exec -T postgres pg_restore --list <"$partial/db.dump" >/dev/null

container=$(docker compose ps -q mologate)
test -n "$container"
volume=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{.Name}}{{end}}{{end}}' "$container")
test -n "$volume"
docker run --rm --network none --read-only \
  -v "$volume:/data:ro" -v "$partial:/out" alpine:3 \
  sh -c 'umask 077; tar -czf /out/data.tgz -C /data .'
tar -tzf "$partial/data.tgz" >/dev/null

# Preserve the encryption key and connection settings needed to recover the DB.
config_files=(.env compose.yaml Caddyfile caddy-sites)
if [[ -f compose.staging.yaml ]]; then config_files+=(compose.staging.yaml); fi
tar -czf "$partial/config.tgz" "${config_files[@]}"
chmod 600 "$partial/"*
(
  cd "$partial"
  sha256sum db.dump data.tgz config.tgz >SHA256SUMS
)
mv -- "$partial" "$backup_dir/$stamp"

# Retention runs only after all three parts are saved and verified.
find "$backup_dir" -mindepth 1 -maxdepth 1 -type d -name '20*' \
  -mmin +10080 -exec rm -rf -- {} +
echo "Mologate backup complete: $backup_dir/$stamp"
