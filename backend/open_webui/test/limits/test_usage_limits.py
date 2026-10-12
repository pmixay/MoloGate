"""Limits regressions: key credit limits, limit outlook, concurrency queue and the usage ledger.

Run with an isolated DATA_DIR and test database, never a live database (see test_provider_accounts).
"""

import asyncio
import contextlib
import os
import time
import unittest
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from sqlalchemy import select

from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_accounts import seal_secret
from open_webui.models.usage_events import UsageEvent, UsageEvents
from open_webui.routers import provider_accounts as routes
from open_webui.utils import chat_slots, model_prices, provider_accounts, usage_ledger
from open_webui.utils import provider_account_proxy as proxy
from open_webui.utils.provider_limits import (
    account_rates,
    burn_rate,
    get_provider_limits,
    limit_outlook,
    parse_openrouter_key,
    period_end,
    record_provider_limits,
    record_provider_snapshot,
    snapshot_exhausted,
    track_reading,
)
from test_provider_accounts import (
    OTHER,
    OWNER,
    DatabaseCase,
    Response,
    account,
    chat_chunk,
    chat_reply,
    read_stream,
)


def utc(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp())


def session_returning(get):
    """An aiohttp.ClientSession whose get() is the given function."""
    manager = Response()
    manager.__class__ = type(
        'SessionContext', (Response,), {'__aenter__': AsyncMock(return_value=SimpleNamespace(get=get))}
    )
    return manager


class OpenRouterKeyTests(unittest.IsolatedAsyncioTestCase):
    def test_periods_end_at_midnight_utc(self):
        # Thursday 2026-10-15 13:20 UTC.
        now = utc(2026, 10, 15, 13, 20)
        self.assertEqual(period_end('daily', now), utc(2026, 10, 16))
        self.assertEqual(period_end('weekly', now), utc(2026, 10, 19))
        self.assertEqual(period_end('monthly', now), utc(2026, 11, 1))
        self.assertEqual(period_end('monthly', utc(2026, 12, 31, 23)), utc(2027, 1, 1))
        # A week runs Monday to Sunday: on a Monday it ends next Monday.
        self.assertEqual(period_end('weekly', utc(2026, 10, 19, 0, 1)), utc(2026, 10, 26))
        self.assertIsNone(period_end(None, now))

    def test_credit_limit_and_budget_come_from_the_key(self):
        now = utc(2026, 10, 15, 13, 20)
        info = {'limit': 40, 'limit_remaining': 10, 'limit_reset': 'weekly', 'usage_monthly': 12.5}
        windows = {window['id']: window for window in parse_openrouter_key(info, {'amount': 50}, now)}
        self.assertEqual(windows['credits']['used_percent'], 75)
        self.assertEqual((windows['credits']['limit'], windows['credits']['remaining']), (40, 10))
        self.assertEqual(windows['credits']['resets_at'], utc(2026, 10, 19))
        self.assertEqual(windows['credits']['unit'], 'usd')
        self.assertEqual(windows['budget']['used_percent'], 25)
        self.assertEqual((windows['budget']['remaining'], windows['budget']['spent']), (37.5, 12.5))
        self.assertEqual(windows['budget']['resets_at'], utc(2026, 11, 1))
        # No limit on the key and no budget: nothing is invented.
        self.assertEqual(parse_openrouter_key({'limit': None, 'limit_remaining': None, 'usage_monthly': 3}, None), [])
        # Spent past the budget: full, but the owner's budget never makes the key count as empty.
        over = parse_openrouter_key({'usage_monthly': 80}, {'amount': 50}, now)
        self.assertEqual(over[0]['used_percent'], 100)
        self.assertFalse(snapshot_exhausted({'windows': over}, now))
        spent = parse_openrouter_key({'limit': 10, 'limit_remaining': 0, 'limit_reset': 'daily'}, None, now)
        self.assertTrue(snapshot_exhausted({'windows': spent}, now))

    async def test_header_and_key_readings_keep_each_others_windows(self):
        account_id = 'merge-' + uuid.uuid4().hex
        headers = {'x-ratelimit-limit': '20', 'x-ratelimit-remaining': '15'}
        await record_provider_limits(None, account_id, headers)
        key_windows = parse_openrouter_key({'limit': 40, 'limit_remaining': 30, 'usage_monthly': 10}, {'amount': 20})
        await record_provider_snapshot(None, account_id, key_windows, source='key')
        ids = lambda snapshot: sorted(window['id'] for window in snapshot['windows'])  # noqa: E731
        self.assertEqual(ids(await get_provider_limits(None, account_id)), ['budget', 'credits', 'requests'])
        # A new header reading replaces the old header windows only.
        await record_provider_limits(None, account_id, {'x-ratelimit-limit': '20', 'x-ratelimit-remaining': '5'})
        snapshot = await get_provider_limits(None, account_id)
        self.assertEqual(ids(snapshot), ['budget', 'credits', 'requests'])
        self.assertEqual(next(w for w in snapshot['windows'] if w['id'] == 'requests')['remaining'], 5)
        # A key without a limit any more drops its credit window, and keeps the header window.
        await record_provider_snapshot(
            None, account_id, parse_openrouter_key({'usage_monthly': 11}, {'amount': 20}), source='key'
        )
        self.assertEqual(ids(await get_provider_limits(None, account_id)), ['budget', 'requests'])

    async def test_key_limits_are_read_with_the_key_and_kept_current(self):
        seen = []

        def get(url, **kwargs):
            seen.append((url, kwargs['headers']))
            self.assertFalse(kwargs['allow_redirects'])
            data = {'limit': 25, 'limit_remaining': 20, 'limit_reset': 'monthly', 'usage_monthly': 5}
            return Response({'data': data})

        key = account(
            id='openrouter-' + uuid.uuid4().hex,
            provider='openrouter',
            plan='api',
            budget={'amount': 10, 'currency': 'USD'},
            secret=seal_secret({'api_key': 'sk-or-test', 'base_url': 'https://openrouter.ai/api/v1'}),
        )
        with patch('open_webui.utils.provider_accounts.aiohttp.ClientSession', return_value=session_returning(get)):
            await provider_accounts.refresh_account_limits(key, None, max_age=60)
            # A reading less than a minute old is kept.
            await provider_accounts.refresh_account_limits(key, None, max_age=60)
        self.assertEqual(seen, [(provider_accounts.OPENROUTER_KEY_URL, {'Authorization': 'Bearer sk-or-test'})])
        windows = {w['id']: w for w in (await get_provider_limits(None, key.id))['windows']}
        self.assertAlmostEqual(windows['credits']['used_percent'], 20)
        self.assertAlmostEqual(windows['budget']['used_percent'], 50)
        # Keys of other providers have no such endpoint.
        with patch('open_webui.utils.provider_accounts.aiohttp.ClientSession') as session:
            await provider_accounts.refresh_account_limits(account(provider='openai_api', plan='api'), None)
        session.assert_not_called()
        self.assertTrue(provider_accounts.reports_limits(account(provider='zai', plan='lite')))
        self.assertFalse(provider_accounts.reports_limits(account(provider='zai', plan='api')))


