"""Phase 2 metering regressions: readings, intervals, outside use and calibration.

Run with an isolated DATA_DIR and test database, never a live database (see test_provider_accounts).
"""

import importlib
import math
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select

from open_webui.internal.db import async_engine, get_async_db_context
from open_webui.models.metering import AccountReading, AccountWindow, MeterInterval, Metering, UsageCalibration
from open_webui.models.usage_events import UsageEvent, UsageEvents
from open_webui.utils import calibration as fitting
from open_webui.utils import metering, usage_ledger
from test_provider_accounts import OTHER, OWNER, DatabaseCase

METERING_TABLES = (
    AccountWindow.__table__,
    AccountReading.__table__,
    MeterInterval.__table__,
    UsageCalibration.__table__,
)
HOUR = 3600
DAY = 24 * HOUR
# A Monday, 00:00 UTC.
START = 1_791_244_800


def reading(at, used, resets_at=None, minutes=None):
    return {'observed_at': at, 'used_percent': used, 'resets_at': resets_at, 'window_minutes': minutes}


def tokens(input_tokens=0, output_tokens=0, cached=0, reasoning=0):
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'cached_tokens': cached,
        'reasoning_tokens': reasoning,
    }


class IntervalTests(unittest.TestCase):
    def test_features_separate_cached_input_and_reasoning(self):
        self.assertEqual(
            fitting.features(tokens(1_000_000, 500_000, cached=250_000, reasoning=100_000)), [0.75, 0.25, 0.4, 0.1]
        )
        # Counts a provider reports wrongly never go negative.
        self.assertEqual(fitting.features(tokens(10, 10, cached=50, reasoning=50)), [0.0, 1e-05, 0.0, 1e-05])

    def test_only_rises_and_new_periods_are_kept(self):
        last = reading(0, 20, resets_at=10_000)
        self.assertTrue(fitting.keeps(None, last))
        self.assertTrue(fitting.keeps(last, reading(1, 21, resets_at=10_000)))
        # Equal or a little lower: a stale reading.
        self.assertFalse(fitting.keeps(last, reading(1, 20, resets_at=10_000)))
        self.assertFalse(fitting.keeps(last, reading(1, 17, resets_at=10_050)))
        # A big drop or a reset time that moved on: the window started over.
        self.assertTrue(fitting.keeps(last, reading(1, 2, resets_at=10_000)))
        self.assertTrue(fitting.keeps(last, reading(1, 20, resets_at=30_000)))

    def test_first_reading_starts_at_its_period(self):
        now = START + 2 * HOUR
        intervals = fitting.build_intervals([reading(START + HOUR, 30, START + 4 * HOUR, 300)], None, now)
        self.assertEqual(
            intervals,
            [
                {
                    'start': START - HOUR,
                    'end': START + HOUR,
                    'used_start': 0.0,
                    'used_end': 30,
                    'rise': 30,
                    'first': True,
                }
            ],
        )

    def test_intervals_span_a_block_and_wait_for_settled_readings(self):
        readings = [reading(START + minute * 60, minute / 5, START + DAY, 10080) for minute in range(0, 50, 5)]
        intervals = fitting.build_intervals(readings, START, START + 46 * 60)
        # Readings every 5 minutes: intervals of 15; the reading at 45 is not settled yet, and the
        # one at 40 waits for the next.
        self.assertEqual([(i['start'] - START, i['end'] - START) for i in intervals], [(0, 900), (900, 1800)])
        self.assertEqual([i['rise'] for i in intervals], [3, 3])
        later = fitting.build_intervals(readings[6:9], START + 1800, START + 3 * HOUR)
        # Nothing new for a while: the last interval closes at the last change.
        self.assertEqual([(i['start'] - START, i['end'] - START) for i in later], [(1800, 2400)])

    def test_a_reset_closes_the_interval_and_starts_from_the_new_period(self):
        readings = [
            reading(START, 10, START + HOUR, 300),
            reading(START + 600, 40, START + HOUR, 300),
            reading(START + 2 * HOUR, 4, START + 6 * HOUR, 300),
        ]
        intervals = fitting.build_intervals(readings, START, START + 3 * HOUR)
        self.assertEqual(
            [(i['start'] - START, i['end'] - START, i['rise']) for i in intervals],
            [(0, 600, 30), (HOUR, 2 * HOUR, 4)],
        )

    def test_a_stale_reading_after_a_higher_one_never_counts_twice(self):
        readings = [reading(START, 10), reading(START + 300, 14), reading(START + 600, 12), reading(START + 1000, 15)]
        intervals = fitting.build_intervals(readings, START, START + HOUR)
        self.assertEqual(sum(interval['rise'] for interval in intervals), 5)

    def test_outside_use_beyond_the_margin(self):
        self.assertIsNone(fitting.detect_outside(5, None))
        self.assertEqual(fitting.detect_outside(3, 0.0), 3)
        self.assertEqual(fitting.detect_outside(0.5, 0.0), 0.0)
        # 20% of what Mologate explains, or at least a point.
        self.assertEqual(fitting.detect_outside(11.5, 10), 0.0)
        self.assertEqual(fitting.detect_outside(13, 10), 3)


