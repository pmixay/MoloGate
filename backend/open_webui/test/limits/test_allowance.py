"""Phase 3 regressions: the allowance engine, its state, the observe gate and the replay.

Run with an isolated DATA_DIR and test database, never a live database (see test_provider_accounts).
"""

import importlib
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select

from open_webui.internal.db import get_async_db_context
from open_webui.models.metering import Metering
from open_webui.models.usage_events import UsageEvent, UsageEvents
from open_webui.utils import allowance, allowance_gate, usage_ledger
from open_webui.utils import calibration as fitting
from open_webui.utils import provider_account_proxy as proxy
from open_webui.utils.allowance import Account, Period, Request, State, Window, build_state, decide
from test_metering import MeteringCase
from test_provider_accounts import ADMIN, OTHER, OWNER, Response, chat_reply

HOUR = 3600
DAY = 24 * HOUR
START = 1_791_244_800
ANN, BOB, CAT = 'ann', 'bob', 'cat'


def plus(identifier, owner, **values):
    return Account(identifier, 'codex', owner, **values)


def state(accounts, windows, used=None, members=3, **values):
    return State(accounts=accounts, windows=windows, members=members, used=used or {}, **values)


def ask(user_id, model='gpt-6', **values):
    return Request(user_id, 'codex', model, worker_model='gpt-luna', **values)