def five_hour(used, resets_in=3 * 3600, now=0):
    return {'id': 'primary', 'used_percent': used, 'window_minutes': 300, 'resets_at': now + resets_in}


class OutlookTests(unittest.IsolatedAsyncioTestCase):
    def test_readings_are_kept_per_minute_and_start_over_with_the_window(self):
        history = {}
        track_reading(history, [five_hour(10, now=0), {'id': 'requests', 'used_percent': 5}], 0)
        track_reading(history, [five_hour(11, now=0)], 20)
        # Readings within a minute of each other collapse into the latest.
        self.assertEqual([point[:2] for point in history['primary']], [[20, 11]])
        track_reading(history, [five_hour(14, now=0)], 90)
        track_reading(history, [five_hour(13, now=0)], 200)
        # A slightly lower reading is a stale one; short windows are not tracked.
        self.assertEqual([point[1] for point in history['primary']], [11, 14, 14])
        self.assertNotIn('requests', history)
        # A reset (the reset time moved on, or a big drop) starts the history over.
        track_reading(history, [five_hour(2, resets_in=5 * 3600, now=4000)], 4000)
        self.assertEqual([point[1] for point in history['primary']], [2])
        # Readings older than two hours are dropped.
        track_reading(history, [five_hour(3, resets_in=5 * 3600, now=4000)], 4000 + 3 * 3600)
        self.assertEqual(len(history['primary']), 1)

    def test_pace_is_the_rise_over_the_last_half_hour(self):
        now = 10_000
        self.assertIsNone(burn_rate([], now))
        # One reading tells no pace; nothing read in the last half hour means no recent use.
        self.assertIsNone(burn_rate([[now - 60, 50, None]], now))
        self.assertEqual(burn_rate([[now - 3600, 50, None]], now), 0)
        points = [[now - 3600, 20, None], [now - 1900, 30, None], [now - 600, 36, None], [now - 60, 42, None]]
        # From the last reading before the half hour (30 at -1900 s) to 42: 12 points in ~31.7 min.
        self.assertAlmostEqual(burn_rate(points, now), 12 / (1900 / 60))
        # Readings seconds apart are measured over at least five minutes.
        self.assertAlmostEqual(burn_rate([[now - 30, 40, None], [now - 10, 42, None]], now), 2 / 5)

    def test_accounts_that_take_over_from_each_other_add_up(self):
        now = 0
        low = {'weight': 1, 'windows': [five_hour(85)], 'rates': {}}
        # One account 85% used, pace unknown: low, with no estimate.
        (outlook,) = limit_outlook([low], now)
        self.assertEqual((outlook['level'], outlook['minutes_left']), ('low', None))
        # A second account with room: together 47.5% used, nothing to warn about.
        fresh = {'weight': 1, 'windows': [five_hour(10)], 'rates': {}}
        (outlook,) = limit_outlook([low, fresh], now)
        self.assertEqual((outlook['level'], outlook['used_percent']), ('ok', 47.5))
        # At 1.5 points a minute on each, the 105 points left last 35 minutes: before the reset.
        busy = [{**entry, 'rates': {'primary': 1.5}} for entry in (low, fresh)]
        (outlook,) = limit_outlook(busy, now)
        self.assertEqual((outlook['minutes_left'], outlook['level']), (35, 'low'))
        self.assertFalse(outlook['lasts_until_reset'])
        # The same pace with the window refilling in 20 minutes: it lasts, no warning.
        soon = [{**entry, 'windows': [five_hour(entry['windows'][0]['used_percent'], 1200)]} for entry in busy]
        (outlook,) = limit_outlook(soon, now)
        self.assertEqual(outlook['level'], 'ok')
        self.assertTrue(outlook['lasts_until_reset'])
        # A bigger account counts for more; an empty one adds nothing.
        empty = {'weight': 1, 'windows': [five_hour(100)], 'rates': {}}
        pro = {'weight': 3, 'windows': [five_hour(50)], 'rates': {}}
        (outlook,) = limit_outlook([empty, pro], now)
        self.assertEqual(outlook['used_percent'], 62.5)
        (outlook,) = limit_outlook([empty], now)
        self.assertEqual(outlook['level'], 'empty')
        # Accounts that report nothing have no outlook.
        self.assertEqual(limit_outlook([{'weight': 1, 'windows': [], 'rates': {}}], now), [])

    async def test_readings_give_each_account_its_pace(self):
        account_id = 'pace-' + uuid.uuid4().hex
        start = time.time()
        await record_provider_snapshot(None, account_id, [five_hour(10, now=start)])
        self.assertIsNone((await account_rates(None, account_id))['primary'])
        with patch('open_webui.utils.provider_limits.time.time', return_value=start + 600):
            await record_provider_snapshot(None, account_id, [five_hour(15, now=start)])
            rates = await account_rates(None, account_id)
        # 5 points in 10 minutes.
        self.assertAlmostEqual(rates['primary'], 0.5, places=2)

    async def test_model_limits_show_the_account_a_request_would_use(self):
        from open_webui.routers.models import get_model_limits

        own_empty = account(id='own-empty-' + uuid.uuid4().hex, label='Account #Empty1')
        shared = account(
            id='shared-' + uuid.uuid4().hex, owner_user_id=OTHER.id, sharing='shared', label='Account #Room22'
        )
        await record_provider_snapshot(None, own_empty.id, [five_hour(100, now=time.time())])
        await record_provider_snapshot(None, shared.id, [five_hour(40, now=time.time())])
        model_id = 'account:codex:test-model'
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={model_id: {'id': model_id}}))
        )
        with (
            patch(
                'open_webui.utils.provider_accounts.ProviderAccounts.list', AsyncMock(return_value=[own_empty, shared])
            ),
            patch('open_webui.models.config.Config.get', AsyncMock(return_value='observe')),
        ):
            limits = await get_model_limits(request, model_id, OWNER)
        # The owner's own account comes first, but it is empty, so the request goes to the shared one.
        self.assertEqual(limits['account']['label'], 'Account #Room22')
        self.assertEqual(limits['windows'][0]['used_percent'], 40)
        self.assertEqual(limits['accounts'], 2)
        self.assertEqual(limits['outlook'][0]['used_percent'], 70)
        self.assertEqual(limits['outlook'][0]['level'], 'ok')