def true_units(k, scale, request):
    return scale * sum(a * b for a, b in zip(k, fitting.features(request)))


def synthetic_intervals(rng, count, accounts, k, scales):
    intervals = []
    for index in range(count):
        account_id, weight, true_weight = accounts[index % len(accounts)]
        requests = {}
        used = 0.0
        for _ in range(rng.randint(1, 6)):
            model_id = rng.choice(list(scales))
            request = tokens(
                rng.randint(5_000, 80_000), rng.randint(200, 5_000), cached=0, reasoning=rng.randint(0, 150)
            )
            request['cached_tokens'] = int(request['input_tokens'] * rng.random() * 0.8)
            request['reasoning_tokens'] = int(request['output_tokens'] * rng.random() * 0.6)
            total = requests.setdefault(model_id, tokens())
            for key in total:
                total[key] += request[key]
            used += true_units(k, scales[model_id], request)
        intervals.append(
            {
                'account_id': account_id,
                'rise': used / true_weight,
                'weight': weight,
                'requests': 1,
                'features': requests,
                'first': False,
            }
        )
    return intervals


class FitTests(unittest.TestCase):
    K = [4.0, 0.4, 16.0, 20.0]
    SCALES = {'gpt-6': 1.0, 'gpt-6-mini': 0.25}

    def test_nnls_ridge_keeps_coefficients_non_negative(self):
        rows = [[1, 0], [0, 1], [1, 1]]
        # Unconstrained, k = (2, -1); with k >= 0 the best is (1.5, 0).
        self.assertEqual([round(value, 3) for value in fitting.nnls_ridge(rows, [2, -1, 1], [0, 0], 0.0)], [1.5, 0.0])

    def test_too_few_intervals_fit_nothing(self):
        rng = random.Random(1)
        intervals = synthetic_intervals(rng, fitting.MIN_INTERVALS - 1, [('a', 1, 1)], self.K, self.SCALES)
        self.assertIsNone(fitting.fit(intervals))
        # Intervals without Mologate traffic or from before Mologate never count.
        intervals = synthetic_intervals(rng, 30, [('a', 1, 1)], self.K, self.SCALES)
        self.assertIsNone(fitting.fit([{**interval, 'first': True} for interval in intervals]))
        self.assertIsNone(fitting.fit([{**interval, 'requests': 0} for interval in intervals]))

    def test_fit_recovers_costs_model_scales_and_capacity_weights(self):
        rng = random.Random(7)
        # A is a Plus account (anchor), B a Pro account five times its size that still weighs 1.
        intervals = synthetic_intervals(rng, 160, [('a', 1, 1), ('b', 1, 5)], self.K, self.SCALES)
        calibration = fitting.fit(intervals, anchors={'a'})
        self.assertTrue(calibration['anchored'])
        for model_id, scale in self.SCALES.items():
            for request in (tokens(40_000, 2_000, 10_000, 500), tokens(9_000, 300, 0, 0)):
                self.assertAlmostEqual(
                    fitting.units(calibration, model_id, request) / true_units(self.K, scale, request), 1, delta=0.1
                )
        self.assertLess(calibration['accounts']['a']['error'], 0.02)
        self.assertAlmostEqual(calibration['accounts']['b']['fitted_weight'], 5, delta=0.25)
        self.assertTrue(calibration['accounts']['b']['weight_mismatch'])
        self.assertFalse(calibration['accounts']['a']['weight_mismatch'])
        self.assertLess(calibration['error'], 0.02)
        # A model seen too rarely gets the most expensive known scale.
        self.assertEqual(fitting.scale_of(calibration, 'new-model'), calibration['default_scale'])
        self.assertEqual(calibration['default_scale'], max(calibration['scales'].values()))

    def test_outside_use_is_trimmed_from_the_fit(self):
        rng = random.Random(3)
        intervals = synthetic_intervals(rng, 80, [('a', 1, 1)], self.K, self.SCALES)
        for interval in intervals[::10]:
            interval['rise'] += 8
        calibration = fitting.fit(intervals, anchors={'a'})
        self.assertGreaterEqual(calibration['trimmed'], 8)
        request = tokens(40_000, 2_000, 10_000, 500)
        self.assertAlmostEqual(
            fitting.units(calibration, 'gpt-6', request) / true_units(self.K, 1, request), 1, delta=0.1
        )


