# Development updates — October 2026

This records the work discussed in this chat. The summary is in the
[roadmap](../ROADMAP.md#october-2026-development-status). Status distinguishes merged code from
open review and from deployment: **an open PR is not merged or shipped; a merge alone does not
confirm a production deployment.**

## PR and commit status

| Work                                                                               | Status                                    | Reference                                                                                                                |
| ---------------------------------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| GitHub device authorization button and a single `hammer-c` working-model animation | Merged into main                          | [PR #8](https://github.com/pmixay/MoloGate/pull/8), merge [`3dea56a`](https://github.com/pmixay/MoloGate/commit/3dea56a) |
| Follow-up catalog fix                                                              | On main                                   | [`c334456`](https://github.com/pmixay/MoloGate/commit/c334456)                                                           |
| Default context compaction                                                         | Open, not shipped                         | [PR #9](https://github.com/pmixay/MoloGate/pull/9)                                                                       |
| Live hammer elapsed time and tool details                                          | Open, not shipped                         | [PR #10](https://github.com/pmixay/MoloGate/pull/10)                                                                     |
| Sidebar delete-all action                                                          | Open, not shipped                         | [PR #11](https://github.com/pmixay/MoloGate/pull/11)                                                                     |
| Sandbox temporary storage                                                          | Open, not shipped                         | [PR #12](https://github.com/pmixay/MoloGate/pull/12)                                                                     |
| File-change tracking correctness                                                   | In progress on `fix/file-change-tracking` | New PR not yet assigned; do not assume #13                                                                               |

## Latest validation status

- PR #10 and PR #11: latest CI checks are green; both PRs remain open, not merged or shipped.
- PR #12: node and entrypoint tests and the node-image build pass; the sandbox-image build is
  pending. This is not a completed two-image rollout or deployment.

## GitHub authorization and working-model animation — merged

PR #8 adds the GitHub device authorization button and uses one `hammer-c` working-model animation rather than duplicate
working indicators. It merged into main as `3dea56a`; the catalog follow-up is `c334456`.

Disconnecting GitHub in the application does **not** revoke the GitHub authorization grant. If
revocation is needed, revoke that grant in GitHub as well. Do not describe disconnect as revocation.

## Context compaction — PR #9 open

- Enable automatic compaction by default when the user has no saved choice.
- Preserve an existing explicit saved `enable: false`; do not silently turn it back on.
- The request is to compact at 75% of known model context. The current trigger also retains the
  80,000-token safety threshold: compact at **75% of known context or 80k tokens, whichever comes
  first**. Without known context, retain the absolute safety threshold.
- For a 128k context, 75% is 96k, so the default 80k threshold triggers earlier (62.5%). Claiming
  that the default always waits for 75% would be incorrect.

Review should cover missing settings, saved disabled settings, known and unknown context sizes,
and both sides of the safety threshold. This PR remains open, not a shipped capability.

## Tool-use display and timing — PR #10 open

- Show a live elapsed timer with the single `hammer-c` working-model animation while work runs.
- Use actual server-recorded tool start and end timestamps, persisted with the tool result, for
  per-tool timings. Do not infer tool duration from rendering time or total response time.
- Preserve timings after reload. Hide timing when required timestamps are missing rather than
  presenting fabricated or zero durations for older messages.
- Keep code snippets collapsed by default, group tool calls, and make the display usable on mobile.

Review should include live generation, completed calls, reload, older messages without timestamps,
collapsed snippets, grouped calls and narrow screens. The PR remains open.

## Delete all chats — PR #11 open

Add the sidebar delete-all action with explicit confirmation and permission enforcement. The UI
must not substitute for server-side authorization. Review confirmation, cancel, permitted and denied
users before merge. This is a destructive action; do not use it as a deployment or rollback step.

## Sandbox temporary storage — PR #12 open

- Increase sandbox `/tmp` to **512 MiB** (the earlier roadmap baseline is 256 MiB).
- Put the server's default `TMPDIR` on disk under `/home/user`, avoiding filling the memory-backed
  `/tmp` with server temporary work. Preserve explicitly configured overrides.
- Document `XDG_CACHE_HOME` so cache placement is clear; retain explicit cache-location overrides.
- Rebuild **both** the sandbox image and the container-node image to roll out the corresponding
  changes. Updating only the application image is insufficient.
- Preserve existing chat containers and their `/home/user` workspaces. Do not remove volumes or
  delete/recreate active chat containers merely to apply a default. New image defaults do not
  retroactively alter every existing container; plan any existing-container migration separately.

Review new containers, explicit environment overrides, temporary/cache locations and continued
access to existing workspaces. This PR remains open; 512 MiB is not claimed as deployed.

## File-change tracking — implemented locally; PR/CI pending

Working branch: `fix/file-change-tracking` in `/home/user/worktrees/file-changes`.

The new work addresses false change counts from freshly cloned repositories, generated artifacts
and caches. A new clone should establish a clean baseline, not count the entire repository as agent
edits. Generated/cache noise should not obscure meaningful work, while real user-facing edits must
remain visible **during generation and after the response completes**.

The backend/UI implementation now refreshes changes after tools return and at response end, deduplicated and throttled
with a minimum two-second interval; `write_file` and `replace_file_content` force a refresh. Updates
are saved to the message, and the change summary is visible while work is running. Verification
confirmed that an empty change record clears the summary after a revert.

This is **tool-event-driven refresh, not a filesystem watcher**; do not claim continuous observation
of arbitrary filesystem writes. The new PR and its CI are pending. Final exclusion rules and broader
validation remain **implemented locally; PR/CI pending**: do not mark the fix merged, shipped or complete, or infer a PR
number before one is created. Review must guard against overly broad filters hiding real source
edits, changes to already tracked files, or legitimate new files.

Remaining checks include clean clone baselines; generated/cache-only activity; real additions, edits
and deletions; live updates during generation; and retained accurate changes after completion.
Only the verification explicitly recorded above is confirmed here; broader results are not assumed.

## Deployment and safe rollback

1. Review and merge each open PR separately before treating it as available on main. Record the
   exact source revision and immutable image tag or digest for every deployed component, retaining
   the previous immutable images for rollback.
2. Before deploying or migrating, back up the **chat database and workspace data**, including
   existing chat `/home/user` contents. Verify that the backup can be restored; avoid a database
   copy taken while writes can make it inconsistent.
3. Rebuild both sandbox and container-node images for PR #12. Preserve existing workspace data,
   environment overrides and service configuration. Validate with a new test chat first and check
   existing chats still retain their files.
4. Smoke-test the affected behavior: GitHub connection/catalog, compaction settings and threshold,
   live and persisted timings, permission-gated confirmed deletion, temporary/cache placement and
   file-change accuracy. Do not claim production verification until it has actually been done.
5. For a source rollback, create a **new branch** from the current integration state and use
   `git revert` for the relevant commits (a merge revert needs the correct mainline parent), then
   review the rollback PR. Do **not** use destructive `git reset --hard`, rewrite shared history,
   delete workspaces or remove data volumes.
6. For a deployment rollback, redeploy the **previous immutable image** tag/digest and compatible
   configuration. Check database/schema compatibility first. If data restoration is necessary,
   stop writes and restore the verified database and workspace backups deliberately; an image
   rollback by itself does not reverse data changes or restore deleted chats.

## Snapshot and attribution limitations

Change tracking uses API-triggered snapshots after tools return and at response end, **not a
filesystem watcher**. It cannot guarantee updates while a long-running tool is still executing.
For a newly discovered repository, edits already committed before its first scan are indistinguishable
from the repository's baseline `HEAD`. Existing stored summaries are not retrospectively rewritten.
Parallel subagents share the workspace, so overlapping changes cannot be guaranteed to be attributed
to the correct individual agent or turn.

## Local verification snapshot

Reported local checks passed: 11 live-change tests, 14 backend container tests and an initial run of
22 container-node tests. These are local test results, not final CI, merge or deployment claims.
Broader validation and the new PR's CI remain pending.
