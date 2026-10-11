#!/usr/bin/env bash
# Mologate: adopt the Phase 0 files on the existing production VPS.
set -euo pipefail
umask 077
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
stack_dir=/opt/mologate

if [[ $EUID != 0 ]]; then
  echo "Run this script as root on the production VPS." >&2
  exit 1
fi
# Fail before touching deployment files when the session lacks host access.
if ! docker info >/dev/null 2>&1; then
  echo "Docker access is unavailable. Run this script in a host session with access to the Docker socket." >&2
  exit 1
fi
test -f "$stack_dir/.env"
test -f "$stack_dir/compose.yaml"
cd "$stack_dir"
exec 9>.install.lock
flock -n 9 || { echo "Another Mologate install is running" >&2; exit 1; }
docker compose config --quiet

docker compose config --format json | python3 -c \
  'import json,sys; assert json.load(sys.stdin)["name"] == "mologate"'
postgres_container=$(docker compose ps -q postgres)
test -n "$postgres_container"
previous_postgres=$(docker inspect --format '{{.Config.Image}}' "$postgres_container")
./backup.sh
saved_config=$(mktemp -d /root/mologate-config-XXXXXXXX)
cp -a .env compose.yaml Caddyfile backup.sh update.sh "$saved_config/"
echo "Previous configuration saved to $saved_config"

# Keep the database's current image and data directory format on adoption.
PREVIOUS_POSTGRES_IMAGE="$previous_postgres" python3 - <<'PY'
import os
from pathlib import Path
import re

path = Path('.env')
content = path.read_text()
setting = 'POSTGRES_IMAGE=' + os.environ['PREVIOUS_POSTGRES_IMAGE']
if re.search(r'^POSTGRES_IMAGE=', content, re.MULTILINE):
    content = re.sub(r'^POSTGRES_IMAGE=.*$', lambda match: setting, content, flags=re.MULTILINE)
else:
    content += '\n' + setting + '\n'
path.write_text(content)
PY
chmod 600 .env
install -m 644 "$source_dir/compose.yaml" "$source_dir/Caddyfile" .
install -m 700 "$source_dir/backup.sh" "$source_dir/update.sh" .
install -d -m 700 caddy-sites backups
install -m 644 "$source_dir/mologate-backup.cron" /etc/cron.d/mologate-backup
docker network inspect mologate_edge >/dev/null 2>&1 || docker network create mologate_edge
docker compose config --quiet
docker compose up -d --wait --wait-timeout 180
./backup.sh
echo "Phase 0 hosting files applied. Verify signups, team login/chat, an external port scan and a restore using the README."
