# Phase 1: provider accounts

Implementation branch: `phase1-accounts`, stacked on `limits-and-accounts`.
Phase 1 remains in progress until two members connect Codex accounts in the browser and both
complete chats. Production continues to use the Phase 0 deployment.

## Account management

Open **Settings → Provider accounts** on staging. Approved members and admins can connect
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
OpenRouter verification calls `/key` because `/models` is public. Duplicate provider/key fingerprints
and duplicate ChatGPT account IDs are rejected by a database uniqueness constraint.

## Routing and limits

Models use `account:{provider}:{upstream_model_id}`. Model IDs remain stable when members connect,
disconnect, or reorder accounts. Each request selects an eligible owned account first, then a
healthy shared account. Private access is checked again during inference, including for admins,
direct OpenAI routes, API-key requests, and model presets. Admin management permission does not
permit inference through another member's private account.

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
no chat turns or tool requests are sent to app-server. Inference uses the managed access token
with `https://chatgpt.com/backend-api/codex/responses`. Chat clients receive translated text/tool
SSE deltas and normalized token usage. Provider auth failures mark the account as needing login.

Initial Codex windows are read after login and refreshed when verifying or starting a request;
response headers can update them afterwards. Scheduled polling and the usage ledger belong to
Phase 2. Owner reserves, account weights, credits, and spending enforcement are not applied yet.

The Z.ai spike on the VPS used the existing key in memory without copying it into staging. The
coding model endpoint returned 11 models, rejected a deliberately invalid key with HTTP 401, and
did not return limit headers. `GET https://api.z.ai/api/monitor/usage/quota/limit` returned two
`CREDIT_LIMIT` windows: `(unit=3, number=5)` for five hours and `(unit=6, number=1)` for a week,
with `percentage` and `nextResetTime`. Connect/verify reads these as best-effort observations.
Unknown window types or unavailable quota never imply zero usage. This console endpoint can
change independently of the published inference API.

## Verification

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
before publishing the staging tag. Build frontend/images in GitHub Actions, not on this VPS.

The real installed app-server has also been checked with an empty isolated home: initialization,
empty `account/read`, `chatgptDeviceCode` issuance to `auth.openai.com`, and login cancellation all
succeeded. No real ChatGPT account was connected by that probe. Browser acceptance still requires
two users to complete OpenAI's login themselves and send chats through their connected models.

Protocol reference: [official app-server documentation](https://learn.chatgpt.com/docs/app-server).
Provider references: [OpenRouter key verification](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
[Z.ai API](https://docs.z.ai/api-reference/introduction).
