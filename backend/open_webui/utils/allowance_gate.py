"""Mologate: the allowance gate: decide every request, record it, and in ``enforce`` act on it.

Each request to a provider account is decided by the allowance engine (utils/allowance.py) from
what metering knows: every account window's latest reading and each member's tokens and outside
use since the window last reset (utils/metering.py, the usage ledger). The decision is written on
the request's ledger rows.

In ``observe`` (phase 3) nothing is blocked, swapped or delayed: deciding runs beside the request
and never slows or fails it. In ``enforce`` (phase 4) the account proxy decides first and serves
the request as decided (utils/provider_account_proxy.py): the decided account, failing over to
the others of the same tier, the worker model past the share, and a limit error when the pool is
empty. A decision that cannot be made in time lets the request go ahead as in observe.

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
from open_webui.utils import calibration as fitting
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
                # A budget starts over with the month.
                resets_at = period_start('monthly', since + 32 * 86400)
            else:
                current = latest.get((account.id, name))
                since = metering.window_since(current, name, now)
            usage = await UsageEvents.tokens_by_user_model(account.id, since)
            periods.setdefault(account.id, {})[name] = allowance.Period(
                used=current.used_percent if current is not None else None,
                resets_at=resets_at if name == 'budget' else current.resets_at if current is not None else None,
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


async def extras(user_id: str) -> tuple[float, float]:
    """The member's credit balance (phase 5) and their active overrides in pool points (phase 4)."""
    from open_webui.models.limit_overrides import LimitOverrides
    from open_webui.utils import credits

    return await credits.balance(user_id), await LimitOverrides.active_points(user_id)


async def member_state(user_id: str, provider: str) -> tuple[dict, allowance.State]:
    snapshot = await pool_snapshot(provider)
    credits, extra_points = await extras(user_id)
    state = allowance.build_state(
        user_id,
        provider,
        snapshot['accounts'],
        snapshot['periods'],
        snapshot['owner_active'],
        snapshot['members'],
        snapshot['calibrations'],
        credits=credits,
        extra_points=extra_points,
        worker_models=snapshot['worker_models'],
    )
    return snapshot, state


async def decide(
    user_id: str, provider: str, model: str, chat_id: str | None = None, exclude: frozenset = frozenset()
) -> allowance.Decision:
    snapshot, state = await member_state(user_id, provider)
    request = allowance.Request(
        user_id=user_id,
        provider=provider,
        model=model,
        previous_account=await UsageEvents.last_account(user_id, chat_id) if chat_id else None,
        worker_model=snapshot['worker_model'],
        exclude=frozenset(exclude),
    )
    return allowance.decide(request, state)


async def limits_mode() -> str:
    """The mode a request is served in. Unreadable settings never stop a request: observe then."""
    try:
        return await Config.get('mologate.limits_mode', 'observe')
    except Exception as e:
        log.warning('Could not read the limits mode; observing: %s', e)
        return 'observe'


async def decide_now(
    user_id: str, provider: str, model: str, chat_id: str | None = None, exclude: frozenset = frozenset()
) -> allowance.Decision | None:
    """Enforce: decide before the request is sent. None when the engine cannot answer in time; the
    request then goes ahead as in observe, since a broken gate must not stop the team's work."""
    try:
        return await asyncio.wait_for(decide(user_id, provider, model, chat_id, exclude), DECISION_TIMEOUT_SECONDS)
    except Exception as e:
        log.warning('Could not decide a request; it goes ahead unenforced: %s', e)
        return None


