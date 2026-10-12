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
  (default `observe` until phase 4 ships). When the environment sets it, that value wins at every
  start. In `observe` the gate computes decisions and records them but never blocks, swaps
  models, delays or caps anyone's work. Like the tool-call review, it stays out of the way: in
  every mode, only what would harm the system or a member is stopped, and only when it is very
  critical, namely requests without sign-in, using a member's private account or credentials for
  someone else, and load that would take the server down. Switching to another account when one
  is empty or rate-limited is not a block.
- **Never** log or return secrets. Never put prompts in the usage ledger.

---

## 2. Settled decisions

These come from the team and are not up for redesign. Numbers are defaults an admin can change.

1. **Mologate enforces limits itself.** ChatGPT accounts run through an internal, unmodified
   codex-lb (pinned v1.24.0) that holds their tokens, limits and reset coupons and sends requests
   as the official Codex client. Its own UI and pooling are not used: Mologate picks the account
   for every request and shows everything in its own interface.
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
   | GLM coding plan (Lite, Pro, Max)                            | relative to Lite, from Z.ai's published plan sizes | 10 × multiplier | 4 × multiplier    |
   | Pay-as-you-go API key (Anthropic, OpenAI, OpenRouter, Z.ai) | monthly budget ÷ $20                               | 10 × multiplier | 4 × multiplier    |

   The admin keeps the multiplier table in settings. Read the plan from the provider when possible
   (ChatGPT plans come from codex-lb); otherwise the member picks it when connecting and an admin
   can correct it (Z.ai coding plans: Lite, Pro, Max). Claude subscriptions are deferred (§12,
   Phase 7), so they have no row here; their credits are decided if that phase starts.

8. **No credit farming.** Credits are tied to the provider's own account ID. One provider account
   earns credits once, ever, no matter how many times or by whom it is re-added.
9. **Automatic reset.** Only when every account that can serve a request is empty, Mologate
   redeems a provider limit-reset coupon on one of them by itself, through codex-lb. There is no
   button. Any team member can turn this off for everyone.

---

## 3. Providers and what their terms allow

This records the project's pool choices and the providers' restrictions, checked on 2026-10-11.
A team-accepted risk is a project decision, not permission from the provider.

| Provider                                  | Access                                       | Into the pool?                      | Limits source                                                             |
| ----------------------------------------- | -------------------------------------------- | ----------------------------------- | ------------------------------------------------------------------------- |
| **Codex / ChatGPT** (Plus, Pro, Business) | Sign-in link or device code through codex-lb | Yes (team-accepted risk, see below) | codex-lb account summary (5h + weekly), polled from OpenAI every minute   |
| **Anthropic API key** (Claude Console)    | Key pasted in by its owner                   | Yes                                 | Owner-set monthly budget; `anthropic-ratelimit-*` headers for rate limits |
| **Claude Pro / Max subscription**         | Deferred and disabled (Phase 7)              | Not now                             | Decided if Phase 7 starts                                                 |
| **OpenAI API key**, **OpenRouter key**    | Key                                          | Yes                                 | Owner-set budget; OpenRouter also reports key usage                       |
| **Z.ai GLM coding plan**                  | Key                                          | Yes                                 | Coding-plan quota endpoint (5h + weekly `CREDIT_LIMIT`), best effort      |
| **OpenCode free models**                  | None (runs in a container)                   | Not an account                      | Per-user rate limit only                                                  |

**Claude subscriptions:** deferred and disabled on 2026-10-12; they are not part of the pool, and
nothing for them is built or scheduled. Codex's plan for them, in [claude-runners.md](claude-runners.md),
lists its open proposals; none of it is decided. The owner's direction was to pool them while
lowering the risk of an account ban, and to choose the connect flow when the work starts.
Anthropic distinguishes hosting the unmodified CLI for a user signing in with their own account
from routing other users' requests through subscription credentials; its published restrictions
also cover collecting or relaying Claude.ai tokens. Containers and conservative request pacing
do not remove those restrictions. The team accepts this risk; the plan does not claim provider
approval or protection from suspension. See [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).

