"""Mologate: published model prices, to estimate what a request on an API key cost.

OpenAI, Anthropic and Z.ai do not report a request's cost, and their inference keys cannot read
spending, so the usage ledger estimates it from token counts and the models.dev catalog (prices in
USD per million tokens, by provider and model id). OpenRouter reports each request's cost itself.

The catalog is fetched in the background at most once a day and kept in memory, reduced to the
providers Mologate connects. Set ``MOLOGATE_PRICE_CATALOG_URL`` to another copy, or to nothing to
turn estimates off; offline mode turns them off too. Without a price, a request's cost stays
unknown and the accounts page says so.
"""

import asyncio
import json
import logging
import math
import os
import re
import time

import aiohttp

from open_webui.env import OFFLINE_MODE

log = logging.getLogger(__name__)

CATALOG_URL = os.environ.get('MOLOGATE_PRICE_CATALOG_URL', 'https://models.dev/api.json').strip()
REFRESH_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60 * 60
# Mologate's provider -> the catalog's.
CATALOG_PROVIDERS = {'openai_api': 'openai', 'anthropic_api': 'anthropic', 'openrouter': 'openrouter', 'zai': 'zai'}
DATE_SUFFIX = re.compile(r'-(\d{8}|\d{4}-\d{2}-\d{2})$')

_prices: dict[str, dict[str, dict]] = {}
_fetched_at = -math.inf
_failed_at = -math.inf
_task: asyncio.Task | None = None


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def reduce_catalog(catalog: dict) -> dict[str, dict[str, dict]]:
    """Only the prices of the providers Mologate connects."""
    prices = {}
    for provider, name in CATALOG_PROVIDERS.items():
        models = ((catalog.get(name) or {}) if isinstance(catalog, dict) else {}).get('models') or {}
        prices[provider] = {
            model_id: entry['cost']
            for model_id, entry in models.items()
            if isinstance(entry, dict) and isinstance(entry.get('cost'), dict)
        }
    return prices


async def _fetch() -> None:
    global _prices, _fetched_at, _failed_at
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(CATALOG_URL, allow_redirects=False) as response:
                response.raise_for_status()
                body = await response.read()
        # A few megabytes of JSON: parse it off the event loop.
        _prices = await asyncio.to_thread(lambda: reduce_catalog(json.loads(body)))
        _fetched_at = time.monotonic()
    except Exception as e:
        _failed_at = time.monotonic()
        log.info('Model prices unavailable; costs stay unknown for now: %s', e)


def refresh_soon() -> None:
    """Fetch the catalog in the background when it is a day old (or an hour after a failure)."""
    global _task
    if OFFLINE_MODE or not CATALOG_URL or (_task is not None and not _task.done()):
        return
    now = time.monotonic()
    if now - _fetched_at < REFRESH_SECONDS or now - _failed_at < RETRY_SECONDS:
        return
    _task = asyncio.create_task(_fetch())


async def ready() -> None:
    """Fetch the catalog first when it is due, so the first requests after a start get a price too.
    Ledger rows are written in the background, so they can wait for it."""
    refresh_soon()
    if _task is not None and not _task.done():
        # A writer that is cancelled leaves the fetch running for the next one.
        await asyncio.shield(_task)


def price(provider: str, model_id: str, prices: dict | None = None) -> dict | None:
    """The model's price per million tokens, or None when the catalog has none."""
    models = (_prices if prices is None else prices).get(provider) or {}
    return models.get(model_id) or models.get(DATE_SUFFIX.sub('', model_id))


def estimate(cost: dict | None, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
    """USD for one request: uncached input, cached input and output (reasoning is billed as output).

    A price with context tiers uses the tier the prompt reached.
    """
    if not cost:
        return None
    for tier in sorted(cost.get('tiers') or [], key=lambda item: (item.get('tier') or {}).get('size') or 0):
        size = _number((tier.get('tier') or {}).get('size'))
        if (tier.get('tier') or {}).get('type') == 'context' and size is not None and input_tokens > size:
            cost = {**cost, **{key: value for key, value in tier.items() if key != 'tier'}}
    rate_in, rate_out = _number(cost.get('input')), _number(cost.get('output'))
    if rate_in is None or rate_out is None:
        return None
    rate_cached = _number(cost.get('cache_read'))
    cached = min(max(0, cached_tokens), max(0, input_tokens))
    total = (input_tokens - cached) * rate_in + cached * (rate_in if rate_cached is None else rate_cached)
    return (total + max(0, output_tokens) * rate_out) / 1_000_000