class MeteringCase(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with async_engine.begin() as connection:
            for table in METERING_TABLES:
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
                await connection.run_sync(table.create)
        metering._last.clear()
        metering._locks.clear()
        metering._calibrations.clear()
        metering._calibrations_read = 0.0

    async def asyncTearDown(self):
        await metering.flush()
        async with async_engine.begin() as connection:
            for table in METERING_TABLES:
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
        await super().asyncTearDown()

    async def read(self, account_id, windows, at, source='headers'):
        await metering._store(account_id, metering.readings_from(windows), source, at)

    async def ledger(self, account, user_id, at, model_id='gpt-6', request=None, status='ok'):
        request = request or tokens(20_000, 1_000)
        await UsageEvents.insert(
            ts=at,
            user_id=user_id,
            account_id=account.id,
            provider=account.provider,
            model_id=model_id,
            source='chat',
            tier='reserve' if user_id == account.owner_user_id else 'pool',
            status=status,
            **request,
        )

    async def rows(self, model):
        async with get_async_db_context() as db:
            return list((await db.execute(select(model))).scalars().all())


def weekly(used, resets_at, minutes=10080):
    return [{'id': 'secondary', 'used_percent': used, 'resets_at': resets_at, 'window_minutes': minutes}]


class MeteringDatabaseTests(MeteringCase):
    async def test_readings_keep_rises_and_the_latest_window(self):
        account = await self.create_account(provider='codex', plan='plus', sharing='shared')
        resets = START + 5 * DAY
        await self.read(account.id, weekly(10, resets), START)
        await self.read(account.id, weekly(10, resets), START + 30)
        await self.read(account.id, weekly(8, resets), START + 90)
        await self.read(account.id, weekly(12, resets), START + 120)
        await self.read(account.id, weekly(1, resets + 7 * DAY), START + 200)
        # Requests, the Z.ai coding-plan quota and codex-lb's view report other windows too.
        await self.read(account.id, [{'id': 'tokens', 'used_percent': 50}], START + 210)
        readings = await self.rows(AccountReading)
        self.assertEqual([(r.observed_at - START, r.used_percent) for r in readings], [(0, 10), (120, 12), (200, 1)])
        self.assertEqual(readings[0].window, 'weekly')
        [window] = await Metering.windows()
        self.assertEqual(
            (window.used_percent, window.resets_at, window.observed_at), (1, resets + 7 * DAY, START + 200)
        )

        # The stale reading only said the window was still there, never lowered it.
        metering._last.clear()
        await self.read(account.id, weekly(0.5, resets + 7 * DAY), START + 400)
        [window] = await Metering.windows()
        self.assertEqual((window.used_percent, window.observed_at), (1, START + 400))

    async def test_snapshots_from_requests_are_kept(self):
        from open_webui.utils.provider_limits import record_provider_limits

        account = await self.create_account(provider='codex', plan='plus')
        await record_provider_limits(
            None,
            account.id,
            {
                'x-codex-primary-used-percent': '12',
                'x-codex-primary-window-minutes': '300',
                'x-codex-secondary-used-percent': '30',
            },
        )
        await metering.flush()
        readings = sorted((r.window, r.used_percent, r.source) for r in await self.rows(AccountReading))
        self.assertEqual(readings, [('5h', 12, 'headers'), ('weekly', 30, 'headers')])

    async def test_metering_is_off_with_the_limits(self):
        account = await self.create_account(provider='codex', plan='plus')
        with patch('open_webui.models.config.Config.get', AsyncMock(return_value='off')):
            await self.read(account.id, weekly(10, START + DAY), START)
        self.assertEqual(await self.rows(AccountReading), [])

    async def test_rises_without_mologate_requests_are_the_owners(self):
        account = await self.create_account(provider='codex', plan='plus', sharing='shared')
        resets = START + 6 * DAY
        await self.read(account.id, weekly(5, resets), START + 2 * HOUR)
        await self.read(account.id, weekly(9, resets), START + 3 * HOUR)
        # Mologate traffic in the next interval: unknown until calibrated, never guessed.
        await self.ledger(account, OTHER.id, START + 3 * HOUR + 60)
        await self.read(account.id, weekly(11, resets), START + 4 * HOUR)
        self.assertEqual(await metering.process(START + 5 * HOUR), 3)
        intervals = sorted(await self.rows(MeterInterval), key=lambda interval: interval.end)
        self.assertEqual(
            [(i.first, i.rise, i.requests, i.expected, i.outside) for i in intervals],
            # Before Mologate first read it (the period began a day before), then the owner's
            # own use, then a rise with Mologate traffic.
            [(True, 5, 0, 0.0, 5), (False, 4, 0, 0.0, 4), (False, 2, 1, None, None)],
        )
        outside = [row for row in await self.rows(UsageEvent) if row.source == 'outside']
        self.assertEqual(
            sorted((row.user_id, row.window, row.units, row.tier) for row in outside),
            [(OWNER.id, 'weekly', 4, 'owner_outside'), (OWNER.id, 'weekly', 5, 'owner_outside')],
        )
        # The member list counts requests, not outside use.
        self.assertEqual([member['user_id'] for member in await UsageEvents.by_member(account.id, 0)], [OTHER.id])
        # Measured once: running again finds nothing new.
        self.assertEqual(await metering.process(START + 6 * HOUR), 0)

    async def test_calibrated_requests_explain_the_rise(self):
        account = await self.create_account(provider='codex', plan='plus', sharing='shared')
        await Metering.save_calibration(
            'codex',
            'weekly',
            {'k': [100.0, 10.0, 400.0, 400.0], 'scales': {'gpt-6': 1.0}, 'default_scale': 1.0},
            50,
            0.05,
        )
        resets = START + 6 * DAY
        await self.read(account.id, weekly(10, resets), START)
        # 20,000 input and 1,000 output tokens: 2 + 0.4 = 2.4 units each.
        for minute in (2, 6, 10):
            await self.ledger(account, OTHER.id, START + minute * 60)
        await self.read(account.id, weekly(17, resets), START + 20 * 60)
        await self.ledger(account, OWNER.id, START + 22 * 60)
        await self.read(account.id, weekly(27, resets), START + 40 * 60)
        await metering.process(START + 3 * HOUR)
        intervals = sorted(await self.rows(MeterInterval), key=lambda interval: interval.end)
        self.assertEqual(
            [(i.rise, i.requests, round(i.expected, 2), i.outside) for i in intervals if not i.first],
            [(7, 3, 7.2, 0.0), (10, 1, 2.4, 7.6)],
        )

    async def test_member_summary_and_admin_report(self):
        account = await self.create_account(provider='codex', plan='plus', sharing='shared')
        await Metering.save_calibration(
            'codex',
            'weekly',
            {
                'k': [100.0, 10.0, 400.0, 400.0],
                'scales': {'gpt-6': 1.0},
                'default_scale': 1.0,
                'accounts': {account.id: {'intervals': 20, 'error': 0.04, 'observed_percent': 30}},
            },
            20,
            0.04,
        )
        now = START + 2 * HOUR
        await self.read(account.id, weekly(10, now + 3 * DAY), now - HOUR)
        await self.ledger(account, OTHER.id, now - 600)
        await self.ledger(account, OTHER.id, now - 300, model_id='unseen-model')
        summary = await metering.member_summary(OTHER.id, now)
        report = await metering.report(now)
        [entry] = summary['accounts']
        self.assertEqual((entry['requests'], entry['owner'], entry['input_tokens']), (2, False, 40_000))
        weekly_entry = next(item for item in entry['windows'] if item['window'] == 'weekly')
        self.assertEqual(
            (weekly_entry['units'], weekly_entry['percent'], weekly_entry['account_used_percent']), (4.8, 4.8, 10)
        )
        self.assertFalse(next(item for item in entry['windows'] if item['window'] == '5h')['calibrated'])
        self.assertEqual(summary['units'], {'codex': {'weekly': 4.8}})
        owner = await metering.member_summary(OWNER.id, now)
        self.assertEqual(owner['accounts'][0]['windows'][1]['outside_units'], 0)

        [fit] = report['calibrations']
        self.assertEqual((fit['provider'], fit['window'], fit['k']['output']), ('codex', 'weekly', 400.0))
        self.assertTrue(fit['accounts'][0]['within_target'])
        self.assertEqual(report['accounts'][0]['windows'][0]['used_percent'], 10)

    async def test_routes_need_sign_in_and_admin(self):
        from fastapi import HTTPException

        from open_webui.routers import provider_accounts as routes

        self.assertEqual((await routes.my_usage(user=OTHER))['accounts'], [])
        from open_webui.utils.auth import get_admin_user

        with self.assertRaises(HTTPException):
            get_admin_user(user=OTHER)

    async def test_ledger_rows_carry_the_time_the_answer_ended(self):
        account = await self.create_account(provider='codex', plan='plus')
        meter = usage_ledger.Meter(SimpleNamespace(state=SimpleNamespace()), OTHER, {}, 'gpt-6')
        with patch('time.time', return_value=START):
            meter.record(account, 'ok', {'input_tokens': 10, 'output_tokens': 5})
        await usage_ledger.flush()
        [row] = await self.rows(UsageEvent)
        self.assertEqual(row.ts, START)


class SimulatedProvider:
    """A ChatGPT account as the provider sees it: exact use, reported in whole percents."""

    LENGTHS = {'5h': 5 * HOUR, 'weekly': 7 * DAY}

    def __init__(self, capacity, weekly_reset):
        self.capacity = capacity
        self.used = {'5h': 0.0, 'weekly': 0.0}
        self.resets = {'5h': START + 5 * HOUR, 'weekly': weekly_reset}

    def advance(self, now):
        for window, length in self.LENGTHS.items():
            while now >= self.resets[window]:
                self.used[window] = 0.0
                self.resets[window] += length

    def spend(self, now, units):
        self.advance(now)
        for window, value in units.items():
            self.used[window] = min(100.0, self.used[window] + value / self.capacity)

    def windows(self, now):
        self.advance(now)
        return [
            {
                'id': snapshot_id,
                'used_percent': math.floor(self.used[window]),
                'resets_at': self.resets[window],
                'window_minutes': self.LENGTHS[window] // 60,
            }
            for snapshot_id, window in (('primary', '5h'), ('secondary', 'weekly'))
        ]


class SimulationTests(MeteringCase):
    """Six days of a small team on two ChatGPT accounts, with known true costs.

    A is Ann's Plus account (the anchor), B is Bob's Pro account, five times as big but left at the
    default weight of 1. Ann also uses A from her laptop on days 4 to 6. Metering runs observe-only
    over days 1 to 3, calibrates, and measures days 4 to 6 with the fit.
    """

    K = {'5h': [24.0, 2.4, 96.0, 120.0], 'weekly': [4.0, 0.4, 16.0, 20.0]}
    SCALES = {'gpt-6': 1.0, 'gpt-6-mini': 0.25}
    POLL_SECONDS = 10 * 60

    def _setupAsyncioRunner(self):
        # Thousands of database writes: asyncio's debug mode (a stack per callback) would make
        # this test take minutes.
        super()._setupAsyncioRunner()
        self._asyncioRunner.get_loop().set_debug(False)

    async def simulate(self, rng, accounts, start, end, outside_at=()):
        events = []
        for account, provider, users, gap in accounts:
            for day in range(int((end - start) // DAY)):
                at = start + day * DAY + 8 * HOUR
                while at < start + day * DAY + 22 * HOUR:
                    events.append((at, 'request', account, provider, rng.choice(users)))
                    at += rng.randint(gap // 2, gap * 3 // 2)
            for poll in range(int(start), int(end), self.POLL_SECONDS):
                events.append((poll, 'poll', account, provider, None))
        for at, account, provider, units in outside_at:
            events.append((at, 'outside', account, provider, units))
        injected = 0.0
        for at, kind, account, provider, extra in sorted(events, key=lambda event: (event[0], event[1])):
            if kind == 'poll':
                await self.read(account.id, provider.windows(at), at, 'poll')
            elif kind == 'outside':
                provider.spend(at, extra)
                injected += extra['weekly'] / provider.capacity
            else:
                # The answer's headers carry the window before this request; its row is written
                # when it ends, and the provider counts it from then.
                await self.read(account.id, provider.windows(at), at)
                model_id = rng.choice(list(self.SCALES))
                request = tokens(rng.randint(5_000, 60_000), rng.randint(300, 4_000))
                request['cached_tokens'] = int(request['input_tokens'] * rng.random() * 0.8)
                request['reasoning_tokens'] = int(request['output_tokens'] * rng.random() * 0.6)
                await self.ledger(account, extra, at + 20, model_id, request)
                provider.spend(
                    at + 20,
                    {window: true_units(k, self.SCALES[model_id], request) for window, k in self.K.items()},
                )
        return injected

    async def test_a_week_of_traffic_calibrates_within_ten_percent(self):
        rng = random.Random(11)
        ann, bob, cat = OWNER.id, OTHER.id, 'cat'
        a = await self.create_account(provider='codex', plan='plus', sharing='shared', external_account_id='a')
        b = await self.create_account(
            provider='codex', plan='pro', sharing='shared', external_account_id='b', owner_user_id=bob
        )
        provider_a = SimulatedProvider(1, START + 3 * DAY)
        provider_b = SimulatedProvider(5, START + 3 * DAY + 7 * HOUR)
        accounts = [(a, provider_a, [ann, cat], 8 * 60), (b, provider_b, [bob, cat, ann], 3 * 60)]
        # Days 1 to 3: observe, then calibrate.
        await self.simulate(rng, accounts, START, START + 3 * DAY)
        await metering.process(START + 3 * DAY)
        fitted = await metering.calibrate(START + 3 * DAY)
        self.assertEqual(set(fitted), {('codex', '5h'), ('codex', 'weekly')})
        for window in ('5h', 'weekly'):
            calibration = fitted[('codex', window)]
            self.assertTrue(calibration['anchored'])
            self.assertLessEqual(calibration['accounts'][a.id]['error'], metering.TARGET_ERROR, window)
        # B's weight is fitted against A, the Plus account (decision 7's 1x).
        weekly_b = fitted[('codex', 'weekly')]['accounts'][b.id]
        self.assertAlmostEqual(weekly_b['fitted_weight'], 5, delta=0.75)
        self.assertTrue(weekly_b['weight_mismatch'])
        # The mismatched account stays out of the headline error.
        self.assertEqual(fitted[('codex', 'weekly')]['error'], fitted[('codex', 'weekly')]['accounts'][a.id]['error'])

        # Days 4 to 6: Ann also uses A from her laptop, 2.5 weekly units at a time.
        outside = [
            (START + day * DAY + hour * HOUR, a, provider_a, {'weekly': 2.5, '5h': 15.0})
            for day in (3, 4, 5)
            for hour in (7, 12.5, 23)
        ]
        injected = await self.simulate(rng, accounts, START + 3 * DAY, START + 6 * DAY, outside)
        await metering.process(START + 6 * DAY + HOUR)
        rows = [row for row in await self.rows(UsageEvent) if row.source == 'outside' and row.ts > START + 3 * DAY]
        detected = sum(row.units for row in rows if row.account_id == a.id and row.window == 'weekly')
        self.assertTrue(all(row.user_id == ann for row in rows if row.account_id == a.id))
        self.assertAlmostEqual(detected / injected, 1, delta=0.3)
        # B had no outside use. Its weight is still 1, so Mologate's requests explain its rises.
        self.assertLess(sum(row.units for row in rows if row.account_id == b.id and row.window == 'weekly'), 2)

        # The phase 2 bar on the anchor account after the week: within 10%.
        report = await metering.calibrate(START + 6 * DAY + HOUR)
        self.assertLessEqual(report[('codex', 'weekly')]['accounts'][a.id]['error'], metering.TARGET_ERROR)


class MigrationTests(unittest.TestCase):
    def test_metering_migration_round_trip(self):
        ledger = importlib.import_module('open_webui.migrations.versions.a8d4e2c7f915_add_usage_event')
        migration = importlib.import_module('open_webui.migrations.versions.c4e8a2d6f1b9_add_metering')
        self.assertEqual(migration.down_revision, 'b5d9e1f3a7c2')
        target = os.environ.get('TEST_MIGRATION_DATABASE_URL', 'sqlite://')
        if target != 'sqlite://' and 'provider-tests' not in target:
            raise RuntimeError('Use an isolated provider-tests database.')
        engine = create_engine(target)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                ledger.upgrade()
                migration.upgrade()
                names = inspect(connection).get_table_names()
                # The models and the migration agree.
                for table in METERING_TABLES:
                    self.assertIn(table.name, names)
                    columns = {column['name'] for column in inspect(connection).get_columns(table.name)}
                    self.assertEqual(columns, set(table.columns.keys()), table.name)
                    indexes = {index['name'] for index in inspect(connection).get_indexes(table.name)}
                    self.assertEqual(indexes, {index.name for index in table.indexes}, table.name)
                self.assertIn('window', [column['name'] for column in inspect(connection).get_columns('usage_event')])
                migration.downgrade()
                self.assertNotIn('meter_interval', inspect(connection).get_table_names())
                self.assertNotIn(
                    'window', [column['name'] for column in inspect(connection).get_columns('usage_event')]
                )
                ledger.downgrade()
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