def chat(chat_id, **overrides):
    """The metadata of a chat a member sends from the app."""
    return {'session_id': 'session', 'chat_id': chat_id, 'user_message': {'id': 'm', 'meta': {}}} | overrides


class ChatSlotTests(unittest.IsolatedAsyncioTestCase):
    """The per-member cap on chats generating at once, kept in memory (no Redis)."""

    redis = None

    async def asyncSetUp(self):
        self.mode = 'enforce'
        self.limit = 2
        self.user = 'member-' + uuid.uuid4().hex

        async def config(key, default=None):
            return {'mologate.limits_mode': self.mode, 'mologate.max_concurrent_chats': self.limit}.get(key, default)

        self.config = patch('open_webui.utils.chat_slots.Config.get', side_effect=config)
        self.config.start()
        self.poll = patch('open_webui.utils.chat_slots.POLL_SECONDS', 0.01)
        self.poll.start()

    async def asyncTearDown(self):
        self.poll.stop()
        self.config.stop()

    def test_only_chats_the_member_sends_take_a_slot(self):
        self.assertTrue(chat_slots.counts(chat('c1')))
        self.assertFalse(chat_slots.counts(chat('c1', internal=True)))
        self.assertFalse(chat_slots.counts(chat('c1', automation_id='a1')))
        self.assertFalse(chat_slots.counts(chat('channel:c1')))
        self.assertFalse(chat_slots.counts(chat('c1', session_id=None)))
        # Background results, timers and agent messages start turns with an internal message.
        self.assertFalse(chat_slots.counts(chat('c1', user_message={'meta': {'internal': True, 'type': 'timer'}})))

    async def test_limit_has_a_hard_cap(self):
        self.limit = 50
        self.assertEqual(await chat_slots.chat_limit(), 10)
        self.limit = 0
        self.assertEqual(await chat_slots.chat_limit(), 1)
        self.limit = 'many'
        self.assertEqual(await chat_slots.chat_limit(), 3)

    async def test_chats_past_the_cap_wait_in_line_in_order(self):
        first = await chat_slots.acquire(self.redis, self.user, 'c1')
        second = await chat_slots.acquire(self.redis, self.user, 'c2')
        seen = {'c3': [], 'c4': []}

        def notice(chat_id):
            async def on_wait(place, active, limit):
                seen[chat_id].append((place, active, limit))

            return on_wait

        third = asyncio.create_task(chat_slots.acquire(self.redis, self.user, 'c3', notice('c3')))
        await asyncio.sleep(0.05)
        fourth = asyncio.create_task(chat_slots.acquire(self.redis, self.user, 'c4', notice('c4')))
        await asyncio.sleep(0.05)
        self.assertFalse(third.done() or fourth.done())
        self.assertEqual(seen, {'c3': [(0, 2, 2)], 'c4': [(1, 2, 2)]})
        # A slot frees up: the first in line takes it, and the next moves up.
        await chat_slots.release(self.redis, first)
        third_slot = await asyncio.wait_for(third, 1)
        await asyncio.sleep(0.05)
        self.assertFalse(fourth.done())
        self.assertEqual(seen['c3'][-1], (None, 0, 2))
        self.assertEqual(seen['c4'][-1], (0, 2, 2))
        await chat_slots.release(self.redis, second)
        fourth_slot = await asyncio.wait_for(fourth, 1)
        for slot in (third_slot, fourth_slot):
            await chat_slots.release(self.redis, slot)
        self.assertEqual(await chat_slots.active_count(self.redis, self.user), 0)

    async def test_stopping_a_waiting_chat_leaves_the_line(self):
        held = [await chat_slots.acquire(self.redis, self.user, f'c{index}') for index in range(2)]
        waiting = asyncio.create_task(chat_slots.acquire(self.redis, self.user, 'stopped'))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        later = asyncio.create_task(chat_slots.acquire(self.redis, self.user, 'later'))
        await chat_slots.release(self.redis, held[0])
        # The stopped chat is no longer ahead of the later one.
        slot = await asyncio.wait_for(later, 1)
        for item in (held[1], slot):
            await chat_slots.release(self.redis, item)

    async def test_models_answering_in_one_chat_share_its_slot(self):
        one = await chat_slots.acquire(self.redis, self.user, 'multi')
        two = await chat_slots.acquire(self.redis, self.user, 'multi')
        self.assertEqual(await chat_slots.active_count(self.redis, self.user), 1)
        await chat_slots.release(self.redis, one)
        self.assertEqual(await chat_slots.active_count(self.redis, self.user), 1)
        await chat_slots.release(self.redis, two)
        self.assertEqual(await chat_slots.active_count(self.redis, self.user), 0)

    async def test_a_slot_left_behind_expires(self):
        now = time.time()
        for index in range(2):
            await chat_slots.try_acquire(self.redis, self.user, f'lost{index}', 2, now, now)
        self.assertEqual(await chat_slots.try_acquire(self.redis, self.user, 'next', 2, now, now), 0)
        later = now + chat_slots.LEASE_SECONDS + 1
        self.assertEqual(await chat_slots.try_acquire(self.redis, self.user, 'next', 2, now, later), -1)

    async def test_observe_mode_never_makes_a_chat_wait(self):
        self.mode = 'observe'
        slots = [await asyncio.wait_for(chat_slots.acquire(self.redis, self.user, f'c{i}'), 1) for i in range(4)]
        self.assertEqual(await chat_slots.active_count(self.redis, self.user), 4)
        for slot in slots:
            await chat_slots.release(self.redis, slot)
        self.mode = 'off'
        self.assertIsNone(await chat_slots.acquire(self.redis, self.user, 'c9'))

    async def test_broken_storage_never_blocks_a_chat(self):
        broken = SimpleNamespace(eval=AsyncMock(side_effect=OSError), zcount=AsyncMock(side_effect=OSError))
        self.assertIsNone(await chat_slots.acquire(broken, self.user, 'c1'))


