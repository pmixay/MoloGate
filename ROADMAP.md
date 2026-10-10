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
- [ ] **Low-limit warning:** warn when remaining limits run low, with an estimate of how many minutes of
      work are left based on recent usage.
- [ ] **Fair use:** wide per-person limits on the shared accounts, so one person can't use up the
      quota for everyone. Proposed defaults under [Proposed limits](#proposed-limits).

## Tools, containers and capabilities

### Web search

- [ ] Web search is always on, everywhere, with no toggle. The model decides when to use it.

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

- [ ] **Subagents:**
  - Users can open and read each subagent's conversation.
  - Preconfigured subagent profiles, such as "general purpose".
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
- [ ] **Agent-to-agent messaging:** agents in the same chat can message each other, including the main
      agent.
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
  - The user can link a repository and branch to the chat. Mologate then clones it into the chat's
    container, sets up git, and tells the agent to work in that repository.

## Security

- [ ] **Permission prompts:**
  - Auto-approve is the default and only mode, with low sensitivity.
  - The user is asked for permission only when an action is critical or very dangerous.
  - Every such prompt includes "Always allow in this chat". Once chosen, even these prompts stop
    appearing in that chat.
- [ ] **Per-user concurrency limit:** cap how many chats one user can have generating at the same
      time. Requests over the cap wait in a visible queue. Subagents, workflow agents, background tasks
      and scheduled runs don't count toward it; each is held to its own limits instead. Proposed
      defaults under [Proposed limits](#proposed-limits).

## Deployment

- [ ] **Deploy node:**
  - Container nodes for long-running deployments.
  - The agent picks a preset Small, Medium or Large container for a project.
  - A dedicated agent tool puts a site online.
- [ ] Cloudflare API support, also exposed as an agent tool, to create subdomains for deployed sites.

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
- [ ] **Personal chats:** chats outside a workspace are private to their creator by default. They can
      be opened to the team with the share feature (see [Interface](#interface)).

## General

### Interface

- [ ] Responsive layout for phones.
- [ ] **Effort dropdown:**
  - Sits to the right of the model selector, styled the same way.
  - Gets each model's supported effort levels automatically, so unsupported levels never show.
- [ ] Hide image attachments automatically when the selected model can't read images.
- [ ] **Context indicator:**
  - A ring indicator to the right of the effort dropdown.
  - Opening it shows a breakdown of what is filling the context window, plus the provider limits
    described under [Limits](#limits).
- [ ] Share a chat with the team, by opening it to the team or with a link.

### Models and providers

- [ ] **Free OpenCode models:**
  - Run OpenCode inside a container and use its free models from there.
  - Test the main usage scenarios.
  - Restricting OpenCode completely doesn't work, so a different approach to locking it down is
    needed.
- [ ] Pull in the latest models automatically.
- [ ] **Default system model:** background jobs such as chat titles and follow-up suggestions use the
      latest ChatGPT Luna model (currently Luna 6), and follow it automatically as new versions ship.
      If it's unavailable, they fall back to GLM-5.3 Flash.
- [ ] **AI lab logos:** upload logos for each lab and group models by lab in the model dropdown.
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
