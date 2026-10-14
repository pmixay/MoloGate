"""Mologate: phase 2 metering, observe-only (docs/limits-and-accounts.md, section 8).

- Every reading of an account's 5-hour and weekly windows (from response headers, codex-lb's view
  or Z.ai's quota endpoint) is noted: ``account_window`` keeps the latest, and ``account_reading``
  every rise or new period.
- A loop on one worker polls the accounts every minute, so use outside Mologate shows up even
  while nobody chats here. Every few minutes it cuts the readings into intervals, sets each rise
  against the tokens Mologate sent in it, and writes what its requests do not explain as an
  ``outside`` ledger row for the account's owner (decision 4).
- Once a day it fits each provider window's units per token and the accounts' capacity weights
  (utils/calibration.py). Calibration stays a report: it never changes an account's weight.

Nothing here blocks, delays or changes a request, in any mode; ``MOLOGATE_LIMITS_MODE=off`` turns
it off.
"""

import asyncio
import logging
import time
import uuid

from open_webui.models.config import Config
from open_webui.models.metering import Metering
from open_webui.models.provider_accounts import ProviderAccounts
from open_webui.models.usage_events import UsageEvents
from open_webui.utils import calibration as fitting

log = logging.getLogger(__name__)

POLL_SECONDS = 60
# Z.ai coding plans and OpenRouter keys are read through their own endpoints, less often.
LIMIT_POLL_SECONDS = 5 * 60
PROCESS_SECONDS = 5 * 60
CALIBRATE_SECONDS = 24 * 60 * 60
# Without a calibration yet, try again this often as data comes in.
CALIBRATE_RETRY_SECONDS = 60 * 60
CALIBRATION_DAYS = 14
RETENTION_DAYS = 35
# The latest window is rewritten at most this often when the reading has not changed.
WINDOW_TOUCH_SECONDS = 60
LEADER_KEY = 'mologate:metering:leader'
WORKER_ID = str(uuid.uuid4())
# Base plans weigh 1 by definition (the 1x of decision 7); other plans' weights are fitted
# against them.
BASE_PLANS = {'codex': {'plus'}, 'zai': {'lite'}}
# Calibrations are re-read from the database at most this often.
CALIBRATION_CACHE_SECONDS = 5 * 60

_last: dict[tuple[str, str], dict] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}
_tasks: set = set()
_calibrations: dict[tuple[str, str], dict] = {}
# When calibrations were last read (time.monotonic()); None until the first read.
_calibrations_read: float | None = None


async def enabled() -> bool:
    return await Config.get('mologate.limits_mode', 'observe') != 'off'


def readings_from(windows: list[dict]) -> list[dict]:
    """The 5-hour and weekly windows of a snapshot, as metering keeps them."""
    readings = []
    for window in windows:
        name = fitting.WINDOWS.get(window.get('id'))
        used = fitting._number(window.get('used_percent'))
        if name is None or used is None:
            continue
        resets_at = fitting._number(window.get('resets_at'))
        minutes = fitting._number(window.get('window_minutes'))
        readings.append(
            {
                'window': name,
                'used_percent': max(0.0, min(100.0, used)),
                'resets_at': int(resets_at) if resets_at else None,
                'window_minutes': int(minutes) if minutes else None,
            }
        )
    return readings


def note_reading(account_id: str, windows: list[dict], source: str = 'poll') -> None:
    """Keep a reading of an account's windows, in the background. Never raises or waits."""
    readings = readings_from(windows)
    if not readings:
        return
    try:
        task = asyncio.create_task(_store(account_id, readings, source, int(time.time())))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _store(account_id: str, readings: list[dict], source: str, now: int) -> None:
    try:
        if not await enabled():
            return
        for reading in readings:
            key = (account_id, reading['window'])
            async with _locks.setdefault(key, asyncio.Lock()):
                await _store_one(account_id, reading, source, now)
    except Exception as e:
        log.warning('Could not keep an account reading: %s', e)