def redis_client():
    """A Redis for the Lua path: TEST_REDIS_URL, else fakeredis with Lua, else none (skipped)."""
    if url := os.environ.get('TEST_REDIS_URL'):
        from redis.asyncio import Redis

        return Redis.from_url(url, decode_responses=True)
    try:
        import fakeredis
        import lupa  # noqa: F401

        return fakeredis.FakeAsyncRedis(decode_responses=True)
    except ImportError:
        return None


@unittest.skipIf(redis_client() is None, 'Set TEST_REDIS_URL or install fakeredis[lua] to test the Redis slots.')
class RedisChatSlotTests(ChatSlotTests):
    """The same rules with the slots in Redis, shared by every worker."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.redis = redis_client()

    async def asyncTearDown(self):
        await self.redis.aclose()
        await super().asyncTearDown()

    async def test_keys_share_one_cluster_slot_and_expire(self):
        slot = await chat_slots.acquire(self.redis, self.user, 'c1')
        keys = chat_slots._keys(self.user)
        self.assertEqual({key.split('}')[0] for key in keys}, {keys[0].split('}')[0]})
        self.assertGreater(await self.redis.ttl(keys[0]), 0)
        await chat_slots.release(self.redis, slot)


class LedgerUnitTests(unittest.TestCase):
    def test_usage_shapes_are_read_alike(self):
        chat = {
            'prompt_tokens': 1000,
            'completion_tokens': 200,
            'prompt_tokens_details': {'cached_tokens': 400},
            'completion_tokens_details': {'reasoning_tokens': 50},
            'cost': 0.0123,
        }
        self.assertEqual(
            usage_ledger.usage_numbers(chat),
            {'input_tokens': 1000, 'output_tokens': 200, 'cached_tokens': 400, 'reasoning_tokens': 50, 'cost': 0.0123},
        )
        responses = {
            'input_tokens': 10,
            'output_tokens': 5,
            'input_tokens_details': {'cached_tokens': 2},
            'output_tokens_details': {'reasoning_tokens': 1},
        }
        numbers = usage_ledger.usage_numbers(responses)
        self.assertEqual((numbers['input_tokens'], numbers['cached_tokens'], numbers['cost']), (10, 2, None))
        self.assertEqual(usage_ledger.usage_numbers(None)['input_tokens'], 0)
        self.assertEqual(usage_ledger.usage_numbers({'prompt_tokens': 'NaN', 'cost': -1})['cost'], None)

    def test_sources(self):
        api = SimpleNamespace(state=SimpleNamespace(auth_type='api_key'))
        web = SimpleNamespace(state=SimpleNamespace(auth_type='jwt'))
        self.assertEqual(usage_ledger.request_source(web, {'chat_id': 'c', 'task': 'title_generation'}), 'task')
        self.assertEqual(usage_ledger.request_source(web, {'chat_id': 'c', 'internal': True}), 'subagent')
        self.assertEqual(usage_ledger.request_source(web, {'chat_id': 'c', 'automation_id': 'a'}), 'scheduled')
        self.assertEqual(usage_ledger.request_source(api, {}), 'api_key')
        self.assertEqual(usage_ledger.request_source(api, {'chat_id': 'c'}), 'chat')
        self.assertEqual(usage_ledger.request_source(SimpleNamespace(), None), 'chat')

    def test_costs_from_published_prices(self):
        price = {'input': 2, 'output': 10, 'cache_read': 0.5}
        # 600 uncached and 400 cached input tokens, 200 output tokens, per million.
        self.assertAlmostEqual(model_prices.estimate(price, 1000, 200, 400), (600 * 2 + 400 * 0.5 + 200 * 10) / 1e6)
        # Without a cache price, cached input costs as input.
        self.assertAlmostEqual(model_prices.estimate({'input': 1, 'output': 1}, 100, 0, 50), 100 / 1e6)
        tiered = {
            'input': 1,
            'output': 2,
            'tiers': [{'input': 3, 'output': 4, 'tier': {'type': 'context', 'size': 1000}}],
        }
        self.assertAlmostEqual(model_prices.estimate(tiered, 999, 1), (999 + 2) / 1e6)
        self.assertAlmostEqual(model_prices.estimate(tiered, 2000, 1), (6000 + 4) / 1e6)
        self.assertIsNone(model_prices.estimate(None, 10, 10))
        self.assertIsNone(model_prices.estimate({'input': 1}, 10, 10))
        catalog = {
            'openai': {'models': {'gpt-x': {'cost': price}, 'image': {}}},
            'anthropic': {'models': {'claude-x-5': {'cost': price}}},
            'other': {'models': {'y': {'cost': price}}},
        }
        prices = model_prices.reduce_catalog(catalog)
        self.assertEqual(set(prices), {'openai_api', 'anthropic_api', 'openrouter', 'zai'})
        self.assertEqual(prices['openai_api'], {'gpt-x': price})
        # Dated model ids fall back to the undated price.
        self.assertEqual(model_prices.price('anthropic_api', 'claude-x-5-20260101', prices), price)
        self.assertIsNone(model_prices.price('zai', 'glm-x', prices))

    def test_subscriptions_have_no_request_cost(self):
        numbers = usage_ledger.usage_numbers({'prompt_tokens': 10, 'completion_tokens': 10, 'cost': 1})

        def cost(item):
            return asyncio.run(usage_ledger.request_cost(item, 'm', numbers))

        self.assertEqual(cost(account(plan='plus')), (None, None))
        self.assertEqual(cost(account(provider='zai', plan='lite')), (None, None))
        self.assertEqual(cost(account(provider='openrouter', plan='api')), (1, 'provider'))

    def test_the_first_requests_wait_for_the_price_catalog(self):
        catalog = {'openai': {'models': {'m': {'cost': {'input': 1, 'output': 1}}}}}

        async def fetch():
            await asyncio.sleep(0.05)
            model_prices._prices = model_prices.reduce_catalog(catalog)
            model_prices._fetched_at = time.monotonic()

        numbers = usage_ledger.usage_numbers({'prompt_tokens': 500_000, 'completion_tokens': 500_000})
        state = (model_prices._prices, model_prices._fetched_at, model_prices._failed_at, model_prices._task)
        try:
            model_prices._prices, model_prices._fetched_at, model_prices._failed_at = {}, -1e18, -1e18
            model_prices._task = None
            with (
                patch.object(model_prices, '_fetch', fetch),
                patch.object(model_prices, 'CATALOG_URL', 'https://catalog.example/api.json'),
                patch.object(model_prices, 'OFFLINE_MODE', False),
            ):
                result = asyncio.run(
                    usage_ledger.request_cost(account(provider='openai_api', plan='api'), 'm', numbers)
                )
            self.assertEqual(result, (1.0, 'estimate'))
        finally:
            model_prices._prices, model_prices._fetched_at, model_prices._failed_at, model_prices._task = state


class LedgerTests(DatabaseCase):
    """Every request an account serves, or fails, leaves one ledger row."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)), state=SimpleNamespace())
        prices = {'openai_api': {'test-model': {'input': 2, 'output': 10, 'cache_read': 0.5}}}
        self.prices = patch.dict(model_prices._prices, prices)
        self.prices.start()
        self.fetch = patch.object(model_prices, 'refresh_soon')
        self.fetch.start()

    async def asyncTearDown(self):
        self.fetch.stop()
        self.prices.stop()
        await super().asyncTearDown()

    def key(self, identifier, provider='openai_api', **overrides):
        secret = seal_secret({'api_key': f'sk-{identifier}', 'base_url': 'https://provider.example/v1'})
        return account(id=identifier, provider=provider, plan='api', secret=secret, **overrides)

    def patches(self, accounts, replies):
        session = SimpleNamespace(post=AsyncMock(side_effect=replies))
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(proxy, 'select_accounts', AsyncMock(return_value=accounts)))
        stack.enter_context(patch.object(proxy, 'get_session', AsyncMock(return_value=session)))
        stack.enter_context(patch.object(proxy, 'auto_reset_enabled', AsyncMock(return_value=False)))
        return stack

    async def rows(self):
        await usage_ledger.flush()
        async with get_async_db_context() as db:
            rows = (await db.execute(select(UsageEvent).order_by(UsageEvent.created_at))).scalars().all()
        return sorted(rows, key=lambda row: row.status != 'limited')

    async def test_served_and_limited_attempts_are_recorded_with_estimated_cost(self):
        usage = {'prompt_tokens': 1000, 'completion_tokens': 200, 'prompt_tokens_details': {'cached_tokens': 400}}
        replies = [
            Response({'error': {'code': 'rate_limit_exceeded'}}, status=429),
            Response(chat_reply('Hi') | {'usage': usage}),
        ]
        form = {'model': 'account:openai_api:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        metadata = {'chat_id': 'chat-1', 'message_id': 'message-1'}
        with self.patches([self.key('busy'), self.key('shared', owner_user_id=OTHER.id)], replies):
            await proxy.account_completion(self.request, form, OWNER, metadata=metadata)
        limited, served = await self.rows()
        self.assertEqual((limited.account_id, limited.status, limited.input_tokens), ('busy', 'limited', 0))
        self.assertEqual((served.account_id, served.status, served.source), ('shared', 'ok', 'chat'))
        self.assertEqual((served.input_tokens, served.output_tokens, served.cached_tokens), (1000, 200, 400))
        self.assertAlmostEqual(served.cost, 0.0034)
        self.assertEqual(served.cost_source, 'estimate')
        # The owner's own account is their reserve; anyone else's is the pool.
        self.assertEqual((limited.tier, served.tier), ('reserve', 'pool'))
        self.assertEqual((served.chat_id, served.request_id, served.user_id), ('chat-1', 'message-1', OWNER.id))

    async def test_streams_are_recorded_when_they_end(self):
        chunks = [
            chat_chunk(content='Hel'),
            chat_chunk(content='lo', finish_reason='stop'),
            {'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'cost': 0.5}},
            '[DONE]',
        ]
        form = {'model': 'account:openrouter:test-model', 'messages': [], 'stream': True}
        with self.patches([self.key('router', provider='openrouter')], [Response(events=chunks)]):
            answer = await proxy.account_completion(self.request, form, OWNER, metadata={'task': 'title_generation'})
            self.assertEqual(await self.rows(), [])
            await read_stream(answer.body_iterator)
        (row,) = await self.rows()
        self.assertEqual((row.status, row.input_tokens, row.output_tokens, row.source), ('ok', 7, 2, 'task'))
        self.assertEqual((row.cost, row.cost_source), (0.5, 'provider'))

    async def test_responses_streams_and_interrupted_streams(self):
        completed = {'type': 'response.completed', 'response': {'usage': {'input_tokens': 4, 'output_tokens': 3}}}
        events = [{'type': 'response.created', 'response': {}}, completed]
        form = {'model': 'account:openai_api:test-model', 'input': 'Hi', 'stream': True}
        with self.patches([self.key('responses')], [Response(events=events)]):
            answer = await proxy.account_completion(self.request, form, OWNER, endpoint='responses')
            [chunk async for chunk in answer.body_iterator]
        (row,) = await self.rows()
        self.assertEqual((row.status, row.input_tokens, row.output_tokens), ('ok', 4, 3))
        # A stream cut off before it finished is an error, with whatever it used.
        cut = [chat_chunk(content='Hel')]
        form = {'model': 'account:openai_api:test-model', 'messages': [], 'stream': True}
        with self.patches([self.key('cut')], [Response(events=cut, drop=True)]):
            answer = await proxy.account_completion(self.request, form, OWNER)
            await read_stream(answer.body_iterator)
        self.assertEqual([row.status for row in await self.rows()], ['ok', 'error'])

    async def test_a_refused_answer_leaves_one_row_with_what_it_used(self):
        filtered = chat_reply(None, finish_reason='content_filter') | {'usage': {'prompt_tokens': 9}}
        form = {'model': 'account:openai_api:test-model', 'messages': []}
        with self.patches([self.key('filtered')], [Response(filtered)]), self.assertRaises(HTTPException):
            await proxy.account_completion(self.request, form, OWNER)
        (row,) = await self.rows()
        self.assertEqual((row.status, row.input_tokens), ('error', 9))

    async def test_accounts_page_shows_each_members_use_and_the_budget_left(self):
        key = await self.create_account(sharing='shared', budget={'amount': 10, 'currency': 'USD'})
        now = time.time()
        for user_id, cost in ((OWNER.id, 1.5), (OTHER.id, 2.0), (OTHER.id, None)):
            await UsageEvents.insert(
                ts=int(now),
                user_id=user_id,
                account_id=key.id,
                provider='openai_api',
                model_id='test-model',
                source='chat',
                input_tokens=100,
                output_tokens=10,
                cost=cost,
                status='ok',
            )
        # Last month's use and failed requests do not count.
        await UsageEvents.insert(
            ts=int(now - 40 * 86400),
            user_id=OWNER.id,
            account_id=key.id,
            provider='openai_api',
            model_id='m',
            source='chat',
            cost=5,
            status='ok',
        )
        await UsageEvents.insert(
            ts=int(now),
            user_id=OWNER.id,
            account_id=key.id,
            provider='openai_api',
            model_id='m',
            source='chat',
            status='limited',
        )
        names = {OWNER.id: SimpleNamespace(name='Owner'), OTHER.id: SimpleNamespace(name='Other')}
        with (
            patch.object(routes.codex_accounts, 'sync_accounts', AsyncMock()),
            patch.object(routes.Users, 'get_user_by_id', AsyncMock(side_effect=lambda user_id: names.get(user_id))),
        ):
            (item,) = (await routes.list_accounts(self.request, OTHER))['data']
        usage = item['usage']
        self.assertEqual(usage['period'], 'month')
        self.assertEqual(
            [(m['name'], m['requests'], m['input_tokens'], m['cost'], m['unpriced']) for m in usage['members']],
            [('Other', 2, 200, 2.0, 1), ('Owner', 1, 100, 1.5, 0)],
        )
        budget = next(window for window in item['limits']['windows'] if window['id'] == 'budget')
        self.assertEqual((budget['spent'], budget['remaining'], budget['used_percent']), (3.5, 6.5, 35))
        self.assertEqual((budget['source'], budget['unpriced']), ('ledger', 1))


class LedgerMigrationTests(unittest.TestCase):
    def test_usage_event_table_round_trip(self):
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        migration = importlib.import_module('open_webui.migrations.versions.a8d4e2c7f915_add_usage_event')
        self.assertEqual(migration.down_revision, 'f7c2a9d4b8e1')
        engine = create_engine('sqlite://')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            columns = {column['name'] for column in inspect(connection).get_columns('usage_event')}
            # The model and the migration agree.
            self.assertEqual(columns, set(UsageEvent.__table__.columns.keys()))
            indexes = {index['name'] for index in inspect(connection).get_indexes('usage_event')}
            self.assertEqual(indexes, {'ix_usage_event_user_ts', 'ix_usage_event_account_ts'})
            migration.downgrade()
            self.assertNotIn('usage_event', inspect(connection).get_table_names())
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