Anthropic **API-key** accounts remain the Phase 1 integration.

**ChatGPT accounts:** OpenAI's Terms of Use say account holders may not share their credentials or
make their account available to others. The team has accepted this risk. Lower it: keep
sharing opt-in per account, keep the owner's traffic on their own account, keep requests
per account at ordinary human volumes (per-account concurrency caps in phase 4), and prefer
Business seats and API keys where it matters.

---

## 4. What already exists

The table below is the baseline verified on `main` at `a20a336`.
The [Phase 1 implementation](phase1-accounts.md), on `limits-and-accounts`, adds account storage,
connect UI and routing. Later-phase components are not implemented merely because they share this
branch.

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
chat, Responses and proxy paths; §10 added `limit_reset_event`, and Phase 2 started the usage
ledger (`usage_event`, §8). `account_window`, `decide()` and credit tables remain work for
Phases 2, 3 and 5.

---

## 5. Concepts and units

- **Account:** one connected provider credential with an owner (`provider_account`), or a team
  account with none. A team account has no owner reserve: its whole window is pool.
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
`id`, `owner_user_id` (empty for a team account: no owner, admins manage it, everyone uses it, all
of its use is pool use; migration `f7c2a9d4b8e1`), `provider` (`codex` | `anthropic_api` | `openai_api` | `openrouter` |
`zai`), `plan` (`plus`, `pro`, `business`, `api`, ...), `external_account_id` (the provider's own
ID: codex-lb's account ID for ChatGPT, or a SHA-256 fingerprint of an API key), `label` (a generated
name such as `Account #1n9Dk5`; members do not choose labels), `secret` (Fernet-encrypted JSON; for
ChatGPT, the codex-lb account ID and a key scoped to that one account), `sharing` (`shared` | `private` |
`paused`), `capacity_weight`, `budget` (API keys: amount and currency per month), `status` (`ok` |
`needs_login` | `error` | `cooldown`), `last_error`. There is no per-account reset switch: the global
auto-reset setting (§10) alone decides whether coupons are spent (migration `e5a3c1f8d2b7` dropped
`allow_team_resets`).
Unique: (`provider`, `external_account_id`).

Account uniqueness prevents simultaneous duplicates; the permanent `credit_grant` below prevents
reconnect credit farming after an account row is deleted.

**`account_window`** (latest snapshot per account and window; history in the ledger)
`account_id`, `window` (`5h` | `weekly` | `budget`), `used_percent`, `resets_at`, `source`
(`poll` | `headers` | `ledger`), `observed_at`.

**`usage_event`** (one row per provider request)
`id`, `ts`, `user_id`, `api_key_id` (nullable), `account_id`, `provider`, `model_id`, `source`
(`chat` | `subagent` | `task` | `api_key` | `scheduled` | `workflow` | `outside`), `tier`
(`reserve` | `pool` | `credit` | `worker` | `owner_outside`), `input_tokens`, `output_tokens`,
`cached_tokens`, `reasoning_tokens`, `cost` (USD) and `cost_source` (`provider` | `estimate`),
`est_units`, `units` (after calibration), `credits_charged`, `status` (`ok` | `error` |
`limited`), `chat_id`, `request_id`. Index on (`user_id`, `ts`) and (`account_id`, `ts`).
`outside` rows are the unexplained jumps from §8. Migration `a8d4e2c7f915`. Until the allowance
engine assigns tiers, a row's tier says only whose account served it: `reserve` for the owner,
`pool` for anyone else.

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

- **Ledger (implemented, `utils/usage_ledger.py`):** the account proxy writes a `usage_event` for
  every attempt on a provider account, whichever route it came by (chat, Responses, the proxy) and
  whatever started it (chat, sub-agent, task, API key, automation): `limited` when the account is
  used up and the request moves on, `error` when it fails, and `ok` with the token counts once the
  answer, or its stream, ends. Rows are written in the background and never fail or slow a
  request. Models of connections that stay connections (§9) are not metered.