async def _store_one(account_id: str, reading: dict, source: str, now: int) -> None:
    key = (account_id, reading['window'])
    last = _last.get(key)
    if last is None:
        row = await Metering.latest_reading(*key)
        if row is not None:
            last = {
                'used_percent': row.used_percent,
                'resets_at': row.resets_at,
                'window_minutes': row.window_minutes,
                'touched': 0,
            }
    if fitting.keeps(last, reading):
        await Metering.add_reading(
            account_id=account_id,
            window=reading['window'],
            used_percent=reading['used_percent'],
            resets_at=reading['resets_at'],
            window_minutes=reading['window_minutes'],
            source=source,
            observed_at=now,
        )
        current = {**reading}
    elif now - last.get('touched', 0) >= WINDOW_TOUCH_SECONDS:
        # A lower reading is a stale one: the window stays where it was, only seen again.
        current = {**last, 'resets_at': last.get('resets_at') or reading['resets_at']}
        current['window_minutes'] = last.get('window_minutes') or reading['window_minutes']
    else:
        return
    await Metering.save_window(
        account_id,
        reading['window'],
        used_percent=current['used_percent'],
        resets_at=current.get('resets_at'),
        window_minutes=current.get('window_minutes'),
        source=source,
        observed_at=now,
    )
    _last[key] = {
        'used_percent': current['used_percent'],
        'resets_at': current.get('resets_at'),
        'window_minutes': current.get('window_minutes'),
        'touched': now,
    }


async def flush() -> None:
    """Wait for pending reading writes (tests, shutdown)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def calibrations(refresh: bool = False) -> dict[tuple[str, str], dict]:
    """Each provider window's latest fit, {(provider, window): values}."""
    global _calibrations_read
    if refresh or _calibrations_read is None or time.monotonic() - _calibrations_read > CALIBRATION_CACHE_SECONDS:
        try:
            rows = await Metering.calibrations()
        except Exception as e:
            log.debug('Could not read calibrations: %s', e)
            return _calibrations
        _calibrations.clear()
        for row in rows:
            _calibrations[(row.provider, row.window)] = {**(row.values or {}), 'fitted_at': row.fitted_at}
        _calibrations_read = time.monotonic()
    return _calibrations


async def measure(account, window: str, interval: dict, calibration: dict | None) -> dict:
    """Set one interval's rise against Mologate's requests in it; write the unexplained rest as
    the owner's use outside Mologate."""
    requests, tokens = await UsageEvents.tokens_by_model(account.id, interval['start'], interval['end'])
    weight = account.capacity_weight or 1.0
    expected = fitting.expected_percent(calibration, tokens, weight)
    outside = fitting.detect_outside(interval['rise'], expected)
    event_id = None
    if outside:
        event_id = await UsageEvents.insert(
            ts=interval['end'],
            # A team account has no owner: its outside use is nobody's.
            user_id=account.owner_user_id or '',
            account_id=account.id,
            provider=account.provider,
            model_id='',
            source='outside',
            tier='owner_outside',
            units=round(outside * weight, 4),
            window=window,
            status='ok',
        )
    values = dict(
        account_id=account.id,
        provider=account.provider,
        window=window,
        start=interval['start'],
        end=interval['end'],
        used_start=interval['used_start'],
        used_end=interval['used_end'],
        rise=interval['rise'],
        weight=weight,
        requests=requests,
        features=tokens or None,
        first=interval['first'],
        expected=round(expected, 4) if expected is not None else None,
        outside=outside,
        outside_event_id=event_id,
    )
    await Metering.add_interval(**values)
    return values


async def process(now: float | None = None) -> int:
    """Measure every account window's new intervals. Returns how many were measured."""
    now = time.time() if now is None else now
    accounts = {account.id: account for account in await ProviderAccounts.list()}
    fits = await calibrations(refresh=True)
    measured = 0
    for account_id, window in await Metering.reading_keys():
        account = accounts.get(account_id)
        if account is None:
            continue
        last = await Metering.last_interval(account_id, window)
        last_end = last.end if last is not None else None
        rows = await Metering.readings(account_id, window, last_end if last_end is not None else 0)
        readings = [
            {
                'observed_at': row.observed_at,
                'used_percent': row.used_percent,
                'resets_at': row.resets_at,
                'window_minutes': row.window_minutes,
            }
            for row in rows
        ]
        for interval in fitting.build_intervals(readings, last_end, now):
            await measure(account, window, interval, fits.get((account.provider, window)))
            measured += 1
    await Metering.prune(int(now - RETENTION_DAYS * 86400))
    return measured


