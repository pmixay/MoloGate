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
- [ ] **Multi-agent workflows.** Proposed design, to refine:
  - **Definition:** a workflow is a reusable plan that runs several agents together in phases, for
    example "plan → work in parallel → verify → merge results".
  - **Agent steps:** each step names an agent profile, its instructions, its inputs, and the shape of
    the result it must return. The orchestrator can pass one step's results into the next.
  - **Patterns:**
    - fan-out: the same task split across N agents, such as reviewing a diff from several angles;
    - pipeline: each result is handed to the next agent as soon as it is ready;
    - verification: a separate agent checks every finding before it is reported;
    - majority vote: several agents answer independently and the answers are compared.
  - **Live view:** each running workflow shows its phases, every agent's status and progress, and a
    link to each agent's conversation.
  - **Repository:** a workflow starts without a linked repository by default. The user can pick a
    repository and branch instead. Mologate then clones it automatically, git is set up, and the agents
    are told to work in that repository.
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

### GitHub

- [ ] **GitHub integration:**
  - Agent tools for working with repositories.
  - Each user authorizes by linking their own GitHub token, and can unlink or replace it.
  - A "Create PR" button.

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
- [ ] **Projects:** a workspace is a project that holds chats and shared files.
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
- [ ] **AI lab logos:** upload logos for each lab and group models by lab in the model dropdown.
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

### Multi-agent workflows

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

## Open questions

- **Workspace files in chat containers:** each chat has its own container. Should a workspace's
  shared files appear automatically in the container of every chat in that workspace, and should
  changes made there sync back to the shared files?
- **Fair use of shared accounts:** since accounts are shared by the team, should there be a per-user
  share of each account's limits, so one person can't use up an account for everyone?