- **Cost:** OpenRouter reports each request's cost, and its `/key` endpoint this month's spend.
  OpenAI, Anthropic and Z.ai keys report neither, so the ledger estimates a request's cost from its
  token counts (uncached input, cached input, output) and published prices, from the models.dev
  catalog (`utils/model_prices.py`, fetched at most once a day; `MOLOGATE_PRICE_CATALOG_URL`
  points elsewhere or, empty, turns estimates off, as does offline mode). A model with no
  published price has an unknown cost, and the accounts page says how many such requests it
  could not count. Subscriptions have no per-request cost.
- **Pace (the low-limit warning):** each account keeps two hours of readings of its 5-hour,
  weekly and credit windows (one per minute, in Redis with the limit snapshots). The rise over the
  last 30 minutes gives a pace; the remaining shares of every account that can serve a model,
  weighted by `c_a`, divided by their combined pace, give the minutes of work left
  (`limit_outlook` in `utils/provider_limits.py`, returned by `GET /api/v1/models/model/limits`).
  A window is low at 20% left or an hour of work, critical at 10% or 15 minutes, unless it resets
  first.
- **Polling:** codex-lb reads each ChatGPT account's windows from OpenAI every minute and from
  every response; Mologate takes them from its account summary. Store them in `account_window`.
- **Owner and team split (started, an estimate):** until the ledger exists, Mologate counts the
  requests each account serves for its owner and for everyone else, per 5-hour and weekly window.
  When a reading shows the window's used percent rose, the rise is shared out by those counts and
  the counts start again; a reading with no rise keeps them for the next one. A rise with no
  requests from Mologate, and whatever was used before Mologate first read the window, is the
  owner's (decision 4). A reset time that moves on, or a drop of more than 5 points, starts the
  split over. Kept in Redis (or memory) with the limit snapshots; `utils/provider_limits.py`.
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

**ChatGPT (through codex-lb):**

1. The member clicks **Connect → ChatGPT** and picks a method. Only one ChatGPT connection can be
   in progress across the team at a time: codex-lb runs one device sign-in at a time, and the new
   account is recognised by comparing codex-lb's account list before and after.
2. Mologate asks codex-lb to start the sign-in (`POST /api/oauth/start` with `forceMethod`):
   - **Link:** the UI shows OpenAI's sign-in link. After signing in, the browser opens
     `http://localhost:1455/auth/callback?code=…&state=…`, which does not load on the member's
     computer. The member pastes that address back. Mologate checks its `state` against this
     sign-in and hands codex-lb only `code` and `state` (`/api/oauth/manual-callback`); the
     pasted host is never contacted.
   - **Code:** the UI shows OpenAI's device page and the code.
3. When codex-lb reports success, Mologate finds the new account, creates a codex-lb API key
   assigned to that account only (never an empty assignment, which codex-lb treats as the whole
   pool), names the account (`Account #1n9Dk5`) and stores the `provider_account`. Signing in
   again to a connected account reconnects it for the same owner.
4. codex-lb refreshes tokens in the background. When it reports `reauth_required` or
   `deactivated`, Mologate marks the account `needs_login`.

**Inference decision (revised 2026-10-11):** requests go to codex-lb's
`/backend-api/codex/responses` with the account's own key, so codex-lb can route them only to
that account. codex-lb sends them upstream as the official Codex CLI (client name, version, a
per-account installation ID, store disabled) and refreshes tokens. Mologate requires codex-lb's
API-key authentication to be on and refuses ChatGPT traffic otherwise, translates the SSE
text/tool deltas and final usage into Chat Completions, and preserves native SSE for `/responses`.
The model list is codex-lb's Codex catalog (`GET /backend-api/codex/models`, read with an
account's key and cached for 5 minutes), the same entries the Codex client reads: each model keeps
its effort levels, input types and context window, so the effort selector, the image check and the
context indicator work as for any configured connection. Requests and the accounts page bring new
models in. Accounts that were connected through the earlier `codex app-server` integration are
imported into codex-lb from their `auth.json` the first time they are used.

