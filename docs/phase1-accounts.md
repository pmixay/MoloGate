# Phase 1: provider accounts

Implementation branch: **`limits-and-accounts`**, shared by all roadmap phases.
Phase 1 remains in progress until two members connect Codex accounts in the browser and both
complete chats. At the owner's request, perform that acceptance test on
[ai.arbuz.lol → Provider accounts](https://ai.arbuz.lol/?settings=provider_accounts).

Deployed to production on 2026-10-11 from `limits-and-accounts` image commit `05a3311`, after
[image CI and account regressions](https://github.com/pmixay/MoloGate/actions/runs/38141648967)
passed on both architectures. The live image is pinned to
`ghcr.io/pmixay/mologate@sha256:d983bdc3081560bc0312f46591b5f72d2646dc44a7960bcbf8bb555582716abd`.
Existing production users, provider connections, data and registration policy are retained.
Later commits on the branch are not deployed yet: automatic account switching and limit resets,
admin-only account creation, ChatGPT link sign-in, full-text uploads, and ChatGPT accounts through
codex-lb with generated account names and a single team-wide auto-reset switch, and team accounts
(admin connections to a supported provider move into them on the first start). They add
migrations `c3d9e7a5b1f0`, `d8e2b6c4a9f3`, `e5a3c1f8d2b7` and `f7c2a9d4b8e1`, and a `codex-lb`
service in the compose file.
[Draft PR #3](https://github.com/pmixay/MoloGate/pull/3) contains the consolidated implementation.
Deployment for testing does not mean the PR has merged or Phase 1 acceptance is complete.

Anthropic API-key accounts are in this phase. Claude Pro/Max subscription containers are a
separate [Phase 7 plan](claude-runners.md); they do not change Phase 1's completion criteria.

## Account management

Open **Settings → Provider accounts** on https://ai.arbuz.lol. Approved members and admins can connect
ChatGPT/Codex, Anthropic API, OpenAI API, OpenRouter, or Z.ai accounts. API-key accounts require a
monthly USD budget; the accounts page shows how much of it is left this month (from OpenRouter,
or estimated from the usage ledger for other keys), but spending enforcement belongs to Phase 4. Z.ai coding
plans use their dedicated endpoint. Every account is shared with the team (since 2026-10-14; private accounts were made shared). Owners and admins can verify,
pause, change sharing/budget, or disconnect an account. A paused account cannot serve anyone.

`/api/v1/provider-accounts` provides list/create, `/{id}` provides PATCH/DELETE, and
`/{id}/verify` refreshes verification. POST `/codex` starts a ChatGPT login with
`method: "link"` or `"device_code"`; GET `/codex/logins/{id}` returns its status only to its
initiating user, POST `/codex/logins/{id}/callback` takes the pasted callback address of a link
login, and DELETE `/codex/logins/{id}` cancels it. One pending login per member and one ChatGPT
connection at a time across the team are allowed. Login attempts expire after 15 minutes. The UI
remembers the attempt ID so reopening Settings can resume. Accounts are named by the server
(`Account #1n9Dk5`); members do not set labels. PATCH `/{id}` takes `sharing`, `budget` and
`capacity_weight` only; automatic resets are switched on or off for the whole team at
`/auto-reset`, never per account.

With a link login, the member opens OpenAI's sign-in page, signs in, and pastes back the address
their browser opens at `http://localhost:1455/auth/callback`. Only the `code` and `state` of that
address are passed to codex-lb, after a state check.

Provider secrets use the existing Fernet facility. Responses contain an explicit public-field
allowlist; neither ciphertext, key fingerprints, nor raw credentials are returned. Input validation
does not echo credentials. Verification only contacts fixed HTTPS origins, with redirects disabled.
Optional request/response audit logging redacts all provider-account bodies, including device codes.
OpenRouter verification calls `/key` because `/models` is public. Duplicate provider/key fingerprints
and duplicate ChatGPT account IDs are rejected by a database uniqueness constraint.

## Routing and limits

Models use `account:{provider}:{upstream_model_id}`. Model IDs remain stable when members connect,
disconnect, or reorder accounts. Each request selects an eligible owned account first, then a
healthy shared account. An empty account, or one that answers 429, hands the request to the
next one ([automatic limit reset](limits-and-accounts.md#10-automatic-limit-reset)). Private
access is checked again during inference, including for admins, direct OpenAI routes, API-key
requests, and model presets. Admin management permission does not
permit inference through another member's private account.

Admin → Models labels an account-backed model **Public** while at least one healthy account
offering it is shared with approved members. Otherwise it is **Private** to its eligible owner.
This label follows current account sharing, including when multiple accounts offer the same model;
saved model ACLs do not override it. Its sharing controls open **Provider accounts**. Public here
means available to approved team members; it does not grant anonymous or pending users access.
An admin's overrides on an account model (name, vision, effort levels, context window, hiding)
change how it behaves, never who may use it: chat requests decide access by account sharing alone,
while presets built on an account model keep their own access settings.

Account models carry the details their provider lists for them (effort levels, input types,
context window) under `openai`, like models from a configured connection, so the effort selector,
the image check and the context indicator treat them the same way. Z.ai lists no effort levels
for its GLM models, so `utils/reasoning_effort.py` supplies the ones Z.ai documents (GLM-5.3: low,
high, max, default max); an admin's override still wins. Only a 401 from the provider
marks an account "Sign-in required"; a 403 refuses that one request and leaves the account in use.

Provider errors reach the member in the provider's own words (`utils/provider_errors.py`): one
line of at most 500 characters, control characters removed and anything that looks like a key or
token redacted, since providers sometimes echo part of a key. Bodies and keys are never logged.
A used-up quota or credit (HTTP 429 or 402, codes such as `usage_limit_reached`,
`rate_limit_exceeded` or `insufficient_quota`, or a Responses `response.failed` with such a code
before any output) hands the request to the next account; when none is left the member sees the
usage-limit message with the wait (`retry-after` counts too), or that the API key is out of credit.
A refusal (Responses refusal parts, `message.refusal`, `finish_reason: content_filter`, a
moderation 403) is shown as the model declining and never fails over; the chat flags it with a
warning under the answer (docs/refusals.md). A stream that stops early
keeps its text and ends with a `length` or `content_filter` finish and an error chunk saying why:
cut off at the output token limit, filtered, interrupted, or the provider's error.

The new catalog is refreshed separately from legacy connection caches. Existing configured
connections remain usable. New snapshots use provider-account UUIDs; legacy snapshots use a hash
of their URL and credential so reordering a connection cannot attach another account's limits.
Chat, Responses, and the deprecated proxy all record reported headers. Redis stores snapshots
with an in-memory fallback. `mologate.limits_mode=off` disables the new endpoints and models;
the default is `observe`.

ChatGPT accounts run through codex-lb (`ghcr.io/soju06/codex-lb:1.24.0`), an internal service
on a private network that only Mologate reaches. codex-lb stores the tokens, refreshes them,
polls limits and reset coupons every minute, syncs the model catalog every 5 minutes, and sends
requests upstream as the official Codex client. Each Mologate account holds a codex-lb key scoped
to that one account, and Mologate requires codex-lb's key authentication to be on before sending
any ChatGPT request, so a request can only reach the account Mologate chose. Chat clients receive
translated text/tool SSE deltas and normalized token usage. An account codex-lb reports as
`reauth_required` or `deactivated` is marked as needing a new sign-in. Accounts connected through
the earlier `codex app-server` integration are imported into codex-lb from their `auth.json` the
first time they are used, and their old home directory is then deleted.

Scheduled ledger polling belongs to Phase 2. Owner reserves, account weights, credits, and
spending enforcement are not applied yet.

The initial Z.ai spike on the VPS read the existing key in memory. The
coding model endpoint returned 11 models, rejected a deliberately invalid key with HTTP 401, and
did not return limit headers. `GET https://api.z.ai/api/monitor/usage/quota/limit` returned two
`CREDIT_LIMIT` windows: `(unit=3, number=5)` for five hours and `(unit=6, number=1)` for a week,
with `percentage` and `nextResetTime`. Z.ai's own usage plugin sends the key itself in
`Authorization` (no `Bearer`), names the windows `TOKENS_LIMIT` and treats `TIME_LIMIT` as the
monthly MCP allowance; Mologate now calls it the same way and reads both window types. The quota
is read at connect and Verify, when the accounts page lists the account, and after each request,
at most once a minute per account. Unknown window types or unavailable quota never imply zero
usage. This console endpoint is undocumented and can change independently of the inference API.

## Verification

- The suite now has 117 backend tests; image CI runs them on amd64 and arm64 before publishing.
  The limits work after Phase 1 is tested in `test_usage_limits.py`.
  The 38-test suite of `aec1626` passed there:
  [build 38146942579](https://github.com/pmixay/MoloGate/actions/runs/38146942579).
- Shared-account visibility regression: all 24 backend tests of that change passed, including
  mixed private/shared pools in either order, unhealthy accounts, pending users, sharing
  revocation, and saved model ACLs.
  An ordinary temporary member saw all seven models from the owner's connected Codex account and
  completed a real chat with reported usage on `https://ai.arbuz.lol`; the test member was removed.
  This verifies shared use of one real account. Acceptance still requires a second member to
  connect a distinct real Codex account and complete a chat.
- Image `05a3311` passed account regressions on amd64 and arm64 and was deployed on the main site.
  Live desktop/mobile Chromium checks confirm all seven Codex models display **Public** and the
  sharing shortcut opens Provider accounts. Browser-only response fixtures verify that saved
  private/public model ACLs cannot override current account sharing. No browser errors occurred.
  A fresh ordinary member also saw all seven models with shared visibility after deployment;
  this temporary member was removed.
- Production at `https://ai.arbuz.lol` now passes trusted HTTPS, HTTP/old-hostname redirects,
  frontend asset loading, existing administrator sign-in, the authenticated account API and a
  real `glm-5.3-flash` chat. Migration reached `b1a7c9e4d205`; all 3 existing users and 11 model
  records were retained. Limits mode remains `observe`. Self-registration was later removed in
  code (`41ae16a`, not deployed yet); admins create every account.
- 20 backend regressions passed against an isolated PostgreSQL database; 21 passed on SQLite
  after adding the audit-redaction regression. Image CI passed all 21 on both amd64 and arm64:
  [build 38136120277](https://github.com/pmixay/MoloGate/actions/runs/38136120277).
- A full application startup against a fresh pgvector/PostgreSQL database migrated through the
  new head and reached `/ready`. Full HTTP checks confirmed owner-only model visibility,
  forbidden direct chat/Responses access by another member, immediate sharing, and pause behavior.
- A test deployment (since removed) migrated to `b1a7c9e4d205` and reached healthy status. A
  temporary Z.ai account and temporary ordinary member completed real `glm-5.3-flash` chats
  through the shared account, both normal and streamed; usage and both quota windows were
  returned. Private access was rejected. The temporary provider credentials and member were
  deleted afterwards.
- Both changed Svelte components compiled without warnings. That deployment's Settings screen
  passed desktop and mobile Chromium checks, including switching between Z.ai and Codex, with no
  browser page errors. Screenshots were inspected for layout and readability.

The pre-Phase-1 production image ID is recorded in `/opt/mologate/phase1-rollback-image`, and
the image before the sharing-label fix (`45ec77e`) in `/opt/mologate/sharing-rollback-image`.
Backups are off during development, so an image older than the current database migration
cannot be started without losing the data written since.

The stdlib regression suite lives at `backend/open_webui/test/limits/test_provider_accounts.py`.
It covers at-rest encryption and response redaction, database deduplication, owner/admin/private
permissions, live visibility changes, disabled mode, owner deletion, ChatGPT sign-in through a
fake codex-lb (link and device code, one connection at a time, reconnects), single-account key
scoping, legacy account import, SSE chunk boundaries/Unicode/tools/usage, provider errors
(messages, key redaction, credit and limit failover, refusals, interrupted and cut-off answers),
OpenRouter key verification, header/quota parsing, migration up/down, account failover and
automatic resets, the owner and team usage split, generated account names, sign-up refusal, and
the full-text upload migration. `test_usage_limits.py` covers OpenRouter key limits, the limit
outlook and pace, the per-member chat queue (in memory, and in Redis with `TEST_REDIS_URL` or
`fakeredis[lua]`, otherwise skipped), the usage ledger with its cost estimates, and the
`usage_event` migration.
Run against an isolated database whose name contains `provider-tests`; never use a live database.

```bash
docker run --rm --network none \
  -v /root/MoloGate/backend:/app/backend:ro \
  -e DATA_DIR=/tmp/provider-tests -e STATIC_DIR=/tmp/provider-static \
  -e DATABASE_URL=sqlite:////tmp/provider-tests/webui.db \
  -e ENABLE_DB_MIGRATIONS=False -e WEBUI_SECRET_KEY=provider-test-only \
  -e OFFLINE_MODE=True -e CORS_ALLOW_ORIGIN=http://localhost \
  --entrypoint python ghcr.io/pmixay/mologate:limits-and-accounts-slim \
  -m unittest discover -s open_webui/test/limits -v
```

The same tests support a disposable PostgreSQL database via `DATABASE_URL` and
`TEST_MIGRATION_DATABASE_URL`. Image CI runs them against the built runtime on both architectures
before publishing the branch's image tag. Build frontend/images in GitHub Actions, not on this VPS.

Earlier, the real `codex app-server` 0.162.1 was checked with an empty isolated home; it has since
been replaced by codex-lb. Browser acceptance still requires two users to complete OpenAI's login
themselves and send chats through their connected models.

codex-lb reference: [Soju06/codex-lb](https://github.com/Soju06/codex-lb).
Provider references: [OpenRouter key verification](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
[Z.ai API](https://docs.z.ai/api-reference/introduction).