async def calibrate(now: float | None = None) -> dict[tuple[str, str], dict]:
    """Fit every provider window that has enough intervals from the last two weeks."""
    now = time.time() if now is None else now
    accounts = await ProviderAccounts.list()
    groups: dict[tuple[str, str], list[dict]] = {}
    for interval in await Metering.intervals(int(now - CALIBRATION_DAYS * 86400)):
        groups.setdefault((interval.provider, interval.window), []).append(
            {
                'account_id': interval.account_id,
                'rise': interval.rise,
                'weight': interval.weight,
                'requests': interval.requests,
                'features': interval.features or {},
                'first': interval.first,
            }
        )
    fitted = {}
    for (provider, window), intervals in groups.items():
        anchors = {
            account.id
            for account in accounts
            if account.provider == provider and account.plan in BASE_PLANS.get(provider, ())
        }
        result = fitting.fit(intervals, anchors)
        if result is None:
            continue
        await Metering.save_calibration(provider, window, result, result['samples'], result['error'])
        fitted[(provider, window)] = result
    await calibrations(refresh=True)
    return fitted


async def poll(redis) -> None:
    """Read every account's 5-hour and weekly windows: codex-lb's view of ChatGPT accounts (it
    reads them from OpenAI every minute), and Z.ai coding plans' quota endpoint. OpenRouter keys
    have no such windows."""
    from open_webui.utils import codex_accounts
    from open_webui.utils.provider_accounts import refresh_account_limits, reports_limits

    await codex_accounts.sync_accounts(redis)
    for account in await ProviderAccounts.list():
        if account.provider == 'zai' and reports_limits(account) and account.status == 'ok':
            await refresh_account_limits(account, redis, max_age=LIMIT_POLL_SECONDS)


async def lead(redis) -> bool:
    """One worker meters; it holds the lead while it keeps renewing it."""
    if redis is None:
        return True
    try:
        if await redis.set(LEADER_KEY, WORKER_ID, nx=True, ex=POLL_SECONDS * 3):
            return True
        holder = await redis.get(LEADER_KEY)
        if (holder.decode() if isinstance(holder, bytes) else holder) == WORKER_ID:
            await redis.expire(LEADER_KEY, POLL_SECONDS * 3)
            return True
    except Exception as e:
        log.debug('Metering lead unavailable: %s', e)
    return False


async def calibration_due(now: float, last_attempt: float) -> bool:
    fits = await calibrations()
    if fits:
        return now - min(fit.get('fitted_at', 0) for fit in fits.values()) >= CALIBRATE_SECONDS
    return now - last_attempt >= CALIBRATE_RETRY_SECONDS


async def run_once(redis, state: dict, now: float | None = None) -> None:
    now = time.time() if now is None else now
    await poll(redis)
    if now - state.get('processed', 0) >= PROCESS_SECONDS:
        await process(now)
        state['processed'] = now
    if await calibration_due(now, state.get('calibrated', 0)):
        state['calibrated'] = now
        await calibrate(now)