def wait_text(seconds: float) -> str:
    """'3 h 12 min', '45 min', '2 d 4 h'."""
    minutes = max(1, -(-int(seconds) // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f'{days} d {hours} h' if hours else f'{days} d'
    if not hours:
        return f'{minutes} min'
    return f'{hours} h {minutes} min' if minutes else f'{hours} h'


def model_name(model_id: str, rows: list) -> str:
    """A model's display name from the accounts' catalogs, else its id."""
    for row in rows:
        for model in row.models or []:
            if model.get('id') == model_id and model.get('name'):
                return model['name']
    return model_id


def swap_notice(decision: allowance.Decision, asked: str, rows: list, now: float | None = None) -> str:
    """What the member reads when their reply uses a worker model instead of the one they picked."""
    now = time.time() if now is None else now
    worker = model_name(decision.model, rows)
    if decision.reason.startswith('No account has room'):
        return f'No account has room for {model_name(asked, rows)} right now, so this reply uses {worker}.'
    text = f'Your share is used up, so this reply uses {worker}.'
    if decision.resets_at and decision.resets_at > now:
        text += f' Resets in {wait_text(decision.resets_at - now)}.'
    return text


PROVIDER_NAMES = {
    'codex': 'ChatGPT',
    'openai_api': 'OpenAI',
    'anthropic_api': 'Anthropic',
    'openrouter': 'OpenRouter',
    'zai': 'Z.ai',
}


async def worker_elsewhere(user, provider: str) -> tuple[str, str] | None:
    """A worker model on another provider that would serve the member now: (model id, its name)."""
    from open_webui.utils.provider_accounts import can_use, model_id

    rows = await ProviderAccounts.list()
    for other in sorted({row.provider for row in rows} - {provider}):
        usable = [row for row in rows if row.provider == other and can_use(row, user)]
        if not usable:
            continue
        worker, _ = await worker_models(other, [to_account(row) for row in usable])
        if not worker:
            continue
        decision = await decide_now(user.id, other, worker)
        if decision is not None and decision.tier != 'empty' and decision.model == worker:
            return model_id(other, worker), f'{model_name(worker, usable)} ({PROVIDER_NAMES.get(other, other)})'
    return None


async def empty_error(user, provider: str, decision: allowance.Decision, now: float | None = None) -> str:
    """Tier 5: the pool has no room. When it frees up, and a worker on another provider meanwhile."""
    now = time.time() if now is None else now
    name = PROVIDER_NAMES.get(provider, provider)
    text = f"The team's shared {name} accounts have no room left for you right now."
    if decision.resets_at and decision.resets_at > now:
        text += f' The first one frees up in {wait_text(decision.resets_at - now)}.'
    try:
        elsewhere = await worker_elsewhere(user, provider)
    except Exception as e:
        log.debug('Could not find a worker on another provider: %s', e)
        elsewhere = None
    if elsewhere:
        text += f' Meanwhile, pick {elsewhere[1]}: it still has room.'
    return text


# Section 8's pace: the last 30 minutes of the member's use, counted from their first request in
# it (at least 5 minutes, so one request does not read as a sprint).
PACE_SECONDS = 30 * 60
MIN_PACE_SECONDS = 5 * 60
# The warning starts at 80% of what the member may use of a model.
WARN_SHARE = 0.8


def _weighted(tokens: dict) -> float:
    """Tokens weighted by typical price ratios, the same as metering's before calibration."""
    return fitting._dot(fitting.PRIOR, fitting.features(tokens))


async def standing(user, provider: str, model: str, now: float | None = None) -> dict:
    """What the member has left of a model, for the context indicator and the warning.

    Per window, everything they may still use of it (``allowance.room_for``): their own accounts'
    reserve still free plus what is left of their share of the pool and its extra, as far as the
    accounts serving the model have room; the level (`ok`, `low` from 80% of it, `empty`), when it
    frees up and, at the last 30 minutes' pace across all their use, about how many minutes of work
    are left. `limit` is the window that runs out first, and `then` what comes after it: a worker
    model, or waiting for it to free up. Also the tier their next request gets and their credits.
    """
    now = time.time() if now is None else now
    snapshot, state = await member_state(user.id, provider)
    request = allowance.Request(user.id, provider, model, worker_model=snapshot['worker_model'])
    decision = allowance.decide(request, state)
    room = allowance.room_for(request, state)
    serving = {account.id for account in state.accounts if account.status == 'ok' and allowance.serves(account, model)}
    # The member's tokens (or dollars, for a budget) in each window's period, on every account of
    # the provider, their own included, and in the last 30 minutes.
    period_amount: dict = {}
    for windows in snapshot['periods'].values():
        for name, period in windows.items():
            if name == 'budget':
                amount = period.cost.get(user.id, 0.0)
            else:
                amount = sum(_weighted(tokens) for tokens in period.tokens.get(user.id, {}).values())
            period_amount[name] = period_amount.get(name, 0.0) + amount
    recent = [
        row for row in await UsageEvents.member_tokens(user.id, int(now - PACE_SECONDS)) if row['provider'] == provider
    ]
    rows = {row.id: row for row in await ProviderAccounts.list() if row.provider == provider}
    windows = {}
    for name, item in room.items():
        allowed, left = item['allowed'], item['left']
        percent = (1 - left / allowed) * 100 if allowed > 0 else 100.0
        level = 'empty' if left <= 0 else 'low' if percent >= WARN_SHARE * 100 else 'ok'
        resets = [
            window.resets_at
            for account_id, account_windows in state.windows.items()
            for window_name, window in account_windows.items()
            if account_id in serving and window_name == name and window.resets_at and window.resets_at > now
        ]
        minutes_left = None
        if recent and period_amount.get(name):
            amount = sum((row['cost'] or 0.0) if name == 'budget' else _weighted(row) for row in recent)
            used_recently = item['used'] * min(1.0, amount / period_amount[name])
            span = max(MIN_PACE_SECONDS, now - min(row['ts'] for row in recent)) / 60
            if used_recently > 0:
                minutes_left = max(0, round(left / (used_recently / span)))
        windows[name] = {
            **item,
            'percent': round(min(100.0, max(0.0, percent)), 1),
            'level': level,
            'resets_at': min(resets) if resets else None,
            'minutes_left': minutes_left,
        }
    # The window that runs out first: the soonest at the recent pace, else the fullest.
    limit = min(
        windows,
        key=lambda name: (
            windows[name]['level'] != 'empty',
            windows[name]['minutes_left'] if windows[name]['minutes_left'] is not None else float('inf'),
            -windows[name]['percent'],
        ),
        default=None,
    )
    worker = snapshot['worker_model']
    if model in state.worker_models:
        # A worker model goes on past the share, as long as the pool has room (tier 4).
        then, then_model = 'continues', model
    elif worker and any(allowance.serves(account, worker) for account in state.accounts):
        then, then_model = 'worker', worker
    else:
        then, then_model = 'wait', None
    credits, extra_points = await extras(user.id)
    return {
        'mode': await limits_mode(),
        'tier': decision.tier,
        'model': decision.model,
        'model_name': model_name(decision.model, list(rows.values())),
        'asked_name': model_name(model, list(rows.values())),
        'swapped': decision.swapped,
        'reason': decision.reason,
        'credits': round(credits, 2),
        'extra_points': extra_points,
        'limit': limit,
        'then': then,
        'then_name': model_name(then_model, list(rows.values())) if then_model else None,
        'windows': windows,
    }


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


async def decision_of(task: asyncio.Task | dict | None) -> dict | None:
    if task is None or isinstance(task, dict):
        return task
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
