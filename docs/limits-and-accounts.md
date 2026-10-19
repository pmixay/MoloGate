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
  critical, namely requests without sign-in, using a paused account or a member's credentials
  for someone else, and load that would take the server down. Switching to another account when one
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
5. **Anyone can use big models** from the pool, up to their personal share. **Every account is
   shared** with the whole team (revised 2026-10-14): there is no "Only me". Its owner keeps the
   reserve (decision 2) and can pause it, which stops it serving anyone, the owner included.
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
   | Pay-as-you-go API key (Anthropic, OpenAI, OpenRouter, Z.ai) | dollars actually spent through it ÷ $20            | 10 × multiplier | 4 × multiplier    |

   The admin keeps the multiplier table in settings. Read the plan from the provider when possible
   (ChatGPT plans come from codex-lb); otherwise the member picks it when connecting and an admin
   can correct it (Z.ai coding plans: Lite, Pro, Max). Claude subscriptions are deferred (§12,
   Phase 7), so they have no row here; their credits are decided if that phase starts.

   **API keys earn credits by the money actually spent through them** (2026-10-14), not by the
   budget their owner types in: every dollar anyone spends through a key, its owner included,
   gives the key's owner 0.5 credits (10 per $20, the same rate as a 1x plan), as the spending
   happens. The dollars are the provider's reported cost (OpenRouter) or the ledger's estimate
   from published prices (§8). A free key, or free models (OpenRouter's `:free` models, Z.ai's free
   Flash models), costs nothing and so earns nothing. A team key has no owner and earns nobody
   credits. Since credits follow spending, re-adding a key cannot earn them twice (decision 8).

8. **No credit farming.** Credits are tied to the provider's own account ID. One provider account
   earns credits once, ever, no matter how many times or by whom it is re-added.
9. **Manual reset only.** An owner/admin explicitly confirms a coupon redemption for a selected
   ChatGPT account through codex-lb. Exhausted requests never spend reset credits; automatic
   failover to another account remains available.

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
make their account available to others. The team has accepted this risk; since 2026-10-14
every account is shared (decision 5), which raises it. Lower it: keep the owner's traffic on
their own account, let owners pause their account at any time, keep requests
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
chat, Responses and proxy paths; §10 added `limit_reset_event`, and Phase 2 added the usage
ledger (`usage_event`) and metering (`account_window`, `account_reading`, `meter_interval`,
`usage_calibration`, §8), and Phase 3 the allowance engine (`decide()`, §7) in observe mode. The
credit tables remain work for Phase 5.

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
ChatGPT, the codex-lb account ID and a key scoped to that one account), `sharing` (`shared` |
`paused`; migration `e1b4c7a9d3f6` made every private account shared), `capacity_weight`, `budget` (API keys: amount and currency per month), `status` (`ok` |
`needs_login` | `error` | `cooldown`), `last_error`. There is no automatic reset switch:
manual owner/admin action (§10) alone decides whether coupons are spent (migration `e5a3c1f8d2b7` dropped
`allow_team_resets`).
Unique: (`provider`, `external_account_id`).

Account uniqueness prevents simultaneous duplicates; the permanent `credit_grant` below prevents
reconnect credit farming after an account row is deleted.

**`account_window`** (latest reading per account and window; migration `c4e8a2d6f1b9`)
`account_id`, `window` (`5h` | `weekly`), `used_percent`, `resets_at`, `window_minutes`, `source`
(`poll` | `headers`), `observed_at`. API-key budgets are not stored here: they come from the
ledger's costs when the accounts page asks (§8).

**`account_reading`** (each change of a window, kept 35 days)
`id`, `account_id`, `window`, `used_percent`, `resets_at`, `window_minutes`, `source`,
`observed_at`. A reading is kept only when the window rises or starts a new period.

