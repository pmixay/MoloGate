# Tool context management

A long tool-using response can accumulate more tool arguments and results than user prose.
The context bar's **Tools** category counts definitions; **Tool calls** counts activity payloads.
Prompt caching can reduce cost for repeated prefixes, but does not reduce context occupancy.

## Fixes

- Apply request-only tool-result clearing on fresh turns and every tool/code-interpreter round.
  Full stored output and visible expandable tool rows are not rewritten.
- Keep up to 12,000 text characters for each of the latest four results, 1,000 for older results,
  and at most 64,000 result-text characters total. Preserve beginning/end with an explicit notice.
  Image blocks are kept; the text budget is not a total multimodal token cap.
- Shorten oversized **completed** call arguments in request copies only, maintaining valid JSON.
  Pending/executable arguments and call/result IDs are unchanged. Large completed arguments have
  a separate budget. Models can request targeted reads to recover omitted details.
- Check normal compaction thresholds during tool loops, not only at the next user message.
  Summarize only at assistant-round boundaries, retaining recent complete tool-call/result sets,
  original system/developer instructions and the latest user request. Cache a prefix checkpoint
  within the active response so rebuilding display history does not resurrect summarized output.
- Include structured tool results in summarization input, with a bounded text budget. Previously
  summary prompt formatting could miss tool-only assistant output.
- Tool continuations use complete local history, not `previous_response_id`; otherwise remote
  provider history could keep cleared results. This sacrifices stateful continuation cache savings
  to make local context editing effective; ordinary provider prompt caching is separate.
- Refresh the context breakdown on each request, and mark how much visible output it already
  includes so the UI does not add the preserved transcript a second time.
- Make delegation explicitly optional: work directly by default, delegate substantial independent
  work only when useful, request bounded output and avoid redundant polling/watchers. This does
  not disable agents or override an administrator's explicit custom instructions.
- Completion notifications omit repeated full task/context text and use a 5,000-character preview.
  The child conversation and task result retain the fuller report. This reduces notification size;
  it does not claim to eliminate every possible duplicate delivery race.

## Configuration and limitations

`ENABLE_TOOL_CONTEXT_CLEARING=true` seeds `chat.tool_context.enable` for new configuration.
Existing saved configuration wins, as with other seeded settings. Setting this key false disables
request-only tool clearing; summary compaction remains controlled separately by
`chat.context_compaction.enable` and its existing thresholds (75% model-window basis plus fixed
safety threshold). No provider-specific proprietary implementation is copied.

Character counts are estimates, not tokenizer-enforced hard limits. Tool definitions, images,
reasoning, pending calls, user text and system instructions are not covered by the result-text
budget. Compaction requires a working model; on summary failure bounded tool results remain and
calls/results stay paired. Full-context replay can still be large if images or instructions alone
fill the model window. Editing may change cache hit rates.

Deploy the updated backend and frontend. No database migration or sandbox image rebuild is needed
for this feature. Long-running requests already in flight need a new turn/request to run the new
code. This does not refund tokens already used or rewrite saved chat history. To roll back,
redeploy the previous immutable application image or disable the clearing config key; preserve the
chat database and workspace volumes.

## Public design references

Anthropic documents separate strategies for compaction, tool-result clearing and memory:

- [Context engineering: memory, compaction, and tool clearing](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools)
- [Manage tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context)

This implementation follows those general principles across MoloGate providers, not an assertion
that MoloGate now behaves identically to Claude Code.