async def loop(app) -> None:
    state: dict = {}
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            if not await enabled():
                continue
            redis = getattr(app.state, 'redis', None)
            if await lead(redis):
                await run_once(redis, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning('Metering round failed: %s', e)


ROLLING_SECONDS = {'5h': 5 * 60 * 60, 'weekly': 7 * 24 * 60 * 60}
TARGET_ERROR = 0.10


def window_since(current, window: str, now: float) -> int:
    """When an account window's current period began, or a rolling stretch while unknown."""
    if current is not None and current.resets_at and current.resets_at > now and current.window_minutes:
        return int(current.resets_at - current.window_minutes * 60)
    return int(now - ROLLING_SECONDS[window])


async def member_summary(user_id: str, now: float | None = None) -> dict:
    """A member's own numbers, read-only: their requests, tokens and cost this week, and the units
    of each account window they used in its current period (once calibrated). Owners also see their
    use outside Mologate."""
    now = time.time() if now is None else now
    accounts = {account.id: account for account in await ProviderAccounts.list()}
    latest = {(window.account_id, window.window): window for window in await Metering.windows()}
    fits = await calibrations()
    rows = await UsageEvents.member_tokens(user_id, int(now - ROLLING_SECONDS['weekly']))
    by_account: dict[str, list[dict]] = {}
    for row in rows:
        by_account.setdefault(row['account_id'], []).append(row)
    owned = {account_id for account_id, account in accounts.items() if account.owner_user_id == user_id}
    totals: dict[str, dict[str, float]] = {}
    entries = []
    for account_id in sorted(set(by_account) | owned):
        account = accounts.get(account_id)
        if account is None:
            continue
        own_rows = by_account.get(account_id, [])
        costs = [row['cost'] for row in own_rows if row['cost'] is not None]
        entry = {
            'id': account.id,
            'label': account.label,
            'provider': account.provider,
            'plan': account.plan,
            'owner': account_id in owned,
            'requests': len(own_rows),
            'input_tokens': sum(row['input_tokens'] for row in own_rows),
            'output_tokens': sum(row['output_tokens'] for row in own_rows),
            'cost': round(sum(costs), 4) if costs else None,
            'windows': [],
        }
        if account.plan != 'api':
            weight = account.capacity_weight or 1.0
            for window in ROLLING_SECONDS:
                current = latest.get((account_id, window))
                since = window_since(current, window, now)
                fit = fits.get((account.provider, window))
                used = None
                if fit is not None:
                    used = sum(
                        fitting.units(fit, row['model_id'], row) or 0.0 for row in own_rows if row['ts'] >= since
                    )
                    provider_totals = totals.setdefault(account.provider, {})
                    provider_totals[window] = round(provider_totals.get(window, 0.0) + used, 4)
                item = {
                    'window': window,
                    'since': since,
                    'resets_at': current.resets_at if current is not None else None,
                    'account_used_percent': current.used_percent if current is not None else None,
                    'calibrated': fit is not None,
                    'units': round(used, 4) if used is not None else None,
                    'percent': round(used / weight, 3) if used is not None else None,
                }
                if account_id in owned:
                    outside = (await UsageEvents.outside_units([account_id], since)).get(account_id, {})
                    units = outside.get(window, 0.0)
                    item['outside_units'] = round(units, 4)
                    item['outside_percent'] = round(units / weight, 3)
                entry['windows'].append(item)
        entries.append(entry)
    entries.sort(key=lambda entry: (-entry['requests'], entry['label']))
    return {'accounts': entries, 'units': totals, 'since': int(now - ROLLING_SECONDS['weekly'])}


async def report(now: float | None = None) -> dict:
    """For admins: each provider window's calibration against the phase 2 bar (ledger units within
    10% of the provider's percent changes per account), and each account's latest windows,
    intervals and outside use this week."""
    now = time.time() if now is None else now
    accounts = {account.id: account for account in await ProviderAccounts.list()}
    fits = await calibrations(refresh=True)
    label = lambda account_id: accounts[account_id].label if account_id in accounts else 'Removed account'  # noqa: E731
    result = []
    for (provider, window), fit in sorted(fits.items()):
        entries = []
        for account_id, entry in (fit.get('accounts') or {}).items():
            error = entry.get('error')
            entries.append(
                {
                    'account_id': account_id,
                    'label': label(account_id),
                    **entry,
                    'within_target': error is not None and error <= TARGET_ERROR,
                }
            )
        result.append(
            {
                'provider': provider,
                'window': window,
                'fitted_at': fit.get('fitted_at'),
                'samples': fit.get('samples'),
                'trimmed': fit.get('trimmed'),
                'anchored': fit.get('anchored'),
                'error': fit.get('error'),
                'k': dict(zip(fitting.FEATURES, fit.get('k') or [])),
                'scales': fit.get('scales') or {},
                'default_scale': fit.get('default_scale'),
                'accounts': entries,
            }
        )
    week = int(now - ROLLING_SECONDS['weekly'])
    measured: dict[str, dict] = {}
    for interval in await Metering.intervals(week):
        item = measured.setdefault(interval.account_id, {}).setdefault(
            interval.window, {'intervals': 0, 'rise': 0.0, 'outside': 0.0, 'unknown': 0}
        )
        item['intervals'] += 1
        item['rise'] = round(item['rise'] + interval.rise, 3)
        item['outside'] = round(item['outside'] + (interval.outside or 0.0), 3)
        item['unknown'] += interval.outside is None
    latest: dict[str, list] = {}
    for window in await Metering.windows():
        latest.setdefault(window.account_id, []).append(
            {
                'window': window.window,
                'used_percent': window.used_percent,
                'resets_at': window.resets_at,
                'observed_at': window.observed_at,
                'source': window.source,
            }
        )
    return {
        'mode': await Config.get('mologate.limits_mode', 'observe'),
        'target_error': TARGET_ERROR,
        'calibrations': result,
        'accounts': [
            {
                'id': account.id,
                'label': account.label,
                'provider': account.provider,
                'plan': account.plan,
                'capacity_weight': account.capacity_weight,
                'windows': sorted(latest.get(account.id, []), key=lambda item: item['window']),
                'week': measured.get(account.id, {}),
            }
            for account in accounts.values()
            if account.plan != 'api'
        ],
    }
