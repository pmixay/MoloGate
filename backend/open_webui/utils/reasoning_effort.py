"""Reasoning effort levels each model supports, for the chat effort selector.

Every model in the listing gets a normalized ``reasoning_effort_options`` entry when its
levels are known, resolved in this order:

1. An admin override stored in the model's ``meta.reasoning_effort_levels``.
2. Metadata the provider returned for the model on its ``/models`` endpoint.
3. Levels the provider documents but does not list (Z.ai's GLM models, see ``ZAI_LEVELS``).
4. Otherwise nothing, and the frontend hides the selector.

Custom models built on a base model inherit the base model's levels unless they set
their own override.
"""

import re
from typing import Any

from open_webui.utils.model_ids import model_version

OPTIONS_KEY = 'reasoning_effort_options'
META_KEY = 'reasoning_effort_levels'

# Offered when a provider only says the model accepts a reasoning effort, without levels.
GENERIC_LEVELS = ['low', 'medium', 'high']

# Z.ai takes ``reasoning_effort`` from GLM-5.2 on (docs.z.ai, chat completion reference), but its
# /models lists no levels. GLM-5.3 and 5.3-Flash accept low, high and max. GLM-5.2 also accepts
# none and minimal (thinking skipped), maps low and medium to high and xhigh to max, so only the
# distinct ones are offered. Thinking runs at max unless asked otherwise.
ZAI_LEVELS: dict[tuple[int, int], list[tuple[str, str | None]]] = {
    (5, 3): [('low', None), ('high', None), ('max', None)],
    (5, 2): [('none', 'Skip thinking'), ('high', None), ('max', None)],
}
ZAI_DEFAULT = 'max'

_LEVEL_PATTERN = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')


def _normalize_level(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    level = value.strip().lower()
    return level if _LEVEL_PATTERN.match(level) else None


def _parse_levels(raw: Any) -> list[dict]:
    """Turn a provider or admin list into [{'value': str, 'description': str | None}]."""
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list):
        return []

    levels = []
    seen = set()
    for item in raw:
        description = None
        if isinstance(item, dict):
            value = _normalize_level(item.get('effort') or item.get('level') or item.get('value'))
            if isinstance(item.get('description'), str) and item['description'].strip():
                description = item['description'].strip()
        else:
            value = _normalize_level(item)
        if value and value not in seen:
            seen.add(value)
            levels.append({'value': value, 'description': description})
    return levels


def get_provider_reasoning_effort(model: dict) -> tuple[list[dict], str | None]:
    """Levels and default a provider advertises for one model in its /models response."""
    raw = model.get('openai') if isinstance(model.get('openai'), dict) else model

    # Codex / ChatGPT style: [{'effort': 'low', 'description': '...'}, ...]
    levels = _parse_levels(raw.get('supported_reasoning_levels'))
    # Plain lists.
    if not levels:
        levels = _parse_levels(raw.get('supported_reasoning_efforts'))
    if not levels:
        levels = _parse_levels(raw.get('reasoning_efforts'))
    # OpenRouter style: the model accepts a reasoning parameter, levels unspecified.
    if not levels:
        supported_parameters = raw.get('supported_parameters')
        if isinstance(supported_parameters, list) and (
            'reasoning' in supported_parameters or 'reasoning_effort' in supported_parameters
        ):
            levels = [{'value': value, 'description': None} for value in GENERIC_LEVELS]

    default = _normalize_level(raw.get('default_reasoning_level') or raw.get('default_reasoning_effort'))
    if default not in {level['value'] for level in levels}:
        default = None
    return levels, default


def get_known_reasoning_effort(model: dict) -> tuple[list[dict], str | None]:
    """Levels a provider documents for a model whose /models entry lists none."""
    if model.get('provider_account_provider') != 'zai':
        return [], None

    raw = model.get('openai') if isinstance(model.get('openai'), dict) else {}
    upstream_id = str(raw.get('id') or model.get('upstream_model_id') or '')
    if not upstream_id.lower().startswith('glm'):
        return [], None

    levels = ZAI_LEVELS.get(model_version(upstream_id)[:2])
    if not levels:
        return [], None
    return [{'value': value, 'description': description} for value, description in levels], ZAI_DEFAULT


def _get_meta(model: dict) -> dict:
    info = model.get('info')
    meta = info.get('meta') if isinstance(info, dict) else None
    return meta if isinstance(meta, dict) else {}


def _resolve(model: dict, models_by_id: dict, seen: set) -> dict | None:
    seen.add(model.get('id'))

    levels, default = get_provider_reasoning_effort(model)
    if not levels:
        levels, default = get_known_reasoning_effort(model)
    detected = {'levels': levels, 'default': default, 'source': 'provider'} if levels else None

    # Custom models have no provider metadata of their own; inherit from the base model.
    info = model.get('info') if isinstance(model.get('info'), dict) else {}
    base_model_id = info.get('base_model_id')
    if detected is None and base_model_id:
        base_model = models_by_id.get(base_model_id) or models_by_id.get(f'{base_model_id}:latest')
        if base_model and base_model.get('id') not in seen:
            detected = _resolve(base_model, models_by_id, seen)

    admin_levels = _parse_levels(_get_meta(model).get(META_KEY))
    if not admin_levels:
        return detected

    detected_levels = (detected or {}).get('levels') or []
    descriptions = {level['value']: level['description'] for level in detected_levels}
    values = [level['value'] for level in admin_levels]
    detected_default = (detected or {}).get('default')
    return {
        'levels': [{'value': value, 'description': descriptions.get(value)} for value in values],
        'default': detected_default if detected_default in values else None,
        'source': 'admin',
        # What the provider reports, shown next to the override in the model editor.
        'detected': [level['value'] for level in detected_levels],
    }


def apply_reasoning_effort_options(models: list[dict]) -> None:
    """Set or clear ``reasoning_effort_options`` on each model dict, in place."""
    models_by_id = {model.get('id'): model for model in models}
    resolved = {model.get('id'): _resolve(model, models_by_id, set()) for model in models}
    for model in models:
        options = resolved.get(model.get('id'))
        if options:
            model[OPTIONS_KEY] = options
        else:
            model.pop(OPTIONS_KEY, None)