**`meter_interval`** (a stretch of one account window, measured against the ledger; kept 35 days)
`id`, `account_id`, `provider`, `window`, `start`, `end`, `used_start`, `used_end`, `rise`
(percent), `weight` (the account's `capacity_weight` then), `requests` and `features` (the
ledger's tokens in the interval, by model), `first` (the first reading Mologate ever saw),
`expected` (percent Mologate's requests explain; empty before calibration), `outside` (percent
recorded as outside use) and `outside_event_id`.

**`usage_calibration`** (one row per provider and window)
`id` (`provider:window`), `values` (`k`, model `scales`, `default_scale`, and each account's
fit: intervals, observed and predicted percent, error, fitted weight), `samples`, `error`,
`fitted_at`.

**`usage_event`** (one row per provider request)
`id`, `ts`, `user_id`, `api_key_id` (nullable), `account_id`, `provider`, `model_id`, `source`
(`chat` | `subagent` | `task` | `api_key` | `scheduled` | `workflow` | `outside`), `tier`
(`reserve` | `pool` | `credit` | `worker` | `owner_outside`), `input_tokens`, `output_tokens`,
`cached_tokens`, `reasoning_tokens`, `cost` (USD) and `cost_source` (`provider` | `estimate`),
`est_units`, `units`, `window`, `decision`, `credits_charged`, `status` (`ok` | `error` |
`limited`), `chat_id`, `request_id`. Index on (`user_id`, `ts`) and (`account_id`, `ts`).
Migration `a8d4e2c7f915`; `c4e8a2d6f1b9` added `window`, `d7a3f9c2e5b1` `decision` (what the
allowance engine decided for the request, §7.6). `ts` is when the answer ended. Request rows leave
`units` empty: a request's units in a window come from its tokens and that window's calibration
(§8) when they are needed, so a refit re-prices history. `outside` rows are the unexplained rises
from §8: no tokens, their `units` and `window` set, `tier` `owner_outside`, and the owner as
`user_id` (empty for a team account). Until the allowance engine assigns tiers, a request row's
tier says only whose account served it: `reserve` for the owner, `pool` for anyone else.

**`credit_grant`** (migration `b6d1e8f3a2c7`)
`id`, `user_id`, `provider`, `external_account_id`, `plan`, `amount`, `granted_at`, `forfeited_at`.
Unique: (`provider`, `external_account_id`), which is what stops credit farming. A subscription's
`amount` is set once; an API key's grows with the money spent through it.

**`credit_entry`** (append-only balance changes; migration `b6d1e8f3a2c7`)
`id`, `user_id`, `delta`, `reason` (`grant` | `spend` | `forfeit` | `admin`), `usage_event_id`,
`grant_id` (the grant a grant or forfeit entry belongs to), `ts`. A member's balance is the sum of
their entries.

**`limit_override`** (migration `a3f6c9d2b8e4`)
`id`, `user_id` (empty for a team override: every member gets it), `granted_by`, `extra_points`,
`until` (empty: until revoked), `reason`, `revoked_at`, `revoked_by`. Rows are never deleted, so
the table is the audit log of every override.

**`limit_reset_event`** (one explicit redemption; kept after the account is removed)
`id`, `account_id`, `provider`, `triggered_by` (the member whose request found every account
empty), `credit_id` (provider's), `idempotency_key`, `outcome` (`pending` | `reset` |
`nothingToReset` | `noCredit` | `alreadyRedeemed` | `error`), `created_at`, `updated_at`.

Personal API keys stay in the existing `api_key` table. Allow several named keys per user, with
`data = {name, hint, limits: {rpm, concurrency, models, efforts, daily_points}, revoked_at}`. A
personal key is stored as `sha256:<hash>`; the key Open WebUI made per member before (id
`key_<user id>`) keeps its stored form and works as before (§11). `usage_event.est_units` holds each
served request's units in its account's weekly window, or a key's budget window, for a key's
daily points (migration `d2f8b5c1e9a4` turns keys on for existing installs; no schema change).

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

1. **Reserve:** the requester owns an account of this provider with `o_a < R` and `u_a < 100`.
   Use it, with any model. A member's own account is always used first, up to their half.
2. **Pool:** `used_u < share_u` in every window, and some account has pool headroom. Any model.
3. **Credit:** `used_u < share_u + extra_u` in every window. Any model. The part above `share_u` is
   charged in credits, using the weekly window: `credits = over_units / (0.004 * C_weekly)`.
4. **Worker:** past all of the above, but some account still has pool headroom. Only worker models.
   If a big model was asked for, the gate swaps to the provider's worker model and the UI says so
   ("Your share is used up, so this reply uses Luna. Resets in 3 h 12 min.").
5. **Empty:** no account of this provider has pool headroom. Return a limit error with the
   natural reset time, and offer a worker on another provider. Never redeem a reset coupon.

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

### 7.6 Implementation (Phase 3)

`utils/allowance.py` holds the engine: `decide(request, state)` is pure, and every example above
is a test (`test/limits/test_allowance.py`). `build_state` turns what metering knows into the
engine's numbers, for the live gate and the replay alike:

- `u_a` is the account window's latest reading (§8); an unknown window counts as unused, never
  empty. A member's units on an account come from their tokens since the window last reset and
  the provider's calibration. Before there is one, the account's own rise, less outside use, is
  shared out by tokens weighted 1 : 0.1 : 4 : 4, so the members' units add up to the reading.
- `o_a` is the owner's units on their account plus their outside use (§8), over `c_a`, at most
  `u_a`. The owner is idle when neither has happened in 24 hours; their own request ends the loan.
- `used_u` is the member's units on accounts that are not theirs, plus their own shared
  account's use past the reserve, outside use included (decision 4).
- `N` counts every approved member (role user or admin).

Where this section is silent, the engine does the following:

- A **team** account is all pool (`C` gets `c_a * 100`). A **paused** account gives nothing.
- A connected, changed or removed account changes the pool at once: the 15-second cache below is
  rebuilt on the next request.
- An account **cooling down** still counts towards `C`, but is not chosen.
- An **API key** with a monthly budget has one window, `budget`: `u_a` is this month's spend in the
  ledger over the budget, and a $20 budget weighs 1 (a dollar is 5 units), so keys of different
  sizes share one pool like plans do. A key without a budget has no window and limits nobody.
- When no account with room serves the model asked for, a member within their share also gets the
  provider's worker model (tier 4), with a note that says so.
- **Worker models** are found by family in the provider's model list: Luna for ChatGPT and OpenAI,
  Haiku for Anthropic, Flash for Z.ai, Luna for OpenRouter (the newest of the family, as the
  default system model is found). The `mologate.worker_models` setting, `{provider: family}`,
  changes them. A worker model asked for within the share is plain pool use.
- Credits (§7.8) and overrides in force (§7.7) add their points to `extra_u`.

`utils/allowance_gate.py` runs it on every request a provider account serves, beside the
request: it never slows or fails one. The pool's numbers are shared for 15 seconds. The decision
(tier, account, model, whether it would swap, the reason and each window's capacity, share,
extra, use and what is left) is written on the request's ledger rows as `decision`, with the
mode. In `observe`, nothing changes: `tier` still says whose account served the request, and
the request goes where it would have gone. Enforcing the decision is Phase 4.

`GET /api/v1/provider-accounts/allowance?days=7` (admins) shows, per member, how many requests
went to each tier, and the same days decided again by `replay`: the ledger's requests, outside
use and the account readings, in order, under the rules. `blocked_with_room` counts requests the
rules refused, or sent to a worker within the member's share, while an account had room for
them; it must stay 0.

Tested with a week of five members on a ChatGPT Plus account (Ann's) and a Pro account five times
its size (Bob's), with Ann's laptop use, every decision enforced. Over five seeds: no request
refused; Dan, who works without a break, got about 570 big-model requests within his share and
3,200 on workers after; Cat, steadily busy, about 620 and 530; Eve, now and then, never ran past
her share; Ann used her reserve, then the pool; Bob's own Pro account always had room. Throughout,
big models went to the pool only within the share, the pool never took more than one request
from an owner's locked reserve, and an owner with reserve left always got it.

### 7.7 Enforcement (Phase 4)

With `MOLOGATE_LIMITS_MODE=enforce`, the account proxy (`utils/provider_account_proxy.py`,
`enforced_completion`) decides each request before sending it and serves it as decided.
`observe` works exactly as in Phase 3.

- **Account order:** the decision lists every account that can serve its tier, the decided one
  first (`Decision.candidates`), then the others by room for their size. Accounts known to be
  empty go last, as before.
- **Failover within a tier:** an account that answers 429 or a limit error is left out
  (`Request.exclude`) and the request is decided again. It moves to another account in the same
  tier, or to the next tier when none is left: an owner whose own account is rate-limited goes to
  the pool. An account it found used up is never tried again for that request.
- **Worker swap:** at tier 4 the request is sent with the provider's worker model and the member
  is told, as a status line under the reply ("Your share is used up, so this reply uses GPT Luna.
  Resets in 3 h 12 min."). API clients get the same text as `mologate.notice` in the answer, or
  the `X-Mologate-Notice` header of a stream. The reset time is the first reset of a window the
  member has used up. A model no account has room for, within the share, says so instead.
- **Tier 5:** a 429 that says when the first account frees up and, when one has room, names a
  worker model on another provider ("The team's shared ChatGPT accounts have no room left for you
  right now. The first one frees up in 3 h 12 min. Meanwhile, pick Claude Haiku 5.5 (Anthropic):
  it still has room."). No reset coupon is spent. An owner may explicitly redeem one (§10),
  but the rules' own limits, such as a locked reserve, are not lifted by a coupon.
  When every account failed for lack of credit, the 402 stays.
- **Ledger:** an enforced request's rows carry the decided tier in `tier`, the model actually
  served in `model_id`, and the decision with `mode: enforce`.
- **Per-account concurrency cap** (`utils/account_slots.py`): one account serves at most
  `mologate.max_account_requests` requests at once (default 4, hard cap 20; admins set it under
  Settings → General, env `MOLOGATE_MAX_ACCOUNT_REQUESTS`). A request holds its slot until its
  answer, or its stream, ends. When the decided account is full, the request goes to the next
  account of its tier; when all are full, it waits up to 2 minutes and is then refused as busy.
  Slots are leases in Redis (memory without it), renewed while held. `observe` counts them and
  logs a request that would have waited, but never waits.
- **Failing open:** a decision that cannot be made within 10 seconds, or unreadable settings, let
  the request go ahead as in `observe`; a broken gate never stops the team's work (§1).
- **Overrides** (`limit_override`, §6): admins grant a member, or the whole team, extra pool
  points until a date or until revoked, with a reason (Settings → Provider accounts →
  Overrides; `GET`, `POST /api/v1/provider-accounts/overrides`, `DELETE …/overrides/{id}`). Points
  in force add `extra_points / 100 × C` to the member's extra in every window, the same as
  credits, so past the share an override puts them in tier 3. Revoked and ended overrides stay
  listed with who granted and revoked them.
- **What the member sees:** `GET /api/v1/models/model/limits` adds `allowance` for an account
  model: the tier their next request would get, the model it would use, their credits, and per
  window their share, extra, use, what is left, when it frees up and, at the last 30 minutes'
  pace, about how many minutes of work are left. The context indicator's panel shows it under
  "Fair use" beside the account windows, and the warning above the message box starts at 80% of
  the share ("Weekly share: 84% used, about 40m of work at the recent pace. It frees up in 2d
  3h.") and says when replies use a worker model. A member on their own account gets no share
  warning: they are not drawing on the pool. When no shared account has room for the member
  (tier 5), the warning says that instead of their share. In `observe` the panel and the warning
  say what the rules would do ("Once limits are enforced, replies would use GPT Luna. Limits are
  not enforced yet, so nothing changes for now."), in amber, never as a refusal.
- **Tested** (`test/limits/test_allowance.py`): `EnforceTests` runs the real proxy in `enforce`
  with two keys and three members: the owner on their own account, the worker swap and its
  notice, failover within the pool tier and from a limited own account to the pool, tier 5 with
  its reset time and a worker on another provider, the per-account cap handing a request on and
  then refusing it as busy, a stream holding its slot to the end, a broken gate, overrides, the
  member's standing and observe unchanged. The simulated week now also fails one request in 20
  over: it never returns to an account that refused it and never moves to an earlier tier.
- **Tested in the running app** (2026-10-14, `test/limits/e2e/`): the real server, in `enforce`
  and then `observe`, against a fake OpenAI-compatible provider, with four members, two member
  keys and a team Anthropic key. Over HTTP: the owner on their own account; a member in the pool
  and at 88% of the share; past it, the worker model with the notice in the answer and in a
  stream's header; failover within the pool tier, and from an owner's limited key to the pool;
  three requests at once never putting two on one key; the pool spent until the refusal (429)
  says when it frees up and offers Claude Haiku on Anthropic, which then serves; the owner still
  on their locked reserve; and in `observe` the same member served as asked. 26 checks pass. In
  the browser: the share warning, the "Fair use" panel, the status line under a swapped reply,
  and granting and revoking an override. Found and fixed: the chat endpoint turned every error
  on the API path into a 400, so a limit error now keeps its 429 (or 402).
- **Not shown yet:** the panel lists an API key's budget only on the accounts page; for a key the
  "Fair use" share is what the panel shows.

### 7.8 Credits (Phase 5)

`utils/credits.py` and `models/credits.py`.

- **Subscriptions** earn their owner `10 × multiplier` credits when connected, once ever per
  provider account (decision 8): a ChatGPT account when codex-lb reports its plan, at connect or
  later, a Z.ai coding plan when it is added or an admin corrects its plan. The multipliers are a
  table admins edit in Settings → Provider accounts → Credits (`mologate.credit_multipliers`,
  `PUT /api/v1/provider-accounts/credits/multipliers`). Defaults: ChatGPT Plus, Business, Team,
  Enterprise and Edu 1, Pro Lite 5, Pro 20; Z.ai Coding Lite 1, Pro 5, Max 20. A plan without a
  multiplier earns nothing until an admin adds one; a changed table applies to accounts granted
  after the change.
- **API keys** earn their owner 0.5 credits for each dollar anyone spends through them, when the
  ledger writes the request's cost. Free models (cost 0) earn nothing; team accounts earn nobody
  anything. Requests through a key at the same time all count.
- **Spending:** a request served in tier 3 (`enforce` only) is charged
  `credits_for(units, C)` in the weekly window, or the budget window of an API key: the whole
  request, since a tier-3 request is past the share. Units come from the calibration, or before
  it from the request's part of the account's rise. The charge is written on the ledger row
  (`credits_charged`) and never takes a balance below zero; one member's charges are made one at
  a time.
- **Disconnecting** forfeits what the member got from that account and has not spent, as far as
  the balance still holds it. Reconnecting never grants again.
- **What the member sees:** the balance and what earned or used it under "Credits" in Settings →
  Provider accounts (`GET /api/v1/provider-accounts/credits`), and the balance in the context
  indicator's panel. The live gate passes the balance to `build_state(credits=...)`.
- **Tested:** `test/limits/test_credits.py` (grants once per provider account, multipliers, an
  admin's table, team accounts, earning through a key, free models, requests at once, spending in
  tier 3 and its charge, spending at once, forfeits on disconnect, the migration), and in the
  running app (`test/limits/e2e`, `drive.py credits`): a Z.ai Max plan earns 200, $4 through a
  key earns its owner 2, a member past the share keeps GPT-6 on credits and is charged 66.67 for
  40 units of a 150-unit pool, disconnecting forfeits the rest, and then past the share is the
  worker model again.

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
  weekly and credit windows (the first and the latest of each minute, in Redis with the limit
  snapshots). Readings come with requests, so a gap of over 10 minutes between two is idle time;
  the rise over the latest run of use within the last 30 minutes gives a pace. The remaining
  shares of every account that can serve a model, weighted by `c_a`, divided by their combined
  pace, give the minutes of work left (`limit_outlook` in `utils/provider_limits.py`, returned by
  `GET /api/v1/models/model/limits`). A window is low at 20% left or an hour of work, critical at
  10% or 15 minutes, unless it resets first. The chat shows it above the message box
  (`LimitsWarning.svelte`), and the context indicator's panel shows the account windows.
- **Readings (implemented, `utils/metering.py`):** every reading of an account's 5-hour and
  weekly windows, from answer headers, codex-lb's account summary or Z.ai's quota endpoint,
  updates `account_window` and, when the window rose or started a new period, adds an
  `account_reading`. A lower reading is a stale one and never lowers the window. Writes happen in
  the background and never slow a request.
- **Polling (implemented):** one worker (a Redis lease; every worker without Redis) runs the
  metering loop. Every minute it reads codex-lb's view of the ChatGPT accounts (codex-lb reads them
  from OpenAI every minute) and, every 5 minutes, each Z.ai coding plan's quota. So the owner's use
  outside Mologate shows up even while nobody uses Mologate. `MOLOGATE_LIMITS_MODE=off` stops it.
- **Owner and team split (started, an estimate):** until the ledger exists, Mologate counts the
  requests each account serves for its owner and for everyone else, per 5-hour and weekly window.
  When a reading shows the window's used percent rose, the rise is shared out by those counts and
  the counts start again; a reading with no rise keeps them for the next one. A rise with no
  requests from Mologate, and whatever was used before Mologate first read the window, is the
  owner's (decision 4). A reset time that moves on, or a drop of more than 5 points, starts the
  split over. Kept in Redis (or memory) with the limit snapshots; `utils/provider_limits.py`.
- **Intervals (implemented, `utils/calibration.py`):** every 5 minutes the loop cuts each window's
  readings into intervals of at least 15 minutes, using readings at least 2 minutes old, so the
  lag between a request ending and the provider counting it falls inside an interval. A new
  period starts its own interval at the period start with the window at 0. The first reading
  Mologate ever sees starts one at its period start: the use before it is the owner's.
- **Outside use (implemented):** an interval's expected rise is the units of the ledger rows in it
  (failed ones too: a cut-off stream still used the account) divided by `c_a`. A rise larger than
  that by at least 1 point and 20% of the expected rise becomes an `outside` row for the owner
  (decision 4). An interval without Mologate requests is all outside use. One with requests but
  no calibration yet stays unknown, and nothing is written.
- **Calibration (implemented):** once a day (hourly until a provider window has its first fit),
  each provider window is fitted from the last 14 days of intervals that had Mologate traffic,
  except an account's first. Features are uncached input, cached input, output (without
  reasoning) and reasoning tokens, in millions. The fit is non-negative least squares pulled
  towards typical price ratios (1 : 0.1 : 4 : 4) when the data cannot tell the features apart.
  It is alternated with a scale per model seen in at least 8 intervals; other models get the
  highest scale (the provider's most expensive known `k`, a conservative estimate). Intervals far
  from the fit (2.5 robust spreads and at least 1.5 points) are left out, in up to two rounds,
  since outside use only adds to a rise. At least 12 intervals are needed. When the provider's
  base-plan accounts (ChatGPT Plus, Z.ai Lite: weight 1) have enough intervals, `k` comes from
  them alone, and every other account's capacity weight is fitted against them (`fitted_weight`).
  The fit never changes an account's `capacity_weight`: an account whose fitted weight is more
  than 25% off is flagged (`weight_mismatch`) for an admin to set, and kept out of the headline
  error until then.
- **Reports (implemented):** `GET /api/v1/provider-accounts/usage` gives a member their requests,
  tokens and cost of the last 7 days per account, their share of each account window since it
  last reset (once calibrated), and, for owners, their outside use. Settings → Provider accounts
  shows it under "Your use". Admins get every calibration, each account's error against the 10%
  bar and its fitted weight, the latest windows, and a week of intervals and outside use from
  `GET /api/v1/provider-accounts/metering`. `POST /api/v1/provider-accounts/metering/run` polls,
  measures and calibrates at once.
- **Tested** (`test/limits/test_metering.py`): besides the unit tests, a simulation of six days
  of a small team on a ChatGPT Plus account and a Pro account five times its size (left at weight
  1), with costs known, whole-percent readings, resets and the owner's laptop use on days 4 to 6.
  Over eight random seeds: the fit after three days is within 0.5 to 2.5% on the Plus account,
  the Pro account's fitted weight is 4.9 to 5.0, 83 to 91% of the outside use is found with none
  found on the Pro account, and after six days, outside use included, the Plus account is within
  2.3 to 5.6%. Real providers are noisier; the week of `observe` below is the real test.
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

**Current generation only:** for the labs in `utils/model_generations.py`, the model list shows
only the current generation: the GLM-5.3 family for Z.ai, and GPT-6 for OpenAI, where the Sol tier
counts from GPT-6.1. Older models are flagged hidden, like an admin hiding a model (Admin → Models
shows them): they leave the selectors but stay callable, so chats and presets built on one keep
working. Other labs are not filtered. To follow a release, change that lab's rule.

**Team accounts:** an admin can connect a key or a ChatGPT account as a team account (the "Team
account" switch). It belongs to no member: everyone uses it, admins manage it, it is shared or
paused, and all of its use is pool use. Admin connections to a supported
provider (OpenAI, Anthropic, OpenRouter, Z.ai) with a plain key become team accounts at startup
and whenever an admin saves the connections (`utils/connection_accounts.py`), so their models
come from providers through account selection, failover and per-account limits. Local servers,
Azure, and connections with custom headers or another sign-in stay connections, as does a key a
member already connected. The adopted connection's model ids keep working: saved model settings,
presets, access grants and the default, pinned, ordered and task models move to the account ids,
and a request naming an old id is sent to the new one (`mologate.model_aliases`).

**Account row:** collapsed by default to its generated name, provider, plan, owner, a note only
when it matters (Team, Paused, Sign-in required), the 5-hour and weekly bars, and a coupon badge
(count; the tooltip has the last successful reset). On a shared account each of those windows is
two bars: the owner's reserve and the team pool, each out of 50 points (decision 2), with the
owner's use past 50 shown as pool use. An API key's row shows its credit limit and budget
instead. Expanded: every limit window, with "You · 8/50" and "Team · 15/50" under a shared
window, an API key's budget left this month (estimated, §8, except OpenRouter's), each member's
requests, tokens and cost from the ledger (this week for a subscription, this month for a key), a
"Who can use" dropdown (Team, Paused), and Verify, budget and Disconnect buttons. Manual limit reset
is an explicit owner/admin action, never an automatic pool refill. The split is shown only: nothing enforces the reserve until Phase 4, and the idle-owner
loan (decision 3) is not shown yet.

**Disconnecting:** unspent credits from that account's grant are forfeited (a `forfeit` entry, §7.8).
Reconnecting the same account doesn't grant again.

---

## 10. Manual limit reset

Account failover remains automatic, but inference never redeems reset coupons in either
`observe` or `enforce` mode. When all usable accounts are exhausted, return a limit error with
the natural provider reset time where known. Unknown quota never counts as empty.

- **Account switching first:** requests try the member's own accounts and then shared accounts,
  with known-empty accounts last. Provider exhaustion, 429 and applicable codex-lb 503 responses
  cause failover to another eligible account, without spending any reset credits.
- **Explicit action only:** in **Settings → Provider accounts**, an account owner or admin can
  select **Reset limits** on a ChatGPT account and confirm the irreversible coupon spend.
  `POST /api/v1/provider-accounts/{account_id}/reset` requires authentication and account management
  permission. It targets that account only, never another owner's account in the pool. Non-ChatGPT
  accounts return 400; unavailable coupons, cooldowns or unconfirmed outcomes return 409.
- **Provider API:** codex-lb lists coupons with `GET /api/accounts/{id}/rate-limit-reset-credits`
  and consumes the selected account's soonest-expiring coupon with
  `POST …/rate-limit-reset-credits/consume {redeemRequestId}`. The account is re-read under a
  lock first; an account already refilled needs no coupon. A pending `limit_reset_event` records
  the acting user and idempotency key. Send redemption once, never automatically retry an
  ambiguous outcome, then refresh the account's limits and invalidate allowance cache.
- **Guards:** pending, successful and ambiguous/failed redemption events block another coupon
  for 10 minutes. A codex-lb refusal before contacting OpenAI does not block. Audit history,
  coupon counts, last reset time (`last_reset_at`) and credit/usage visibility remain available.
- **No automation or global switch:** the former `/auto-reset` endpoints and
  `MOLOGATE_AUTO_RESET` / `mologate.auto_reset` configuration are removed. Old stored settings
  have no effect. No background job or exhausted inference request may consume a coupon.
- A redeemed coupon cannot be transferred or undone. Normal allowance rules still apply to the
  refilled window.

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

**Implementation (Phase 6, 2026-10-14):**

- `models/api_keys.py`, `utils/api_key_limits.py`, `routers/api_keys.py`
  (`GET`, `POST /api/v1/api-keys`, `PATCH`, `DELETE /api/v1/api-keys/{id}`), and Settings → API keys
  (`ApiKeys.svelte`). API keys and the members' permission are on by default (`ENABLE_API_KEYS`,
  `USER_PERMISSIONS_FEATURES_API_KEYS`), and migration `d2f8b5c1e9a4` turns them on once for
  installs that stored the old defaults.
- **Keys:** up to 20 named keys per member. A key is shown once, when it is made, and stored as a
  hash with a short hint (`sk-5b…44f7`) to recognise it by. Revoking keeps its record, so the
  ledger still names it; a revoked or expired key is refused at once (401). Open WebUI's one key
  per member keeps working, is listed as "Default key", and its old Account settings section now
  points to the keys page.
- **Auth** (`utils/auth.py`) finds the key, puts its id and limits on `request.state`, and the
  ledger writes the id into `api_key_id`, with source `api_key`. Every request a key makes is its
  owner's: decided by the allowance engine, counted in the owner's share, and refused or swapped
  exactly as the owner would be.
- **Endpoints:** a member's key reaches only `/api/chat/completions`, `/api/v1/chat/completions`,
  `/api/models`, `/api/v1/models`, `/api/v1/models/model/limits`,
  `/api/v1/provider-accounts/usage` and `/api/v1/provider-accounts/credits`, matched exactly
  (the models router also lives under `/api/v1/models`). Not `/openai/*`, the account routes or
  the key routes, so a key cannot make more keys. An admin's key keeps the whole API.
- **Limits**, each optional and checked against the owner: requests per minute (up to 600, every
  endpoint); requests at once to provider-account models (up to the owner's chat cap, §12 Phase
  4); the model ids and reasoning efforts it may ask for (checked against the id the client sent);
  pool points a day per provider (summed from the ledger's `est_units` over the provider's
  weekly or budget capacity, starting at midnight UTC); expiry. Limits a member set on their own
  key apply in every mode but `off`. A key without a concurrency limit runs at most as many
  requests at once as its owner may run chats, in `enforce` only.
- **Tested:** `test/limits/test_api_keys.py` (hashing and showing once, auth with the key's id,
  revoked, expired and default keys, the endpoint allowlist, each limit, the owner's bounds, the
  ledger and share, the migration) and in the running app with the official OpenAI Python SDK
  (`test/limits/e2e`, `drive.py keys`): the SDK lists the models, chats and streams with Cat's
  key; the ledger names the key as her use; her share rises by exactly the units the requests
  cost; her keys page shows the use and today's pool points; the key gets 403 on the key routes,
  `/openai/*` and the account routes; a 2-a-minute key gets 429 on its third request; a key
  cannot run more at once than its owner; a revoked key gets 401 at once. 14 checks pass, in
  `enforce` and in `observe`.

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
- [x] Account polling, outside-use detection, calibration jobs (§8, `utils/metering.py`).
- [x] Accounts page and a "my usage" page (read-only numbers): the accounts page shows each
      shared account's owner and team use, estimated from request counts (§8), each member's
      requests, tokens and cost from the ledger, and an API key's budget left; "Your use" in
      Settings → Provider accounts shows the member's own numbers, and admins have the metering
      report (`GET /api/v1/provider-accounts/metering`).
- [ ] Done when: after a week, ledger units match the provider's percent changes within 10% per
      account.

### Phase 3: allowance engine

- [x] `decide()` as a pure module with every example in §7.5 as a test, plus a simulation that
      replays a week of ledger data under the rules (§7.6: `utils/allowance.py`, the replay in
      `GET /api/v1/provider-accounts/allowance`).
- [x] `MOLOGATE_LIMITS_MODE=observe` records decisions on every request (`usage_event.decision`).
- [ ] Done when: tests pass and the simulation shows no member blocked while the pool has headroom
      for their tier. The tests and the simulated week pass (§7.6); the replay of a week of the
      team's real requests is still to come.

### Phase 4: enforcement

**Built and tested in the running app: 2026-10-14** (§7.7). Waiting for `enforce` on the
deployed site.

- [x] Gate on: tiers, account choice, worker swap with a notice, limit errors with reset times.
      Done: in `enforce` the request is decided first and served as decided; past the share the
      reply uses the worker model and says so; tier 5 answers with when the pool frees up and a
      worker on another provider.
- [x] Failover on 429 or limit errors to another account in the same tier. Done: the account
      is left out and the request decided again, so it stays in its tier while one is left.
- [x] Per-user concurrency cap (3 chats generating, from ROADMAP.md): `utils/chat_slots.py`.
      Past the cap a chat waits in line and shows its place; sub-agents, background results,
      timers, automations and channel replies take no slot. Admins set it under Settings →
      General (hard cap 10). It waits only in `enforce`; `observe` logs a chat that would have.
- [x] Per-account concurrency cap. Done: `utils/account_slots.py`, 4 requests per account at
      once by default (admins change it under Settings → General).
- [x] Context indicator shows tier, share left, credits and the account windows; warning at 80% of
      the share with an estimate of minutes left from the last 30 minutes of use. Done: "Fair
      use" in the context indicator's panel (the limits ring was folded into the panel in
      PR #7), and the share warning above the message box.
- [x] Admin and team overrides (`limit_override`), with an audit log. Done: Settings → Provider
      accounts → Overrides, for admins; revoked overrides stay listed.
- [ ] Done when: in `enforce`, the §7.5 scenarios behave as written on the deployed site.

### Phase 5: credits and resets

- [x] Grants on connect (a subscription's), and for API keys as money is spent through them
      (decision 7); forfeit on disconnect, spending in tier 3, balance in the UI. Done
      (2026-10-14, §7.8): tested in the running app as well as by the suite.
- [x] Explicit owner/admin manual limit reset (§10), without a global automatic switch.
- [ ] Done when: a confirmed manual reset refills a real account; exhausted inference never
      spends a coupon.

### Phase 6: personal API keys (§11)

**Built and tested in the running app: 2026-10-14** (§11).

- [x] Several named keys per member, the key's id in `request.state` and the ledger.
- [x] Per-key limits never beyond the owner's; keys restricted to the gated endpoints.
- [x] A "my keys" page (Settings → API keys).
- [x] Done when: a member uses Mologate from an outside OpenAI-compatible client with their own
      key, and that use shows up in their share. Done with the official OpenAI Python SDK
      against the running app (§11); on the deployed site once this branch is there.

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
- Reset policy supersedes the 2026-10-11 automatic-redemption design: only explicit owner/admin
  manual resets are permitted. Exhaustion still triggers account failover, not coupon spending.
- 2026-10-12: Claude subscriptions are deferred and disabled. Codex's Phase 7 plan, including the
  two entries he wrote into this log on 2026-10-11, moved to [claude-runners.md](claude-runners.md)
  as proposals awaiting review; none of it is a decision.
- 2026-10-12: observe mode never interrupts work; only very critical harm is stopped in any mode
  (no sign-in, a paused account or someone else's credentials, load that would take the server
  down).
- 2026-10-12: team accounts: an admin's key or ChatGPT account can belong to no member and serve
  everyone. Admin connections to a supported provider become team accounts; others stay
  connections. Any approved member can connect an account. Admins can correct a Z.ai coding plan.
  Deployment switches set in the environment win at every start.
- 2026-10-14: every account is shared with the team; "Only me" is removed and existing private
  accounts become shared. Owners keep their reserve and can pause their account. A member's own
  account is used first. API keys earn credits by the dollars actually spent through them, by
  anyone, for their owner (0.5 per dollar); free keys and free models earn none.
- 2026-10-14: Phase 4 choices where the spec was silent: failover re-decides without the account
  that refused, so a request may move to a later tier (an owner's limited account sends them to
  the pool); a coupon reset is tried at tier 5 only for accounts the provider reported used up;
  a team override is a `limit_override` without a member and applies to everyone; a request
  waits up to 2 minutes for a busy account; a gate that cannot decide lets the request through.
- 2026-10-14: Phase 5 choices where the spec was silent: default multipliers as in §7.8 (Z.ai
  from its published plan sizes, Pro 5× Lite and Max 4× Pro); a ChatGPT plan read only after
  connecting still earns its grant, once; a tier-3 request is charged for all its units in the
  weekly window (or a key's budget); a forfeit takes what the account gave the member and is
  still in their balance; an API key's grant stays with the key, and its earnings go to whoever
  owns the key now.
- 2026-10-14: Phase 6 choices where the spec was silent: personal keys are stored hashed and
  shown once; Open WebUI's one key per member keeps working; a member's key reaches only the
  gated endpoints, an admin's the whole API; limits a member sets on their own key apply in
  `observe` too (they are the member's own choice), the default cap on a key's requests at once
  only in `enforce`; daily points are counted per provider from midnight UTC.
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
