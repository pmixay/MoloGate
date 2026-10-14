"""Mologate: write a usage ledger row for every request a provider account serves.

The account proxy (utils/provider_account_proxy.py) records each attempt: a used-up account as
``limited``, a failed request as ``error``, and a served one with the token counts and cost its
answer reported, once the answer (or its stream) ends. Writing happens in the background and never
fails or slows a request. See docs/limits-and-accounts.md, sections 6 and 8.
"""

import asyncio
import json
import logging
import math
import time

from open_webui.models.usage_events import UsageEvents
from open_webui.utils import model_prices

log = logging.getLogger(__name__)

_writes: set = set()


def _count(value) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(number) if math.isfinite(number) and number > 0 else 0


def usage_numbers(usage: dict | None) -> dict:
    """Token counts from Chat Completions or Responses usage, and OpenRouter's cost when given."""
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    completion = usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
    cost = usage.get('cost')
    return {
        'input_tokens': _count(usage.get('prompt_tokens', usage.get('input_tokens'))),
        'output_tokens': _count(usage.get('completion_tokens', usage.get('output_tokens'))),
        'cached_tokens': _count(prompt.get('cached_tokens') if isinstance(prompt, dict) else 0),
        'reasoning_tokens': _count(completion.get('reasoning_tokens') if isinstance(completion, dict) else 0),
        'cost': float(cost) if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0 else None,
    }


def request_source(request, metadata: dict | None) -> str:
    metadata = metadata or {}
    if metadata.get('task'):
        return 'task'
    if metadata.get('internal'):
        return 'subagent'
    if metadata.get('automation_id'):
        return 'scheduled'
    if not metadata.get('chat_id') and getattr(getattr(request, 'state', None), 'auth_type', None) == 'api_key':
        return 'api_key'
    return 'chat'


async def request_cost(account, model_id: str, numbers: dict) -> tuple[float | None, str | None]:
    """What a request cost in USD: the provider's figure, an estimate from published prices, or
    unknown. Subscriptions (ChatGPT, Z.ai coding plans) have no per-request cost."""
    if account.plan != 'api':
        return None, None
    if numbers['cost'] is not None:
        return numbers['cost'], 'provider'
    if account.provider not in model_prices.CATALOG_PROVIDERS:
        return None, None
    await model_prices.ready()
    cost = model_prices.estimate(
        model_prices.price(account.provider, model_id),
        numbers['input_tokens'],
        numbers['output_tokens'],
        numbers['cached_tokens'],
    )
    return (round(cost, 6), 'estimate') if cost is not None else (None, None)


class Meter:
    """The ledger rows of one request to a provider model, one per account it tries."""

    def __init__(self, request, user, metadata: dict | None, model_id: str):
        metadata = metadata if isinstance(metadata, dict) else {}
        self.user_id = user.id
        self.model_id = model_id
        self.source = request_source(request, metadata)
        self.chat_id = metadata.get('chat_id') or None
        self.request_id = metadata.get('message_id') or None
        self.rows = 0

    def record(self, account, status: str, usage: dict | None = None) -> None:
        self.rows += 1
        numbers = usage_numbers(usage)
        values = dict(
            # When the answer ended, not when the row is written: metering sets rows against the
            # provider's readings by time.
            ts=int(time.time()),
            user_id=self.user_id,
            account_id=account.id,
            provider=account.provider,
            model_id=self.model_id,
            source=self.source,
            # Observe: who the account belongs to, until the allowance engine assigns tiers.
            tier='reserve' if account.owner_user_id == self.user_id else 'pool',
            input_tokens=numbers['input_tokens'],
            output_tokens=numbers['output_tokens'],
            cached_tokens=numbers['cached_tokens'],
            reasoning_tokens=numbers['reasoning_tokens'],
            status=status,
            chat_id=self.chat_id,
            request_id=self.request_id,
        )
        task = asyncio.create_task(_write(values, account if status == 'ok' else None, self.model_id, numbers))
        _writes.add(task)
        task.add_done_callback(_writes.discard)


async def _write(values: dict, served_by, model_id: str, numbers: dict) -> None:
    try:
        if served_by is not None:
            values['cost'], values['cost_source'] = await request_cost(served_by, model_id, numbers)
        await UsageEvents.insert(**values)
    except Exception as e:
        log.warning('Could not write a usage ledger row: %s', e)


async def flush() -> None:
    """Wait for pending ledger writes (tests, shutdown)."""
    if _writes:
        await asyncio.gather(*list(_writes), return_exceptions=True)


def responses_usage(line: bytes | str) -> dict | None:
    """The usage in a Responses stream's final event line, if this is it."""
    text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else line
    if not text.startswith('data:') or '"usage"' not in text:
        return None
    try:
        event = json.loads(text[5:].strip())
    except ValueError:
        return None
    if isinstance(event, dict) and event.get('type') in ('response.completed', 'response.incomplete'):
        return (event.get('response') or {}).get('usage')
    return None
