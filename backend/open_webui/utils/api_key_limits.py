"""Mologate: what a personal API key may do (docs/limits-and-accounts.md §11, phase 6).

A key acts as its owner: every request it makes is the owner's, decided by the allowance engine and
counted in the owner's share, so a key can never do more than its owner. Its owner can hold it to
less:

- ``rpm``: requests per minute, across every endpoint the key reaches;
- ``concurrency``: requests to provider-account models at the same time;
- ``models``: the model ids it may ask for;
- ``efforts``: the reasoning efforts it may ask for;
- ``daily_points``: points of a provider's pool it may use in a UTC day.

A member's key reaches only the gated endpoints (chat completions, the model list and the limits
endpoints), never ``/openai/*`` or the account and key routes. Limits a member set on their own key
apply in every mode but ``off``; without one, a key runs at most as many requests at once as its
owner may run chats, and that default, like every Mologate limit, applies only in ``enforce``.
"""

import logging
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from open_webui.utils import account_slots
from open_webui.utils.rate_limit import RateLimiter

log = logging.getLogger(__name__)

# Exact paths: the models router also lives under /api/v1/models and must stay out of reach.
KEY_ENDPOINTS = frozenset(
    {
        '/api/chat/completions',
        '/api/v1/chat/completions',
        '/api/models',
        '/api/v1/models',
        '/api/v1/models/model/limits',
        '/api/v1/provider-accounts/usage',
        '/api/v1/provider-accounts/credits',
    }
)
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')
MAX_RPM = 600
SLOT_PREFIX = 'apikey:'
_limiters: dict[int, RateLimiter] = {}


def endpoint_allowed(path: str) -> bool:
    return path.rstrip('/') in KEY_ENDPOINTS or path in KEY_ENDPOINTS


def key_of(request) -> tuple[str | None, dict]:
    """The personal key a request came with, and its limits."""
    state = getattr(request, 'state', None)
    return getattr(state, 'api_key_id', None), getattr(state, 'api_key_limits', None) or {}


async def check_rate(request, key_id: str, limits: dict) -> None:
    rpm = limits.get('rpm')
    if not rpm:
        return
    limiter = _limiters.setdefault(int(rpm), RateLimiter(limit=int(rpm), window=60, bucket_size=10))
    redis = getattr(getattr(request, 'app', None), 'state', None)
    if await limiter.is_limited(getattr(redis, 'redis', None), SLOT_PREFIX + key_id):
        raise HTTPException(429, f'This API key allows {rpm} requests a minute. Try again shortly.')


def requested_effort(form_data: dict) -> str | None:
    params = form_data.get('params') if isinstance(form_data.get('params'), dict) else {}
    reasoning = form_data.get('reasoning') if isinstance(form_data.get('reasoning'), dict) else {}
    return form_data.get('reasoning_effort') or params.get('reasoning_effort') or reasoning.get('effort')


def check_request(request, model_id: str | None, form_data: dict) -> None:
    """The model and effort a key asks for must be ones its owner allowed it."""
    key_id, limits = key_of(request)
    if not key_id:
        return
    models = limits.get('models') or []
    if models and model_id not in models:
        raise HTTPException(403, 'This API key may not use this model.')
    efforts = limits.get('efforts') or []
    effort = requested_effort(form_data)
    if efforts and effort and effort not in efforts:
        raise HTTPException(403, f'This API key may not ask for {effort} reasoning effort.')


async def acquire(request, user, enforce: bool):
    """A slot for this request on its key, or None when the key has no limit here. Refuses past it."""
    key_id, limits = key_of(request)
    if not key_id:
        return None
    limit = limits.get('concurrency')
    if not limit:
        if not enforce:
            return None
        from open_webui.utils.chat_slots import chat_limit

        # Without a limit of its own, a key runs at most as much at once as its owner may.
        limit = await chat_limit()
    redis = getattr(request.app.state, 'redis', None)
    ticket = uuid.uuid4().hex
    try:
        taken = await account_slots.take(redis, SLOT_PREFIX + key_id, ticket, int(limit))
    except Exception as e:
        log.warning('API key slots unavailable; the request goes ahead: %s', e)
        return None
    if not taken:
        raise HTTPException(429, f'This API key already has {limit} requests running. Wait for one to finish.')
    return account_slots.Slot(redis, SLOT_PREFIX + key_id, ticket)


def hold_until_done(result, slot):
    """Keep the key's slot until the answer, or its stream, is done."""
    if slot is None:
        return result
    if isinstance(result, StreamingResponse):
        body = result.body_iterator

        async def release_at_end():
            try:
                async for chunk in body:
                    yield chunk
            finally:
                slot.release()

        result.body_iterator = release_at_end()
        return result
    slot.release()
    return result


async def check_daily_points(request, user, provider: str, now: float | None = None) -> None:
    """A key's pool points today on this provider, against the daily limit its owner set."""
    key_id, limits = key_of(request)
    allowed = limits.get('daily_points')
    if not key_id or not allowed:
        return
    used = await points_today(key_id, user.id, provider, now)
    if used >= allowed:
        raise HTTPException(
            429, f'This API key has used its {allowed:g} points of the pool for today. It starts again at midnight UTC.'
        )


async def points_today(key_id: str, user_id: str, provider: str, now: float | None = None) -> float:
    from open_webui.models.usage_events import UsageEvents
    from open_webui.utils import allowance, allowance_gate
    from open_webui.utils.provider_limits import period_start

    now = time.time() if now is None else now
    units = await UsageEvents.key_units(key_id, provider, period_start('daily', now))
    if not units:
        return 0.0
    _, state = await allowance_gate.member_state(user_id, provider)
    numbers = allowance.pool_numbers(state, provider)
    window = numbers.get('weekly') or numbers.get('budget')
    if not window or not window['capacity']:
        return 0.0
    return units / window['capacity'] * 100


async def clean_limits(limits: dict, user) -> dict:
    """A key's limits as stored, never beyond its owner's."""
    from open_webui.utils.chat_slots import chat_limit

    cleaned = {}
    if limits.get('rpm'):
        cleaned['rpm'] = max(1, min(MAX_RPM, int(limits['rpm'])))
    if limits.get('concurrency'):
        owner_cap = await chat_limit()
        if int(limits['concurrency']) > owner_cap:
            raise HTTPException(400, f'A key can run at most {owner_cap} requests at once, as many as you can.')
        cleaned['concurrency'] = max(1, int(limits['concurrency']))
    if limits.get('models'):
        cleaned['models'] = sorted({str(model) for model in limits['models'] if model})
    if limits.get('efforts'):
        unknown = set(limits['efforts']) - set(EFFORTS)
        if unknown:
            raise HTTPException(400, 'Pick efforts from: ' + ', '.join(EFFORTS) + '.')
        cleaned['efforts'] = [effort for effort in EFFORTS if effort in limits['efforts']]
    if limits.get('daily_points'):
        points = float(limits['daily_points'])
        if not 0 < points <= 100:
            raise HTTPException(400, 'Daily points are a share of the pool, between 0 and 100.')
        cleaned['daily_points'] = points
    return cleaned
