# Limits and Accounts Roadmap

How Mologate connects the team's provider accounts, shares their limits fairly, and gives every
member personal API keys. This document is the spec for the work. It replaces the "Fair use" items
and the "Fair use of shared accounts" table in [ROADMAP.md](../ROADMAP.md).

Branch: `limits-and-accounts`. Land work in small PRs into `main`, one phase (or less) at a time.

---

## 1. Notes for the implementer

- **Read before writing code:** this whole document, then the files listed in
  [section 4](#4-what-already-exists). Mologate is an Open WebUI v0.11.4 fork. Reuse what Open
  WebUI has before adding anything.
- **Conventions in this repo:**
  - Every change to upstream Open WebUI code carries a short `# Mologate: ...` comment (see
    `config.py`, `routers/models.py`). New Mologate-only modules don't need it on every line.
  - Schema changes are Alembic migrations in `backend/open_webui/migrations/versions/`. Run
    `alembic heads` for the current head; never edit an existing migration.
  - Shared state goes in Redis, with an in-memory fallback when Redis is not configured (follow
    `utils/provider_limits.py` and `utils/rate_limit.py`).
  - Config uses `Config.get('dotted.key')` with env defaults in `config.py`.
  - The `ROADMAP.md` style: plain sentences, a "Done:" note under an item when it ships.
- **Tests:** there are no backend tests in the repo yet (`pytest` and `pytest-asyncio` are in
  `pyproject.toml`). Start `backend/open_webui/test/limits/`. The allowance engine
  ([section 7](#7-allowance-engine)) must be a pure function with table-driven tests covering every
  worked example in this document.
- **Modes:** everything here runs behind `MOLOGATE_LIMITS_MODE` = `off` | `observe` | `enforce`
  (default `observe` until phase 4 ships). In `observe` the gate computes decisions and records
  them but never blocks or swaps models.
- **Never** log or return secrets. Never put prompts in the usage ledger.

---

## 2. Settled decisions

These come from the team and are not up for redesign. Numbers are defaults an admin can change.

1. **Mologate enforces limits itself.** codex-lb was a UX reference only; nothing is embedded or
   forked from it.
2. **50/50 split.** Every shared account's quota, in each provider window (5-hour and weekly), is
   split in two: 50% is the **owner reserve** for the member who connected it, and 50% goes to the
   **public pool**.
3. **Idle owner loan.** If the owner has had no usage on the account for the last 24 hours, up to 25
   points of their unused reserve are lent to the pool. The loan stops as soon as the owner uses
   the account again; what the pool already consumed is not given back.
4. **Use outside Mologate.** If the owner uses the account directly (the Codex app on their laptop,
   for example), that usage counts against their reserve first, then against the pool as their own
   pool usage.
5. **Anyone can use big models** from the pool, up to their personal share.
6. **Worker tier.** Members who have used up what they were given keep access to the pool through
   cheap worker models only (GPT Luna, Claude Haiku 5.5, GLM-5.3 Flash class).
7. **Credits** are a one-time reward for each account a member adds to the pool. Members who add
   no account get none. A credit is worth 1/2.5 = 0.4 percentage points of extra pool use.
   Formula: **credits = 10 × the plan's usage multiplier**, where the provider's base paid tier is 1x.

   | Account added | Multiplier | Credits | Extra pool points |
   | --- | --- | --- | --- |
   | ChatGPT Plus | 1x | 10 | 4 |
   | ChatGPT Pro tiers (for example 5x, 20x) | the tier's multiplier | 50, 200 | 20, 80 |
   | GLM coding plan (Lite, Pro, Max) | relative to Lite, from Z.ai's published plan sizes | 10 × multiplier | 4 × multiplier |
   | Pay-as-you-go API key (Anthropic, OpenAI, OpenRouter, Z.ai) | monthly budget ÷ $20 | 10 × multiplier | 4 × multiplier |

   The admin keeps the multiplier table in settings. Read the plan from the provider when possible
   (Codex `account/read`); otherwise the member picks it when connecting and an admin can correct it.

8. **No credit farming.** Credits are tied to the provider's own account ID. One provider account
   earns credits once, ever, no matter how many times or by whom it is re-added.
9. **Reset button.** When the pool is empty, members get a button that redeems a provider limit reset
   on a pool account, through the provider's own CLI tooling.

---

## 3. Providers and what their terms allow

This decides what can go into the pool. It was checked against the providers' own pages on
2026-10-11.

| Provider | Access | Into the pool? | Limits source |
| --- | --- | --- | --- |
| **Codex / ChatGPT** (Plus, Pro, Business) | Device-code login through the official `codex app-server` | Yes (team-accepted risk, see below) | `account/rateLimits/read` (5h + weekly), `x-codex-*` headers |
| **Anthropic API key** (Claude Console) | Key pasted in by its owner | Yes | Owner-set monthly budget; `anthropic-ratelimit-*` headers for rate limits |
| **Claude Pro / Max subscription** | Only its owner, only through the unmodified Claude Code binary | Yes | Not read by Mologate |
| **OpenAI API key**, **OpenRouter key** | Key | Yes | Owner-set budget; OpenRouter also reports key usage |
| **Z.ai GLM coding plan** | Key | Yes | Spike needed (phase 1) |
| **OpenCode free models** | None (runs in a container) | Not an account | Per-user rate limit only |

**Claude subscriptions can be pooled.** Anthropic's Claude Code legal page says developers may
not "route requests through Free, Pro, or Max plan credentials on behalf of their users" and "may
not collect, store, or intermediate Claude.ai credentials or session tokens"; sign-in "must
complete through Anthropic's own flow". Therefore find the way to do it with lowering the risk of account ban. What is allowed:
- An Anthropic **API key**, configured "for use by the customer's own authorized users", billed to
  the key owner. These go into the pool like any account, with a budget instead of windows.
- A member signing in to the **unmodified Claude Code binary** with their own subscription,
  for example in their own runner container. Mologate never sees or stores that token. This is a
  later, runner-phase feature and is outside the pool.

The team wants Claude subscriptions in the pool as well. as team i say yes!

**ChatGPT accounts:** OpenAI's Terms of Use say account holders may not share their credentials or
make their account available to others. The team has accepted this risk (PLAN.md, risk 1). Lower
it: keep sharing opt-in per account, keep the owner's traffic on their own account, keep requests
per account at ordinary human volumes (per-account concurrency caps in phase 4), and prefer
Business seats and API keys where it matters.

---

## 4. What already exists

Verified on `main` at `a20a336`.

| Piece | Where | Use it for |
| --- | --- | --- |
| Provider limits from response headers | `utils/provider_limits.py`, recorded at `routers/openai.py` (`generate_chat_completion`), read by `GET /api/v1/models/model/limits` | Keep the parsers; rekey from `openai:{url_idx}` to account ID (indices break when connections are reordered). Only the chat-completions path records today; `/responses` and the proxy do not. |
| Context indicator ring and panel | `src/lib/components/chat/ContextIndicator.svelte` | Show the user's tier, share left and the account windows. |
| Connections (URL + key + per-connection config) | `routers/openai.py` (`OPENAI_API_CONFIGS`, `auth_type`, `headers`, `prefix_id`) | API-key accounts can be expressed as connections, but the gate picks the account per request. |
| Anthropic Messages conversion | `utils/anthropic.py` | Anthropic API-key accounts work through it. |
| Fernet-encrypted token storage | `models/oauth_sessions.py` | Same pattern for account secrets. |
| Per-user API keys | `api_key` table (`data` JSON, `expires_at`, `last_used_at`), `utils/auth.py`, `features.api_keys` permission, endpoint restrictions | Personal keys. `get_current_user_by_api_key` drops the key's ID; carry it on `request.state`. |
| Usage on chat messages, analytics | `models/chat_messages.py` (`usage` column), `routers/analytics.py` | Only chats are recorded: API-key calls and unsaved chats leave nothing. The new ledger fixes that. |
| Rolling-window counter | `utils/rate_limit.py` (`RateLimiter`, Redis + memory) | Per-key requests per minute. |
| Groups, permissions, access grants | `models/groups.py`, `models/access_grants.py` | Admin role, who may connect accounts. |
| Event bus | `events.py` (`publish_model_provider_request_failed`) | Detect 429 and limit errors for failover. |
| All internal model calls | `utils/chat.py:generate_chat_completion` (chat, tasks, memory, compaction, tool review, subagents through the middleware) | Where the gate's pre-check goes. |
| Default system model | `config.py` `DEFAULT_SYSTEM_MODEL_FAMILY='luna'` | Background jobs already prefer Luna, a worker model. |

There are **no** account, ledger or credit tables, and no account UI.

---

## 5. Concepts and units

- **Account:** one connected provider credential with an owner (`provider_account`).
- **Window:** a provider limit window: `5h` and `weekly` for Codex; `budget` (an owner-set monthly
  amount) for API keys. Every rule below runs per window, and a request must pass in every window
  of the account it uses.
- **Used percent `u_a`:** how much of account `a`'s window is used, from the provider (or from the
  ledger for budget windows). It includes use outside Mologate.
- **Capacity weight `c_a`:** how big account `a`'s window is in common units, so a Pro window counts
  for more than a Plus window. Starts from a plan table (ChatGPT Plus = 1) and is recalibrated from
  data (phase 2).
- **Unit:** one percent of a weight-1 window. A request that moves account `a` by `Δ` percent costs
  `c_a × Δ` units.
- **Pool capacity `C`:** the total units in the pool for a window (formula in §7).
- **Pool points:** percentages of `C`. Credits are measured in pool points.
- **Model cost weight `k`:** expected units per weighted token for a model on a provider, learned
  in phase 2. It lets the gate estimate a request's cost before the provider reports it.
- **Worker model:** a model on the admin's worker list, for example `gpt-luna`, `claude-haiku-5-5`
  and `glm-5.3-flash`. Each provider can have its own.

---

## 6. Data model

New tables (Alembic migrations), all with `created_at` and `updated_at` epoch columns.

**`provider_account`**
`id`, `owner_user_id`, `provider` (`codex` | `anthropic_api` | `openai_api` | `openrouter` |
`zai`), `plan` (`plus`, `pro`, `business`, `api`, ...), `external_account_id` (the provider's own
ID: ChatGPT account ID, or a SHA-256 fingerprint of an API key), `label`, `secret` (Fernet-encrypted
JSON; for Codex, the path of its isolated `CODEX_HOME`), `sharing` (`shared` | `private` |
`paused`), `capacity_weight`, `budget` (API keys: amount and currency per month), `allow_team_resets`
(bool, default true), `status` (`ok` | `needs_login` | `error` | `cooldown`), `last_error`.
Unique: (`provider`, `external_account_id`).

**`account_window`** (latest snapshot per account and window; history in the ledger)
`account_id`, `window` (`5h` | `weekly` | `budget`), `used_percent`, `resets_at`, `source`
(`poll` | `headers` | `ledger`), `observed_at`.

**`usage_event`** (one row per provider request)
`id`, `ts`, `user_id`, `api_key_id` (nullable), `account_id`, `provider`, `model_id`, `source`
(`chat` | `subagent` | `task` | `api_key` | `scheduled` | `workflow` | `outside`), `tier`
(`reserve` | `pool` | `credit` | `worker` | `owner_outside`), `input_tokens`, `output_tokens`,
`cached_tokens`, `reasoning_tokens`, `est_units`, `units` (after calibration), `credits_charged`,
`status` (`ok` | `error` | `limited`), `chat_id`, `request_id`. Index on (`user_id`, `ts`) and
(`account_id`, `ts`). `outside` rows are the unexplained jumps from §8.

**`credit_grant`**
`id`, `user_id`, `provider`, `external_account_id`, `plan`, `amount`, `granted_at`, `forfeited_at`.
Unique: (`provider`, `external_account_id`), which is what stops credit farming.

**`credit_entry`** (append-only balance changes)
`id`, `user_id`, `delta`, `reason` (`grant` | `spend` | `forfeit` | `admin`), `usage_event_id`,
`ts`.

**`limit_override`**
`id`, `user_id`, `granted_by`, `extra_points`, `until`, `reason`.

**`limit_reset_event`**
`id`, `account_id`, `pressed_by`, `credit_id` (provider's), `result`, `windows_reset`, `ts`.

Personal API keys stay in the existing `api_key` table. Allow several named keys per user, with
`data = {name, limits: {rpm, concurrency, models, efforts, daily_points}, revoked_at}`.

---

## 7. Allowance engine

A pure function: `decide(request, state) -> Decision`. `state` holds the accounts, their windows,
the requester's usage in each window, the member count, credits and overrides. `Decision` holds the
tier, the chosen account, the model to use (possibly swapped to a worker), the estimated cost and
a human-readable reason.

### 7.1 Per-account numbers (per window)

```
R      = 50                                   owner reserve, in account percent
o_a    = owner's attributed use of a          (Mologate use + outside use)
L_a    = min(25, max(0, R - o_a))  if the owner had no use of a in the last 24 h, else 0
lock_a = max(0, R - o_a - L_a)                reserve still locked for the owner
pool may use a while  u_a < 100 - lock_a
owner may use a while u_a < 100
```

The owner's use counts against `R` first. Beyond `R`, the owner draws on the pool and that use counts
toward their personal share, like anyone else's.

### 7.2 Pool and personal share (per window)

```
C        = sum over shared accounts a of  c_a * (50 + L_a)          pool capacity, units
N        = number of team members (all, active or not; min 1)
share_u  = C / N                                                    units
extra_u  = credits_u * 0.004 * C  +  active overrides               units
used_u   = units of u's pool use in this window (tiers pool, credit, worker, owner overflow)
```

### 7.3 Tiers, checked in order for the model's provider

1. **Reserve:** the requester owns a shared or private account of this provider with
   `o_a < R` and `u_a < 100`. Use it, with any model.
2. **Pool:** `used_u < share_u` in every window, and some account has pool headroom. Any model.
3. **Credit:** `used_u < share_u + extra_u` in every window. Any model. The part above `share_u` is
   charged in credits, using the weekly window: `credits = over_units / (0.004 * C_weekly)`.
4. **Worker:** past all of the above, but some account still has pool headroom. Only worker models.
   If a big model was asked for, the gate swaps to the provider's worker model and the UI says so
   ("Your share is used up, so this reply uses Luna. Resets in 3 h 12 min.").
5. **Empty:** no account of this provider has pool headroom. Return a limit error with the reset
   time, and offer a worker on another provider and the reset button (§10).

### 7.4 Choosing the account (tiers 2 to 4)

Among this provider's shared, healthy accounts that have pool headroom, prefer the chat's previous
account (sticky, so prompt caches stay warm). Otherwise pick the account with the most pool headroom
times `c_a`. Skip `paused`, `cooldown` and `needs_login` accounts. Never pick an account whose
per-account concurrency cap (phase 4) is full.

### 7.5 Worked examples (write these as tests)

Accounts: **A** is ChatGPT Plus owned by Ann (`c=1`); **B** is ChatGPT Plus owned by Bob (`c=1`).
Members: Ann, Bob, Cat (no account). Weekly window unless stated.

1. Nothing used yet. `C = 50 + 50 = 100`, `N = 3`, `share = 33.3` units each. Cat uses
   big models until her pool use reaches 33.3 units. The next request is tier 4 and gets Luna.
2. Ann has used 10% of A. `lock_A = 40`, so the pool may use A until `u_A` reaches 60.
3. Ann hasn't touched A for 24 h and has used 0 this window. `L_A = 25`, `C = 125`, `N = 3`, so
   `share = 41.7` units each.
4. Ann then sends a request. `L_A` drops to 0. If the pool had already pushed `u_A` to 70, Ann is
   left with 30% of A, not 50%. This follows decision 3.
5. Ann's own Codex app use moves `u_A` from 20 to 35 with no matching Mologate traffic. That counts
   as 15 of Ann's reserve (`owner_outside`). Once her reserve is spent, outside use counts as her
   pool use.
6. Bob has 10 credits (from adding B). With `C = 100`, his share extends by `10 * 0.004 * 100 = 4`
   units. Using 2 of them costs 5 credits.
7. 5-hour window: Cat has share left this week but has used her 5-hour share. She is in tier 4
   until the 5-hour window moves on.
8. A and B both reach `u = 100 - lock`. Every non-owner gets tier 5. Ann and Bob still use their
   locked reserves (tier 1).

---

## 8. Metering and calibration

- **Ledger:** the gate writes a `usage_event` for every provider request: chat, subagent, task,
  API key, scheduled, workflow. Hook the post-step where the response's `usage` is known, in
  `routers/openai.py` for `generate_chat_completion`, `responses` and `proxy`, and in the Codex
  adapter.
- **Polling:** read each Codex account's windows every 5 minutes through `account/rateLimits/read`
  (more often while it is busy), and after every response from its headers. Store them in
  `account_window`.
- **Outside use:** between two readings of `u_a`, the expected change is the sum of `units` of the
  ledger rows in that interval. An increase larger than that, beyond a noise margin, becomes an
  `outside` row attributed to the owner (decision 4).
- **Calibration:** for each provider and model, fit `k` (units per weighted token, with input, cached,
  output and reasoning tokens as separate features) by least squares over polling intervals. Refit
  daily. Until a model has enough samples, use the provider's most expensive known `k` (a
  conservative estimate). Also refit `c_a` across accounts.
- **Run `observe` for at least a week** of real team use before switching to `enforce`. Compare
  decisions to what actually happened.

---

## 9. Adding accounts

**Codex (first, and the easiest):**
1. The member clicks **Connect account → ChatGPT / Codex**.
2. Mologate starts an isolated `codex app-server` for the new account, with its own `CODEX_HOME`
   under the data volume (mode 0700), and calls `account/login/start` with
   `{"type": "chatgptDeviceCode"}`.
3. The UI shows the code and the URL. The member finishes the login on their own device, through
   OpenAI's flow.
4. On `account/login/completed`, Mologate calls `account/read` to get the plan and the ChatGPT
   account ID, creates the `provider_account`, grants credits if that account ID never earned any,
   and reads its windows.
5. app-server refreshes tokens itself. On an auth failure, mark the account `needs_login` and
   notify the owner.

The methods above are in OpenAI's open-source `openai/codex` app-server protocol (the Codex TUI
uses the same ones). **Inference decision (2026-10-11):** use app-server 0.162.1 for device login,
token refresh, model catalogs and windows. Send inference to the Codex Responses endpoint with
those managed credentials. Translate its SSE text/tool deltas and final usage into Chat Completions
for Mologate; preserve native SSE for `/responses`. No chat prompts or tool execution go through
app-server. Protocol and streaming regressions are covered; two live browser connections remain
the final acceptance check.

**API keys** (Anthropic, OpenAI, OpenRouter, Z.ai): a form with the key and a monthly budget, a
"Verify" call, then the account is created. `external_account_id` is the key fingerprint, so the
same key can't earn credits twice.

**Claude subscriptions:** connect flow (see §3). decide yourself how.

**Account card:** owner, provider, plan, windows as two-part bars (owner reserve and pool), status,
sharing toggle (shared, private, paused), "allow team resets", disconnect.

**Disconnecting:** unspent credits from that account's grant are forfeited (`credit_entry.forfeit`).
Reconnecting the same account doesn't grant again.

---

## 10. Reset button

- **Codex only.** No reset mechanism exists for the other providers.
- **Shown when** the requester is in tier 5 for Codex and at least one pool account has
  `allow_team_resets` and an unredeemed reset (`account/rateLimits/read` returns
  `rateLimitResetCredits`).
- **Pressing it:** a confirmation names the account, its owner, what will reset and when the reset
  coupon expires. It picks the coupon that expires soonest, calls
  `account/rateLimitResetCredit/consume` with a fresh idempotency key (reuse it on retries), and
  writes a `limit_reset_event`.
- **Rules:** a redemption is irreversible and can't be transferred. Any team member can turn
  resets off globally (existing roadmap item). The account owner can turn them off per account.
- The refilled window is split 50/50 like any other.

---

## 11. Personal API keys

- Turn on `ENABLE_API_KEYS` and the `features.api_keys` permission for members.
- Several named keys per member; the key's ID is carried through auth into `request.state` and the
  ledger.
- Each key's use counts toward its owner's share and tiers. A key can be limited further but never
  beyond its owner: requests per minute (`RateLimiter`), concurrency, allowed models and efforts,
  daily pool points, expiry.
- Restrict keys to the gated endpoints (`/api/chat/completions`, `/api/models`, and the new limits
  endpoints) with the existing endpoint restrictions, so a key can't reach `/openai/*` directly
  and skip the gate.
- A "my keys" page shows each key's use from the ledger.

---

## 12. Phases

Each phase lists what must be true to call it done.

### Phase 0: host on the VPS

**Done: 2026-10-11.** All Phase 0 acceptance checks below are complete.

Hosting files and the deployment/recovery procedure are in
[deploy/vps/README.md](../deploy/vps/README.md). Installed and verified on
2026-10-11: production and isolated staging have HTTPS and nightly backups.
At the owner's subsequent request, production allows registration with admin
approval for new accounts; staging keeps signups closed.
A regular team account completed a chat, and a production
database backup was restored successfully. The full external TCP scan and
direct confirmation found only ports 22, 80 and 443 reachable.

- [x] The VPS runs `main` behind HTTPS (details in [§13](#13-hosting)).
- [x] Team onboarding: registration enabled with admin approval; admins can also create accounts.
- [x] Nightly backup of the database and data volume.
- [x] Done when: the team can log in and chat, and nothing but 22, 80 and 443 is reachable.

### Phase 1: accounts

**In progress: 2026-10-11**, on `phase1-accounts`, based on `limits-and-accounts`.
Implementation and verification notes: [Phase 1 accounts](phase1-accounts.md).
Deployed to staging; 21 automated regressions, real Z.ai normal/streamed chats, and desktop/mobile
browser checks pass. The remaining acceptance check is two real Codex accounts serving chats.

- [x] `provider_account` table, encrypted secrets, account CRUD API (owner or admin).
- [x] Codex device-code connect through `codex app-server` (§9), with the inference spike decided.
- [x] API-key accounts (Anthropic, OpenAI, OpenRouter, Z.ai) with verification.
- [x] Models come from providers, not connection indices; the gate chooses the account per request.
- [x] Provider limits rekeyed by account ID; `/responses` and the proxy record them too.
- [x] Z.ai spike: coding-plan quota endpoint reports 5-hour and weekly `CREDIT_LIMIT` windows;
  the model-list response has no rate-limit headers. Empty/unknown quota stays unknown.
- [ ] Done when: two members connect Codex accounts from the browser and both serve chats.

### Phase 2: metering (observe-only)
- [ ] `usage_event` ledger written for every source, including API keys and unsaved chats.
- [ ] Account polling, outside-use detection, calibration jobs.
- [ ] Accounts page and a "my usage" page (read-only numbers).
- [ ] Done when: after a week, ledger units match the provider's percent changes within 10% per
      account.

### Phase 3: allowance engine
- [ ] `decide()` as a pure module with every example in §7.5 as a test, plus a simulation that
      replays a week of ledger data under the rules.
- [ ] `MOLOGATE_LIMITS_MODE=observe` records decisions on every request.
- [ ] Done when: tests pass and the simulation shows no member blocked while the pool has headroom
      for their tier.

### Phase 4: enforcement
- [ ] Gate on: tiers, account choice, worker swap with a notice, limit errors with reset times.
- [ ] Failover on 429 or limit errors to another account in the same tier.
- [ ] Per-user concurrency cap (3 chats generating, from ROADMAP.md) and per-account concurrency
      cap.
- [ ] Context indicator shows tier, share left, credits and the account windows; warning at 80% of
      the share with an estimate of minutes left from the last 30 minutes of use.
- [ ] Admin and team overrides (`limit_override`), with an audit log.
- [ ] Done when: in `enforce`, the §7.5 scenarios behave as written on the staging site.

### Phase 5: credits and resets
- [ ] Grants on connect, forfeit on disconnect, spending in tier 3, balance in the UI.
- [ ] Reset button (§10).
- [ ] Done when: a reset is redeemed from the browser on a test account and the windows refill.

### Phase 6: personal API keys (§11)
- [ ] Done when: a member uses Mologate from an outside OpenAI-compatible client with their own
      key, and that use shows up in their share.

### Later
- Global limits on the status page.

---

## 13. Hosting

**When:** from the start. Phase 0 runs before or alongside phase 1, not at the end, because:
- calibration (phase 2) needs a week of real team traffic on a real deployment;
- device-code logins, provider headers and polling can only be tested against real accounts;
- this VPS is small (1 vCPU, 3.8 GB RAM, 42 GB free disk), and that is better found out early;
- the implementer needs somewhere to check each phase.

**Setup on this VPS (Ubuntu 24.04, x86_64, IP 5.83.140.221):**
- Docker and Docker Compose. Don't build images here: one vCPU can't build the frontend in
  reasonable time. The repo's `docker.yaml` workflow publishes images to
  `ghcr.io/pmixay/mologate` from `main` and a slim-only image from `limits-and-accounts`.
  Documentation/deployment-only pushes skip image builds. The unused Docker Hub copy step
  was removed. The package is private, so the VPS needs a token with `read:packages`.
- Containers: Mologate (the slim image, with the embedding model offloaded to an API, since local
  embeddings would eat the RAM), Postgres, Redis, Caddy. Add a 4 GB swap file.
- Caddy handles HTTPS on a domain (or `5-83-140-221.sslip.io` until there is one). The firewall
  allows only 22, 80 and 443.
- **Staging:** a second, smaller stack on `staging.<domain>` that runs the `limits-and-accounts`
  branch's image (add the branch to the workflow's triggers), with its own database. The team uses
  production; the implementer tests on staging.
- Backups: nightly `pg_dump` and a copy of the data volume, kept 7 days, later off the box.

---

## 14. Decisions log

- 2026-10-11: personal share is the pool divided equally among all members.
- 2026-10-11: credits = 10 × plan multiplier for every poolable account; API keys by budget.
- 2026-10-11: idle owner loan: no owner use for 24 hours lends up to 25 points.
- 2026-10-11: hosting on the VPS from the start: https://5-83-140-221.sslip.io, compose stack in
  `/opt/mologate`; GLM coding plan connected and Exa web search on.

---

## Sources

- OpenAI Terms of Use, registration and access: https://openai.com/policies/terms-of-use/
- Codex app-server protocol, `openai/codex`: `codex-rs/app-server-protocol` (`account/login/start`,
  `account/rateLimits/read`, `account/rateLimitResetCredit/consume`)
