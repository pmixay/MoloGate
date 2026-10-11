# Phase 0: VPS hosting

Production is https://ai.arbuz.lol on the Ubuntu VPS at `5.83.140.221`.
The runtime directory is `/opt/mologate`. At the owner's request, Phase 1 testing now takes
place on this site using the image built from `limits-and-accounts`, pinned in the live `.env`.
See [Phase 1 deployment notes](../../docs/phase1-accounts.md). All development remains on that
single working branch.

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

The script saves the current deployment files under `/root/mologate-config-*`.
It preserves the existing `.env` and database image (`postgres:17-alpine` on
this VPS), installs the hosting files, removes the nightly backup job of older
installs, creates the shared proxy network and waits for readiness. It stops
before changing files if Docker access is unavailable.

The project remains `mologate`, so the database and data volumes keep their
original names. Caddy is the only container that publishes ports. Readiness
checks wait for PostgreSQL, Redis and Mologate; log rotation and memory caps
limit disk and RAM use.

## Fresh installation

Install Docker Engine with the Compose plugin, GitHub CLI and UFW.
Copy this directory to `/opt/mologate`, create the shared proxy network with
`docker network create mologate_edge`, and copy `.env.example` to `.env` with mode 600. Set a unique
`WEBUI_SECRET_KEY` and `POSTGRES_PASSWORD`, using separate runs of
`openssl rand -hex 32`. Set the domain's A record to the VPS before starting
Caddy. The current production hostname is `ai.arbuz.lol`, with an A record to `5.83.140.221`.

Authenticate the existing GitHub account for the private registry without
displaying its token:

```bash
cd /opt/mologate
registry_user=$(gh api user --jq .login)
gh auth token | docker login ghcr.io -u "$registry_user" --password-stdin
docker compose pull
docker compose up -d --wait --wait-timeout 180
```

On an empty database, create the first admin immediately through the initial
setup screen. After that, Mologate has no self-registration: the sign-up
endpoint refuses every request once a user exists, whatever a stored setting or
`ENABLE_SIGNUP` says, and the admin settings no longer offer the switch.

Admins create every member account in **Admin Panel → Users → Add User**, with
the member's name, email, a starting password and the **User** role. Members
then sign in and can change their password. Confirm a new member can sign in and
complete a chat using the existing provider connection.