class WorkedExamples(unittest.TestCase):
    """Section 7.5: A is Ann's ChatGPT Plus account, B is Bob's; Cat has none. Weekly window."""

    A = plus('A', ANN)
    B = plus('B', BOB)

    def windows(self, a=None, b=None, name='weekly'):
        return {'A': {name: a or Window()}, 'B': {name: b or Window()}}

    def test_1_cat_uses_big_models_up_to_her_share(self):
        current = self.windows()
        numbers = allowance.pool_numbers(state([self.A, self.B], current), 'codex')['weekly']
        self.assertEqual((numbers['capacity'], round(numbers['share'], 1)), (100, 33.3))
        within = decide(ask(CAT), state([self.A, self.B], current, {'weekly': 33.0}))
        self.assertEqual((within.tier, within.model, within.swapped), ('pool', 'gpt-6', False))
        past = decide(ask(CAT), state([self.A, self.B], current, {'weekly': 33.4}))
        self.assertEqual((past.tier, past.model, past.swapped), ('worker', 'gpt-luna', True))
        self.assertIn('uses gpt-luna', past.reason)

    def test_2_the_pool_stops_at_the_owners_locked_reserve(self):
        self.assertEqual(allowance.lock(self.A, Window(used=10, owner_used=10)), 40)
        self.assertGreater(allowance.pool_room(self.A, {'weekly': Window(used=59.9, owner_used=10)}), 0)
        self.assertLessEqual(allowance.pool_room(self.A, {'weekly': Window(used=60, owner_used=10)}), 0)
        decision = decide(ask(CAT), state([self.A], {'A': {'weekly': Window(used=60, owner_used=10)}}))
        self.assertEqual(decision.tier, 'empty')

    def test_3_an_idle_owner_lends_a_quarter(self):
        current = self.windows(a=Window(owner_idle=True))
        self.assertEqual(allowance.loan(self.A, current['A']['weekly']), 25)
        numbers = allowance.pool_numbers(state([self.A, self.B], current), 'codex')['weekly']
        self.assertEqual((numbers['capacity'], round(numbers['share'], 1)), (125, 41.7))

    def test_4_the_loan_stops_when_the_owner_comes_back(self):
        # The pool used A up to 70 while Ann was away.
        current = self.windows(a=Window(used=70, owner_idle=True))
        self.assertGreater(allowance.pool_room(self.A, current['A']), 0)
        decision = decide(ask(ANN), state([self.A, self.B], current))
        self.assertEqual((decision.tier, decision.account_id), ('reserve', 'A'))
        # She is left with 30% of A, not 50%; the pool's 70 is not given back.
        self.assertEqual(allowance.owner_room(self.A, {'weekly': Window(used=70)}), 30)
        # Once she has used it, the pool may not use A any more.
        back = self.windows(a=Window(used=70, owner_idle=False))
        self.assertEqual(decide(ask(CAT), state([self.A, self.B], back)).account_id, 'B')

    def test_5_outside_use_counts_against_the_reserve_then_the_pool(self):
        accounts = [self.A, self.B]
        calibration = {'k': [100.0, 0.0, 0.0, 0.0], 'scales': {}, 'default_scale': 1.0}
        # 0.2 million uncached input tokens: 20 units of Mologate use; then 15 outside.
        tokens = {ANN: {'gpt-6': {'input_tokens': 200_000}}}
        periods = {'A': {'weekly': Period(used=35, tokens=tokens, outside={ANN: 15})}}
        current = build_state(ANN, 'codex', accounts, periods, {'A': True}, 3, {('codex', 'weekly'): calibration})
        self.assertEqual(current.windows['A']['weekly'].owner_used, 35)
        self.assertEqual(allowance.owner_room(self.A, current.windows['A']), 15)
        self.assertEqual(current.used.get('weekly', 0), 0)
        # 45 outside: her reserve is spent and the 15 past it are her pool use.
        periods = {'A': {'weekly': Period(used=65, tokens=tokens, outside={ANN: 45})}}
        current = build_state(ANN, 'codex', accounts, periods, {'A': True}, 3, {('codex', 'weekly'): calibration})
        self.assertEqual(current.used['weekly'], 15)
        self.assertEqual(decide(ask(ANN), current).tier, 'pool')

    def test_6_credits_extend_the_share(self):
        # Bob has spent his reserve, so his requests are pool requests.
        current = self.windows(b=Window(used=50, owner_used=50))
        numbers = allowance.pool_numbers(state([self.A, self.B], current, credits=10), 'codex')['weekly']
        self.assertEqual(numbers['extra'], 4)
        share = numbers['share']
        decision = decide(ask(BOB), state([self.A, self.B], current, {'weekly': share + 2}, credits=10))
        self.assertEqual(decision.tier, 'credit')
        self.assertEqual(allowance.credits_for(2, 100), 5)
        spent = decide(ask(BOB), state([self.A, self.B], current, {'weekly': share + 4}, credits=10))
        self.assertEqual(spent.tier, 'worker')

    def test_7_the_five_hour_share_runs_out_first(self):
        windows = {key: {'5h': Window(), 'weekly': Window()} for key in ('A', 'B')}
        five_hour = allowance.pool_numbers(state([self.A, self.B], windows), 'codex')['5h']['share']
        decision = decide(ask(CAT), state([self.A, self.B], windows, {'5h': five_hour + 1, 'weekly': 10}))
        self.assertEqual(decision.tier, 'worker')
        # The 5-hour window moves on.
        self.assertEqual(decide(ask(CAT), state([self.A, self.B], windows, {'5h': 0, 'weekly': 10})).tier, 'pool')

    def test_8_full_accounts_refuse_the_pool_but_not_their_owners(self):
        full = Window(used=70, owner_used=20)
        current = self.windows(a=full, b=full)
        self.assertEqual(decide(ask(CAT), state([self.A, self.B], current)).tier, 'empty')
        self.assertEqual(decide(ask(ANN), state([self.A, self.B], current)).account_id, 'A')
        self.assertEqual(decide(ask(BOB), state([self.A, self.B], current)).account_id, 'B')
        self.assertEqual(decide(ask(BOB), state([self.A, self.B], current)).tier, 'reserve')


