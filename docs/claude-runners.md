# Phase 7: Claude subscription runners

Status: **planned, not implemented**. Added on 2026-10-11 to the
[Limits and Accounts Roadmap](limits-and-accounts.md). This is the implementation handoff for
automatic provisioning: **one connected Claude subscription = one isolated container**.

Read the main roadmap and [Phase 1 implementation notes](phase1-accounts.md) first. The roadmap,
account implementation and this specification all use **`limits-and-accounts`**. Continue work
on that branch; do not create a separate Claude or phase branch. Implement the required earlier
phases before their integration points. Descriptions below are proposed contracts, not claims
that the services or later-phase tables already exist.

## 0. Goal, scope and phase placement

An owner chooses **Connect → Claude subscription** in Mologate. The backend provisions an
isolated container on the VPS running the unmodified official `claude` executable. The owner
authenticates through Anthropic's flow; verification then makes the account usable, with optional
team sharing and the roadmap's owner reserve/pool rules.

**Phase 1 already covers Anthropic API keys.** The complete subscription integration belongs to
Phase 7. Its infrastructure and authentication spike can start after Phase 1 without waiting for
all intervening phases, but it must remain isolated and disabled for team traffic until its
dependencies are ready:

| Dependency | Required for                                                                 |
| ---------- | ---------------------------------------------------------------------------- |
| Phase 1    | Account ownership, encrypted metadata, sharing, model IDs and access checks  |
| Phase 2    | Usage ledger, account windows and calibration                                |
| Phase 3    | Pure `decide()` allowance engine and account selection                       |
| Phase 4    | Gate coverage, concurrency, worker fallback and limit UI                     |
| Phase 5    | Permanent credit grants, spending and disconnect forfeits                    |
| Phase 6    | Not required for this MVP; subscription runners accept interactive chat only |

Do not patch the CLI, spoof its user agent, intercept provider TLS, automate the login page or
rotate proxies. Tool execution and long autonomous agent loops are outside this phase.

