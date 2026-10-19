"""Request-only tool-result clearing. Stored transcripts and tool execution stay lossless.

Keep call/result IDs and ordering intact. Shed old payloads, not user instructions. This is
provider-independent context editing, not prompt caching (which doesn't shrink context).
"""

from copy import deepcopy
import json

RECENT_RESULTS = 4
RECENT_CHARS = 12000
OLD_CHARS = 1000
RESULT_BUDGET = 64000
ARGUMENT_CHARS = 12000
NOTICE = '[Tool context shortened; full output remains in the chat. Use targeted reads or rerun with a line range/tail when needed.]'
TOOL_GUIDANCE = (
    'Work directly by default. Delegate only a substantial, independent task when parallel work '
    'or isolation is worth the overhead, or when the user asks. Do not delegate routine commands '
    'or small edits. Batch independent reads, request bounded output, and use one watcher instead '
    'of repeatedly polling the same task. Old tool output may be shortened in model context; '
    'the full transcript remains in the chat. Re-read relevant files with targeted ranges if needed.'
)


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(NOTICE) + 2:
        return NOTICE[:limit]
    remaining = limit - len(NOTICE) - 2
    head = remaining * 2 // 3
    return text[:head] + '\n' + NOTICE + '\n' + text[-(remaining - head) :]


def _content(content, limit: int):
    if isinstance(content, str):
        return shorten(content, limit)
    if not isinstance(content, list):
        return content
    # Text blocks share one budget. Preserve image blocks: provider image ordering is significant.
    remaining = limit
    result = []
    for part in content:
        if isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text'):
            key = 'text'
            text = part.get(key)
            if isinstance(text, str):
                kept = shorten(text, max(0, remaining))
                result.append({**part, key: kept})
                remaining = max(0, remaining - len(kept))
                continue
        result.append(deepcopy(part))
    return result


def _argument_value(value):
    if isinstance(value, str):
        return shorten(value, 2000)
    if isinstance(value, dict):
        return {k: _argument_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_argument_value(v) for v in value]
    return value


def clear_tool_context(messages: list[dict]) -> list[dict]:
    """Bound replayed payloads without mutating stored output or executable call objects."""
    result = deepcopy(messages)
    tools = [i for i, m in enumerate(result) if m.get('role') == 'tool']
    remaining = RESULT_BUDGET
    for position, index in enumerate(reversed(tools)):
        limit = min(RECENT_CHARS if position < RECENT_RESULTS else OLD_CHARS, remaining)
        result[index]['content'] = _content(result[index].get('content'), limit)
        content = result[index].get('content')
        if isinstance(content, str):
            remaining -= len(content)
        elif isinstance(content, list):
            remaining -= sum(
                len(p.get('text', '')) for p in content if isinstance(p, dict) and isinstance(p.get('text'), str)
            )
        remaining = max(0, remaining)
    completed_ids = {result[i].get('tool_call_id') for i in tools}
    argument_budget = RESULT_BUDGET
    for message in reversed(result):
        if message.get('role') != 'assistant':
            continue
        for call in message.get('tool_calls') or []:
            function = call.get('function') or {}
            arguments = function.get('arguments')
            if call.get('id') not in completed_ids:
                continue  # Never touch pending/executable arguments.
            limit = max(200, min(ARGUMENT_CHARS, argument_budget))
            if isinstance(arguments, str) and len(arguments) > limit:
                try:
                    parsed = _argument_value(json.loads(arguments))
                    encoded = json.dumps(parsed, ensure_ascii=False)
                    # Keep valid JSON even for pathological large arrays/dictionaries.
                    function['arguments'] = (
                        encoded if len(encoded) <= limit else json.dumps({'_context_shortened': NOTICE})
                    )
                except (ValueError, TypeError):
                    # Do not fabricate valid execution arguments for a malformed original call.
                    pass
            if isinstance(function.get('arguments'), str):
                argument_budget = max(0, argument_budget - len(function['arguments']))
    return result


def safe_tool_boundary(messages: list[dict], keep_rounds: int = 4) -> int:
    """Start of recent complete assistant/tool rounds; never split a parallel call/result set."""
    starts = [i for i, m in enumerate(messages) if m.get('role') == 'assistant']
    return starts[-keep_rounds] if len(starts) > keep_rounds else 0


def summary_messages(messages: list[dict]) -> list[dict]:
    """Expose tool names/arguments/results to the summarizer, not just assistant prose."""
    prepared = []
    remaining = 48000
    for message in reversed(messages):
        item = dict(message)
        if message.get('output'):
            # Raw stored Responses output is otherwise invisible to the text prompt formatter.
            text = json.dumps(message['output'], ensure_ascii=False, default=str)
            item['content'] = shorten(text, 12000)
        elif message.get('tool_calls'):
            item['content'] = (
                str(message.get('content') or '')
                + '\nTool calls: '
                + shorten(json.dumps(message['tool_calls'], ensure_ascii=False), 4000)
            )
        elif message.get('role') == 'tool':
            item['content'] = (
                str(message.get('tool_call_id') or '') + ': ' + shorten(str(message.get('content') or ''), 4000)
            )
        content = str(item.get('content') or '')
        item['content'] = shorten(content, min(remaining, 12000))
        remaining = max(0, remaining - len(item['content']))
        prepared.append(item)
    return list(reversed(prepared))