Make the intended models available to team members in **Workspace → Models**.
Provider connections alone can leave regular users with an empty model list.
Share model read access with all authenticated users (`principal_type=user`,
`principal_id=*`, `permission=read` in the access-grants API). The `anyone`
principal means unauthenticated access and is not accepted for models. The
existing 11 GLM models on production have authenticated-user read grants.
Models from **Settings → Provider accounts** need no grant: they follow the
account's sharing instead (see the
[Phase 1 notes](../../docs/phase1-accounts.md#routing-and-limits)).

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

## Updates

Run `/opt/mologate/update.sh` to pull the selected prebuilt image and restart Mologate with a
readiness check. Older images stay on the host. Deploy a previous image by setting `IMAGE` to its
tag or digest in `.env` and running the same script.

**There are no backups while Mologate is in development.** Code that predates a database migration
cannot read the newer schema, so rolling back across a migration loses the data written since.
Add backups (kept off this VPS) before the team relies on the data.

## Uploads and embeddings

Uploaded files reach the model as their full extracted text, like plain attachments: no
embeddings and no vector database (`BYPASS_EMBEDDING_AND_RETRIEVAL`, on by default; a migration
switches existing installs once). Per-chat sandboxes that receive the files themselves are a later
roadmap item. While this setting is on, knowledge bases are sent as full text too, so embeddings
are needed only if an admin turns retrieval back on in **Admin Settings → Documents**.

For that, slim has no local embedding model. Set `RAG_OPENAI_API_BASE_URL`,
`RAG_OPENAI_API_KEY` and `RAG_EMBEDDING_MODEL` for an OpenAI-compatible embeddings
provider, then select the same engine in **Admin Settings → Documents** if a
database setting already exists. Chat and uploads do not require an embeddings key.

Fresh installations use PostgreSQL 17 with pgvector, following the
[pgvector Docker instructions](https://github.com/pgvector/pgvector#docker).
The original VPS's `postgres:17-alpine` image has no vector extension. Document
retrieval needs a planned logical dump/restore into a **new** pgvector-backed
volume first. Do not switch an existing Alpine data directory to a Debian
image in place; preserve the current database for rollback.

## Production domain and Phase 1 testing

On 2026-10-11, the owner selected `ai.arbuz.lol` and asked to test Phase 1 directly there.
HTTP redirects to HTTPS, and Caddy manages the certificate. The old production hostname
`5-83-140-221.sslip.io` redirects to `https://ai.arbuz.lol`, preserving the request path and query.
The application's `webui.url` database setting and `WEBUI_URL` environment both use the new URL.
Users sign in on the new hostname with their existing credentials.

Production runs the verified `limits-and-accounts` image from `05a3311`, pinned as
`ghcr.io/pmixay/mologate@sha256:d983bdc3081560bc0312f46591b5f72d2646dc44a7960bcbf8bb555582716abd`.
Later commits on the branch are not deployed yet. The pre-Phase-1 application image ID is in
`/opt/mologate/phase1-rollback-image`, and the earlier Phase 1 image (`45ec77e`) in
`/opt/mologate/sharing-rollback-image`. Preserve the existing PostgreSQL image, volumes and
secrets. Pre-Phase-1 code cannot read the newer schema.

To retain the old hostname on a recovered deployment, copy `legacy-production.caddy.example`
to `/opt/mologate/caddy-sites/legacy-production.caddy`, then validate and reload Caddy. Keep
`DOMAIN=ai.arbuz.lol` in the production `.env`.

```bash
cd /opt/mologate
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

## Phase 7: Claude runners (planned)

The [English runner specification](../../docs/claude-runners.md) describes this future addition.
No runner-manager or Claude account containers are deployed by the current compose files.
Anthropic API-key accounts already belong to Phase 1; subscription runners are separate work.

Add the manager in a separate compose overlay, with its own pinned image and control-state
volume. Only the manager receives `docker.sock`. Socket access is effectively host-root access:
even a private endpoint needs service authentication and strict resource/operation validation.
Do not mount the socket into Mologate or any runner. Keep manager port 8085 and runner port 8090
unpublished, with dedicated networks, labels and secrets. An internal-only
network also blocks provider access; provide controlled outbound DNS/HTTPS for runners without
joining the database or public proxy networks.

Each connected subscription has one labeled private volume owned by the runner UID/GID, with
mode 0700 directories and mode 0600 credential files. Neither tokens nor full CLI environments
belong in compose environment values, logs, database rows or Redis. Backend credential relay
must be redacted through the complete request/error/audit path.

This VPS already hosts production. Start with one live runner and measure CPU/RAM
headroom before allowing two; two is the combined host ceiling. Account volumes survive an idle
stop. A single admission controller must cover creation, verification, chat and restart, and
stop only idle runners for LRU. Let the manager restart runners under the cap rather than using
unconditional container restart policies. Keep the deployment switch off by default and add a
runtime disable switch that stops dispatch and drains/stops runners without a redeploy.

Build images in CI. Before collecting credentials, add backups (there are none during
development) that cover the new private volumes/control state, encrypt and restrict them,
document retention and test restore.
Include stored CLI conversation transcripts in the retention policy. Verify that only the
existing public ports remain reachable.

## Evidence from this session

The following initial Phase 0 checks were completed on 2026-10-11. They predate the production
domain/Phase 1 update described above; current deployment evidence is in the Phase 1 notes.

- Production ran `main-slim` at `a20a336` and passed HTTPS readiness.
- Production admin sign-in passed. Initially, signups were closed
  as specified by Phase 0. Production registration was subsequently reopened
  at the owner's request, with new accounts requiring admin approval. Public
  registration was tested: a pending account could not access models, then
  could sign in and list models after admin approval. Self-registration was
  later removed in code (`41ae16a`, not deployed yet), so admins create every account.
- A disposable regular user could sign in, list shared models and receive a
  `glm-5.3-flash` chat completion. The test user was deleted afterward.
- Nightly backups and a tested restore were part of Phase 0. They were removed
  afterwards for the development period and will come back later.
- UFW is active with only 22/tcp, 80/tcp, 443/tcp and 443/udp allowed. Only
  Caddy publishes container ports, and the host has 4 GB of active swap.
- A one-off [external check](https://github.com/pmixay/MoloGate/actions/runs/38131735417),
  from the since-removed port-check workflow, passed HTTPS, the initial closed-signup policy,
  an SSH handshake and rejection/filtering of 13 private/control TCP ports. A scan of all
  65,535 TCP ports plus direct confirmation found only 22, 80 and 443 reachable. The VPS has no
  global IPv6 address.

Passwords remain outside Git. There are no backups during development. Remote document embeddings
remain unconfigured; the verified team workflow is chat.