**API keys** (Anthropic, OpenAI, OpenRouter, Z.ai): a form with the key and a monthly budget, a
"Verify" call, then the account is created. `external_account_id` is the key fingerprint, so the
permanent Phase 5 `credit_grant` can prevent the same key from earning credits twice. The model
list leaves out what chat cannot use (embeddings, speech, images, moderation, realtime) and older
generations (OpenAI's GPT-3.5, GPT-4 and completion-only models, its dated copies of listed
models, and models OpenRouter is retiring).

**Team accounts:** an admin can connect a key or a ChatGPT account as a team account (the "Team
account" switch). It belongs to no member: everyone uses it, admins manage it, it is shared or
paused (never private), and all of its use is pool use. Admin connections to a supported
provider (OpenAI, Anthropic, OpenRouter, Z.ai) with a plain key become team accounts at startup
and whenever an admin saves the connections (`utils/connection_accounts.py`), so their models
come from providers through account selection, failover and per-account limits. Local servers,
Azure, and connections with custom headers or another sign-in stay connections, as does a key a
member already connected. The adopted connection's model ids keep working: saved model settings,
presets, access grants and the default, pinned, ordered and task models move to the account ids,
and a request naming an old id is sent to the new one (`mologate.model_aliases`).

**Account row:** collapsed by default to its generated name, provider, plan, owner, a note only
when it matters (Team, Paused, Sign-in required), the 5-hour and weekly bars, and a coupon badge
(count; the tooltip has the last automatic reset). On a shared account each of those windows is
two bars: the owner's reserve and the team pool, each out of 50 points (decision 2), with the
owner's use past 50 shown as pool use. An API key's row shows its credit limit and budget
instead. Expanded: every limit window, with "You · 8/50" and "Team · 15/50" under a shared
window, an API key's budget left this month (estimated, §8, except OpenRouter's), each member's
requests, tokens and cost from the ledger (this week for a subscription, this month for a key), a
"Who can use" dropdown (Only me, Team, Paused), and Verify, budget and Disconnect buttons. Auto-reset has one switch for the whole team, in the page
header. The split is shown only: nothing enforces the reserve until Phase 4, and the idle-owner
loan (decision 3) is not shown yet.

**Disconnecting:** unspent credits from that account's grant are forfeited (`credit_entry.forfeit`).
Reconnecting the same account doesn't grant again.

---

## 10. Automatic limit reset

Implemented on 2026-10-11 in `utils/provider_account_proxy.py` and `utils/codex_accounts.py`, with
codex-lb for ChatGPT accounts. There is no reset button: members never redeem a coupon by hand.

- **Account switching first.** A request tries the accounts that can serve its model: the
  member's own, then shared ones, with accounts known to be empty last. A ChatGPT account is
  skipped when codex-lb reports it `rate_limited`, `quota_exceeded` or paused, when a window is
  used up and has not reset, or when codex-lb answers 429 or a 503 meaning the account cannot
  serve the request. Other providers' accounts are skipped on 429. Unknown quota never counts as
  empty.
- **ChatGPT only.** No reset mechanism exists for the other providers. codex-lb lists an account's
  coupons (`GET /api/accounts/{id}/rate-limit-reset-credits`) and redeems the soonest-expiring
  one (`POST …/rate-limit-reset-credits/consume {redeemRequestId}`).
- **When:** only after every ChatGPT account the member can use is empty. Any of their coupons
  can be spent; there is no per-account opt-out. It runs in `observe` and `enforce`: an empty
  account is provider availability, not an allowance decision.
- **What it does:** under a lock, read every candidate again (a concurrent request may already
  have refilled one), pick the account whose coupon expires soonest, write a pending
  `limit_reset_event`, and redeem once with that event's idempotency key (redemption is not
  idempotent upstream, so it is never retried). After a reset, read the account again and send
  the request there.
- **Guards:** a pending, successful or failed redemption blocks another one for 10 minutes on
  every worker, so a refill that does not take never spends a second coupon. A refusal from
  codex-lb before it contacts OpenAI does not block. If no reset is possible, the request gets a
  limit error with the time until the first account resets.
