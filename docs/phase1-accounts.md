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
[Draft PR #3](https://github.com/pmixay/MoloGate/pull/3) contains the consolidated implementation.
Deployment for testing does not mean the PR has merged or Phase 1 acceptance is complete.

Anthropic API-key accounts are in this phase. Claude Pro/Max subscription containers are a
separate [Phase 7 plan](claude-runners.md); they do not change Phase 1's completion criteria.

## Account management

Open **Settings → Provider accounts** on https://ai.arbuz.lol. Approved members and admins can connect
ChatGPT/Codex, Anthropic API, OpenAI API, OpenRouter, or Z.ai accounts. API-key accounts require a
monthly USD budget; this value is stored but spending enforcement belongs to Phase 4. Z.ai coding
plans use their dedicated endpoint. New accounts default to private. Owners and admins can verify,
pause, change sharing/budget, or disconnect an account. A paused account cannot serve anyone.

`/api/v1/provider-accounts` provides list/create, `/{id}` provides PATCH/DELETE, and
`/{id}/verify` refreshes verification. POST `/codex` starts a device login;
GET `/codex/logins/{id}` returns its status only to its initiating user. At most one pending login
per member and four concurrent attempts per worker are allowed. Login attempts expire after
15 minutes. The UI remembers the attempt ID so reopening Settings can resume polling.

Provider secrets use the existing Fernet facility. Responses contain an explicit public-field
allowlist; neither ciphertext, key fingerprints, nor raw credentials are returned. Input validation
does not echo credentials. Verification only contacts fixed HTTPS origins, with redirects disabled.
Optional request/response audit logging redacts all provider-account bodies, including device codes.
OpenRouter verification calls `/key` because `/models` is public. Duplicate provider/key fingerprints
and duplicate ChatGPT account IDs are rejected by a database uniqueness constraint.

## Routing and limits

Models use `account:{provider}:{upstream_model_id}`. Model IDs remain stable when members connect,
disconnect, or reorder accounts. Each request selects an eligible owned account first, then a
healthy shared account. Private access is checked again during inference, including for admins,
direct OpenAI routes, API-key requests, and model presets. Admin management permission does not
permit inference through another member's private account.

Admin → Models labels an account-backed model **Public** while at least one healthy account
offering it is shared with approved members. Otherwise it is **Private** to its eligible owner.
This label follows current account sharing, including when multiple accounts offer the same model;
saved model ACLs do not override it. Its sharing controls open **Provider accounts**. Public here
means available to approved team members; it does not grant anonymous or pending users access.

The new catalog is refreshed separately from legacy connection caches. Existing configured
connections remain usable. New snapshots use provider-account UUIDs; legacy snapshots use a hash
of their URL and credential so reordering a connection cannot attach another account's limits.
Chat, Responses, and the deprecated proxy all record reported headers. Redis stores snapshots
with an in-memory fallback. `mologate.limits_mode=off` disables the new endpoints and models;
the default is `observe`.

Codex app-server is pinned to **0.162.1**, downloaded from the official npm package with a pinned
SHA-512 integrity check during the image build. Each account has a mode-0700 `CODEX_HOME` within
the data volume and a mode-0600 managed `auth.json`. The database encrypts the home identifier.
Subprocesses do not inherit application secrets. Per-home file locks serialize refresh operations;
after `account/read` confirms the ChatGPT login, the managed file's `tokens.account_id` supplies
the unique provider identity (the current `account/read` response supplies the plan, not the ID).
no chat turns or tool requests are sent to app-server. Inference uses the managed access token
with `https://chatgpt.com/backend-api/codex/responses`. Chat clients receive translated text/tool
SSE deltas and normalized token usage. Provider auth failures mark the account as needing login.

Initial Codex windows are read after login and refreshed when verifying or starting a request;
response headers can update them afterwards. Scheduled polling and the usage ledger belong to
Phase 2. Owner reserves, account weights, credits, and spending enforcement are not applied yet.

The initial Z.ai spike on the VPS read the existing key in memory. The
coding model endpoint returned 11 models, rejected a deliberately invalid key with HTTP 401, and
did not return limit headers. `GET https://api.z.ai/api/monitor/usage/quota/limit` returned two
`CREDIT_LIMIT` windows: `(unit=3, number=5)` for five hours and `(unit=6, number=1)` for a week,
with `percentage` and `nextResetTime`. Connect/verify reads these as best-effort observations.
Unknown window types or unavailable quota never imply zero usage. This console endpoint can
change independently of the published inference API.

## Verification

- Shared-account visibility regression: all 24 backend tests pass, including mixed private/shared
  pools in either order, unhealthy accounts, pending users, sharing revocation, and saved model ACLs.
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
  records were retained. Limits mode remains `observe`, and registration still requires approval.
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

Production pre-deployment backup: `/opt/mologate/backups/2026-10-11T12-36-21-020692649Z`.
The previous production image ID is recorded in `/opt/mologate/phase1-rollback-image`.

Before the sharing-label fix, a further backup was taken at
`/opt/mologate/backups/2026-10-11T13-01-40-451168846Z`, including the connected Codex account.
Its previous Phase 1 image reference is in `/opt/mologate/sharing-rollback-image`; this fix adds
no database migration.
To roll back, restore the matching database backup before starting the old image, because its
Alembic code does not know the new revision. The data volume and encryption secret must be
restored together.

The stdlib regression suite lives at `backend/open_webui/test/limits/test_provider_accounts.py`.
It covers at-rest encryption and response redaction, database deduplication, owner/admin/private
permissions, live visibility changes, disabled mode, owner deletion, simulated device login with
real subprocess JSON-RPC, duplicate ChatGPT IDs, SSE chunk boundaries/Unicode/tools/usage,
provider errors, OpenRouter key verification, header/quota parsing, and migration up/down.
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

The real installed app-server has also been checked with an empty isolated home: initialization,
empty `account/read`, `chatgptDeviceCode` issuance to `auth.openai.com`, and login cancellation all
succeeded. No real ChatGPT account was connected by that probe. Browser acceptance still requires
two users to complete OpenAI's login themselves and send chats through their connected models.

Protocol reference: [official app-server documentation](https://learn.chatgpt.com/docs/app-server).
Provider references: [OpenRouter key verification](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
[Z.ai API](https://docs.z.ai/api-reference/introduction).
