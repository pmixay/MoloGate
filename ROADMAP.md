# Mologate Roadmap

Planned features for Mologate, the Molotok team's fork of Open WebUI. Items are grouped by area and
are not in priority order. Tick an item off when it ships.

## Limits

- [ ] **Accounts overview:** a list of all connected accounts showing, for each one, the remaining limits,
      the subscription type, and whether it is an API key or a full account. Accounts are shared by the
      whole team.
- [ ] **Per-account limits:** show usage limits for every connected account or key where the provider
      makes them available.
- [ ] **Account switching and limit resets:**
  - When an account reaches 0%, switch automatically to another shared account that still has quota.
  - Only when every account is empty, redeem one of the provider's limit-reset coupons (Codex supports
    these).
  - Any team member can turn coupon auto-reset off globally.
- [ ] **Per-model limits:**
  - Show each model's own limits when it is selected.
  - In the message box, next to the model switcher, show a ring indicator with that model's 5-hour and
    weekly limits.
  - Started: the context indicator's panel shows the selected model's limits when its provider reports
    them in response headers (Codex's 5-hour and weekly windows, or OpenAI-style request and token
    limits). They are kept per connection, so every model on a shared account shows the same ones.
- [ ] **Low-limit warning:** warn when remaining limits run low, with an estimate of how many minutes of
      work are left based on recent usage.
- [ ] **Fair use:** wide per-person limits on the shared accounts, so one person can't use up the
      quota for everyone. Proposed defaults under [Proposed limits](#proposed-limits).
- [ ] **Per-user concurrency limit:** cap how many chats one user can have generating at the same
      time. Requests over the cap wait in a visible queue. Subagents, workflow agents, background tasks
      and scheduled runs don't count toward it; each is held to its own limits instead. Proposed
      defaults under [Proposed limits](#proposed-limits).

## Tools, containers and capabilities

### Web search

- [x] Web search is always on, everywhere, with no toggle. The model decides when to use it.
  - Done: once an admin sets up a search engine, it is on in every chat as a tool the model calls
    when needed. It is not forced on for models set to legacy tool calling, where it would run on
    every message.

### Container nodes

- [ ] Each chat gets its own container.
- [ ] The container node can be installed on the same machine as the site or on separate machines.
- [ ] Support several container nodes with load balancing between them.
- [ ] Option to cut a container off from the network. The default is full network access.
- [ ] Basic network rules and settings inside containers so they can't overload or harm the system.
- [ ] Resource and network limits per container, including bandwidth limits if they can be implemented
      reliably.
- [ ] **Container lifecycle:**
  - Pause a container automatically after a period of inactivity.
  - Delete it automatically after 7 days of inactivity.
  - "Warm up" the container as soon as the user starts typing.
  - Keep everything resource-efficient, even though Mologate only serves a few people.
- [ ] **Base image:** build a container image that ships with Python and the most-needed tools.

### Files

- [ ] Accept files of any type up to 500 MB.
  - Copy attachments into the sandbox automatically.
  - Tell the agent which files were attached, if that isn't already done.
- [ ] Browse the files in the current container and preview most of them online, with syntax
      highlighting.
- [ ] **Change summary:**
  - After each turn, show the list of files the agent edited in that turn.
  - Show the number of lines added and removed (+ / −) for the whole session.

### Agents and tasks

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
    verify → merge results". It shows up in the task list like other background tasks. It is a
    separate feature from team [Workspaces](#workspaces): workflows coordinate agents, workspaces
    coordinate people.
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
- [ ] **Task list:** the agent's checklist shows the task in progress and how many are left.
  - **Open WebUI today:** the `create_tasks` and `update_task` tools keep a checklist for the chat. A
    panel above the message box lists it with "X out of N tasks completed", but only while a reply is
    generating.
  - Show the task in progress in the panel's header, with the number of tasks left.
  - Keep the panel, collapsed to that line, while tasks remain after the reply ends.
- [ ] **Background tasks:**
  - A task moves to the background automatically after a set time.
  - Users can see the list of running tasks and stop any of them.
- [ ] **Monitor tasks:** wait for a specific condition to become true, within limits (see
      [Proposed limits](#proposed-limits)).
- [ ] **Scheduled tasks:** delayed or recurring tasks, with strict limits (see
      [Proposed limits](#proposed-limits)).
- [ ] **Ask tool with a timer:**
  - When the agent asks the user a question and gets no answer for more than a minute, the recommended
    option is chosen automatically.
  - A crossed-out-timer button turns the timer off, for when the user is still thinking.

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
      [agent-swarm workflows](#agents-and-tasks).
- [ ] **Shared files in chats:**
  - When a chat is created in a workspace, the workspace's shared files appear in that chat's container.
  - On creation, the chat's agent is given the current picture of the workspace.
  - Changes to the shared files sync back, so every chat in the workspace sees them.
- [ ] **Agent sync:** agents in a workspace's chats can send messages to each other and receive updates
      about what the others are doing, to stay in sync.
- [ ] **Authorship:** each chat shows who created it, and each message shows who sent it.
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
- [ ] **codex-lb integration for ChatGPT accounts:**
  - Cut codex-lb down to the minimum it needs: accounts and their limits.
  - Show everything inside Mologate instead of the codex-lb interface.
- [ ] **Providers needed now:**
  - Codex, via codex-lb as described above;
  - OpenCode, the free-model setup described above;
  - Z.ai coding plan (GLM);
  - OpenRouter.
  - When connecting each provider, check with a real key that the chosen effort level reaches it in
    the format it expects. Only a test server has been checked so far; OpenRouter, for example,
    documents a `reasoning: {effort}` object.

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

Deliberately wide, so they only stop real overuse.

| Limit                                                 | Default | Range   |
| ----------------------------------------------------- | ------- | ------- |
| Share of the team's combined 5-hour quota, per person | 70%     | 30–100% |
| Share of the team's combined weekly quota, per person | 50%     | 20–100% |

- **Warning:** a person sees a warning at 80% of their share.
- **At the limit:** their new requests wait until the window moves on.
- **Solo use:** the limit doesn't apply while nobody else on the team has used Mologate in the last
  24 hours, so a person working alone is never blocked.
- **Overrides:** any team member can lift the limit for someone temporarily.