- **Global switch:** `mologate.auto_reset` (env `MOLOGATE_AUTO_RESET`, default on). Any approved
  member turns it off or on for everyone with the **Auto-reset** switch in **Settings → Provider
  accounts** (`GET`/`PUT /api/v1/provider-accounts/auto-reset`); its tooltip shows who changed it
  last. It is the only reset switch. When `MOLOGATE_AUTO_RESET` is set in the environment, it
  wins at every start; the switch then holds until the next start.
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

**Mostly done: 2026-10-11.** Hosting runs; the final acceptance check is still open (below).

Hosting files and the deployment procedure are in
[deploy/vps/README.md](../deploy/vps/README.md). Installed on 2026-10-11 with HTTPS, and a
production database backup was restored successfully. Evidence, re-checked on 2026-10-12:

- Branch code (local test against a stand-in provider): only the first account on an empty install
  can sign up; later sign-ups are refused (403) whatever the admin setting or `ENABLE_SIGNUP`
  says; an admin-created member signs in and chats, streaming and not, in the API and the browser.
- Production (`ai.arbuz.lol`, still on `05a3311`): HTTPS with a valid certificate, HTTP and the old
  sslip.io name redirect to it. It still offers sign-up (`enable_signup: true`) until `41ae16a` or
  later is deployed.
- Not verified: the 2026-10-11 chat on production was a throwaway test user, not a team member,
  and the port check needs a scan from outside (`nmap -Pn -p- 5.83.140.221`) plus `ufw status` on
  the VPS; Codex's earlier TCP scan reported only 22, 80 and 443.

