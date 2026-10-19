"""Bound model-facing tool output, without modifying the saved/displayed results.

Tool results (terminal logs, fetched pages, MCP responses, etc.) used to be replayed
verbatim on every continuation. Whole-turn compaction cannot help during a long
agent run. Apply these limits at the common model-payload boundary instead.

Budgets count text characters, not exact tokens. Call arguments are deliberately
untouched: shortening them could change the meaning of edits or executed commands.
"""

TOOL_RESULT_MAX_CHARS = 24_000
TOOL_CONTEXT_MAX_CHARS = 96_000
OLDER_RESULT_MIN_CHARS = 256


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    notice = (
        '\n[Tool output shortened for model context; full result remains in chat history. '
        'Request a narrower range if needed.]\n'
    )
    if limit <= len(notice):
        return '[Tool output omitted]'[:limit]
    available = limit - len(notice)
    head = (available * 2) // 3
    tail = available - head
    return text[:head] + notice + (text[-tail:] if tail else '')


def _text_size(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part.get('text', ''))
            for part in content
            if isinstance(part, dict) and isinstance(part.get('text'), str)
        )
    return 0


def _bounded_content(content, limit: int):
    if isinstance(content, str):
        return _excerpt(content, limit)
    if not isinstance(content, list):
        return content
    # Preserve images and other non-text parts; only copy parts we change.
    parts = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get('text'), str):
            text = _excerpt(part['text'], limit)
            if text:
                parts.append({**part, 'text': text})
                limit -= len(text)
        else:
            parts.append(part)
    return parts


def bound_tool_context(messages: list[dict]) -> list[dict]:
    """Cap individual results and the combined result text, favoring recent tools.

    Keep small excerpts of older outputs rather than removing call/result pairs.
    This is a pure projection: callers may share these dicts with saved chat output.
    """
    sizes = {
        index: min(_text_size(message.get('content')), TOOL_RESULT_MAX_CHARS)
        for index, message in enumerate(messages)
        if message.get('role') == 'tool'
    }
    if not sizes:
        return messages
    # Reserve a little context for each older result, then distribute the remaining
    # budget newest-first. Even thousands of results cannot evade the shared cap.
    minimum = min(OLDER_RESULT_MIN_CHARS, TOOL_CONTEXT_MAX_CHARS // len(sizes))
    budgets = {index: min(size, minimum) for index, size in sizes.items()}
    remaining = TOOL_CONTEXT_MAX_CHARS - sum(budgets.values())
    for index in reversed(sizes):
        extra = min(sizes[index] - budgets[index], remaining)
        budgets[index] += extra
        remaining -= extra
    return [
        {**message, 'content': _bounded_content(message.get('content'), budgets[index])}
        if index in budgets
        else message
        for index, message in enumerate(messages)
    ]
