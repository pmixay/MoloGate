"""Mologate: the allowance gate, phase 3: decide every request and record it (observe).

Each request to a provider account is decided by the allowance engine (utils/allowance.py) from
what metering knows: every account window's latest reading and each member's tokens and outside
use since the window last reset (utils/metering.py, the usage ledger). The decision is written on
the request's ledger rows. Nothing is blocked, swapped or delayed: enforcing the decisions is
phase 4. Deciding runs beside the request and never slows or fails it.

``replay`` decides a stretch of past requests again, as the rules would have, for the admin
report (docs/limits-and-accounts.md, Phase 3).
"""

import asyncio
import logging
import time

from open_webui.models.config import Config
from open_webui.models.metering import Metering
from open_webui.models.provider_accounts import ProviderAccounts
from open_webui.models.usage_events import UsageEvents
from open_webui.utils import allowance, metering
from open_webui.utils.model_ids import _family_rank, find_latest_model_in_family, normalize_model_name
from open_webui.utils.provider_limits import period_start

log = logging.getLogger(__name__)

# Each provider's worker models, by family (decision 6). Admins change them with the
# `mologate.worker_models` setting, {provider: family}.
WORKER_FAMILIES = {
    'codex': 'luna',
    'openai_api': 'luna',
    'anthropic_api': 'haiku',
    'zai': 'flash',
    'openrouter': 'luna',
}
# The pool's numbers change slowly; requests within this many seconds share them. A change to
# any account (added, changed, removed) rebuilds them at once on this worker.
CACHE_SECONDS = 15
DECISION_TIMEOUT_SECONDS = 10

_cache: dict[str, tuple[float, int, dict]] = {}
_tasks: set = set()


def to_account(row) -> allowance.Account:
    amount = (row.budget or {}).get('amount') if row.plan == 'api' else None
    budget = float(amount) if isinstance(amount, (int, float)) and amount > 0 else None
    return allowance.Account(
        id=row.id,
        provider=row.provider,
        owner=row.owner_user_id,
        sharing=row.sharing,
        status=row.status,
        weight=row.capacity_weight or 1.0,
        models=frozenset(model['id'] for model in row.models or []),
        budget=budget,
        windows=('budget',) if budget else () if row.plan == 'api' else ('5h', 'weekly'),
    )


async def worker_models(provider: str, accounts: list) -> tuple[str | None, frozenset]:
    families = await Config.get('mologate.worker_models', None)
    family = (families if isinstance(families, dict) else WORKER_FAMILIES).get(provider)
    if not family:
        return None, frozenset()
    catalog = {model_id: {'name': model_id} for account in accounts for model_id in account.models or ()}
    family = normalize_model_name(family)
    workers = frozenset(model_id for model_id in catalog if _family_rank(model_id, family) is not None)
    return find_latest_model_in_family(family, catalog), workers


async def members() -> int:
    from sqlalchemy import func, select

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.users import User

    async with get_async_db_context() as db:
        query = select(func.count()).select_from(User).where(User.role.in_(('user', 'admin')))
        return int((await db.execute(query)).scalar() or 0)


async def pool_snapshot(provider: str, now: float | None = None) -> dict:
    """Everything the engine needs about one provider's accounts, shared by requests for a few seconds."""
    now = time.time() if now is None else now
    cached = _cache.get(provider)
    version = ProviderAccounts.version
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS and cached[1] == version:
        return cached[2]
    rows = [row for row in await ProviderAccounts.list() if row.provider == provider]
    accounts = [to_account(row) for row in rows]
    latest = {(window.account_id, window.window): window for window in await Metering.windows([a.id for a in accounts])}
    periods: dict = {}
    owner_active: dict = {}
    for account in accounts:
        for name in account.windows:
            if name == 'budget':
                since, current = period_start('monthly', now), None
            else:
                current = latest.get((account.id, name))
                since = metering.window_since(current, name, now)
            usage = await UsageEvents.tokens_by_user_model(account.id, since)
            periods.setdefault(account.id, {})[name] = allowance.Period(
                used=current.used_percent if current is not None else None,
                resets_at=current.resets_at if current is not None else None,
                tokens=usage['tokens'],
                cost=usage['cost'],
                outside=await UsageEvents.outside_by_user(account.id, name, since) if name != 'budget' else {},
            )
        if account.owner:
            owner_active[account.id] = await UsageEvents.used_since(
                account.id, account.owner, int(now - allowance.IDLE_SECONDS)
            )
    worker_model, workers = await worker_models(provider, accounts)
    snapshot = {
        'accounts': accounts,
        'periods': periods,
        'owner_active': owner_active,
        'members': await members(),
        'calibrations': await metering.calibrations(),
        'worker_model': worker_model,
        'worker_models': workers,
    }
    _cache[provider] = (time.monotonic(), version, snapshot)
    return snapshot