class EngineTests(unittest.TestCase):
    def test_private_team_paused_and_cooling_accounts(self):
        private = plus('P', ANN, sharing='private')
        team = plus('T', None, weight=2)
        paused = plus('Z', BOB, sharing='paused')
        cooling = plus('C', BOB, status='cooldown')
        windows = {key: {'weekly': Window()} for key in 'PTZC'}
        current = state([private, team, paused, cooling], windows)
        # A private account is not pool; a team account is all pool; a paused one gives nothing;
        # one cooling down counts but is not chosen.
        self.assertEqual(allowance.pool_numbers(current, 'codex')['weekly']['capacity'], 200 + 50)
        self.assertEqual(decide(ask(CAT), current).account_id, 'T')
        # The owner uses all of a private account, not only half.
        mine = state([private], {'P': {'weekly': Window(used=80, owner_used=80)}})
        self.assertEqual(decide(ask(ANN), mine).tier, 'reserve')
        self.assertEqual(decide(ask(CAT), mine).tier, 'empty')
        self.assertEqual(decide(ask(BOB), state([paused], {'Z': {'weekly': Window()}})).tier, 'empty')

    def test_the_chats_account_is_kept_and_otherwise_the_roomiest(self):
        accounts = [plus('A', ANN), plus('B', BOB, weight=5)]
        windows = {'A': {'weekly': Window(used=10)}, 'B': {'weekly': Window(used=40)}}
        self.assertEqual(decide(ask(CAT), state(accounts, windows)).account_id, 'B')
        self.assertEqual(decide(ask(CAT, previous_account='A'), state(accounts, windows)).account_id, 'A')

    def test_a_model_no_account_has_room_for_goes_to_a_worker(self):
        accounts = [plus('A', ANN, models=frozenset({'gpt-6'})), plus('B', BOB, models=frozenset({'gpt-luna'}))]
        windows = {'A': {'weekly': Window(used=50, owner_used=0)}, 'B': {'weekly': Window()}}
        decision = decide(ask(CAT), state(accounts, windows))
        self.assertEqual((decision.tier, decision.account_id, decision.model), ('worker', 'B', 'gpt-luna'))
        self.assertIn('No account has room', decision.reason)
        # A worker model asked for within the share is plain pool use.
        luna = decide(ask(CAT, model='gpt-luna'), state(accounts, windows, worker_models=frozenset({'gpt-luna'})))
        self.assertEqual(luna.tier, 'pool')

    def test_unknown_quota_is_never_empty(self):
        decision = decide(ask(CAT), state([plus('A', ANN)], {}))
        self.assertEqual((decision.tier, decision.account_id), ('pool', 'A'))


class StateTests(unittest.TestCase):
    def test_before_calibration_the_rise_is_shared_by_weighted_tokens(self):
        accounts = [plus('A', ANN)]
        # Cat sent twice Dan's input; 5 of the 35 points were Ann's outside use.
        tokens = {CAT: {'gpt-6': {'input_tokens': 200_000}}, 'dan': {'gpt-6': {'input_tokens': 100_000}}}
        periods = {'A': {'weekly': Period(used=35, tokens=tokens, outside={ANN: 5})}}
        cat = build_state(CAT, 'codex', accounts, periods, {}, 4)
        dan = build_state('dan', 'codex', accounts, periods, {}, 4)
        self.assertEqual((round(cat.used['weekly'], 6), round(dan.used['weekly'], 6)), (20, 10))
        # Ann used nothing in Mologate for a day: idle, though her outside use counts.
        self.assertTrue(cat.windows['A']['weekly'].owner_idle)
        self.assertEqual(cat.windows['A']['weekly'].owner_used, 5)

    def test_an_api_keys_budget_is_a_window_in_dollars(self):
        key = Account('K', 'openai_api', ANN, budget=40.0, windows=('budget',))
        periods = {'K': {'budget': Period(cost={CAT: 2.0, ANN: 12.0})}}
        current = build_state(CAT, 'openai_api', [key], periods, {'K': True}, 3)
        window = current.windows['K']['budget']
        # $40 is a weight-2 window; Cat's $2 is 10 units, Ann's $12 is 30% of it.
        self.assertEqual((window.weight, window.used, window.owner_used), (2.0, 35.0, 30.0))
        self.assertEqual(current.used['budget'], 10)
        self.assertEqual(allowance.pool_numbers(current, 'openai_api')['budget']['capacity'], 100)


K = {'5h': [24.0, 2.4, 96.0, 120.0], 'weekly': [4.0, 0.4, 16.0, 20.0]}
SCALES = {'gpt-6': 1.0, 'gpt-luna': 0.2}
CALIBRATIONS = {('codex', name): {'k': k, 'scales': SCALES, 'default_scale': 1.0} for name, k in K.items()}
LENGTHS = {'5h': 5 * HOUR, 'weekly': 7 * DAY}


