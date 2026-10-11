# Phase 0: VPS hosting

Production is https://5-83-140-221.sslip.io on the Ubuntu VPS at `5.83.140.221`.
The runtime directory is `/opt/mologate`. Production uses the image built from
`main`; the account roadmap stays on `limits-and-accounts`.

These files make the deployment reproducible. They cover hosting only; account
tables, account connections, metering and enforcement belong to later phases.
Do not build the frontend on this one-vCPU machine. GitHub Actions publishes
`ghcr.io/pmixay/mologate:main-slim` and `:limits-and-accounts-slim`.

## Existing VPS: adopt these files

The existing GitHub CLI login is sufficient if its token has `read:packages`
and access to this private package. Keep the existing `.env`, database and
`WEBUI_SECRET_KEY`. Never print them or put them in Git.

Apply the files from a host session as root:

```bash
bash /root/MoloGate/deploy/vps/install.sh
```

The script first backs up the current stack and saves its deployment files
under `/root/mologate-config-*`. It preserves the existing `.env` and database
image (`postgres:17-alpine` on this VPS), installs the hosting files and nightly
cron entry, creates the shared proxy network, waits for readiness and makes a
new backup. It stops before changing files if Docker access is unavailable.

The project remains `mologate`, so the database and data volumes keep their
original names. Caddy is the only container that publishes ports. Readiness
checks wait for PostgreSQL, Redis and Mologate; log rotation and memory caps
limit disk and RAM use.

## Fresh installation

Install Docker Engine with the Compose plugin, GitHub CLI and UFW. Use Compose
2.24.4 or newer (the staging overlay replaces network aliases with `!override`).
Copy this directory to `/opt/mologate`, create the `mologate_edge` network as
above, and copy `.env.example` to `.env` with mode 600. Set a unique
`WEBUI_SECRET_KEY` and `POSTGRES_PASSWORD`, using separate runs of
`openssl rand -hex 32`. Set the domain's A record to the VPS before starting
Caddy. The temporary sslip.io domain already encodes the VPS address.

Authenticate the existing GitHub account for the private registry without
displaying its token:

```bash
cd /opt/mologate
registry_user=$(gh api user --jq .login)
gh auth token | docker login ghcr.io -u "$registry_user" --password-stdin
docker compose pull
docker pull alpine:3
docker compose up -d --wait --wait-timeout 180
```

On an empty database, create the first admin immediately through the initial
setup screen. Upstream allows this bootstrap even with signups disabled. Once
an admin exists, check that **Admin Settings → General → Enable Signups** is off.
Persistent database settings can override environment defaults on an existing
installation. Add team members through **Admin Panel → Users → Add User**, with
role **User**. Confirm a team member can sign in and complete a chat using the
existing provider connection.

Make the intended models available to team members in **Workspace → Models**.
Provider connections alone can leave regular users with an empty model list.
Share model read access with all authenticated users (`principal_type=user`,
`principal_id=*`, `permission=read` in the access-grants API). The `anyone`
principal means unauthenticated access and is not accepted for models. The
existing 11 GLM models on production have authenticated-user read grants.

Install the cron file and run `./backup.sh` once:

```bash
install -m 644 /opt/mologate/mologate-backup.cron /etc/cron.d/mologate-backup
/opt/mologate/backup.sh
```

It runs nightly at 03:30 in the VPS's system timezone (UTC on this host).

## Firewall and swap

This VPS already has an active 4 GB `/swapfile` and UFW enabled with 22/tcp,
80/tcp, 443/tcp and 443/udp allowed. Confirm these on the host:

```bash
swapon --show
ufw status verbose
ss -lntup
docker compose ps
```

For a fresh VPS, allow SSH before enabling UFW:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
```

Create a 4 GB swap file on a fresh VPS if it has no swap, secure it with mode
600, run `mkswap` and `swapon`, and add it to `/etc/fstab`. Do not recreate the
existing swap file. Docker-published ports can bypass UFW, so keep database,
Redis and application ports unpublished. From another machine, scan TCP ports
on both the public IPv4 address and any configured IPv6 address; only 22, 80
and 443 should be open.

## Updates and recovery

Run `/opt/mologate/update.sh` to pull the selected prebuilt image, make a backup,
and restart Mologate with a readiness check. It preserves older images for
rollback. Deploy a previous known-good image by setting `IMAGE` to its tag or
digest in `.env` and running the same script. A database schema downgrade may
also require restoring the matching backup.

Each successful backup is a timestamped, root-only directory containing:

- `db.dump`: a custom-format PostgreSQL dump, checked with `pg_restore --list`.
- `data.tgz`: the application data volume, checked with `tar -tzf`.
- `config.tgz`: `.env`, Compose files and Caddy configuration for recovery.
- `SHA256SUMS`: integrity checks for all three parts.

Failed backups leave no published set, and retention runs only after a complete
set is saved. Sets older than seven days are removed. The script discovers the
application volume from Docker, so staging gets its own data backup too.
The data archive is a live copy; pause application writes for a coordinated
database/files snapshot when performing a migration. Backups currently stay
on this VPS, as specified by Phase 0.

For recovery, validate the selected set first with `sha256sum -c SHA256SUMS`.
On a replacement host, restore `config.tgz` into the stack directory before
starting the database, so the database password and encryption key match. Stop
Mologate while restoring its database and data. For example, from the configured
stack directory with `backup_set` set to the selected absolute path:

```bash
docker compose stop mologate
docker compose exec -T postgres sh -c \
  'exec pg_restore --exit-on-error --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$backup_set/db.dump"