- [x] The VPS runs Mologate behind HTTPS (details in [§13](#13-hosting)): `main` for Phase 0,
      the Phase 1 image since.
- [x] Team onboarding: admins create every account; members cannot register themselves.
      Verified for the branch code; production needs `41ae16a` or later deployed.
- Nightly backups were part of Phase 0 and have been removed for the development period. They
  come back, kept off the VPS, before the team relies on the data.
- [ ] Done when: the team can log in and chat, and nothing but 22, 80 and 443 is reachable.
      Open: a team member signing in and chatting on production, and an external port scan.

### Phase 1: accounts

**In progress: 2026-10-11**, on the single working branch `limits-and-accounts`.
Implementation and verification notes: [Phase 1 accounts](phase1-accounts.md).
38 automated regressions, real Z.ai normal/streamed chats, and desktop/mobile browser checks
pass. Acceptance testing takes place on
[ai.arbuz.lol](https://ai.arbuz.lol/?settings=provider_accounts), where the verified Phase 1 image
from `05a3311` is deployed; later commits on the branch are not deployed yet. The remaining
acceptance check is two real Codex accounts serving chats.

- [x] `provider_account` table, encrypted secrets, account CRUD API (owner or admin).
- [x] ChatGPT connect from the browser (§9), by sign-in link or device code; first through
      `codex app-server`, now through codex-lb.
- [x] API-key accounts (Anthropic, OpenAI, OpenRouter, Z.ai) with verification.
- [x] Models come from providers, not connection indices; the gate chooses the account per request.
      Admin connections to a supported provider become team accounts (§9); local, Azure and
      custom connections stay connections.
- [x] Provider limits rekeyed by account ID; `/responses` and the proxy record them too.
- [x] Z.ai spike: coding-plan quota endpoint reports 5-hour and weekly `CREDIT_LIMIT` windows;
      the model-list response has no rate-limit headers. Empty/unknown quota stays unknown.
- [ ] Done when: two members connect Codex accounts from the browser and both serve chats.

### Phase 2: metering (observe-only)

- [x] `usage_event` ledger written for every source, including API keys and unsaved chats (§8).
- [ ] Account polling, outside-use detection, calibration jobs.
- [ ] Accounts page and a "my usage" page (read-only numbers). Started: the accounts page shows
      each shared account's owner and team use, estimated from request counts (§8), each
      member's requests, tokens and cost from the ledger, and an API key's budget left. The "my
      usage" page is not started.
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
- [x] Per-user concurrency cap (3 chats generating, from ROADMAP.md): `utils/chat_slots.py`.
      Past the cap a chat waits in line and shows its place; sub-agents, background results,
      timers, automations and channel replies take no slot. Admins set it under Settings →
      General (hard cap 10). It waits only in `enforce`; `observe` logs a chat that would have.
- [ ] Per-account concurrency cap.
- [ ] Context indicator shows tier, share left, credits and the account windows; warning at 80% of
      the share with an estimate of minutes left from the last 30 minutes of use. Started: the
      limits ring beside the model selector shows the account windows of every account that can
      serve the model and warns, with minutes left at the last 30 minutes' pace, when they run
      low (§8). Tier, share and credits need the allowance engine.
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

**Deferred and disabled (2026-10-12).** Claude subscriptions are not part of the pool and nothing
is built or scheduled. Codex's plan, with its open proposals and the step list he drafted, is in
[claude-runners.md](claude-runners.md); the owner reviews it before any of it is decided.

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
  come from an API, since local embeddings would eat the RAM), codex-lb, Postgres, Redis, Caddy.
  Add a 4 GB swap file.
- codex-lb (`ghcr.io/soju06/codex-lb:1.24.0`, pinned by digest) runs on a private network that
  only Mologate joins, with no published ports, dashboard login disabled, telemetry off and its
  OAuth listener bound to localhost. It keeps its SQLite database and encryption key in the
  `codex_lb_data` volume. It also checks GitHub, npm and two pricing catalogs hourly for version
  and price data, which cannot be turned off.
- Mologate fetches published model prices from models.dev at most once a day, to estimate what
  API-key requests cost (§8); `MOLOGATE_PRICE_CATALOG_URL=` turns this off.
- Caddy handles HTTPS at `ai.arbuz.lol`; the old `5-83-140-221.sslip.io` hostname redirects there.
  The firewall allows only 22, 80 and 443.
- Backups: none while Mologate is in development. Add them later, kept off the box.
- **Phase 7:** deferred; Codex's hosting proposal is in [claude-runners.md](claude-runners.md).

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
- 2026-10-11: remove backups for the development period; add them back later, off the VPS.
- 2026-10-11: no self-registration: admins create every member account. ChatGPT accounts can
  also connect with a sign-in link whose callback address the member pastes back, since a code
  alone is not enough for every account.
- 2026-10-11: the owner revises decision 1 to follow ROADMAP.md: ChatGPT accounts run through
  an internal codex-lb (accounts, limits, reset coupons and the Codex transport), with Mologate
  choosing the account per request through keys scoped to one account. This replaces the
  per-request `codex app-server` and direct calls to chatgpt.com. Accounts get generated names.
- 2026-10-11: limit resets are automatic, as ROADMAP.md says, not a button. When every eligible
  account is empty, redeem the Codex coupon that expires soonest; any member can turn this off
  for the team.
- 2026-10-12: Claude subscriptions are deferred and disabled. Codex's Phase 7 plan, including the
  two entries he wrote into this log on 2026-10-11, moved to [claude-runners.md](claude-runners.md)
  as proposals awaiting review; none of it is a decision.
- 2026-10-12: observe mode never interrupts work; only very critical harm is stopped in any mode
  (no sign-in, someone else's private account, load that would take the server down).
- 2026-10-12: team accounts: an admin's key or ChatGPT account can belong to no member and serve
  everyone. Admin connections to a supported provider become team accounts; others stay
  connections. Any approved member can connect an account. Admins can correct a Z.ai coding plan.
  Deployment switches set in the environment win at every start.
- 2026-10-11: the staging server was removed; the VPS hosts production only. The VPS port-check
  workflow and the original project plan (`PLAN.md`) were removed from the repository.

---

## Sources

- OpenAI Terms of Use, registration and access: https://openai.com/policies/terms-of-use/
- Claude Code authentication and long-lived tokens: https://code.claude.com/docs/en/authentication
- Claude Code legal and compliance: https://code.claude.com/docs/en/legal-and-compliance
- Claude Pro and Max plan comparison: https://claude.com/pricing
- codex-lb, MIT: https://github.com/Soju06/codex-lb (v1.24.0: `/api/oauth/*`, `/api/accounts`,
  `/api/api-keys/`, `/api/settings`, `/backend-api/codex/responses`, reset-credit endpoints)