class Team:
    """A small team's provider accounts as the provider sees them, with exact use."""

    def __init__(self, accounts, members):
        self.accounts = accounts
        self.members = members
        self.periods = {account.id: {name: Period(used=0.0) for name in LENGTHS} for account in accounts}
        self.resets = {
            account.id: {name: START + length + index * 977 for name, length in LENGTHS.items()}
            for index, account in enumerate(accounts)
        }
        self.owner_seen = {}

    def advance(self, now):
        for account in self.accounts:
            for name, length in LENGTHS.items():
                while now >= self.resets[account.id][name]:
                    self.periods[account.id][name] = Period(used=0.0)
                    self.resets[account.id][name] += length

    def spend(self, now, account, user_id, model, tokens, outside=False):
        self.advance(now)
        for name in LENGTHS:
            period = self.periods[account.id][name]
            units = fitting.units(CALIBRATIONS[('codex', name)], model, tokens)
            period.used += units / account.weight
            if outside:
                period.outside[user_id] = period.outside.get(user_id, 0.0) + units
            else:
                total = period.tokens.setdefault(user_id, {}).setdefault(model, {})
                for key, value in tokens.items():
                    total[key] = total.get(key, 0) + value
        if user_id == account.owner:
            self.owner_seen[account.id] = now

    def state(self, now, user_id):
        self.advance(now)
        active = {account_id: seen >= now - allowance.IDLE_SECONDS for account_id, seen in self.owner_seen.items()}
        return build_state(
            user_id,
            'codex',
            self.accounts,
            self.periods,
            active,
            self.members,
            CALIBRATIONS,
            worker_models=frozenset({'gpt-luna'}),
        )


def random_tokens(rng):
    input_tokens = rng.randint(5_000, 60_000)
    output_tokens = rng.randint(300, 4_000)
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'cached_tokens': int(input_tokens * rng.random() * 0.8),
        'reasoning_tokens': int(output_tokens * rng.random() * 0.6),
    }


