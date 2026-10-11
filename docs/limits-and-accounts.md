# Limits and Accounts Roadmap

How Mologate connects the team's provider accounts, shares their limits fairly, and gives every
member personal API keys. This document is the spec for the work. It replaces the "Fair use" items
and the "Fair use of shared accounts" table in [ROADMAP.md](../ROADMAP.md).

Working branch: **`limits-and-accounts` only**, for the roadmap and all implementation phases.
Do not create separate phase branches. Keep changes reviewable and submit them to `main` from
this branch; the current draft is [PR #3](https://github.com/pmixay/MoloGate/pull/3).

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
- **Tests:** Phase 1 added stdlib `unittest` regressions in `backend/open_webui/test/limits/`
  (`pytest-asyncio` is also in `pyproject.toml`). Extend that directory. The allowance engine
  ([section 7](#7-allowance-engine)) must be a pure function with table-driven tests covering every
  worked example in this document.
- **Modes:** everything here runs behind `MOLOGATE_LIMITS_MODE` = `off` | `observe` | `enforce`
  (default `observe` until phase 4 ships). In `observe` the gate computes decisions and records
  them but never blocks or swaps models for allowance reasons. Authentication, account privacy,
  provider backoff, resource caps and disabled integrations still apply in every mode.
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

   | Account added                                               | Multiplier                                         | Credits         | Extra pool points |
   | ----------------------------------------------------------- | -------------------------------------------------- | --------------- | ----------------- |
   | ChatGPT Plus                                                | 1x                                                 | 10              | 4                 |
   | ChatGPT Pro tiers (for example 5x, 20x)                     | the tier's multiplier                              | 50, 200         | 20, 80            |
   | Claude Pro / Max 5x / Max 20x (Phase 7)                     | 1x / 5x / 20x relative to Pro                      | 10 / 50 / 200   | 4 / 20 / 80       |
   | GLM coding plan (Lite, Pro, Max)                            | relative to Lite, from Z.ai's published plan sizes | 10 × multiplier | 4 × multiplier    |
   | Pay-as-you-go API key (Anthropic, OpenAI, OpenRouter, Z.ai) | monthly budget ÷ $20                               | 10 × multiplier | 4 × multiplier    |

   The admin keeps the multiplier table in settings. Read the plan from the provider when possible
   (Codex `account/read`); otherwise the member picks it when connecting and an admin can correct it.

8. **No credit farming.** Credits are tied to the provider's own account ID. One provider account
   earns credits once, ever, no matter how many times or by whom it is re-added.
9. **Automatic reset.** Only when every account that can serve a request is empty, Mologate
   redeems a provider limit-reset coupon on one of them by itself, through the provider's own
   CLI tooling. There is no button. Any team member can turn this off for everyone.

---

## 3. Providers and what their terms allow

This records the project's pool choices and the providers' restrictions, checked on 2026-10-11.
A team-accepted risk is a project decision, not permission from the provider.

| Provider                                  | Access                                                                    | Into the pool?                          | Limits source                                                                        |
| ----------------------------------------- | ------------------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------ |
| **Codex / ChatGPT** (Plus, Pro, Business) | Device-code login through the official `codex app-server`                 | Yes (team-accepted risk, see below)     | `account/rateLimits/read` (5h + weekly), `x-codex-*` headers                         |
| **Anthropic API key** (Claude Console)    | Key pasted in by its owner                                                | Yes                                     | Owner-set monthly budget; `anthropic-ratelimit-*` headers for rate limits            |
| **Claude Pro / Max subscription**         | Owner connects an isolated runner using the unmodified Claude Code binary | Planned in Phase 7 (team-accepted risk) | CLI usage and verified stream observations; complete quota visibility is not assumed |
| **OpenAI API key**, **OpenRouter key**    | Key                                                                       | Yes                                     | Owner-set budget; OpenRouter also reports key usage                                  |
| **Z.ai GLM coding plan**                  | Key                                                                       | Yes                                     | Coding-plan quota endpoint (5h + weekly `CREDIT_LIMIT`), best effort                 |
| **OpenCode free models**                  | None (runs in a container)                                                | Not an account                          | Per-user rate limit only                                                             |

**Claude subscriptions:** the team has chosen to plan opt-in pooling through isolated runners.
Anthropic distinguishes hosting the unmodified CLI for a user signing in with their own account
from routing other users' requests through subscription credentials; its published restrictions
also cover collecting or relaying Claude.ai tokens. Containers and conservative request pacing
do not remove those restrictions. The team accepts this risk; the plan does not claim provider
approval or protection from suspension. See [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).

Anthropic **API-key** accounts remain the Phase 1 integration. Subscription runners are a separate
[Phase 7 specification](claude-runners.md). In its proposed `setup-token` flow, Mologate and the
manager briefly handle the token in memory, and the manager writes it into the runner's private
volume. The token must never enter SQL, Redis, logs or API responses. This is not a token-free relay.

**ChatGPT accounts:** OpenAI's Terms of Use say account holders may not share their credentials or
make their account available to others. The team has accepted this risk. Lower it: keep
sharing opt-in per account, keep the owner's traffic on their own account, keep requests
per account at ordinary human volumes (per-account concurrency caps in phase 4), and prefer
Business seats and API keys where it matters.

---

## 4. What already exists

The table below is the baseline verified on `main` at `a20a336`.
The [Phase 1 implementation](phase1-accounts.md), on `limits-and-accounts`, adds account storage,
connect UI and routing. Phase 7 must build on that work and the later dependencies listed in its
specification. Later-phase components are not implemented merely because they share this branch.

| Piece                                           | Where                                                                                                                               | Use it for                                                                                                                                                                                                 |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Provider limits from response headers           | `utils/provider_limits.py`, recorded at `routers/openai.py` (`generate_chat_completion`), read by `GET /api/v1/models/model/limits` | Keep the parsers; rekey from `openai:{url_idx}` to account ID (indices break when connections are reordered). At the baseline only the chat-completions path recorded; `/responses` and the proxy did not. |
| Context indicator ring and panel                | `src/lib/components/chat/ContextIndicator.svelte`                                                                                   | Show the user's tier, share left and the account windows.                                                                                                                                                  |
| Connections (URL + key + per-connection config) | `routers/openai.py` (`OPENAI_API_CONFIGS`, `auth_type`, `headers`, `prefix_id`)                                                     | API-key accounts can be expressed as connections, but the gate picks the account per request.                                                                                                              |
| Anthropic Messages conversion                   | `utils/anthropic.py`                                                                                                                | Anthropic API-key accounts work through it.                                                                                                                                                                |
| Fernet-encrypted token storage                  | `models/oauth_sessions.py`                                                                                                          | Same pattern for account secrets.                                                                                                                                                                          |
| Per-user API keys                               | `api_key` table (`data` JSON, `expires_at`, `last_used_at`), `utils/auth.py`, `features.api_keys` permission, endpoint restrictions | Personal keys. `get_current_user_by_api_key` drops the key's ID; carry it on `request.state`.                                                                                                              |
| Usage on chat messages, analytics               | `models/chat_messages.py` (`usage` column), `routers/analytics.py`                                                                  | Only chats are recorded: API-key calls and unsaved chats leave nothing. The new ledger fixes that.                                                                                                         |
| Rolling-window counter                          | `utils/rate_limit.py` (`RateLimiter`, Redis + memory)                                                                               | Per-key requests per minute.                                                                                                                                                                               |
| Groups, permissions, access grants              | `models/groups.py`, `models/access_grants.py`                                                                                       | Admin role, who may connect accounts.                                                                                                                                                                      |
| Event bus                                       | `events.py` (`publish_model_provider_request_failed`)                                                                               | Detect 429 and limit errors for failover.                                                                                                                                                                  |
| All internal model calls                        | `utils/chat.py:generate_chat_completion` (chat, tasks, memory, compaction, tool review, subagents through the middleware)           | Where the gate's pre-check goes.                                                                                                                                                                           |
| Default system model                            | `config.py` `DEFAULT_SYSTEM_MODEL_FAMILY='luna'`                                                                                    | Background jobs already prefer Luna, a worker model.                                                                                                                                                       |

At that baseline there were no account, ledger or credit tables, and no account UI. Phase 1 now
provides `provider_account`, `provider_login` and account UI, and keys limits by account on the
chat, Responses and proxy paths; §10 added `limit_reset_event`. The usage ledger,
`account_window`, `decide()` and credit tables remain work for Phases 2, 3 and 5.

---

## 5. Concepts and units

- **Account:** one connected provider credential with an owner (`provider_account`).
- **Window:** a provider limit window: `5h` and `weekly` for Codex; `budget` (an owner-set monthly
  amount) for API keys. Phase 7 adds Claude windows only when observed from the provider; unknown
  quota stays unknown. Every rule below runs per window, and a request must pass in every window
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
`zai` | `claude_sub` in Phase 7), `plan` (`plus`, `pro`, `business`, `api`, ...), `external_account_id` (the provider's own
ID: ChatGPT account ID, or a SHA-256 fingerprint of an API key), `label`, `secret` (Fernet-encrypted
JSON; for Codex, the path of its isolated `CODEX_HOME`), `sharing` (`shared` | `private` |
`paused`), `capacity_weight`, `budget` (API keys: amount and currency per month), `allow_team_resets`
(bool, default true), `status` (`ok` | `needs_login` | `error` | `cooldown`), `last_error`.
Unique: (`provider`, `external_account_id`).

**Claude runner convention (Phase 7):** `provider='claude_sub'`, with plans `pro`, `max_5x` and
`max_20x`. The encrypted secret contains only server-generated runner metadata, for example
`{"manager":"http://runner-manager:8085","runner_id":"<uuid>","volume":"claude-cfg-<uuid>"}`;
the manager origin is allowlisted server configuration. The provider identity must be verified
before storing its SHA-256 hash as `external_account_id`. A pending registration is excluded from
the pool and earns no credits. Add `verifying` to the lifecycle, and set `allow_team_resets=false`.
The SQL provider column is a string, but the Phase 1 Python `Provider` literal, API validation,
routing and frontend types still need extending. Durable registration/event tracking may require
a migration; see [the complete schema contract](claude-runners.md#2-data-and-identity).
Account uniqueness prevents simultaneous duplicates; the permanent `credit_grant` below prevents
reconnect credit farming after an account row is deleted.

**`account_window`** (latest snapshot per account and window; history in the ledger)
`account_id`, `window` (`5h` | `weekly` | `budget`), `used_percent`, `resets_at`, `source`
(`poll` | `headers` | `ledger` | `stream` in Phase 7), `observed_at`.

**`usage_event`** (one row per provider request)
`id`, `ts`, `user_id`, `api_key_id` (nullable), `account_id`, `provider`, `model_id`, `source`
(`chat` | `subagent` | `task` | `api_key` | `scheduled` | `workflow` | `outside` |
`verification` in Phase 7), `tier`
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

**`limit_reset_event`** (one automatic redemption; kept after the account is removed)
`id`, `account_id`, `provider`, `triggered_by` (the member whose request found every account
empty), `credit_id` (provider's), `idempotency_key`, `outcome` (`pending` | `reset` |
`nothingToReset` | `noCredit` | `alreadyRedeemed` | `error`), `created_at`, `updated_at`.

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
5. **Empty:** no account of this provider has pool headroom, and the automatic reset (§10) could
   not refill one. Return a limit error with the reset time, and offer a worker on another
   provider.

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
- **Claude runners (Phase 7):** record CLI usage and only quota fields actually verified in the
  pinned CLI's output. Stream observations use `source='stream'`, not `headers`. Without two
  authoritative quota readings, outside use and provider-percent calibration remain unknown;
  silence from a runner is not evidence of outside use. See [Phase 7 metering](claude-runners.md#7-metering).

---

## 9. Adding accounts

**Codex (first, and the easiest):**

1. The member clicks **Connect account → ChatGPT / Codex**.
2. Mologate starts an isolated `codex app-server` for the new account, with its own `CODEX_HOME`
   under the data volume (mode 0700), and calls `account/login/start` with the member's chosen
   method:
   - **Link** (`{"type": "chatgpt"}`): the UI shows OpenAI's sign-in link. After signing in, the
     browser opens `http://127.0.0.1:1455/auth/callback?code=…&state=…`, which does not load on
     the member's computer. The member pastes that address into Mologate. Mologate checks the
     `state` against this login and passes only `code` and `state` to app-server's own loopback
     callback inside the container; app-server holds the PKCE verifier and exchanges the code.
     The pasted host is never contacted. All link logins share port 1455, and a second one would
     cancel the first, so only one link login runs at a time.
   - **Code** (`{"type": "chatgptDeviceCode"}`): the UI shows the code and OpenAI's device page.
3. The member finishes the login on their own device, through OpenAI's flow, and can cancel a
   pending attempt.
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
permanent Phase 5 `credit_grant` can prevent the same key from earning credits twice.

**Claude subscriptions (Phase 7, not Phase 1):**

1. The owner chooses **Connect → Claude subscription**, selects Pro / Max 5x / Max 20x and opts
   into sharing if desired. The backend creates a pending registration and an isolated runner.
2. For the proposed MVP, the owner runs `claude setup-token` on their own computer and completes
   Anthropic's browser flow, then submits the token through an authenticated HTTPS form.
3. Mologate relays the bounded body to the manager without body logging or database storage.
   The manager writes a mode-0600 file in the account's mode-0700 volume; the wrapper supplies
   it only to the CLI child process as `CLAUDE_CODE_OAUTH_TOKEN`.
4. A verification request and a provider-verified stable account identity are required before
   `status=ok`. The account enters the pool only if sharing is enabled. Phase 5 grants credits
   once per verified provider identity, not once per runner or submitted token.

The first Phase 7 spike must establish whether `setup-token` exposes enough verified identity
for step 4. Token presence or an `oauthAccount` JSON file is insufficient. The full
[runner plan](claude-runners.md) defines this dependency, APIs, alternatives and acceptance tests.

**Account card:** owner, provider, plan, windows as two-part bars (owner reserve and pool), status,
sharing toggle (shared, private, paused), "allow automatic resets", reset coupons available, last
automatic reset, disconnect.

**Disconnecting:** unspent credits from that account's grant are forfeited (`credit_entry.forfeit`).
Reconnecting the same account doesn't grant again.

---

## 10. Automatic limit reset

Implemented on 2026-10-11 in `utils/provider_account_proxy.py` and `utils/codex_accounts.py`.
There is no reset button: members never redeem a coupon by hand.

- **Account switching first.** A request tries the accounts that can serve its model: the
  member's own, then shared ones, with accounts known to be empty last. An account is skipped
  when Codex reports it empty (`ordinaryUsageAllowed=false`, or a window at 100% when that field
  is unavailable) or when the provider answers 429. Unknown quota never counts as empty.
- **Codex only.** No reset mechanism exists for the other providers. The pinned app-server
  (0.162.1) reports coupons in `account/rateLimits/read` → `rateLimitResetCredits`, and redeems
  them with `account/rateLimitResetCredit/consume {idempotencyKey, creditId}`.
- **When:** only after every eligible account is empty. Accounts whose owner turned off "allow
  automatic resets" (`allow_team_resets`) are left out. It runs in `observe` and `enforce`: an
  empty account is provider availability, not an allowance decision.
- **What it does:** under a lock, read every candidate again (a concurrent request may already
  have refilled one), pick the unexpired coupon that expires soonest, write a pending
  `limit_reset_event`, and consume it with that event's idempotency key, reusing the key for one
  retry. After `reset` (or `nothingToReset`), read the account again and send the request there.
- **Guards:** a pending, successful or failed redemption blocks another one for 10 minutes on
  every worker, so a refill that does not take never spends a second coupon. If no reset is
  possible, the request gets a limit error with the time until the first account resets.
- **Global switch:** `mologate.auto_reset` (env `MOLOGATE_AUTO_RESET`, default on). Any approved
  member turns it off or on for everyone in **Settings → Provider accounts**
  (`GET`/`PUT /api/v1/provider-accounts/auto-reset`); the page shows who changed it last.
- **Rules:** a redemption is irreversible and can't be transferred. The refilled window is split
  50/50 like any other once the allowance engine ships.

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

Hosting files and the deployment procedure are in
[deploy/vps/README.md](../deploy/vps/README.md). Installed and verified on
2026-10-11: production has HTTPS, a regular team account completed a chat, and a production
database backup was restored successfully. The full external TCP scan and
direct confirmation found only ports 22, 80 and 443 reachable. Self-registration has since been
disabled in code (`41ae16a`, not deployed yet), so admins create every member account.

- [x] The VPS runs Mologate behind HTTPS (details in [§13](#13-hosting)): `main` for Phase 0,
      the Phase 1 image since.
- [x] Team onboarding: admins create every account; members cannot register themselves.
- Nightly backups were part of Phase 0 and have been removed for the development period. They
  come back, kept off the VPS, before the team relies on the data.
- [x] Done when: the team can log in and chat, and nothing but 22, 80 and 443 is reachable.

### Phase 1: accounts

**In progress: 2026-10-11**, on the single working branch `limits-and-accounts`.
Implementation and verification notes: [Phase 1 accounts](phase1-accounts.md).
38 automated regressions, real Z.ai normal/streamed chats, and desktop/mobile browser checks
pass. Acceptance testing takes place on
[ai.arbuz.lol](https://ai.arbuz.lol/?settings=provider_accounts), where the verified Phase 1 image
from `05a3311` is deployed; later commits on the branch are not deployed yet. The remaining
acceptance check is two real Codex accounts serving chats.

Claude Pro/Max subscription runners belong to Phase 7. They are not an additional Phase 1
completion requirement; an isolated authentication/protocol spike can follow Phase 1 early.

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
- [ ] Failover on 429 or limit errors to another account in the same tier. Account-level
      failover already ships with §10; tier-aware selection remains.
- [ ] Per-user concurrency cap (3 chats generating, from ROADMAP.md) and per-account concurrency
      cap.
- [ ] Context indicator shows tier, share left, credits and the account windows; warning at 80% of
      the share with an estimate of minutes left from the last 30 minutes of use.
- [ ] Admin and team overrides (`limit_override`), with an audit log.
- [ ] Done when: in `enforce`, the §7.5 scenarios behave as written on the deployed site.

### Phase 5: credits and resets

- [ ] Grants on connect, forfeit on disconnect, spending in tier 3, balance in the UI.
- [x] Automatic limit reset (§10), with a global off switch for any member.
- [ ] Done when: an automatic reset is redeemed on a real account once every account is empty,
      and the windows refill.

### Phase 6: personal API keys (§11)

- [ ] Done when: a member uses Mologate from an outside OpenAI-compatible client with their own
      key, and that use shows up in their share.

### Phase 7: Claude subscription runners

**Planned, not implemented.** Full English specification: [Claude runners](claude-runners.md).
One account gets one isolated runner container and persistent private volume. Ship with the
integration disabled by default. Infrastructure/authentication work can follow Phase 1;
the complete feature depends on Phases 2–5 for metering, selection, enforcement and credits.
Phase 6 is not required for the interactive-chat MVP; personal API keys cannot use these runners.

- [ ] 7.0: verify pinned CLI authentication, stable account identity, usage/limit output and memory
      requirements; record fixtures and resolve the `setup-token` identity gap.
- [ ] 7.1: authenticated runner-manager, compose overlay, create/start/stop/delete,
      isolated networks, hard live-runner cap and working kill-switch.
- [ ] 7.2: pinned runner image, one active request, NDJSON streaming, watchdog and cancellation.
- [ ] 7.3: owner-authorized backend connect API, redacted credential relay, signed/replay-safe
      events, identity deduplication and account status notifications.
- [ ] 7.4: usage ledger and verified account-window observations; unknown quota stays unknown.
- [ ] 7.5: allowance-engine integration, account/session affinity, background-work exclusion,
      Phase 5 credit grants and tier-5 reset-time display without an automatic reset.
- [ ] 7.6: lifecycle reconciliation, idle LRU stop, cooldown/circuit breaker and restart/load tests.
- [ ] 7.7: Connect UI, runner status on account cards and context/usage indicators.
- [ ] 7.8: at least one week of real `observe` traffic; enable enforcement only after
      its metering and isolation acceptance criteria pass.
- [ ] Done when: browser connect produces a verified usable account, three registered accounts
      never exceed the host's two-live-runner ceiling, disconnect removes the correct volume,
      and off/auth-loss/rate-limit/restart scenarios behave as specified.

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
  `ghcr.io/pmixay/mologate` from `main`, and slim-only images from `limits-and-accounts` after
  the provider-account regressions pass.
  Pushes that change only `docs/` or `deploy/` skip image builds. The unused Docker Hub copy step
  was removed. The package is private, so the VPS needs a token with `read:packages`.
- Containers: Mologate (the slim image: uploads reach the model as full text, and any embeddings
  come from an API, since local embeddings would eat the RAM), Postgres, Redis, Caddy. Add a 4 GB
  swap file.
- Caddy handles HTTPS at `ai.arbuz.lol`; the old `5-83-140-221.sslip.io` hostname redirects there.
  The firewall allows only 22, 80 and 443.
- Backups: none while Mologate is in development. Add them later, kept off the box.
- **Phase 7, planned:** add a separate runner-manager and private account volumes.
  Only that service receives `docker.sock`; neither the app nor runners receive it. Manager and
  runner ports stay unpublished. Start with one live runner on this shared VPS and permit at
  most two host-wide after memory/CPU tests. See [deployment requirements](../deploy/vps/README.md#phase-7-claude-runners-planned).

---

## 14. Decisions log

- 2026-10-11: move production to `https://ai.arbuz.lol`, retaining redirects from the old hostname.
  The owner requests Phase 1 testing on that site.
  Deploy the verified `limits-and-accounts` image there after backup, preserving existing data.
- 2026-10-11: the owner requests one working branch, `limits-and-accounts`, for every phase.
  Consolidate the Phase 1 code and Claude runner plan there; no separate phase branches.
- 2026-10-11: personal share is the pool divided equally among all members.
- 2026-10-11: credits = 10 × plan multiplier for every poolable account; API keys by budget.
- 2026-10-11: idle owner loan: no owner use for 24 hours lends up to 25 points.
- 2026-10-11: hosting on the VPS from the start: https://5-83-140-221.sslip.io, compose stack in
  `/opt/mologate`; GLM coding plan connected and Exa web search on.
- 2026-10-11: keep Anthropic API-key accounts in Phase 1; schedule Claude Pro/Max containers as
  Phase 7, with an early authentication/identity spike allowed after Phase 1. The team requests
  opt-in subscription pooling and accepts the provider-terms risk described in §3. No claim that
  an unmodified binary, isolation or pacing makes pooling provider-approved.
- 2026-10-11: Claude Pro is the 1x base paid tier; Max has 5x and 20x variants. Credits require a
  verified stable identity and a permanent grant record. The proposed token relay handles a
  credential transiently; only the runner volume persists it. Phase 7 stays disabled by default.
- 2026-10-11: remove backups for the development period; add them back later, off the VPS.
- 2026-10-11: no self-registration: admins create every member account. ChatGPT accounts can
  also connect with a sign-in link whose callback address the member pastes back, since a code
  alone is not enough for every account.
- 2026-10-11: limit resets are automatic, as ROADMAP.md says, not a button. When every eligible
  account is empty, redeem the Codex coupon that expires soonest; any member can turn this off
  for the team.
- 2026-10-11: the staging server was removed; the VPS hosts production only. The VPS port-check
  workflow and the original project plan (`PLAN.md`) were removed from the repository.

---

## Sources

- OpenAI Terms of Use, registration and access: https://openai.com/policies/terms-of-use/
- Claude Code authentication and long-lived tokens: https://code.claude.com/docs/en/authentication
- Claude Code legal and compliance: https://code.claude.com/docs/en/legal-and-compliance
- Claude Pro and Max plan comparison: https://claude.com/pricing
- Codex app-server protocol, `openai/codex`: `codex-rs/app-server-protocol` (`account/login/start`,
  `account/rateLimits/read`, `account/rateLimitResetCredit/consume`)