async def decide(user_id: str, provider: str, model: str, chat_id: str | None = None) -> allowance.Decision:
    snapshot = await pool_snapshot(provider)
    state = allowance.build_state(
        user_id,
        provider,
        snapshot['accounts'],
        snapshot['periods'],
        snapshot['owner_active'],
        snapshot['members'],
        snapshot['calibrations'],
        # Credits are phase 5 and overrides phase 4: none yet.
        worker_models=snapshot['worker_models'],
    )
    request = allowance.Request(
        user_id=user_id,
        provider=provider,
        model=model,
        previous_account=await UsageEvents.last_account(user_id, chat_id) if chat_id else None,
        worker_model=snapshot['worker_model'],
    )
    return allowance.decide(request, state)


async def _observe(user_id: str, provider: str, model: str, chat_id: str | None) -> dict | None:
    try:
        mode = await Config.get('mologate.limits_mode', 'observe')
        if mode == 'off':
            return None
        decision = await decide(user_id, provider, model, chat_id)
        return decision.as_dict() | {'mode': mode}
    except Exception as e:
        log.warning('Could not decide a request: %s', e)
        return None


def observe(user, provider: str, model: str, metadata: dict | None) -> asyncio.Task | None:
    """Decide a request in the background; the ledger row picks the decision up when it is written."""
    chat_id = (metadata or {}).get('chat_id') or None
    try:
        task = asyncio.create_task(_observe(user.id, provider, model, chat_id))
    except RuntimeError:
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def decision_of(task: asyncio.Task | None) -> dict | None:
    if task is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(task), DECISION_TIMEOUT_SECONDS)
    except Exception:
        return None


async def replay(days: int = 7, now: float | None = None) -> dict:
    """Decide the last `days` of requests again under the rules, from the ledger and the readings."""
    now = time.time() if now is None else now
    since = int(now - days * 86400)
    rows = await ProviderAccounts.list()
    accounts = [to_account(row) for row in rows]
    events = []
    for account in accounts:
        for name in account.windows:
            if name == 'budget':
                continue
            for reading in await Metering.readings(account.id, name, since):
                events.append(
                    {
                        'kind': 'reading',
                        'ts': reading.observed_at,
                        'account_id': account.id,
                        'window': name,
                        'used': reading.used_percent,
                        'resets_at': reading.resets_at,
                        'window_minutes': reading.window_minutes,
                    }
                )
    # Outside rows and requests from a little earlier, so the first periods start with their use.
    for row in await UsageEvents.between(since - 7 * 86400, int(now) + 1):
        if row.source == 'outside':
            events.append(
                {
                    'kind': 'outside',
                    'ts': row.ts,
                    'account_id': row.account_id,
                    'window': row.window,
                    'user_id': row.user_id,
                    'units': row.units,
                }
            )
            continue
        events.append(
            {
                'kind': 'request' if row.ts >= since else 'history',
                'ts': row.ts,
                'user_id': row.user_id,
                'account_id': row.account_id,
                'provider': row.provider,
                'model_id': row.model_id,
                'tokens': {
                    'input_tokens': row.input_tokens,
                    'output_tokens': row.output_tokens,
                    'cached_tokens': row.cached_tokens,
                    'reasoning_tokens': row.reasoning_tokens,
                },
                'cost': row.cost,
                'chat_id': row.chat_id,
                'request_id': row.request_id,
                'status': row.status,
            }
        )
    events.sort(key=lambda event: (event['ts'], event['kind'] != 'reading'))
    workers = {}
    for provider in {account.provider for account in accounts}:
        workers[provider] = await worker_models(provider, [a for a in accounts if a.provider == provider])
    report = allowance.replay(events, accounts, await members(), await metering.calibrations(), workers)
    report.pop('decisions', None)
    return {'days': days, 'since': since, **report}


async def summary(days: int = 7, now: float | None = None) -> dict:
    """What the gate decided over the last days: per member, how many requests went to each tier."""
    now = time.time() if now is None else now
    result: dict = {}
    for user_id, decision in await UsageEvents.decisions(int(now - days * 86400)):
        if not isinstance(decision, dict) or decision.get('tier') not in allowance.TIERS:
            continue
        member = result.setdefault(user_id, {tier: 0 for tier in allowance.TIERS} | {'swapped': 0})
        member[decision['tier']] += 1
        member['swapped'] += bool(decision.get('swapped'))
    return result
