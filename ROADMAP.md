# Mologate Roadmap

Planned features for Mologate, the Molotok team's fork of Open WebUI. Items are grouped by area and
are not in priority order. Tick an item off when it ships.

## Limits

- [ ] **Accounts overview:** a list of all connected accounts showing, for each one, the remaining limits,
      the subscription type, and whether it is an API key or a full account.
- [ ] **Per-account limits:** show usage limits for every connected account or key where the provider
      makes them available.
- [ ] **Automatic limit resets:** automatically apply a limit reset when an account reaches 0%. Any team
      member can turn auto-reset off globally.
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
  - **Limits:** each workflow has a maximum number of agents, a token or limit budget, and a wall-clock
    timeout. The user confirms before a large workflow starts.
  - **Control and reuse:** workflows can be cancelled, and resumed after a failure without re-running
    finished steps. Saved workflows can be shared with the team, e.g. "code review", "research a
    topic", "implement + test + review".
- [ ] **Agent-to-agent messaging:** agents in the same chat can message each other, including the main
      agent.
- [ ] **Background tasks:**
  - A task moves to the background automatically after a set time.
  - Users can see the list of running tasks and stop any of them.
- [ ] **Monitor tasks:** wait for a specific condition to become true, within limits.
- [ ] **Scheduled tasks:** delayed or recurring tasks, with strict limits.
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
- [ ] Concurrency-based rate limits.

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

## Open questions

- **Limit resets:** what does "applying a limit reset" mean for each provider? It could be a
  provider-side reset or switching to another account.
- **Task limits:** what exact limits apply to monitor tasks, scheduled tasks and multi-agent
  workflows (counts, durations, budgets)?
- **Rate limits:** should the concurrency rate limits apply per user, per provider account, or across
  the whole instance?
