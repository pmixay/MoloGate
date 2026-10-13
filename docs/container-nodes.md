# Chat containers and container nodes

Every chat gets its own Linux container. The agent's terminal tools (run a command, read, write
and search files) and the file browser work inside it, the files the user attaches are copied into
it, and it keeps its files between turns. Containers run on **container nodes**: one or more
machines, the site's own included. Mologate places each chat on the least busy node, moves a
stopped container to a quieter node when its own is busy, and pauses, stops and deletes idle ones.

## How it fits together

```text
Browser ──> Mologate backend ──> container node (one per machine) ──> Docker ──> chat containers
              │  chat_container table:                │  per container: limits, firewall guards,
              │  chat → node, network, attachments    │  bandwidth cap, idle pause / stop / delete
              └─ "Chat container" terminal connection └─ HTTP and WebSocket proxy to Open Terminal
```

- **Sandbox image** (`containers/sandbox`, `ghcr.io/pmixay/mologate-sandbox`): Debian with Python
  3.12, Node.js 24, git, build tools and the usual command-line tools (ripgrep, fd, jq, curl, sqlite3,
  zip and so on), plus numpy, pandas, matplotlib, requests and friends. It runs
  [Open Terminal](https://github.com/open-webui/open-terminal) (MIT), the API Open WebUI already
  speaks for terminals and file browsing, in its own virtualenv so nothing the agent installs can
  break it. Open Terminal listens on a Unix socket only, so the API works with the network off.
- **Container node** (`containers/node`, `ghcr.io/pmixay/mologate-node`): a small aiohttp service
  that drives the machine's Docker. Only Mologate calls it, with the node's key.
- **Backend** (`backend/open_webui/utils/containers.py`): picks nodes, moves containers, copies
  attachments and tells the agent about its container. The built-in **Chat container** terminal
  connection (`server_type: mologate`) sends each request to the node holding that chat.

Open WebUI's own orchestrator for this, [Terminals](https://github.com/open-webui/terminals), needs
an enterprise license, so Mologate runs its own small node service on top of the same Open
Terminal API.

## What a chat gets

- **Its own container, on start.** The first message places it on a node; typing in the message
  box starts it a moment earlier (see Lifecycle). Sub-agents work in their main chat's container.
  Only the chat's owner can reach it, from the chat or the file browser.
- **Attachments inside.** Files attached to the chat are copied to `/home/user/uploads/` before the
  turn runs. The agent's system prompt lists their paths and sizes, marks the ones attached to the
  current message, and states the container's limits and whether its network is on.
- **Big attachments.** Files up to 500 MB (`MOLOGATE_MAX_UPLOAD_MB`, or the admin's lower file
  size limit) upload in 8 MiB chunks, three at a time, each retried on failure; text-like files are
  gzip-compressed in the browser first. Chunks are written straight into place on disk, and the
  finished file is moved into storage without a copy. Files over 20 MB (`MOLOGATE_FILE_TEXT_MAX_MB`)
  skip text extraction: the agent is told to read them in the container. Text put into the prompt
  is capped at 400,000 characters (`MOLOGATE_INLINE_TEXT_MAX_CHARS`); the agent reads the rest in
  `/home/user/uploads/`.
- **No decompression bombs.** Every upload is checked before anything extracts it
  (`backend/open_webui/utils/archive_guard.py`): ZIP entries are actually inflated and must match
  the sizes they declare, overlapping entries are refused, nested archives are followed three
  levels deep, and gzip, bzip2, xz and tar are expanded in a stream. An archive is refused if it
  would expand past 4 GiB, past 64 MiB at more than 200 times its size, or into more than 50,000
  entries (`MOLOGATE_ARCHIVE_MAX_EXPANDED_MB`, `MOLOGATE_ARCHIVE_MAX_RATIO`,
  `MOLOGATE_ARCHIVE_MAX_ENTRIES`). Compressed chunks are expanded with a hard limit too, and the
  container proxy refuses compressed responses rather than letting anything expand them.
- **Its files in the browser.** The **Files** button in the chat header opens the container's file
  browser: previews with syntax highlighting for code, and for Markdown, CSV, JSON, notebooks,
  images, PDF, Office, SQLite, audio and video; HTML and served ports open in a frame. Anything else
  can be downloaded.
- **What each response changed.** When a response starts, the node records the container's
  `/home/user` (hashes, plus compressed copies of text files up to 1 MB; dependency, cache and
  version-control folders skipped). When the response has completely finished, sub-agents and
  tool calls included, the message lists the files that were added, edited or deleted, with lines
  added and removed. A bar above the message box adds them up for the whole chat. Clicking a file
  opens it in the file browser.
- **A network switch.** The **⋯** (Extra) button next to **+** opens the container's settings:
  its state (running, paused, stopped), its size behind the **i** button, and **Network access**. Internet is on by
  default. Switching it off disconnects the running container at once; on reconnects it, with the
  bandwidth cap re-applied. It works mid-chat; in a new chat the choice goes with the first
  message. An amber dot on the button shows that the network is off. The agent is told when it is
  off, so it can work offline or ask the user to switch it on.

## Limits and guards

Each container, by default (`NODE_CONTAINER_*` settings below):

| Limit                | Default                    | How                                                        |
| -------------------- | -------------------------- | ---------------------------------------------------------- |
| CPU                  | 1 CPU, half the CPU weight | cgroup quota; the site and services win under load         |
| Memory               | 1 GiB, no swap             | cgroup limit; the kernel kills sandbox processes first     |
| Processes            | 512                        | pids limit                                                 |
| Open files           | 4,096                      | ulimit                                                     |
| `/tmp`               | 256 MiB                    | tmpfs (counts towards memory)                              |
| Disk                 | 10 GiB                     | Docker quota where supported, else watched (see below)     |
| Bandwidth            | 100 MiB/s up and down      | `tc` token bucket up, policing (or host-side shaping) down |
| Logs                 | 1 MiB                      | json-file rotation                                         |
| New connections      | 50 per second              | iptables hashlimit                                         |
| Open TCP connections | 256                        | iptables connlimit                                         |

Every container also runs with all capabilities dropped except a short list that package managers
need (no `NET_RAW`, `NET_ADMIN`, `SYS_ADMIN`, `MKNOD`), `no-new-privileges`, Docker's default
seccomp profile, and on its own bridge (`mologate-sb0`) where containers can't reach each other.
The node installs firewall rules for that bridge on start, from `DOCKER-USER` and `INPUT`:

- nothing reaches the host itself, private and link-local ranges (other Docker networks, the
  database, codex-lb, cloud metadata at 169.254.169.254) or multicast: only the public internet;
- no outgoing mail on port 25;
- the connection limits above, so a container can't flood a target or the host's connection table.

Containers resolve names through public resolvers (`NODE_CONTAINER_DNS`, 1.1.1.1 and 8.8.8.8).

**Disk.** Docker can cap a container's disk only on overlay2 over XFS with `pquota`. Elsewhere the
node measures each container's files every 10 minutes and shows them to the agent; when the
node's own disk falls below `NODE_MIN_FREE_DISK` it stops taking new containers, and stops and keeps
stopped any container over its limit until an admin frees space.

**Bandwidth.** Download is policed with the kernel's police action; on kernels without it, the
node shapes the host side of the container's network pair instead, for the same cap. Admin Settings > Integrations > Chat containers flags a node
that couldn't apply its guards or bandwidth limit.

**Stronger isolation.** With [gVisor](https://gvisor.dev) installed on a node, `NODE_RUNTIME=runsc`
runs every container under it.

## Lifecycle

| When                                | What happens                                                                 | Coming back       |
| ----------------------------------- | ---------------------------------------------------------------------------- | ----------------- |
| The user starts typing              | The chat's container wakes; a new chat gets a spare started on the best node |                   |
| 2 minutes without a request         | Paused: frozen, keeps its memory                                             | about 30 ms       |
| 15 minutes without a request        | Stopped: frees its memory, keeps its files                                   | about 1 second    |
| 7 days without use                  | Deleted with its files                                                       | a fresh container |
| The chat is deleted                 | Deleted within 30 minutes                                                    |                   |
| A spare nobody claims for 5 minutes | Deleted                                                                      |                   |

A request in progress (a long command, a file transfer) keeps the container awake; an open
terminal tab counts while it is used. When a node is at `NODE_MAX_ACTIVE` running or paused
containers, starting one stops the least recently used idle container first; if every container
on it is busy, the backend hands the chat to another node.

## Load balancing and moving containers

Each node reports its load: running and paused containers against its maximum, free memory and
CPU load. The backend scores it from about 0 (idle) to 1 (full) and:

- **places** a new chat on the lowest-scoring node that accepts containers, preferring one with a
  warm spare;
- **moves** a stopped container before starting it when its node scores at least 0.6 and 0.25
  more than the quietest other node, or no longer accepts containers: the node exports
  `/home/user` as a tar stream, the target imports it, and the old copy is deleted (about 0.1 s for
  a small workspace; at most 2 GiB). Everything under `/home/user` moves, including packages
  installed with `pip install --user` or `npm` in the workspace; system packages installed with
  `apt` do not;
- **hands on** a chat whose node is full of busy containers;
- **starts over** on another node when the chat's node is unreachable or removed, and tells the
  agent that earlier files aren't there.

## Setting up a node

### On the site's machine

`deploy/vps/compose.yaml` has a `container-node` service behind the `containers` profile. In
`/opt/mologate/.env`:

```bash
COMPOSE_PROFILES=containers
CONTAINER_NODE_KEY=<openssl rand -hex 32>
MOLOGATE_CONTAINER_NODES=[{"id":"vps","name":"VPS","url":"http://container-node:8090","key":"<the same key>"}]
CONTAINER_NODE_MAX_ACTIVE=2
```

Then run `update.sh`, which also pulls the sandbox image with the machine's registry login. The
node and Mologate talk over a private network; nothing is published. With the site on the same
machine, keep `CONTAINER_NODE_MAX_ACTIVE` low (the production VPS has 1 vCPU and 3.8 GB): idle
containers pause and stop, so two awake at once serve a small team.

### On another machine

Install Docker, copy `deploy/container-node/` to the machine, fill in `.env` from `.env.example`
(the node's domain, the site's IP addresses, a node ID and key), log Docker in to GHCR if the
images are private, pull the sandbox image (`docker pull ghcr.io/pmixay/mologate-sandbox:latest`)
and run `docker compose up -d`. Caddy serves the node over HTTPS and only to the site's addresses.
Then add the node in **Admin Settings > Integrations > Chat containers** (name, `https://` URL and
key), or in `MOLOGATE_CONTAINER_NODES`. Allow ports 80 and 443 in the machine's firewall.

`MOLOGATE_CONTAINER_NODES`, when set, replaces the node list at every start; leave it unset or
empty to manage nodes in the app. `MOLOGATE_CONTAINERS=false` switches chat containers off.

### Node settings

| Variable                          | Default                                  | Meaning                                       |
| --------------------------------- | ---------------------------------------- | --------------------------------------------- |
| `NODE_API_KEY`                    | required                                 | Mologate's key for this node (24+ characters) |
| `NODE_ID`, `NODE_NAME`            | hostname                                 | How Mologate and admins see the node          |
| `NODE_DATA_DIR`                   | `/var/lib/mologate-node`                 | State and sockets; same path in and out       |
| `NODE_SANDBOX_IMAGE`              | `ghcr.io/pmixay/mologate-sandbox:latest` | Image of new containers                       |
| `NODE_HELPER_IMAGE`               | the node image                           | Applies firewall and host-side shaping        |
| `NODE_MAX_ACTIVE`                 | from memory                              | Running or paused containers at once          |
| `NODE_CONTAINER_CPUS`             | `1`                                      |                                               |
| `NODE_CONTAINER_MEMORY`           | `1g`                                     |                                               |
| `NODE_CONTAINER_PIDS`             | `512`                                    |                                               |
| `NODE_CONTAINER_DISK`             | `10g`                                    |                                               |
| `NODE_CONTAINER_TMP`              | `256m`                                   |                                               |
| `NODE_CONTAINER_BANDWIDTH`        | `100mibps`                               | `tc` rate; empty switches the cap off         |
| `NODE_CONTAINER_DNS`              | `1.1.1.1,8.8.8.8`                        |                                               |
| `NODE_IDLE_PAUSE_SECONDS`         | `120`                                    | 0 never pauses                                |
| `NODE_IDLE_STOP_SECONDS`          | `900`                                    | 0 never stops                                 |
| `NODE_DELETE_AFTER_DAYS`          | `7`                                      |                                               |
| `NODE_SPARE_TTL_SECONDS`          | `300`                                    |                                               |
| `NODE_MAX_SPARES`                 | `1`                                      |                                               |
| `NODE_MIN_FREE_DISK`              | `5g`                                     | Below it, no new containers                   |
| `NODE_MAX_CONNECTIONS`            | `256`                                    | Open TCP connections per container            |
| `NODE_NEW_CONNECTIONS_PER_SECOND` | `50`                                     |                                               |
| `NODE_BLOCK_SMTP`                 | `true`                                   |                                               |
| `NODE_GUARDS`                     | `true`                                   | Firewall rules for the sandbox bridge         |
| `NODE_RUNTIME`                    | Docker's default                         | e.g. `runsc` for gVisor                       |
| `NODE_MAX_UPLOAD`                 | `1g`                                     | Largest attachment copied in                  |

## Node API

Every `/v1` call needs `Authorization: Bearer <NODE_API_KEY>`. A context is `chat:<id>` or
`automation:<id>`.

| Method and path                                      | Does                                                        |
| ---------------------------------------------------- | ----------------------------------------------------------- |
| `GET /healthz`                                       | Liveness, no key                                            |
| `GET /v1/status`                                     | Load, limits, lifecycle and which guards are active         |
| `GET /v1/openapi.json`                               | Open Terminal's API description, for the agent's tools      |
| `GET /v1/sandboxes`                                  | Every container on the node                                 |
| `POST /v1/spares`                                    | Start a spare for a chat that doesn't exist yet             |
| `GET, DELETE /v1/sandboxes/{context}`                | One container's state; delete it with its files             |
| `POST /v1/sandboxes/{context}/ensure`                | Create (or take a spare) and start; `{"wait": false}` warms |
| `POST /v1/sandboxes/{context}/network`               | `{"enabled": bool}`, applies at once                        |
| `POST /v1/sandboxes/{context}/pause, stop, touch`    |                                                             |
| `PUT /v1/sandboxes/{context}/files?path=`            | Write a file under `/home/user`, running or not             |
| `GET /v1/sandboxes/{context}/export`                 | Stop it and stream `/home/user` as a tar                    |
| `PUT /v1/sandboxes/{context}/import`                 | Create it from such a tar                                   |
| `POST /v1/sandboxes/{context}/snapshots/{id}`        | Record `/home/user` before a response                       |
| `GET /v1/sandboxes/{context}/snapshots/{id}/changes` | Files changed since, with lines added and removed           |
| `* /v1/sandboxes/{context}/t/{path}`                 | Open Terminal, HTTP and WebSocket, started on demand        |

## Tests

- `containers/node/tests`: lifecycle decisions, eviction, firewall and `tc` scripts, settings.
- `backend/open_webui/test/test_containers.py`: node scoring, placement, moving, failover,
  ownership and the agent's container prompt.
- `backend/open_webui/test/test_archive_guard.py`: ZIP bombs, lying and overlapping ZIP entries,
  nested archives, gzip bombs, tar entry counts and bounded chunk decompression.
- `backend/open_webui/test/test_uploads.py`: chunked uploads (raw and gzip chunks, resends, wrong
  sizes, compressed bombs, ownership, the size limit).
- Checked by hand against Docker: creation in under a second, limits inside the container, the
  firewall (host, metadata, private ranges and port 25 refused; internet and DNS work), the network
  switch on a running container, pause and resume, stop and resume, spares, a move between two
  nodes with files and packages intact, the WebSocket terminal, and a 2 MiB/s test cap measured at
  1.8 MB/s down and 1.9 MB/s up.