container=$(docker compose ps -aq mologate)
data_volume=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{.Name}}{{end}}{{end}}' "$container")
test -n "$data_volume"
docker run --rm --network none -v "$data_volume:/data" -v "$backup_set:/backup:ro" alpine:3 \
  sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -xzf /backup/data.tgz -C /data'
docker compose up -d --wait --wait-timeout 180
```

Test a restore into a disposable stack before calling recovery verified. Keep
the current stack and volumes until the replacement passes login and chat.

## Remote embeddings

Slim has no local embedding model. Set `RAG_OPENAI_API_BASE_URL`,
`RAG_OPENAI_API_KEY` and `RAG_EMBEDDING_MODEL` for an OpenAI-compatible embeddings
provider, then select the same engine in **Admin Settings → Documents** if a
database setting already exists. Chat does not require an embeddings key.

Fresh installations use PostgreSQL 17 with pgvector, following the
[pgvector Docker instructions](https://github.com/pgvector/pgvector#docker).
The original VPS's `postgres:17-alpine` image has no vector extension. Document
retrieval needs a planned logical dump/restore into a **new** pgvector-backed
volume first. Do not switch an existing Alpine data directory to a Debian
image in place; preserve the current database for rollback.

## Staging before Phase 1

Copy the deployment files into `/opt/mologate-staging`, create its own `.env`
with different secrets, and set:

```dotenv
DOMAIN=staging.5-83-140-221.sslip.io
APP_UPSTREAM=mologate-staging
IMAGE=ghcr.io/pmixay/mologate:limits-and-accounts-slim
COMPOSE_FILE=compose.yaml:compose.staging.yaml
```

After the branch image has been published, pull and start this stack. The
overlay uses the `mologate-staging` project, separate database/data volumes,
smaller memory limits and the shared proxy network. It does not start a second
Caddy or publish any ports. Leave the `standalone-proxy` profile disabled.

Copy `staging.caddy.example` to `/opt/mologate/caddy-sites/staging.caddy` (edit
the hostname if using another domain). Validate and reload production's Caddy:

```bash
cd /opt/mologate
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Create a separate staging admin. Add a nightly cron entry for
`/opt/mologate-staging/backup.sh` at 04:00 by installing
`mologate-staging-backup.cron` into `/etc/cron.d/`. Staging serves the branch
image while the team stays on production's `main` image. If stopping staging
while idle, disable its backup cron entry until it is running again.

The live staging stack follows `limits-and-accounts-slim`, published by the
[successful branch build](https://github.com/pmixay/MoloGate/actions/runs/38131457642)
at `fdbce1c`. `IMAGE` also accepts a digest for a pinned deployment.
Its admin uses the production admin's email with a separate password
stored in `/opt/mologate-staging/admin-password` (root only). It has a fresh
database; production's provider keys and chat history were not copied.

## Evidence from this session

On 2026-10-11, these files were installed on the VPS and checked live:

- Production runs `main-slim` at `a20a336`; staging runs the branch's slim image
  with separate PostgreSQL, Redis and data volumes. Both pass HTTPS readiness.
- Production and staging admin sign-in passed. Public signup returned 403 on
  production, and both stacks report signups disabled.
- A disposable regular user could sign in, list shared models and receive a
  `glm-5.3-flash` chat completion. The test user was deleted afterward.
- Production backups run at 03:30 UTC, staging at 04:00 UTC, with seven-day
  retention. Both scripts completed successfully against the live stacks.
- A production database dump was restored into an isolated PostgreSQL 17
  container: 1 user, 11 models, 11 model grants and 354 settings recovered.
  The data/config archives extracted successfully, their checksums passed,
  and the recovered `.env` matched the deployment. Six backup failure and
  retention tests also passed.
- UFW is active with only 22/tcp, 80/tcp, 443/tcp and 443/udp allowed. Only
  Caddy publishes container ports, and the host has 4 GB of active swap.
- [External checks](https://github.com/pmixay/MoloGate/actions/runs/38131735417)
  passed HTTPS, disabled signups, an SSH handshake and rejection/filtering of
  13 private/control TCP ports. A scan of all 65,535 TCP ports plus direct
  confirmation found only 22, 80 and 443 reachable. One additional scan
  candidate failed all three direct connection attempts. Raw SYN scans on
  this route gave inconsistent results; the workflow uses TCP connections
  and preserves scan artifacts. The VPS has no global IPv6 address.

Recovery files and passwords remain outside Git. Backups are local to this
VPS; off-host backup storage belongs to a later step. Remote document embeddings
remain unconfigured; the verified team workflow is chat.
