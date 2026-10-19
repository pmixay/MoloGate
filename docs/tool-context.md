# Tool output and model context

The context indicator's **Tools** category is the tool definitions. **Tool calls**
includes both call arguments and tool results. A small number of large terminal
logs, full file reads, fetched pages, or MCP results can therefore dominate a chat.

## Findings

Previously, `process_messages_with_output` reconstructed every saved tool result
verbatim, and the tool continuation loop sent the complete current turn's output
on every model request. This is normal stateless replay, not recursive duplication
of each previous request, but there was no shared tool-result text budget. A large
result was repeatedly charged as input and remained in later turns. Whole-turn
compaction runs only at the start of a saved-chat request, not during the tool loop,
so it could not protect a long-running agent turn.

There were also replay-specific duplication paths: continuing an existing response
could fold the original output prefix into follow-up output even though it was
already present in the base prompt; optional stateful Responses API requests could
resend output already stored behind `previous_response_id`.

## Current policy

`normalize_messages_for_model` applies `utils/tool_context.py` to model-bound
messages on initial requests, tool continuations, approved-tool resumes, and code
interpreter follow-ups:

- At most **24,000 text characters per tool result**.
- At most **96,000 text characters across tool results** in one model request.
- Recent results get priority, with small excerpts reserved for older ones.
- Long text keeps its beginning and end, with an explicit shortening notice.
- Call IDs and call/result pairs stay intact. Call arguments, user messages,
  assistant prose, and images are not shortened by this policy.
- The full original results remain saved and visible in chat history. Only the
  model payload is projected; database records and file contents are not rewritten.

These are character budgets (approximately 6k/24k tokens using the application's
four-characters-per-token estimate), not tokenizer-enforced total context limits.
Arguments such as large `write_file` inputs, images, and message text still consume
context separately. The optional stateful Responses API retains earlier inputs at
the provider; this projection bounds each new payload, but cannot remove output
already stored behind `previous_response_id` (stateful mode defaults to off). Prefer narrow file ranges, filtered searches, and concise
command output; request omitted ranges explicitly when needed.

The frontend estimates the bounded model-facing results, rather than counting the
full saved logs as active context. Older messages without the result subtotal
retain their historical estimate until a fresh turn records a new breakdown.

## Checks

```sh
PYTHONPATH=backend python -m unittest discover -s backend/open_webui/test -p 'test_tool_context.py' -v
PYTHONPATH=backend python -m unittest discover -s backend/open_webui/test -p 'test_continuation.py' -v
```
