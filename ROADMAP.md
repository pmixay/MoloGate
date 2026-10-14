# Mologate Roadmap

Planned features for Mologate, the Molotok team's fork of Open WebUI. Items are grouped by area and
are not in priority order. Tick an item off when it ships.

## Limits

The full design and phased plan for accounts, limits, credits and personal API keys is in
[docs/limits-and-accounts.md](docs/limits-and-accounts.md). It replaces the fair-use proposal below.

- [x] **Accounts overview:** a list of all connected accounts showing, for each one, the remaining limits,
      the subscription type, and whether it is an API key or a full account. Accounts are shared by the
      whole team.
  - Done: Settings → Provider accounts lists the member's own accounts and every shared one
    (admins see all) as compact rows, collapsed by default. A row shows the generated name
    (`Account #1n9Dk5`), the provider, which tells an API key from a ChatGPT account, the plan,
    owner, the 5-hour and weekly bars (an API key's credit limit and budget instead) and a ChatGPT
    account's reset coupons. Expanded, it shows every limit window the provider reports with
    percent used and reset time, how much of an API key's monthly budget is left, each member's
    requests, tokens and cost on the account (this week for a subscription, this month for a key),
    who can use it, and the budget. An OpenRouter key's spend comes from OpenRouter; OpenAI,
    Anthropic and Z.ai keys can't read their own spending, so theirs is estimated from the usage
    ledger's token counts and published prices (models.dev). A shared account's 5-hour and weekly
    windows are split into the owner's half and the team's half, each with its own bar, estimated
    from who sent the requests between readings (not enforced yet). Admins can add team accounts
    that belong to no member, and admin connections to a supported provider become such accounts.
- [x] **Per-account limits:** show usage limits for every connected account or key where the provider
      makes them available.
  - Done: each provider account keeps the limits its provider reports and shows them on its card
    in Settings → Provider accounts: Codex's 5-hour and weekly windows, Z.ai's coding-plan quota,
    an OpenRouter key's credit limit and this month's spend (from its `/key` endpoint, read on
    connect, on Verify, when the page lists it and after use, at most once a minute), and the
    request and token limits in OpenAI, OpenRouter and Anthropic response headers, which appear
    once the account has served a request.
- [x] **Account switching and limit resets:**
  - When an account reaches 0%, switch automatically to another shared account that still has quota.
  - Only when every account is empty, redeem one of the provider's limit-reset coupons (Codex supports
    these).
  - Any team member can turn coupon auto-reset off globally.
  - Done: a request that finds an account empty, or gets a 429 from it, moves on to the next account
    that can serve the model: the member's own first, then shared ones. When every one is empty,
    Mologate redeems the Codex reset coupon that expires soonest and retries, at most once every
    10 minutes. Any member turns this off for everyone with the Auto-reset switch in Settings →
    Provider accounts; there is no per-account switch. Each ChatGPT account shows its coupon count,
    with the last reset in the tooltip. ChatGPT limits and coupons come from codex-lb.
    Details in [docs/limits-and-accounts.md](docs/limits-and-accounts.md#10-automatic-limit-reset).
- [x] **Per-model limits:**
  - Show each model's own limits when it is selected.
  - In the message box, next to the model switcher, show a ring indicator with that model's 5-hour and
    weekly limits.
  - Done: a limits ring sits just left of the model selector: the outer ring is the 5-hour window
    (or an OpenRouter key's credit limit), the inner one the weekly window. They add up every
    account that can serve the model, since a request moves on to the next one when an account
    runs out. Opening it shows every limit of the account the member's next request will use (their
    own accounts first, accounts known to be empty last), its name, and how many accounts serve
    the model. The context indicator's panel shows the same limits. Limits are kept per account
    or connection, so every model on one account shows the same ones. The ring is hidden for
    models whose provider reports no 5-hour, weekly or credit limit.
- [x] **Low-limit warning:** warn when remaining limits run low, with an estimate of how many minutes of
      work are left based on recent usage.
  - Done: when the selected model's 5-hour, weekly or credit limit runs low, a warning above the
    message box says so, on every screen size: "5-hour limit: 17% left, about 38m of work at the
    recent pace. It resets in 1h 58m." It counts every account that can serve the model, since a
    request moves on to the next one when an account runs out. A window warns at 20% left, or when
    it would run out within an hour before it resets (in red at 10% or 15 minutes, or when used up),
    and the limits ring turns the same color with "~38m left" beside it. The pace comes from the
    last 30 minutes of readings of each account, counting only the latest run of use, so an idle
    hour before a busy session doesn't make the estimate too generous. A dismissed warning stays
    hidden until it gets worse or the window resets. The warning on a member's own share of the
    pool comes with the allowance engine (Fair use).
- [ ] **Fair use:** each shared account is split 50/50 between its owner and a public pool; members
      past their share keep working on cheap worker models. See
      [docs/limits-and-accounts.md](docs/limits-and-accounts.md).
  - Started: the usage ledger records every request a provider account serves or refuses (member,
    account, model, source, tokens, cost), and the accounts page shows each shared window split
    between owner and team. Metering (phase 2) polls every account's 5-hour and weekly windows,
    sets each rise against the tokens Mologate sent, records the owner's use outside Mologate,
    and fits each provider's units per token and each account's capacity weight daily; members
    see their own use under "Your use", admins the calibration report. The allowance engine
    (phase 3) decides every request's tier, account and model, and records it beside the request
    in observe mode; admins see the decisions and a replay of the last days under the rules.
    With `MOLOGATE_LIMITS_MODE=enforce` (phase 4) requests are served as decided: the member's
    own account first, failing over within their tier; past their share the reply uses the
    provider's worker model and says so; when the pool has no room, the error says when it frees
    up and offers a worker on another provider. Each account serves at most 4 requests at once.
    The context indicator's panel shows the member's tier, share and credits, and the warning
    above the message box starts at 80% of the share. Admins grant overrides with a reason,
    kept as an audit log. Credits (phase 5): adding a subscription earns 10 per 1x of its plan,
    once per account, and an API key earns its owner 0.5 per dollar spent through it; past the
    share, credits keep big models available, and disconnecting forfeits what is left. Personal
    API keys (phase 6): members make several named keys in Settings → API keys, each optionally
    held to fewer requests, models, efforts or pool points, and use Mologate from any
    OpenAI-compatible client; that use counts in their share. Missing: a week of real use to
    check the fit and replay the rules before switching the site to `enforce`.
- [x] **Per-user concurrency limit:** cap how many chats one user can have generating at the same
      time. Requests over the cap wait in a visible queue. Subagents, workflow agents, background tasks
      and scheduled runs don't count toward it; each is held to its own limits instead. Proposed
      defaults under [Proposed limits](#proposed-limits).
  - Done: a chat the member sends takes a slot while it generates; past the cap, the next one waits
    in line, in order, and its reply shows "Waiting: 3 of your chats are already generating. This
    one is next." until a slot frees up. Stopping it leaves the line. Sub-agents, background
    results, timers, automations and channel replies take no slot, and several models answering
    in one chat share one. The cap is 3 by default (`MOLOGATE_MAX_CONCURRENT_CHATS`); admins
    change it under Settings → General, up to 10. Slots live in Redis, shared by every worker, and
    free themselves within a minute if a worker stops. Like every limit, it applies when
    `MOLOGATE_LIMITS_MODE` is `enforce`; in `observe`, the default for now, a chat that would
    have waited is only logged. Sub-agents keep Open WebUI's own caps from Settings → Sub-agents:
    20 running at once and 20 in the background by default, counted across the whole server, not
    per chat or per member as [Proposed limits](#proposed-limits) suggests.

## Tools, containers and capabilities

### Web search

- [x] Web search is always on, everywhere, with no toggle. The model decides when to use it.
  - Done: once an admin sets up a search engine, it is on in every chat as a tool the model calls
    when needed.
  - Done: search is on by default through Exa in auto mode, the most accurate search API in an
    independent 2026 benchmark. Set `EXA_API_KEY` on the deployment; until then the model isn't
    offered the search tool, only `fetch_url` for pages it is given. Each result brings up to 3,000
    characters of the page's text (`EXA_MAX_CONTENT_LENGTH`). Existing installs are switched on once,
    and an engine an admin already picked is kept.
  - Done: pages are read through Exa too (its Contents API), for `fetch_url` and links added to a
    chat. It returns clean text and gets through JavaScript-rendered and bot-protected pages that a
    direct request can't read. It uses the same `EXA_API_KEY`; without it, pages are read directly.

### Container nodes

The design, setup and settings are in [docs/container-nodes.md](docs/container-nodes.md).

- [x] Each chat gets its own container.
  - Done: the first message places the chat's container on a container node; the agent's terminal
    tools and the file browser work in it through the built-in "Chat container" connection, which
    new chats select by default. Sub-agents work in their main chat's container, and only the
    chat's owner can reach it. Files attached to the chat are copied to `/home/user/uploads/`, and
    the agent's system prompt lists them (marking the ones attached to the current message) along
    with the container's limits and network state. The container runs Open Terminal (MIT), the API
    Open WebUI already uses for terminals; Open WebUI's own orchestrator needs an enterprise
    license, so Mologate runs a small node service of its own.
- [x] The container node can be installed on the same machine as the site or on separate machines.
  - Done: `deploy/vps/compose.yaml` runs one beside the site (`COMPOSE_PROFILES=containers`, over a
    private network), and `deploy/container-node/` runs one on any other machine behind HTTPS,
    open only to the site's addresses. Admins add, switch off or remove nodes in Admin Settings →
    Integrations → Chat containers, which shows each node's health and load, or list them in
    `MOLOGATE_CONTAINER_NODES`.
- [x] Support several container nodes with load balancing between them.
  - Done: each node reports its running and paused containers, free memory and CPU load; a new
    chat goes to the least busy node, preferring one with a warm spare. A stopped container moves
    before it starts when its node is clearly busier than another (score at least 0.6 and 0.25
    above the quietest) or takes no new containers: its `/home/user` streams to the other node,
    files and user-installed packages included, in about 0.1 s for a small workspace. A node full of
    busy containers hands the chat on, and when a chat's node is unreachable the chat starts over
    in a fresh container elsewhere and the agent is told.
- [x] Option to cut a container off from the network. The default is full network access.
  - Done: the "⋯" (Extra) button next to "+" shows the container's state, limits and node, and a
    Network access switch, on by default. Switching it works mid-chat and applies to the running
    container at once; in a new chat the choice goes with the first message. An amber dot on the
    button shows that the network is off, and the agent is told so it can work offline.
- [x] Basic network rules and settings inside containers so they can't overload or harm the system.
  - Done: containers sit on their own bridge where they can't reach each other, and firewall rules
    keep them off the host, private and link-local ranges (the database, codex-lb, cloud metadata)
    and outgoing mail on port 25, with at most 256 open TCP connections and 50 new connections a
    second each. They run with almost all capabilities dropped, `no-new-privileges`, a 512-process
    limit, rotated logs and a low OOM priority, so the kernel kills sandbox processes before the
    site's. A node short of disk takes no new containers and stops any container over its disk
    limit. gVisor can be switched on per node.
- [x] Resource and network limits per container, including bandwidth limits if they can be implemented
      reliably.
  - Done: 1 CPU at half the CPU weight, 1 GiB of memory without swap, 10 GiB of disk (a Docker quota
    where the storage driver supports it, otherwise measured), a 256 MiB `/tmp` and 100 MiB/s up and
    down, all set per node. Bandwidth is shaped with `tc` from outside the container's privileges;
    kernels without the police action get download shaped on the host side instead. A 2 MiB/s test
    cap measured 1.8 MB/s down and 1.9 MB/s up.
- [x] **Container lifecycle:**
  - Pause a container automatically after a period of inactivity.
  - Delete it automatically after 7 days of inactivity.
  - "Warm up" the container as soon as the user starts typing.
  - Keep everything resource-efficient, even though Mologate only serves a few people.
  - Done: a container pauses after 2 minutes without a request (resumes in about 30 ms), stops
    after 15 minutes to free its memory (resumes in about a second) and is deleted with its files
    after 7 days, or within 30 minutes of its chat being deleted. Typing wakes the chat's
    container, or starts a spare for a new chat that the first message then takes over (a spare
    nobody claims goes after 5 minutes). A node keeps a set number of containers awake and stops
    the least recently used idle one to make room. The node service uses about 50 MB, and an idle
    container about 45 MB.
- [x] **Base image:** build a container image that ships with Python and the most-needed tools.
  - Done: `containers/sandbox` (`ghcr.io/pmixay/mologate-sandbox`): Debian with Python 3.12, bash,
    Node.js 24, git, build tools, ripgrep, fd, jq, curl, sqlite3 and archive tools, plus numpy,
    pandas, matplotlib, requests, Pillow and openpyxl. GitHub Actions builds it and the node image
    for amd64 and arm64.

### Files

- [x] Accept files of any type up to 500 MB.
  - Copy attachments into the sandbox automatically.
  - Tell the agent which files were attached, if that isn't already done.
  - Started: an uploaded file reaches the model as its full extracted text, without embeddings or
    a vector database. Existing installs are switched over once.
  - Done: attachments are copied into the chat's container (`/home/user/uploads/`), and the agent
    is told their paths. Files over 8 MiB upload in chunks, three at a time, retried on failure and
    gzip-compressed in the browser when they compress well, with a progress ring; chunks are
    written into place on disk and the finished file is moved into storage without a copy (420 MB
    in about 7 seconds locally). Files over 20 MB skip text extraction and the agent reads them in
    the container; prompt text is capped at 400,000 characters. Every upload is checked for
    decompression bombs first: ZIP entries are inflated and checked against their declared sizes,
    overlapping entries and nested archives are caught, and gzip, bzip2, xz and tar are expanded
    in a stream under size, ratio and entry limits (docs/container-nodes.md).
- [x] Browse the files in the current container and preview most of them online, with syntax
      highlighting.
  - Started: Open WebUI's file browser opens the chat's own container, since the container runs
    Open Terminal. Not yet checked across file types.
  - Done: a **Files** button in the chat header opens the chat container's file browser. Checked
    with code (syntax highlighting), Markdown, CSV, JSON, notebooks, images, SVG, PDF, Excel,
    SQLite and HTML; other files offer a download. Big media, archives and Office files stream
    through the proxy, and PDFs work in browsers without the newest JavaScript APIs.
- [x] **Change summary:**
  - After each turn, show the list of files the agent edited in that turn.
  - Show the number of lines added and removed (+ / −) for the whole session.
  - Done: once a response has completely finished, it lists the files added, edited or deleted in
    the chat's container, with file-type icons and lines added and removed; clicking one shows its
    diff, and a folder button opens it in the file browser. The bar above the message box with the
    whole chat's changes shows only in chats linked to a GitHub repository (below).

### Agents

- [x] **Subagents:**
  - Users can open and read each subagent's conversation.
  - Preconfigured subagent profiles, such as "general purpose".
  - Done: sub-agents, background ones included, are on by default, and existing installs are switched
    on once. Each `delegate_task` call, background result and agent message links to the sub-agent's
    conversation. The conversation opens read-only, even while the sub-agent is still working, with
    its profile and a link back to the main conversation. Anyone who can read the main chat can read
    its sub-agents. `delegate_task` takes a profile: `general-purpose` (the default), `explore`,
    `plan` or `reviewer`. Each adds role instructions to the sub-agent. Admins add their own profiles
    under Settings → Sub-agents, optionally on another model.
- [ ] **Team-wide and system-wide skills:**
  - **Open WebUI today:**
    - Workspace → Skills holds skills made of a name, a description and one markdown text.
    - They can be imported from `.md` or `.json` files and shared per skill with users or groups.
    - In a chat, a user can pull a skill in with the `$` picker.
    - With native tool calling, the model sees a list of every skill it may use (name and description)
      and loads the full text with a `view_skill` tool when it decides it needs it.
  - **Upload in the interface at two levels:**
    - _Team-wide:_ any team member uploads a skill and everyone can use it.
    - _System-wide:_ admins install skills that every agent always has, including subagents and
      workflow agents.
  - **Full skill packages:** upload a skill folder or `.zip` (a `SKILL.md` plus scripts and other
    files), not only a single markdown text. When a skill is activated, its files are copied into the
    chat's container so the agent can run the bundled scripts.
  - **Agents decide:** agents see all available skills and activate the right one when needed, as
    `view_skill` does today, extended to subagents and workflows.
- [ ] **Agent-swarm workflows.** Proposed design, to refine:
  - **Definition:** a workflow is an agent tool that starts a task in which many agents work together,
    as in Claude Code. It runs a reusable plan in phases, for example "plan → work in parallel →
    verify → merge results". It shows up in the [tasks panel](#tasks) like other background tasks.
    It is a separate feature from team [Workspaces](#workspaces): workflows coordinate agents,
    workspaces coordinate people.
  - **Agent steps:** each step names an agent profile, its instructions, its inputs, and the shape of
    the result it must return. The orchestrator can pass one step's results into the next.
  - **Patterns:**
    - fan-out: the same task split across N agents, such as reviewing a diff from several angles;
    - pipeline: each result is handed to the next agent as soon as it is ready;
    - verification: a separate agent checks every finding before it is reported;
    - majority vote: several agents answer independently and the answers are compared.
  - **Live view:** each running workflow shows its phases, every agent's status and progress, and a
    link to each agent's conversation.
  - **Limits:** each workflow has a maximum number of agents, a token or limit budget, and a wall-clock
    timeout. The user confirms before a large workflow starts. Proposed numbers are under
    [Proposed limits](#proposed-limits).
  - **Control and reuse:** workflows can be cancelled, and resumed after a failure without re-running
    finished steps. Saved workflows can be shared with the team, e.g. "code review", "research a
    topic", "implement + test + review".
- [x] **Agent-to-agent messaging:** agents in the same chat can message each other, including the main
      agent.
  - Done: the main agent and its sub-agents get `send_message` (to `main` or a sub-agent's id) and
    `list_agents`. A running agent reads a message after its current tool call. An idle main agent
    starts a new turn for it. The main agent can restart a finished sub-agent with a message, up to
    10 times, and the answer comes back as a reply. Sub-agents can message running sub-agents, but
    not restart finished ones. Messages show in both conversations as "Message from…" rows.
- [ ] **Ask tool with a timer:**
  - When the agent asks the user a question and gets no answer for more than a minute, the recommended
    option is chosen automatically.
  - A crossed-out-timer button turns the timer off, for when the user is still thinking.
  - **Open WebUI today:** `ask_user` asks one to three questions with two or three options each,
    and the first option is labelled Recommended. When its timeout (one to four minutes, two by
    default) runs out, the question is cancelled instead of answered.

### Tasks

- [ ] **Tasks panel:** one place to see the chat's tasks, running or finished: background sub-agents,
      workflows, monitor and scheduled tasks.
  - Opening a task shows its output where it has one, updating live while the task runs where
    possible.
  - Each task shows how long it took or, while it runs, how long it has been running so far.
  - Running tasks can be stopped.
  - A "Clear" button removes all completed tasks from the list.
- [ ] **Background tasks:** a task moves to the background automatically after a set time.
- [ ] **Monitor tasks:** wait for a specific condition to become true, within limits (see
      [Proposed limits](#proposed-limits)).
- [ ] **Scheduled tasks:** delayed or recurring tasks, with strict limits (see
      [Proposed limits](#proposed-limits)).
  - **Open WebUI today:** an agent can set a one-shot `timer` that sends a prompt back into the
    chat. With the automations permission (off for members by default), `create_automation` runs a
    prompt once or on a recurring schedule; its limits (`AUTOMATION_MAX_COUNT`,
    `AUTOMATION_MIN_INTERVAL`) are unset by default. None of the proposed limits apply yet.
- [ ] **Task checklist:** the agent's checklist shows the task in progress and how many are left.
  - **Open WebUI today:** the `create_tasks` and `update_task` tools keep a checklist for the chat. A
    panel above the message box lists it with "X out of N tasks completed", but only while a reply is
    generating.
  - Show the task in progress in the panel's header, with the number of tasks left.
  - Keep the panel, collapsed to that line, while tasks remain after the reply ends.

### Browser

- [ ] **Micro browser for agents:**
  - Agents get a small built-in browser they can control: open pages, take screenshots and perform
    actions such as clicking and typing.
  - The user can watch the same browser live in a preview panel on the right side of the screen, for
    example to see the product the agent is building.

### Image generation

- [ ] **Image generation tool:**
  - Codex has its own image-generation tool. Inspect how it works.
  - Offer the same tool to agents in Mologate, running through the shared Codex (ChatGPT) accounts.

### GitHub

- [ ] **GitHub integration:**
  - Agent tools for working with repositories.
  - Each user authorizes by linking their own GitHub token, and can unlink or replace it.
  - A "Create PR" button.
- [ ] **Link a repository to a chat:** works in any chat, inside a workspace or not.
  - By default a chat's container has no link to GitHub.
  - When starting a chat, the user can link a repository and branch to it. Mologate then clones it into
    the chat's container, sets up git, and tells the agent to work in that repository.
  - A repository can only be linked when the chat is created, not added to an existing chat. In a chat
    started without one, every GitHub function is off.
  - Started: the change summary is ready for it. A chat's link goes in `meta.repository`, and a
    `git` change source (`utils/change_sources.py`) can report what each response changed and the
    diff against the base branch; the bar above the message box already shows only in linked chats
    (docs/container-nodes.md).

## Security

- [x] **Permission prompts:**
  - Auto-approve is the default and only mode, with low sensitivity.
  - The user is asked for permission only when an action is critical or very dangerous.
  - Every such prompt includes "Always allow in this chat". Once chosen, even these prompts stop
    appearing in that chat.
  - Done: before a batch of tool calls runs, `codex-auto-approve` reviews it (GLM-5.3 Flash if that
    fails or is missing; admins can pick both models under Settings → Interface). Only flagged calls
    stop, with the reviewer's reason and Allow / Always allow in this chat / Deny; the rest run without
    asking. Read-only built-in tools skip the review. With no reviewer available, built-in rules still
    catch the most destructive commands (wiping disks, force-pushing, dropping databases, tearing down
    infrastructure). "Always allow" is saved on the chat and can be turned off from the "+" menu.
    Automations, channels and sub-agent runs aren't reviewed, since nobody is there to answer.

## Deployment

- [ ] **Backups:** nightly database and data backups kept off the VPS, with a tested restore. They
      were removed while Mologate is in development.
- [ ] **Deploy node:**
  - Container nodes for long-running deployments.
  - The agent picks a preset Small, Medium or Large container for a project.
  - A dedicated agent tool puts a site online.
- [ ] Cloudflare API support, also exposed as an agent tool, to create subdomains for deployed sites.

## Status page

- [ ] **Status page** showing:
  - **Service health:** the health of every service, such as the site, the container nodes, the
    deploy nodes and each provider and account.
  - **Global limits:** an indicator of how much of the team's combined limits is left.
  - **Latency:** system-wide average latencies.

## Workspaces

- [ ] **Team workspaces:** Mologate is first of all the team's own tool, so every workspace is visible
      to all users and anyone can work in it.
- [ ] **Projects:** a workspace is a project that holds chats and shared files. Workspaces are for
      team work between people; they are a separate feature from
      [agent-swarm workflows](#agents).
- [ ] **Shared files in chats:**
  - When a chat is created in a workspace, the workspace's shared files appear in that chat's container.
  - On creation, the chat's agent is given the current picture of the workspace.
  - Changes to the shared files sync back, so every chat in the workspace sees them.
- [ ] **Agent sync:** agents in a workspace's chats can send messages to each other and receive updates
      about what the others are doing, to stay in sync.
- [ ] **Authorship:** each chat shows who created it, and each message shows who sent it.
  - Started: the sidebar's Team list shows who created each chat opened to the team.
- [x] **Personal chats:** chats outside a workspace are private to their creator by default. They can
      be opened to the team with the share feature (see [Interface](#interface)).

## General

### Interface

- [ ] Responsive layout for phones.
- [x] **Effort dropdown:**
  - Sits to the right of the model selector, styled the same way.
  - Gets each model's supported effort levels automatically, so unsupported levels never show.
  - Done: levels come from an admin override on the model, else from what the provider reports
    (Codex-style `supported_reasoning_levels`, a plain level list, or OpenRouter-style
    `supported_parameters`); the dropdown hides when a model has none. The choice is saved per chat.
- [x] Don't allow image attachments when the selected model can't read images.
  - Done: a model can't read images when an admin turned its Vision capability off or its provider
    lists input modalities without images (Codex `input_modalities`, OpenRouter `architecture`). While
    no selected model reads images, uploading, pasting or dropping an image is refused, and Capture
    and image files in the "+" menu are greyed out. Images attached before switching to such a model
    stay visible but stop the send until they are removed or a model that reads images is picked. The
    server also swaps any image in earlier messages or tool results for a short note, so such a model
    never receives one.
- [x] **Context indicator:**
  - A ring indicator to the right of the effort dropdown.
  - Opening it shows a breakdown of what is filling the context window, with percents, plus the
    provider limits described under [Limits](#limits).
  - Done: each turn records an estimate of its prompt by category (system instructions, tool
    definitions, summary of earlier messages, messages, tool calls and results, images), calibrated
    against the prompt tokens the provider reports. The panel shows each category and the free space
    as a share of the window. The window size comes from an admin override in the model editor, else
    from provider metadata (`context_window`, `context_length`, ...), else the compaction threshold.
    Provider limits: see Per-model limits above.
- [x] Share a chat with the team, by opening it to the team or with a link. Both need a Mologate
      account.
  - Done: "Open to the team" lets everyone signed in find the chat under Team in the sidebar and read
    its current state, read-only. A share link is a snapshot any signed-in user with the URL can open;
    signed-out visitors are sent to sign in, and no-auth ("anyone") access can no longer be granted.
    Deleting the link keeps the chat open to the team.
- [ ] **Turn and tool-use detail:**
  - A turn is the group of tool uses between two of the agent's messages. Each turn can be expanded to
    show every tool use, or collapsed to a single line.
  - Each turn shows how long it took. While it runs, a live timer shows how long it has been running.
  - Each tool use gets an icon for its kind of action: terminal command, read file, read image, web
    search, and a generic icon for anything else.
  - Thinking blocks get a design of their own.
- [ ] **Live status:** a status line under the message says what is happening right now, so a reply
      never looks stuck at the start: "Sending…", "Waiting for provider", "Thinking" (then "Thinking
      more", "Thinking some more", "Thinking deeply" and "Deep in thought" as it goes on), "Running
      tools", and so on.

### Models and providers

- [ ] **Free OpenCode models:**
  - Run OpenCode inside a container and use its free models from there.
  - Test the main usage scenarios.
  - Restricting OpenCode completely doesn't work, so a different approach to locking it down is
    needed.
- [ ] Pull in the latest models automatically.
- [x] **Native tool calling only:** turn legacy (prompt-based) tool calling off everywhere.
  - Done: every model uses its own tool calling, and the Function Calling switch is gone from chat
    and model settings, along with the legacy tool prompt in admin settings. A "legacy" value saved
    on older chats, models or settings is ignored. Checked against OpenRouter's model list: every
    free chat model, GLM 5.3, GPT 6 and 6.1, Qwen 3.8, Kimi K3 and MiMo v2.6 support native tool
    calls.
- [x] **Default system model:** background jobs such as chat titles and follow-up suggestions use the
      latest ChatGPT Luna model (currently Luna 6), and follow it automatically as new versions ship.
      If it's unavailable, they fall back to GLM-5.3 Flash.
  - Done: when no task model is set, each background job picks the newest model whose id or name says
    Luna (highest version; the plain model over mini variants and dated snapshots), so a new version
    is used as soon as it shows up in the model list. Without one it uses GLM-5.3 Flash, then the
    chat's own model. A task model chosen under Settings → Interface still wins, and that page shows
    which model the default resolves to. `DEFAULT_SYSTEM_MODEL_FAMILY` and
    `DEFAULT_SYSTEM_MODEL_FALLBACK` change the two.
- [x] **AI lab logos:** bundle logos for each lab, assign them to models automatically, and group
      models by lab in the model dropdown.
  - Done: 25 lab logos ship with Mologate, drawn from the MIT-licensed lobe-icons set onto Mologate's
    dark tile (`scripts/build-lab-logos.py` rebuilds them). Each model's lab comes from its
    provider prefix (`z-ai/...`) or model family (`claude-...`, `glm-...`); custom models use their
    base model's. Models without a picture of their own show their lab's logo everywhere, and the
    model dropdown groups models under lab headers when not searching.
- [ ] **Personal API keys:**
  - Each user gets a personal OpenAI-compatible API key.
  - With it they can use Mologate's features and every model in the interface from other apps and
    tools.
  - Open WebUI already has per-user API keys (off by default) and an OpenAI-compatible
    `/api/chat/completions` endpoint to build on.
- [x] **codex-lb integration for ChatGPT accounts:**
  - Cut codex-lb down to the minimum it needs: accounts and their limits.
  - Show everything inside Mologate instead of the codex-lb interface.
  - Done: codex-lb runs beside Mologate as a pinned, unmodified image on a private network, with no
    published port and its dashboard unused. Mologate drives only its sign-in, account, limit,
    coupon and model endpoints, and sends ChatGPT requests through its proxy, which presents them
    as the Codex CLI. Each account gets a codex-lb key scoped to that one account, so Mologate
    alone decides which account serves a request. Sign-in, limits and coupons all show in Settings → Provider
    accounts. Not yet deployed or tried with a live ChatGPT account. Details in
    [docs/limits-and-accounts.md](docs/limits-and-accounts.md).
- [ ] **Providers needed now:**
  - Codex, via codex-lb as described above;
  - OpenCode, the free-model setup described above;
  - Z.ai coding plan (GLM);
  - OpenRouter.
  - When connecting each provider, check with a real key that the chosen effort level reaches it in
    the format it expects. Only a test server has been checked so far; OpenRouter, for example,
    documents a `reasoning: {effort}` object.
  - Started: ChatGPT accounts connect through codex-lb (sign-in link or device code), and Z.ai coding
    plans and OpenRouter keys connect in Settings → Provider accounts, as do Anthropic and OpenAI API
    keys. A Z.ai coding-plan account has served real chats. OpenCode is not started.

## Proposed limits

Starting defaults, sized for a team of about five. An admin can change each default up to its hard
cap.

### Monitor tasks

| Limit                    | Default | Hard cap |
| ------------------------ | ------- | -------- |
| Active monitors per user | 5       | 20       |
| Check interval           | 60 s    | min 15 s |
| Time limit for one check | 30 s    | 30 s     |
| Lifetime of a monitor    | 6 h     | 24 h     |
| Checks per monitor       | 500     | 2,000    |

- A check is a cheap command or HTTP probe run in the container. It never calls a model.
- When the condition becomes true, the monitor wakes the agent once, then stops.
- An active monitor keeps its container from pausing, but only until the monitor ends.

### Scheduled tasks

| Limit                              | Default | Hard cap |
| ---------------------------------- | ------- | -------- |
| Active schedules per user          | 10      | 25       |
| Shortest repeat interval           | 1 h     | 15 min   |
| How far ahead a one-off can be set | 30 days | 90 days  |
| Run time per run                   | 30 min  | 2 h      |
| Lifetime of a repeating schedule   | 90 days | 1 year   |

- **No overlap:** a run never starts while the previous run of the same schedule is still going;
  that run is skipped instead.
- **Failures:** after 3 failed runs in a row, the schedule turns itself off and notifies its owner.
- **Low limits:** a run is postponed when the provider account has less than 10% of its limit left.

### Agent-swarm workflows

| Limit                        | Default | Hard cap |
| ---------------------------- | ------- | -------- |
| Agents per workflow          | 8       | 20       |
| Agents running at once       | 4       | 8        |
| Running workflows per user   | 1       | 2        |
| Wall-clock time per workflow | 1 h     | 4 h      |
| Tool calls per agent         | 100     | 300      |

- **Before it starts:** a workflow shows its estimated limit usage. It asks for confirmation when it
  would use more than 8 agents or more than 25% of the remaining 5-hour limit.
- **When it runs out:** a workflow stops when it hits its budget. Finished steps are kept, so it can
  be resumed later.

### Concurrent chats

| Limit                              | Default | Hard cap |
| ---------------------------------- | ------- | -------- |
| Chats generating at once, per user | 3       | 10       |

Subagents, workflow agents, background tasks and scheduled runs don't count toward this limit. They
use their own limits in this section.

### Subagents and background tasks

| Limit                               | Default | Hard cap |
| ----------------------------------- | ------- | -------- |
| Subagents running at once, per chat | 4       | 10       |
| Background tasks running, per user  | 5       | 15       |
| Run time per background task        | 2 h     | 8 h      |

### Fair use of shared accounts

Replaced by the owner reserve, public pool and worker tier in
[docs/limits-and-accounts.md](docs/limits-and-accounts.md).