The team requested subscription pooling with the risk recorded in
[roadmap §3](limits-and-accounts.md#3-providers-and-what-their-terms-allow).
Anthropic's distinction between hosted personal CLI use and subscription credential pooling
still applies. This architecture does not establish permission or guarantee against suspension.

`MOLOGATE_LIMITS_MODE=off|observe|enforce` retains its existing meaning, with default `observe`.
Add `MOLOGATE_CLAUDE_RUNNERS=off` as an independent deployment kill-switch. Allowance decisions
are observational in `observe`; ownership, authentication, backoff, worker exclusions and hard
resource caps are always enforced.

**Credential boundary:** SQL and Redis contain runner references, not Claude tokens. Flow A
does pass a token through the browser, backend and manager in memory; the manager persists it
only in the runner's private volume. Never describe this as Mologate never handling a token.

## 1. Architecture and repository locations

```text
Browser Connect -> Mologate backend -> runner-manager -> Docker socket
                       ^                    |
                       | signed events      +-> account volume + runner container
                       +--------------------+

utils/chat.py gate -> claude_runner adapter -> manager chat proxy -> runner :8090
                                                                  |
                                                         claude -p / NDJSON
                                                                  |
                       UI SSE <- adapter <- usage and limit observations
```

New components:

- `deploy/runner-manager/`: FastAPI service using the Docker SDK. It is the only new service
  with the host Docker socket; it is not part of the Mologate image.
- `docker/claude-runner/`: reproducible image and `server.js` wrapper using `node:http` and
  `child_process.spawn`. Build images in CI, not on this one-vCPU VPS.
- `backend/open_webui/routers/runners.py`: owner/admin lifecycle and Connect endpoints, plus
  authenticated manager events. Reuse existing account permissions and explicit response fields.
- `backend/open_webui/utils/claude_runner.py`: service client, dispatch, stream adaptation,
  session mapping and metering hooks. Extend `events.py` and `config.py` using repo conventions.
- Existing `ProviderAccounts` settings UI and `ContextIndicator.svelte`: extend these instead of
  creating a second independent account system.

Use a dedicated internal control network, for example `molo-runners-internal`.
Publish neither port 8085 nor 8090 on the host. Runners also need outbound DNS/HTTPS to Anthropic;
an `internal: true` Docker network alone cannot supply that. Add an explicit outbound network
and firewall policy without placing runners on the database or public proxy networks. Restrict
runner-to-runner and control-plane access; an unpublished port still needs service authentication.
See [Docker network isolation](https://docs.docker.com/reference/compose-file/networks/#internal).

The manager uses its own networks, service credentials, container labels and volumes, and stays
disabled by default. The VPS has one vCPU and about 3.8 GB RAM and already hosts the production
application stack. Two live runners is a **host-wide ceiling**, not a
promise that two 512 MiB containers will fit. Start with one and measure before increasing it.
Anthropic lists [4 GB+ RAM as a system requirement](https://code.claude.com/docs/en/setup#system-requirements)
for Claude Code. The proposed 512 MiB container cap is an experiment for this restricted workload,
not a supported sizing claim; moving runners to a larger host may be necessary.

## 2. Data and identity

Use `provider_account.provider='claude_sub'`. Distinguish `plan='pro'|'max_5x'|'max_20x'`.
Pro is the base paid tier: default multipliers are 1, 5 and 20. These are initial capacity weights
and credit multipliers, not fixed token allowances. An administrator can adjust them after
calibration. See [the official plan comparison](https://claude.com/pricing).

Store Fernet-encrypted metadata through `models/oauth_sessions.py`'s existing pattern:

```json
{
	"manager": "http://runner-manager:8085",
	"runner_id": "<server-generated-uuid>",
	"volume": "claude-cfg-<server-generated-uuid>"
}
```

The manager origin, image, network and volume naming are server-owned configuration. Validate
the stored origin against the configured manager; never dispatch to a URL supplied by a member.
Container/volume labels also carry the deployment identity to prevent cross-environment actions.

The SQL `provider` column is a string, so this provider does not itself require an enum migration.
However, Phase 1 has a Python `Provider` literal and frontend/API validation that must be extended.
Persist registration progress and webhook deduplication in a new Alembic migration:

- `runner_registration`: server-generated ID/runner ID, owner, requested plan/sharing/label,
  state, optional linked account ID, credential-generation ID, last applied event sequence,
  expiry, cleanup state and timestamps. No credential column. The existing `provider_login`
  flow is Codex-specific and must not accidentally poll these registrations as Codex logins.
- `runner_event`: unique event ID, registration ID, generation, sequence, event type and applied
  timestamp. Persist only allowlisted non-secret fields. Apply the event and account/credit
  changes in one transaction; retain disconnect tombstones long enough to reject late events.

A pending registration is displayed as a pending account card but is not a selectable
`provider_account`. Lifecycle: `needs_login -> verifying -> ok`; errors lead to `needs_login`,
`cooldown` or `error`. Runtime state (`stopped`, `starting`, `running`, `busy`) is separate from
authentication state: stopping an idle container does not invalidate the account.

On verified connection, use `sha256(provider_verified_account_uuid)` as `external_account_id`.
An `oauthAccount` entry, submitted email or token fingerprint is not by itself proof of identity.
The first spike must establish a supported, provider-verified stable identity source, its exact
field name and its binding to the active credential. Do not assume `oauthAccount.uuid` exists in
`result` output or that `setup-token` populates a local account file. If identity cannot be
established, keep the registration unverified and do not admit it to the pool or grant credits.

Unique `(provider, external_account_id)` rejects concurrent duplicates. Because Phase 1 permits
account deletion, it does **not** enforce once-ever rewards. Only Phase 5's permanent
`credit_grant` uniqueness does that. Grant `10 × multiplier` once when a verified account first
becomes shared and eligible; private, failed and pending registrations earn nothing. Disconnect
forfeits unused credits without deleting the grant history. Set `allow_team_resets=false`.

Redis caches `mologate:<environment>:runners:<id>` with `state`, `last_event`, `busy` and last-used
time. PostgreSQL owns identity, registration and sharing; Docker reports actual process state.
An in-memory cache is acceptable for a single process without Redis. It must not turn into
independent concurrency counters across several workers; the manager owns slot admission.

## 3. Runner-manager API and trust boundary

The manager listens on port 8085 on the private control network. Every API request requires a
separate service credential; webhook signing alone does not protect this API. Use constant-time
secret comparisons, bounded bodies, timeouts and no redirects. Backend owner/admin authorization
precedes every lifecycle request; only the owner can submit a new login credential.

| Method   | Path                       | Contract                                                                                                                                                                                                                                   |
| -------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `POST`   | `/runners`                 | `{runner_id, plan, generation}` from the backend. Idempotently create labeled volume/container, initialize permissions, return `202 {state, status:"needs_login"}`. A login runner also consumes a live slot.                              |
| `POST`   | `/runners/{id}/credential` | Bounded raw token body for the current one-use generation. Write atomically as mode 0600; never echo/log it. Return `202 {status:"verifying"}`. Repeated transport requests are idempotent; replacing a credential needs a new generation. |
| `GET`    | `/runners/{id}`            | Sanitized `{state, logged_in, busy, claude_version, uptime, last_verified_at}`; inspect Docker or proxy health without waking stopped runners.                                                                                             |
| `POST`   | `/runners/{id}/start`      | Reserve a host slot, stop an idle LRU runner if necessary, then start. If all slots are busy, return a retryable capacity response.                                                                                                        |
| `POST`   | `/runners/{id}/stop`       | Drain with a bounded timeout; preserve the volume and account identity.                                                                                                                                                                    |
| `POST`   | `/runners/{id}/chat`       | Authenticated streaming proxy to the runner's `/v1/chat`; holds the slot lease through completion/cancellation.                                                                                                                            |
| `DELETE` | `/runners/{id}`            | Mark deleting, reject new work, drain up to 60 seconds, remove the labeled container and volume. Idempotent retries finish partial cleanup.                                                                                                |
| `GET`    | `/runners/{id}/terminal`   | Optional later flow B only: issue an owner-bound single-use terminal URL, expiry at most 15 minutes.                                                                                                                                       |

The backend exposes owner-facing routes under `/api/v1/runners`, including status and credential
submission, using the existing user authentication. Do not expose the manager API directly to
the browser. Extend `utils/audit.py` redaction to these routes before accepting credentials;
validation errors, middleware, tracing, reverse proxies and HTTP-client exceptions must not
capture their bodies or authentication headers. Never pass raw CLI stderr to users or events.

Only server-generated UUIDs and deployment-owned labeled resources may be operated on. Reject
arbitrary image names, bind paths, commands, ports and Docker options from requests. The Docker
socket grants host-level control: network isolation does not reduce that privilege if the
manager itself is compromised. Keep its API narrow and its dependencies pinned.

Manager-to-backend events use `POST /api/v1/runners/events`. Sign the exact raw body with
`X-Molo-Sig: HMAC-SHA256(body, RUNNERS_WEBHOOK_SECRET)`. Put `event_id`, timestamp, `runner_id`,
generation and monotonic per-generation sequence in the signed body. Verify before parsing
effects, reject stale requests and wrong deployments, and persist deduplication. Retries use the
same event ID with a fresh signed delivery timestamp. Compare the generation and sequence so a
late success cannot undo a newer auth failure, relogin or disconnect. Derive ownership from the
database registration, never from a webhook's claimed user ID.

Events: `account_connected {external_account_id, verified_plan?}`, `verify_failed {reason_code}`,
`auth_lost`, `cooldown {until, reason_code}`, `container_died`. The identity field is already
hashed; never include tokens, prompts, raw provider responses or account JSON. Use a persistent,
bounded manager outbox for delivery retries and reconcile after missed events. Add
`publish_account_status_changed` in `events.py`; reuse the existing
`publish_model_provider_request_failed` integration with sanitized errors.

## 4. Runner image and process contract

Build from a pinned Node slim image with CA certificates and a health-check client. Install a
pinned official Claude Code release with verified package/artifact integrity. Local CLI help
was checked at **2.1.296** on 2026-10-11; this is a protocol-spike candidate, not a completed
container compatibility test. Do not install a floating `latest` version or auto-update a running
image. Follow the [official installation instructions](https://code.claude.com/docs/en/setup).

Create UID/GID 10001, its home and a clean work directory in the image. Persist the complete
runner home, including credential files and resumable sessions, in one account volume; set
`HOME=/home/runner` and `CLAUDE_CONFIG_DIR=/home/runner/.claude` explicitly. Initialize the volume
owner to 10001:10001 and mode 0700 before starting the unprivileged process. Persisting only
`~/.claude` must not accidentally discard state that the pinned CLI writes to `~/.claude.json`.
Never mount host homes, app data or another account's volume.

Container requirements (schematic Docker SDK configuration, not a deployment script):

```python
docker.containers.create(
    PINNED_RUNNER_IMAGE,
    name=f'claude-runner-{deployment}-{runner_id}',
    user='10001:10001',
    environment={'HOME': '/home/runner', 'CLAUDE_CONFIG_DIR': '/home/runner/.claude'},
    volumes={volume: {'bind': '/home/runner', 'mode': 'rw'}},
    mem_limit='512m',
    nano_cpus=350_000_000,
    pids_limit=128,
    cap_drop=['ALL'],
    security_opt=['no-new-privileges:true'],
    read_only=True,
    tmpfs={'/tmp': 'rw,nosuid,nodev,size=64m,mode=1777'},
    network=CONTROL_NETWORK,
    labels=owned_labels,
    restart_policy={'Name': 'no'},
    init=True,
)
# Attach the controlled outbound network before starting under a reserved slot.
```

Use SDK `nano_cpus`, not the Docker CLI's `cpus` option; see the
[Docker SDK reference](https://docker-py.readthedocs.io/en/stable/containers.html).
The manager restarts eligible crashed runners under the same admission lock. Unconditional
`unless-stopped` on every runner would let Docker restart containers outside that lock after
a host reboot. The manager service itself may use `unless-stopped`.

`server.js` exposes authenticated endpoints on 8090:

- `GET /healthz`: process uptime, CLI version, busy flag and last verified authentication state.
  File presence is not a live credential check. Probe the pinned CLI's `claude auth status --json`
  and a bounded verification request; distinguish unknown, expired and verified credentials.
- `POST /v1/chat`: `{task_id, attempt_id, model, prompt, system_prompt, session_id?, max_turns}`,
  with model/size/deadline limits supplied by the backend. Respond with NDJSON. One active child
  and no internal queue: a second request returns `409 {retry_after}` before starting the CLI.
  Scheduling/retries belong to the gate, not an unbounded in-memory FIFO.

Use `spawn` with an argument array and no shell. Send prompt text on stdin. The proposed pinned
CLI arguments are:

```text
claude -p --output-format stream-json --verbose --include-partial-messages
  --safe-mode --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}'
  --tools "" --disable-slash-commands --system-prompt <system_prompt>
  --model <allowed_model> --max-turns <bounded_turn_count>
  [--resume <server_owned_session_uuid>]
```

Here quotes describe argument boundaries; pass actual empty strings in the array. Verify this
combination in the spike, including an empty system prompt and model selection. Safe mode keeps
authentication while disabling customizations; the image must also contain no managed hooks or
MCP configuration. `--bare` does not read subscription OAuth credentials and cannot replace this
configuration. See [CLI flags](https://code.claude.com/docs/en/cli-reference) and
[authentication](https://code.claude.com/docs/en/authentication).

Parse NDJSON across arbitrary byte chunks, with line/output size limits and backpressure.
Normalize `system/init` session metadata, partial `stream_event` deltas, terminal `assistant`
messages, `result` usage and verified rate-limit events. Do not emit the final assistant text
again after already forwarding its deltas. Missing final results and nonzero exits are failures.
See [programmatic streaming](https://code.claude.com/docs/en/headless#stream-responses).

Watchdog: no valid NDJSON event for 180 seconds terminates the CLI process group and emits
`error {kind:"hang"}`. Also enforce an absolute deadline; terminate on client disconnect,
deletion or kill-switch shutdown. Use a short graceful termination then kill the group if it
does not exit. Release the busy flag and slot lease in every exit path. A stream that never
finishes must not keep a runner occupied by sending harmless heartbeat lines forever.

## 5. Connect flows

### A. Proposed MVP: setup-token

1. The approved owner chooses the plan, label and sharing preference. The backend creates a
   durable `runner_registration` and asks the manager to provision its UUID. Limit pending
   registrations and expire abandoned attempts after 15 minutes; reconcile their cleanup.
2. The owner runs `claude setup-token` on their computer and completes Anthropic's browser
   authorization. Submit the result through the owner-authenticated HTTPS form, never a URL,
   chat message or analytics event. Clear the field after submission.
3. The backend streams a bounded body to the manager. The manager writes a private mode-0600
   token file atomically inside the mode-0700 account volume. The wrapper reads it only to set
   `CLAUDE_CODE_OAUTH_TOKEN` in the CLI child environment. Never store the token in Docker
   container environment configuration, where `docker inspect` would expose it.
4. The manager verifies the credential with a small one-turn request and verifies stable
   identity as required in §2. The UI makes clear that verification uses a small amount of the
   subscription. Successful, deduplicated `account_connected` creates/links the account and
   sets it to `ok`. Sharing and credits follow §2 in a transaction.

The [documented token mechanism](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
uses an environment variable and a long-lived, model-request-scoped token; do not invent a CLI
credential JSON format or assume a token can retrieve account details. Do not assume it refreshes
like a normal interactive login. Expiry or revocation leads to `needs_login` and owner notification.

**7.0 release condition:** capture sanitized fixtures showing verified stable identity and its
relationship to the active token. If flow A cannot establish this using a supported mechanism,
keep it pending and promote an owner-driven CLI login alternative before shipping connection or
credits. A successful text response alone does not satisfy this condition.

### B. Optional: owner-operated browser terminal

A later `ttyd` flow may expose only that owner's runner through an authenticated, short-lived
HTTPS/WebSocket session so they operate the official CLI login themselves. It needs its own
tested redirect, OAuth state/PKCE and timeout design. Do not promise that rewriting a callback
URL's host will work: provider redirect validation may reject it. Terminal URLs must be
single-use and owner-bound, and no terminal port is published directly.

### C. Optional: local helper

A helper may run the official login in an isolated local `CLAUDE_CONFIG_DIR` and transfer the
minimum required credential files over a one-use authenticated upload. Define an explicit file
allowlist, size limits and path/symlink checks. Never import an arbitrary `.claude` directory with
hooks, plugins, unrelated credentials or other users' sessions. This is a separate extension,
not a prerequisite if flow A proves sufficient.

## 6. Chat routing and allowance integration

Keep `utils/chat.py:generate_chat_completion` as the common gate entry and cover direct model
routes as Phase 4 requires. Extend the account model IDs as `account:claude_sub:<model_id>`.
The pure `decide()` engine sees ordinary eligible accounts and capacity weights; it has no Docker
dependencies. `utils/claude_runner.py` starts the selected runner under admission control, calls
the manager chat proxy and translates its NDJSON into the UI's existing SSE shape.

Server-side session affinity is keyed by member, chat, account, model and system-prompt version.
Never accept an arbitrary resume ID from a browser or reuse a session across members. A changed
system prompt, edited/forked history or account switch starts a fresh session with the correct
chat history. A valid resume sends only the new turn, avoiding duplicated history. Test the CLI's
system-prompt snapshot behavior and session persistence explicitly.

`--resume` is conversation continuity, not request idempotency. The CLI can retry internally;
the adapter must not multiply those attempts. Allow at most two automatic transport/start
attempts only when no provider request was dispatched. After a hang or partial/ambiguous output,
record the attempt, show an interrupted response and require an explicit user retry. Never
silently replay potentially consumed work or double-charge a repeated terminal usage event.

Only interactive chat uses `claude_sub`. Enforce this using trusted server request context,
including denial through personal API keys and direct proxy/Responses paths. User-controlled
`source="chat"` is not authorization. Background tasks, compaction, tools/subagents, scheduled
jobs and workflows go to configured API/worker providers. The repo's
`DEFAULT_SYSTEM_MODEL_FAMILY='luna'` already expresses a worker preference; use the actual
configured catalog, not an assumed model name's availability.

When the allowance engine selects a worker tier, route to an eligible worker account/provider;
do not consume a Claude subscription for automated fallback work. Record the actual selected
provider and present model changes according to the global mode. In `observe`, record a
quota-driven swap without performing it; the always-on background exclusion still applies.

No automatic Claude reset. Tier 5 displays a provider-observed reset time when available, otherwise
an explicit unknown/retry status and available alternatives. Never invent a reset time from an
estimated token count. The roadmap's reset redemption remains Codex-only.

## 7. Metering

Write to Phase 2's ledger with `provider='claude_sub'`, actual account/model, member/chat/request
IDs, source and attempt identity. Connect smoke tests are verification traffic, not member chat
and earn no credit. Record their usage in the ledger under an explicit verification source added
to the Phase 2 contract. Do not log prompts or credentials in usage records.

Map verified `result.usage` fields for input, output and cache read/write tokens. Reasoning usage
may be unavailable; preserve unknown values rather than fabricating a count. Check whether
each field is per invocation or cumulative across resumed sessions, and store only the correct
increment. Extend Phase 2 fields for cache read/write if necessary; avoid double-counting cached
tokens. Deduplicate terminal results and record uncertain consumption after interrupted attempts.

Rate-limit stream events may supply `5h`/`weekly` observations, but the exact event name and
fields must come from pinned-version fixtures. Store authoritative fields in `account_window`
with `source='stream'` and observation time; extend source validation/migrations if required.
An error saying a limit was hit is not necessarily a numeric used percentage or reset time.

Token-to-unit calibration is an estimate until matched against authoritative window changes.
Expose source, age and confidence. If no provider observations exist, show unknown provider
headroom and a separately labeled local estimate. No polling endpoint is assumed in this phase.
Outside usage cannot be measured from runner silence: it needs two actual total-quota readings,
whose delta can be compared with local ledger usage.

The main roadmap's ±10% calibration target applies only where independent provider readings
exist. Save samples and measurement intervals as evidence. If they do not exist, that acceptance
check is unavailable and quota-based enforcement is not ready; never claim token estimates
validated themselves. A later decision can define an explicit conservative local-budget policy.

## 8. Lifecycle and resources

Enforce the live-runner ceiling atomically across creates, starts, verification, chat and restart
reconciliation. Count starting and draining containers toward the ceiling. One manager process
owns admission initially; multiple managers require a shared host-wide lease. If another
deployment on this host ever runs a manager, the configured allocations must sum to no more than
the host ceiling.

When a slot is needed, stop the least recently used **idle** runner and keep its volume. Never
evict an active request for LRU. If all slots are busy, return a bounded retry response. Reconcile
every 30 seconds and on startup; inventory owned Docker labels, expire abandoned registrations,
retry pending deletion/events and restart only eligible accounts under admission control.
Measure cold-start latency; 2–5 seconds is a target to validate, not an acceptance assumption.

The effective switch is the conjunction of deployment enablement, the runtime database switch
and global limits mode not being `off`. An environment variable cannot change inside a running
process without restart: use `Config.get('mologate.claude_runners.enabled')` plus an authenticated
manager policy update/lease for runtime disablement. Off rejects new work immediately and stops
containers after a bounded drain, retaining volumes. Expired policy leases fail closed to new
work. Re-enabling never wakes all registrations at once.

Use bounded exponential cooldown for provider 429s, honor known reset/Retry-After values and
classify errors from verified fixtures. A configurable error-rate circuit breaker pauses an
account after enough 429/auth failures. Stop idle cooled-down runners without deleting them.
An invalid/expired login becomes `needs_login`, not an endless restart loop. Notify the owner
through the account status event. Do not instantly rotate through the whole pool on a provider
rate-limit response; retries/failover have a bounded budget and delay.

## 9. Traffic safeguards

These are load and fairness controls, not assurances against provider enforcement:

1. Per-account concurrency is one in both manager admission and the runner process.
2. Start with at most eight requests per minute per account, configurable within a conservative
   range; apply the shared limiter across restarts and all callers. Add 1–4 seconds of jitter
   to paced dispatch/backoff to avoid synchronized retries.
3. Require an explicit daily-unit cap before enabling team traffic. Calibrate units in the
   spike/observe period; an unset cap is not unlimited. Separate small verification budgets
   allow the spike while normal chat remains disabled.
4. For the first seven days, apply half the configured daily cap. The optional activity-hours
   schedule is an owner/admin preference with an explicit timezone, not simulated human activity.
5. Keep the existing VPS egress IP, with no rotating proxies or client impersonation. Do not
   treat a fixed IP as a way to bypass aggregate provider limits.
6. Reuse sessions only under §6's ownership rules. Bound turns and wall time; automated agent
   loops remain on API/worker providers.

## 10. Configuration

Follow `Config.get('dotted.key')` and env defaults in `config.py`. Secrets are deployment-only,
excluded from config exports and readable only by the services
that need them. The following are proposed defaults, not environment variables implemented today:

| Environment/config                   | Default / purpose                                              |
| ------------------------------------ | -------------------------------------------------------------- |
| `MOLOGATE_CLAUDE_RUNNERS`            | `off`; deployment enablement ceiling                           |
| `mologate.claude_runners.enabled`    | `false`; runtime switch, requires deployment enablement        |
| `RUNNER_MANAGER_URL`                 | `http://runner-manager:8085`; fixed/allowlisted service origin |
| `RUNNER_MANAGER_API_SECRET`          | No default; service authentication, distinct from webhook key  |
| `RUNNERS_WEBHOOK_SECRET`             | No default; mandatory when enabling the integration            |
| `CLAUDE_MAX_LIVE_RUNNERS`            | `2` ceiling; set to `1` until load tests pass                  |
| `CLAUDE_PLAN_MULTIPLIERS`            | `{"pro":1,"max_5x":5,"max_20x":20}`; admin-adjustable          |
| `CLAUDE_RPM_CAP`                     | `8` per account                                                |
| `CLAUDE_DAILY_UNITS_CAP`             | Unset; must be calibrated/configured before team traffic       |
| `CLAUDE_STREAM_IDLE_TIMEOUT_SECONDS` | `180`                                                          |
| `CLAUDE_REQUEST_TIMEOUT_SECONDS`     | `600` absolute maximum, configurable downward                  |

Also define bounded registration lifetime, one active registration per owner, manager policy
lease lifetime, breaker threshold/minimum sample count and maximum cooldown. Each runner needs
its own service credential, installed as a private file; a runner must not hold the manager's
Docker-control credential. The manager's non-Claude control state and outbox have their own
private persistent volume.

## 11. Tests and acceptance

Extend `backend/open_webui/test/limits/`; use `node --test` for the wrapper. Mock service calls
for deterministic tests and limit real-provider checks to explicitly connected test accounts.

- Connect: create, one-use credential generation, verify, signed event, account becomes `ok`,
  shared eligibility and Phase 5 once-ever grant. Cover duplicate identity, disconnect/reconnect,
  private accounts, plan correction, abandoned registrations and resource cleanup.
- Authorization: wrong owner, admin inference into private accounts, credential replacement,
  forged/replayed/out-of-order events, deleted registrations and arbitrary manager URL/path.
- Secret handling: invalid bodies, validation failures, enabled audit logging, proxy exceptions
  and CLI failures never echo token material. Control-plane secrets never reach the browser.
- Process/stream: split lines and UTF-8, malformed/oversize output, one active child, watchdog,
  cancellation, missing result, duplicate terminal event and complete process-group cleanup.
- Session isolation: two members sharing one account cannot resume each other's conversation;
  edits, branches and system changes preserve the intended history without duplicate turns.
- Policy: table-driven `decide()` cases for `claude_sub`; sticky account selection, worker/source
  exclusions on every endpoint, unknown windows, observe versus enforce and no Claude reset.
- Lifecycle: three registered accounts with ceiling two, concurrent start races, busy LRU
  exclusion, manager/Docker/host restarts, policy lease expiry, kill-switch and 429 storms.
- Host: measure peak RSS, swap pressure, CPU and chat latency with the existing application
  stack. Verify outbound provider access and that 8085/8090/Docker remain unreachable externally.

## 12. Small PR sequence

Keep PRs independently reviewable, following the roadmap's eventual landing order into `main`.
Use the same `limits-and-accounts` working branch throughout; the steps below do not require
separate branches.
Do not deploy a credential-bearing service before authentication, caps and disablement work.

| PR  | Scope                                                            | Acceptance                                                                                                                                                      |
| --- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 7.0 | Authentication/identity/protocol and memory spike                | Pinned version, sanitized fixtures, verified stable identity, quota visibility documented, resource measurements. Resolve flow A before promising it in UI.     |
| 7.1 | Manager and compose overlay                                      | Authenticated create/start/stop/delete with a harmless test image, labeled volumes, slot cap, kill-switch and outbound isolation. No real credentials required. |
| 7.2 | Official runner image and wrapper                                | A connected test runner streams text and correct usage; concurrency, watchdog and disconnect cleanup pass.                                                      |
| 7.3 | Backend API, redaction, registrations/events, notifications      | Owner connects through the API to a verified `ok` account; duplicate/replayed events and identities have no duplicate effects.                                  |
| 7.4 | Metering/account-window integration, after Phase 2               | Ledger reconciles against available provider readings within ±10%; missing authoritative readings are an explicit unresolved criterion.                         |
| 7.5 | Selection/stickiness, worker rules and credits, after Phases 3–5 | Roadmap-style scenarios pass in observe, no background subscription use, grants persist across reconnects, no automatic reset.                                  |
| 7.6 | Reconciliation, LRU and circuit-breaker resilience               | Three accounts never exceed the live ceiling, off stops runners, restart races and provider failure storms preserve state.                                      |
| 7.7 | Connect UI and account/context status                            | Owner completes the selected flow without SSH; flow A still requires local `claude setup-token`. Status and unknown quota are clear.                            |
| 7.8 | Observe and rollout                                              | At least one week of real traffic, accepted metering/isolation/load evidence, rollback procedure verified, then configured enforcement.                         |

No fixed line-count target for manager or wrapper: lifecycle, cancellation and identity checks
are part of the feature, not optional follow-ups. Minimal safety/resource controls ship in 7.1;
7.6 exercises their resilience rather than adding the first cap or kill-switch.

## 13. Documentation and operation

Keep main roadmap §§3, 6, 8, 9, 12 and 14 consistent with this specification. Update
[the VPS deployment guide](../deploy/vps/README.md#phase-7-claude-runners-planned) when the actual
compose overlay ships. Record image digests, tested CLI schema, resource measurements and the
selected identity mechanism. Do not mark the phase done from mocked tests alone.

Before first credentials, add encrypted/restricted runner-volume backups with a documented
retention policy and a restore test. There are no backups during development, and app-data
backups, once they return, will not automatically cover new Docker volumes. Treat resumed
conversation files as sensitive as credentials; apply chat retention and owner-deletion policy.
Disconnect deletes the live volume after drain; backup expiry follows the documented retention
schedule, and owners can revoke credentials at Anthropic.

Rollback: disable runtime dispatch, stop/drain runners, retain account volumes and restore the
previous pinned image/config. Back up the database before migrations; restore a matching schema
when rolling back to code that cannot read the new revision. Do not touch members' existing
accounts or enable runners for team traffic as part of acceptance testing.

## 14. Open questions to resolve during implementation

1. Does flow A yield a provider-verified stable identity through a supported interface? If not,
   choose and prove the owner-operated login alternative before completing 7.3.
2. Which window observations does the pinned CLI actually emit, and are they sufficient for
   calibration? No authoritative observations means no claim of provider-percent enforcement.
3. Does one runner fit the measured memory budget, and can two coexist with the app stack?
   Keep the limit at one or move workloads if the measurements fail; swap is not extra RAM.
4. Confirm token expiry/renewal, plan changes, cancellation and 401/429 classification using
   real fixtures, without consuming more subscription traffic than the verification budget.
5. Keep the explicit provider-terms decision in roadmap §3 current. Neither flow B nor the
   unmodified binary makes shared subscription usage equivalent to personal hosted CLI use.
