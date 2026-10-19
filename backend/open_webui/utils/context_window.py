"""Context window size per model, and what fills it, for the composer's context indicator.

Every model in the listing gets a ``context_window`` (in tokens) when it is known, resolved in
this order:

1. An admin override stored in the model's ``meta.context_window``.
2. Metadata the provider returned for the model on its ``/models`` endpoint.
3. Otherwise nothing, and the indicator measures against the compaction threshold, if any.

Custom models built on a base model inherit the base model's window unless they set their own.

Each fresh turn also records ``context_breakdown`` on its assistant message: an estimate of the
prompt it sent, split into categories. The frontend adds that message's own output (which the
next turn sends back) and calibrates the estimate against the prompt tokens the provider
reported. Estimates use the same four-characters-per-token rule as context compaction.
"""

from typing import Any

from open_webui.utils.json_codec import JSONCodec

FIELD_KEY = 'context_window'
META_KEY = 'context_window'

# Provider /models fields that carry the window, most specific first.
PROVIDER_KEYS = (
    'context_window',  # Codex / ChatGPT
    'context_length',  # OpenRouter, many OpenAI-compatible servers
    'max_context_length',
    'max_model_len',  # vLLM
    'max_input_tokens',
    'input_token_limit',
    'inputTokenLimit',
)

# Compaction appends the summary of older messages to the system message under this header.
SUMMARY_HEADER = '[CONVERSATION SUMMARY]'
# Same flat guess context compaction uses for an image.
IMAGE_TOKENS = 1000
# Per-message framing (role, separators).
MESSAGE_OVERHEAD_TOKENS = 4

CATEGORIES = ('system', 'tools', 'summary', 'messages', 'tool_activity', 'images')


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def get_provider_context_window(model: dict) -> int | None:
    raw = model.get('openai') if isinstance(model.get('openai'), dict) else model
    for key in PROVIDER_KEYS:
        if (window := _positive_int(raw.get(key))) is not None:
            return window
    top_provider = raw.get('top_provider')
    if isinstance(top_provider, dict):
        return _positive_int(top_provider.get('context_length'))
    return None


def _get_meta(model: dict) -> dict:
    info = model.get('info')
    meta = info.get('meta') if isinstance(info, dict) else None
    return meta if isinstance(meta, dict) else {}


def _resolve(model: dict, models_by_id: dict, seen: set) -> int | None:
    seen.add(model.get('id'))
    if (window := _positive_int(_get_meta(model).get(META_KEY))) is not None:
        return window
    if (window := get_provider_context_window(model)) is not None:
        return window

    info = model.get('info') if isinstance(model.get('info'), dict) else {}
    base_model_id = info.get('base_model_id')
    if base_model_id:
        base_model = models_by_id.get(base_model_id) or models_by_id.get(f'{base_model_id}:latest')
        if base_model and base_model.get('id') not in seen:
            return _resolve(base_model, models_by_id, seen)
    return None


def apply_context_windows(models: list[dict]) -> None:
    """Set or clear ``context_window`` on each model dict, in place."""
    models_by_id = {model.get('id'): model for model in models}
    resolved = {model.get('id'): _resolve(model, models_by_id, set()) for model in models}
    for model in models:
        window = resolved.get(model.get('id'))
        if window:
            model[FIELD_KEY] = window
        else:
            model.pop(FIELD_KEY, None)


def estimate_tokens(value: Any) -> int:
    if value is None or value == '':
        return 0
    if not isinstance(value, str):
        try:
            value = JSONCodec.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    return max(1, len(value) // 4) if value else 0


def _split_content(content: Any) -> tuple[str, int]:
    """A message's text and its number of images."""
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return '', 0

    texts = []
    images = 0
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            if part.get('type') in ('image_url', 'input_image'):
                images += 1
            else:
                texts.append(part.get('text') or part.get('content') or '')
    return '\n'.join(text for text in texts if isinstance(text, str)), images


def build_context_breakdown(form_data: dict) -> dict:
    """Estimate, by category, the tokens of the prompt ``form_data`` is about to send."""
    categories = dict.fromkeys(CATEGORIES, 0)
    tool_result_tokens = 0
    categories['tools'] = estimate_tokens(form_data.get('tools')) if form_data.get('tools') else 0

    for message in form_data.get('messages') or []:
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        text, images = _split_content(message.get('content'))
        categories['images'] += images * IMAGE_TOKENS

        if role == 'system':
            summary_start = text.find(SUMMARY_HEADER)
            if summary_start >= 0:
                categories['summary'] += estimate_tokens(text[summary_start:])
                text = text[:summary_start]
            categories['system'] += estimate_tokens(text)
        elif role == 'tool':
            result_tokens = MESSAGE_OVERHEAD_TOKENS + estimate_tokens(text)
            categories['tool_activity'] += result_tokens
            tool_result_tokens += result_tokens
        else:
            categories['messages'] += MESSAGE_OVERHEAD_TOKENS + estimate_tokens(text)
            if message.get('tool_calls'):
                categories['tool_activity'] += estimate_tokens(message['tool_calls'])

    return {
        'categories': categories,
        'estimated_tokens': sum(categories.values()),
        # Separate results from call arguments so the frontend can apply the
        # model-only replay budget without undercounting file edits/commands.
        'tool_result_tokens': tool_result_tokens,
    }
