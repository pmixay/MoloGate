# GitHub in chat containers

Use **Connect GitHub** in a saved chat's attach/extra menu. Copy the device code,
open **https://github.com/login/device**, and authorize GitHub CLI in your browser.
Mologate never asks for a personal access token. Internet access must already be
on for the chat: connecting does not silently turn it on.

After authorization the sandbox runs `gh auth setup-git --hostname github.com`,
so both `gh` and HTTPS Git operations can use the account. This authorizes code
running in that container, including the agent, to access repositories allowed by
GitHub. Only authorize accounts/repositories you intend to expose to the agent.
Sub-agent chats use their root chat's container and therefore share its login;
other independent chats do not inherit this login.

**Disconnect** cancels any pending device flow and runs `gh auth logout` inside
the container. It removes local CLI credentials, not the GitHub-side OAuth grant.
Revoke that separately in GitHub Settings → Applications → Authorized OAuth Apps
→ GitHub CLI if needed. It does not undo repository clones or changes already
made. Sandbox expiry/deletion also removes its local credentials. Credentials
travel with the sandbox workspace when it is moved between container nodes.

## Deployment

Rebuild and deploy `containers/sandbox/Dockerfile`; it installs Debian's `gh`
package via apt. Old containers without `/usr/bin/gh` return an actionable 409;
rebuilding an image does not retroactively update a running container. No backend
host `gh`, host GitHub credentials, application OAuth secret, or database token
storage is involved.

## API

Under `/api/v1/containers/chats/{chat_id}`:

- `GET /github`: read current status, including an existing device flow.
- `POST /github/connect`: start/reuse device authorization.
- `POST /github/poll`: refresh status; poll every five seconds while UI is open.
- `DELETE /github`: cancel pending authorization and disconnect.

All require verified-user access, container integration enabled, and chat ownership
(or admin access), using the same root-chat resolution as other container routes.
Responses contain only `connected`, `pending`, optional `login`, optional `code`,
and optional `verification_uri` (always the fixed GitHub device URL). A pending
response may initially lack a code while GitHub is responding; keep polling.
`connected:false,pending:false` means no active flow (failed, cancelled or expired);
Connect starts another. Requests reuse a live pending process across duplicate
clicks and backend worker restarts. The device worker has a 15-minute timeout;
GitHub can expire its code earlier. No raw CLI errors, token values, arbitrary
URLs or process logs are sent to the browser.

409 indicates internet disabled or an outdated sandbox image; 502/503 indicate
execution/node failure and 504 indicates a bounded request timeout. Request
errors are generic and safe to display. Retrying status is safe. An offline
status uses the last verified login, not a fresh GitHub API validation.

## Implementation/security

The backend routes through `containers.route()` to the node's existing Open
Terminal proxy. Verified against the downloaded `open-terminal==0.14.0` wheel:
`POST /execute?wait=1` accepts `{command,cwd}`; a running response gives an `id`;
`GET /execute/{id}/status?wait=1` reports status and output entries
`{type:"output",data:"..."}`. The controller prints only allowlisted JSON.

A private `0700` sandbox state directory at
`/home/user/.local/state/mologate-github` holds the process identity, device output,
and last verified login. Credentials remain in `/home/user/.config/gh`.
`flock` serializes operations across backend workers. PID identity is checked
before killing a process group; stale state is discarded after sandbox restarts.
The login worker runs `gh auth login --web --hostname github.com --git-protocol
https`; it times out and configures Git only after successful authorization.
Identity is read with `gh api user --jq .login`, never `gh auth status` (which may
print token material). Token/debug environment overrides are removed, CLI/config
paths are fixed, and the backend independently validates all returned fields.
The sandbox user/agent can already read its credentials: the sandbox, not an
untrusted process inside it, is the security boundary.

## Tests

Run the dependency-free fake-CLI tests (no GitHub account, Docker or DB):

```sh
python -m unittest discover -s backend/open_webui/test -p test_github.py -v
```

Tests execute the actual embedded controller with an isolated fake CLI and cover
pending codes, duplicate clicks, successful setup, failure, expiration,
disconnect, offline refusal, response sanitization, and shared route gates.