def schedule(rng, gaps, days, outside=()):
    """Requests from each member during the day, and the owners' use outside Mologate."""
    events = []
    for user_id, gap in gaps.items():
        for day in range(days):
            at = START + day * DAY + 8 * HOUR + rng.randint(0, HOUR)
            while at < START + day * DAY + 22 * HOUR:
                events.append((at, user_id, rng.choice(['gpt-6', 'gpt-6', 'gpt-6', 'gpt-luna'])))
                at += rng.randint(gap // 2, gap * 3 // 2)
    events += [(at, user_id, 'outside') for at, user_id in outside]
    return sorted(events)


class SimulationTests(unittest.TestCase):
    """A week of five members on two ChatGPT accounts, with the decisions enforced.

    Ann owns a Plus account, Bob a Pro account five times its size; Cat, Dan and Eve have none.
    Dan works non-stop, Cat steadily, Eve now and then. Ann also uses her account from her laptop.
    Every request is decided from the team's exact numbers and served as decided; the rules of
    section 7 must hold throughout.
    """

    def run_week(self, seed):
        rng = random.Random(seed)
        a, b = plus('A', ANN), plus('B', BOB, weight=5)
        team = Team([a, b], 5)
        gaps = {ANN: 9 * 60, BOB: 6 * 60, CAT: 5 * 60, 'dan': 90, 'eve': 40 * 60}
        outside = [(START + day * DAY + 23 * HOUR, ANN) for day in range(7)]
        tally = {user_id: {tier: 0 for tier in allowance.TIERS} | {'swapped': 0} for user_id in gaps}
        for at, user_id, model in schedule(rng, gaps, 7, outside):
            tokens = random_tokens(rng)
            if model == 'outside':
                for _ in range(8):
                    team.spend(at, a, ANN, 'gpt-6', random_tokens(rng), outside=True)
                continue
            current = team.state(at, user_id)
            request = ask(user_id, model)
            decision = decide(request, current)
            tally[user_id][decision.tier] += 1
            tally[user_id]['swapped'] += decision.swapped
            # No one is refused, or sent to a worker within their share, while the pool has room.
            self.assertFalse(allowance._independent_room(decision, request, current), (at, user_id, decision))
            numbers = allowance.pool_numbers(current, 'codex')
            if decision.tier in ('worker', 'empty') and decision.reason.startswith('Your share'):
                self.assertTrue(any(item['used'] >= item['share'] for item in numbers.values()))
            for account in (a, b):
                windows = current.windows[account.id]
                # An owner with reserve left always gets it.
                if (
                    user_id == account.owner
                    and allowance.owner_room(
                        account, {name: allowance._active(window) for name, window in windows.items()}
                    )
                    > 0
                ):
                    self.assertEqual(decision.tier, 'reserve')
            # Big models from the pool only within the share (and extra); past it, workers only.
            if decision.tier in ('pool', 'credit') and decision.model not in current.worker_models:
                self.assertTrue(all(item['used'] < item['share'] + item['extra'] for item in numbers.values()))
            if decision.tier == 'worker':
                self.assertEqual(decision.model, 'gpt-luna')
            if decision.tier == 'empty':
                continue
            account = a if decision.account_id == 'A' else b
            before = dict(current.windows[account.id])
            team.spend(at + 20, account, user_id, decision.model, tokens)
            # The pool never digs into an owner's locked reserve by more than one request.
            if user_id != account.owner:
                for name, window in before.items():
                    request_percent = (
                        fitting.units(CALIBRATIONS[('codex', name)], decision.model, tokens) / account.weight
                    )
                    after = team.periods[account.id][name].used
                    if after >= window.used:
                        self.assertLessEqual(after, 100 - allowance.lock(account, window) + request_percent + 1e-9)
            if decision.reason.startswith('Your share'):
                tally[user_id]['past_share'] = tally[user_id].get('past_share', 0) + 1
        return tally

    def test_a_week_under_the_rules(self):
        for seed in (1, 2, 3):
            tally = self.run_week(seed)
            # Dan, past his share, keeps working on the worker model; Eve never runs past hers.
            self.assertGreater(tally['dan']['swapped'], 0, seed)
            self.assertGreater(tally['dan']['past_share'], 0, seed)
            self.assertEqual(tally['eve'].get('past_share', 0), 0, seed)
            # The owners work on their own accounts.
            self.assertGreater(tally[ANN]['reserve'], 0, seed)
            self.assertGreater(tally[BOB]['reserve'], 0, seed)


class ReplayTests(unittest.TestCase):
    def test_replaying_a_week_of_observed_requests(self):
        rng = random.Random(5)
        a, b = plus('A', ANN), plus('B', BOB, weight=5)
        workers = {'codex': ('gpt-luna', frozenset({'gpt-luna'}))}
        events = []
        # As observed: requests went to the owner's account first, else the emptiest shared one.
        team = Team([a, b], 4)
        gaps = {ANN: 9 * 60, BOB: 6 * 60, CAT: 2 * 60, 'eve': 40 * 60}
        for index, (at, user_id, model) in enumerate(schedule(rng, gaps, 5)):
            tokens = random_tokens(rng)
            own = [account for account in (a, b) if account.owner == user_id]
            account = own[0] if own else min((a, b), key=lambda item: team.periods[item.id]['weekly'].used)
            team.spend(at + 20, account, user_id, model, tokens)
            for name in LENGTHS:
                period = team.periods[account.id][name]
                events.append(
                    {
                        'kind': 'reading',
                        'ts': at + 30,
                        'account_id': account.id,
                        'window': name,
                        'used': period.used,
                        'resets_at': team.resets[account.id][name],
                        'window_minutes': LENGTHS[name] // 60,
                    }
                )
            events.append(
                {
                    'kind': 'request',
                    'ts': at + 20,
                    'user_id': user_id,
                    'account_id': account.id,
                    'provider': 'codex',
                    'model_id': model,
                    'tokens': tokens,
                    'cost': None,
                    'chat_id': f'{user_id}-{index // 10}',
                    'request_id': f'r{index}',
                    'status': 'ok',
                }
            )
        events.sort(key=lambda event: (event['ts'], event['kind'] != 'reading'))
        report = allowance.replay(events, [a, b], 4, CALIBRATIONS, workers)
        self.assertEqual(report['blocked_with_room'], 0)
        self.assertEqual(report['requests'], sum(1 for event in events if event['kind'] == 'request'))
        # Cat used far more than her share: the rules would have moved her to the worker model.
        self.assertGreater(report['by_member'][CAT]['swapped'], 0)
        self.assertEqual(report['by_member']['eve']['worker'], 0)
        self.assertGreater(report['by_member'][ANN]['reserve'], 0)
        # Owners were served by their own accounts, as the rules say.
        self.assertGreater(report['same_account'], 0)

    def test_use_from_before_a_window_reset_no_longer_counts(self):
        a = plus('A', ANN)
        events = [
            {
                'kind': 'history',
                'ts': START,
                'user_id': CAT,
                'account_id': 'A',
                'provider': 'codex',
                'model_id': 'gpt-6',
                # 36 weekly units: more than Cat's whole share of 16.7.
                'tokens': {'input_tokens': 9_000_000},
                'status': 'ok',
            },
            # Both windows began after that request.
            {
                'kind': 'reading',
                'ts': START + 3 * DAY,
                'account_id': 'A',
                'window': 'weekly',
                'used': 1,
                'resets_at': START + 8 * DAY,
                'window_minutes': 10080,
            },
            {
                'kind': 'reading',
                'ts': START + 3 * DAY,
                'account_id': 'A',
                'window': '5h',
                'used': 1,
                'resets_at': START + 3 * DAY + 4 * HOUR,
                'window_minutes': 300,
            },
            {
                'kind': 'request',
                'ts': START + 3 * DAY + 60,
                'user_id': CAT,
                'account_id': 'A',
                'provider': 'codex',
                'model_id': 'gpt-6',
                'request_id': 'now',
                'tokens': {'input_tokens': 1},
                'status': 'ok',
            },
        ]
        workers = {'codex': ('gpt-luna', frozenset({'gpt-luna'}))}
        report = allowance.replay(events, [a], 3, CALIBRATIONS, workers)
        self.assertEqual(report['decisions'][0][2], 'pool')
        # Without the readings that start the periods, it would still count.
        report = allowance.replay(events[:1] + events[3:], [a], 3, CALIBRATIONS, workers)
        self.assertEqual(report['decisions'][0][2], 'worker')


class GateTests(MeteringCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        allowance_gate._cache.clear()
        self.members = patch.object(allowance_gate, 'members', AsyncMock(return_value=3))
        self.members.start()

    async def asyncTearDown(self):
        self.members.stop()
        await super().asyncTearDown()

    async def test_decisions_come_from_metering_and_the_ledger(self):
        a = await self.create_account(
            provider='codex', plan='plus', sharing='shared', external_account_id='a', models=[{'id': 'gpt-6'}]
        )
        b = await self.create_account(
            provider='codex',
            plan='plus',
            sharing='shared',
            external_account_id='b',
            owner_user_id=OTHER.id,
            models=[{'id': 'gpt-6'}, {'id': 'gpt-luna-6'}],
        )
        now = START + 2 * DAY
        await Metering.save_calibration('codex', 'weekly', CALIBRATIONS[('codex', 'weekly')], 50, 0.03)
        for account, used in ((a, 20), (b, 30)):
            await Metering.save_window(
                account.id,
                'weekly',
                used_percent=used,
                resets_at=now + 3 * DAY,
                window_minutes=10080,
                source='poll',
                observed_at=now,
            )
        # Cat used 60 units of B this week: past a share of 100/3.
        heavy = {'input_tokens': 15_000_000}
        await self.ledger(b, CAT, now - HOUR, request=heavy)
        await self.ledger(b, CAT, now - 600, request={'input_tokens': 1000})
        with patch('time.time', return_value=now):
            cat = await allowance_gate.decide(CAT, 'codex', 'gpt-6')
            owner = await allowance_gate.decide(OWNER.id, 'codex', 'gpt-6')
        self.assertEqual((cat.tier, cat.model, cat.swapped), ('worker', 'gpt-luna-6', True))
        self.assertEqual(cat.account_id, b.id)
        self.assertEqual(round(cat.windows['weekly']['used'], 3), 60.004)
        self.assertEqual((owner.tier, owner.account_id), ('reserve', a.id))

    async def test_every_request_records_its_decision(self):
        key = await self.create_account(sharing='shared', owner_user_id=OTHER.id, external_account_id='k')
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)), state=SimpleNamespace())
        session = SimpleNamespace(post=AsyncMock(return_value=Response(chat_reply('Hi'))))
        form = {'model': 'account:openai_api:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        with (
            patch.object(proxy, 'get_session', AsyncMock(return_value=session)),
            patch.object(proxy, 'auto_reset_enabled', AsyncMock(return_value=False)),
        ):
            await proxy.account_completion(request, form, OWNER, metadata={'chat_id': 'c1', 'message_id': 'm1'})
        await usage_ledger.flush()
        [row] = await self.rows(UsageEvent)
        self.assertEqual(row.account_id, key.id)
        # Observe: whose account served it stays in the tier; the engine's call goes beside it.
        self.assertEqual(row.tier, 'pool')
        self.assertEqual(
            {name: row.decision[name] for name in ('tier', 'account_id', 'model', 'swapped', 'mode')},
            {'tier': 'pool', 'account_id': key.id, 'model': 'test-model', 'swapped': False, 'mode': 'observe'},
        )
        # A $20 budget is a weight-1 window: half of it is the pool's, and its owner, idle for a
        # day, lends a quarter more; a third of that each.
        self.assertAlmostEqual(row.decision['windows']['budget']['share'], 25, places=3)

        from open_webui.routers import provider_accounts as routes

        report = await routes.allowance_report(days=7, user=ADMIN)
        self.assertEqual(report['decided'][OWNER.id]['pool'], 1)
        self.assertEqual((report['replay']['requests'], report['replay']['blocked_with_room']), (1, 0))

    async def test_a_failing_decision_never_fails_the_request(self):
        key = await self.create_account(sharing='shared', owner_user_id=OTHER.id, external_account_id='k')
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)), state=SimpleNamespace())
        session = SimpleNamespace(post=AsyncMock(return_value=Response(chat_reply('Hi'))))
        form = {'model': 'account:openai_api:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        with (
            patch.object(proxy, 'get_session', AsyncMock(return_value=session)),
            patch.object(proxy, 'auto_reset_enabled', AsyncMock(return_value=False)),
            patch.object(allowance_gate, 'decide', AsyncMock(side_effect=RuntimeError('broken'))),
        ):
            answer = await proxy.account_completion(request, form, OWNER)
            # The decision runs beside the request, after it has answered.
            await usage_ledger.flush()
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hi')
        [row] = await self.rows(UsageEvent)
        self.assertEqual((row.account_id, row.decision), (key.id, None))


class MigrationTests(unittest.TestCase):
    def test_decision_migration_round_trip(self):
        ledger = importlib.import_module('open_webui.migrations.versions.a8d4e2c7f915_add_usage_event')
        metering = importlib.import_module('open_webui.migrations.versions.c4e8a2d6f1b9_add_metering')
        migration = importlib.import_module('open_webui.migrations.versions.d7a3f9c2e5b1_add_usage_decision')
        self.assertEqual(migration.down_revision, metering.revision)
        target = os.environ.get('TEST_MIGRATION_DATABASE_URL', 'sqlite://')
        if target != 'sqlite://' and 'provider-tests' not in target:
            raise RuntimeError('Use an isolated provider-tests database.')
        engine = create_engine(target)
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            ledger.upgrade()
            metering.upgrade()
            migration.upgrade()
            columns = [column['name'] for column in inspect(connection).get_columns('usage_event')]
            self.assertIn('decision', columns)
            migration.downgrade()
            columns = [column['name'] for column in inspect(connection).get_columns('usage_event')]
            self.assertNotIn('decision', columns)
            metering.downgrade()
            ledger.downgrade()
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
