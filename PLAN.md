# Mologate — Plan

**Historical project proposal.** Preserved on the single `limits-and-accounts` working branch.
For current implementation decisions, phase numbers and acceptance criteria, follow
[the Limits and Accounts Roadmap](docs/limits-and-accounts.md) and [ROADMAP.md](ROADMAP.md).
Where they differ, those current roadmaps supersede this original proposal.

A self-hosted AI workspace for the Molotok team: one login-gated site on a VPS where
everyone shares a pool of subscription accounts (Claude, ChatGPT/Codex, GLM, OpenCode, …)
and gets chat + Claude Code–style agents across all of them.

Think of it as **Open WebUI (UI) + a codex-lb–style account pool (gateway) + sandboxed agent runners**.

---

## 1. Architecture

```
Internet ──► Caddy (TLS, gate) ──► Mologate UI (Open WebUI fork)
                                        │  OpenAI-compatible API
                                        ▼
                                 molo-gateway (account pool)
                                  ├─ Claude subs (OAuth)
                                  ├─ ChatGPT / Codex subs (OAuth)
                                  ├─ GLM coding plan
                                  ├─ OpenCode
                                  └─ local models (Ollama: qwen3, gpt-oss, devstral)
                                        │
                                 molo-runner(s)  ◄── WireGuard/Tailscale ──► laptop + Android phone
                                  (agent sessions in containers)
```

| Component | What it is | Built from |
|---|---|---|
| **Gate** | Caddy reverse proxy, HTTPS. Nothing is reachable without login. Signups disabled; admin creates users. | Caddy + Open WebUI auth (optionally Authelia forward-auth in front) |
| **UI** | The screen from the mockup: chats, channels, notes, folders, model picker, effort picker, usage panel. | Fork/theme of Open WebUI |
| **Gateway** | Holds everyone's accounts, refreshes tokens, tracks limits, routes/fails over. Exposes one OpenAI-compatible endpoint to the UI. | New service, modeled on codex-lb (Python/FastAPI + Postgres) |
| **Runners** | Run agent sessions (Claude Code / Codex CLI / OpenCode) in isolated containers with a git workspace, terminal, file tree. | Docker on VPS; same agent on laptop |
| **Storage** | Postgres (users, accounts, usage, sessions), Redis (rate windows, queues), encrypted secrets. | — |

## 2. Core features

**Accounts ("Connect account")**
- User adds a subscription via OAuth / device-code login; tokens stored encrypted (age/KMS key on VPS only).
- Each account card shows: owner, provider, plan, models, live usage (5h / weekly windows), status.
- Owner controls sharing: private / team / specific people, and can pause it any time.

**Models & routing**
- Model list = union of all connected accounts, e.g. `claude-opus`, `gpt-5-codex`, `glm-4.6`.
- Pick a specific account or "auto" (least-used healthy account).
- On limit hit: UI shows which account ran out + when it resets, one click to switch account/model.
  Conversation history stays (Open WebUI keeps it; gateway just changes backend).
- Effort / reasoning level selector per model.

**Agent sessions (Claude Code–like)**
- "New session" → pick repo + runner + model → agent works in a container: edits files, runs
  commands, shows diffs, opens PRs.
- Run the real CLIs (Claude Code, Codex, OpenCode) headless inside the runner and stream their
  events to the UI — reuses their tools, MCP, skills instead of reimplementing them.
- Switch model mid-session; history + workspace persist.
- Web terminal + file browser + diff view; sessions survive browser close.

**Runners**
- `molo-runner` agent: small daemon that registers with the gateway over WireGuard/Tailscale.
- VPS runner: generic Linux containers.
- Laptop runner: Android SDK + `adb` with the connected phone (or emulator) → agents can build,
  install and screenshot Android apps.

**Team stuff (mostly free from Open WebUI)**
- Channels, shared chats, notes, folders, prompts, RAG over docs, web search, code interpreter.
- Per-user usage stats; admin dashboard of the whole pool.

## 3. Phases

| # | Goal | Done when |
|---|---|---|
| 0 | **Infra** — VPS, domain, Caddy, Docker Compose, Postgres, backups, Tailscale | Login page on HTTPS, nothing else exposed |
| 1 | **Chat MVP** — Open WebUI themed as Mologate + local Ollama models + one API key | Team chats with local models |
| 2 | **Gateway v1** — account pool for Codex/ChatGPT (port codex-lb ideas), usage tracking, failover | Two people's ChatGPT accounts usable by everyone, usage visible |
| 3 | **More providers** — Claude, GLM, OpenCode adapters; account sharing settings; limit-switch UX | All mockup models selectable, switch on limit works |
| 4 | **Agent sessions** — VPS runner, Claude Code/Codex headless, workspace, diff/terminal UI | Agent fixes a bug in a repo and opens a PR from the browser |
| 5 | **Laptop + Android runner** | Agent builds and installs an APK on the phone |
| 6 | **Polish** — mobile layout, notifications, audit log, quotas per user | — |

## 4. Risks — decide early

1. **Provider terms.** Consumer plans (Claude Pro/Max, ChatGPT Plus/Pro) are licensed to one person;
   pooling them across a team and using their OAuth tokens from a third-party server can break the
   terms and get accounts banned. Mitigate: each account used mainly by its owner, keep sharing
   opt-in, prefer plans that allow it (Team/Business seats, GLM coding plan, API keys) where it
   matters, and design the gateway so any provider can be swapped to an API key.
2. **Security.** The box holds everyone's tokens and runs arbitrary agent code. Encrypt secrets,
   no shell on the gateway host, runners in rootless/gVisor containers with no access to secrets,
   2FA on login, rate-limit the gate.
3. **Upstream drift.** OAuth flows and CLIs change often — keep each provider in its own adapter
   with tests, pin CLI versions.
4. **Open WebUI fork cost.** Prefer pipes/functions/plugins + theming over deep forking so we can
   keep pulling upstream.

## 5. Repo layout (proposed)

```
mologate/
  deploy/        compose.yaml, Caddyfile, .env.example
  ui/            Open WebUI theme + plugins (accounts panel, usage, sessions)
  gateway/       account pool, provider adapters, usage tracker
  runner/        runner daemon + container images (base, android)
  docs/
```

## 6. Open questions for the team

- Who owns what part (UI / gateway / runners) so parallel work doesn't collide?
- Fork Open WebUI or plugin-only?
- Gateway language: Python (matches Open WebUI, codex-lb) vs Go/Rust?
- How strict is account sharing — everyone uses everything, or owner approves?
