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
from fastapi import HTTPException

from open_webui.utils import account_slots, allowance, allowance_gate, usage_ledger
from open_webui.utils import calibration as fitting
from open_webui.utils import provider_account_proxy as proxy
from open_webui.utils.allowance import Account, Period, Request, State, Window, build_state, decide
from test_metering import MeteringCase
from test_provider_accounts import ADMIN, OTHER, OWNER, Response, chat_reply, read_stream

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
    def test_team_paused_and_cooling_accounts(self):
        team = plus('T', None, weight=2)
        paused = plus('Z', BOB, sharing='paused')
        cooling = plus('C', BOB, status='cooldown')
        windows = {key: {'weekly': Window()} for key in 'TZC'}
        current = state([team, paused, cooling], windows)
        # A team account is all pool; a paused one gives nothing; one cooling down counts but is
        # not chosen.
        self.assertEqual(allowance.pool_numbers(current, 'codex')['weekly']['capacity'], 200 + 50)
        self.assertEqual(decide(ask(CAT), current).account_id, 'T')
        # Paused, it serves nobody, its owner included.
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

    def test_one_requests_units_before_calibration(self):
        account = plus('A', ANN, weight=2)
        # 30 points of a weight-2 account, 10 units of them outside: 50 units shared by tokens.
        period = Period(used=30, tokens={CAT: {'gpt-6': {'input_tokens': 300_000}}}, outside={ANN: 10})
        units = allowance.request_units(account, period, {'input_tokens': 100_000})
        self.assertAlmostEqual(units, 12.5)
        self.assertEqual(allowance.request_units(account, Period(), {'input_tokens': 1}), 0)

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
            failed_over = False
            # Now and then the decided account is rate-limited: the request moves on within its
            # tier, or to a later one, and never back to an account it found used up.
            if decision.account_id and rng.random() < 0.05:
                retried = decide(ask(user_id, model, exclude=frozenset({decision.account_id})), current)
                self.assertNotEqual(retried.account_id, decision.account_id)
                self.assertGreaterEqual(allowance.TIERS.index(retried.tier), allowance.TIERS.index(decision.tier))
                self.assertEqual(
                    retried.candidates, decision.candidates[1:] if retried.tier == decision.tier else retried.candidates
                )
                tally[user_id]['failovers'] = tally[user_id].get('failovers', 0) + 1
                if retried.tier == 'empty':
                    continue
                decision, failed_over = retried, True
            tally[user_id][decision.tier] += 1
            tally[user_id]['swapped'] += decision.swapped
            # No one is refused, or sent to a worker within their share, while the pool has room.
            self.assertFalse(allowance._independent_room(decision, request, current), (at, user_id, decision))
            numbers = allowance.pool_numbers(current, 'codex')
            if decision.tier in ('worker', 'empty') and decision.reason.startswith('Your share'):
                self.assertTrue(any(item['used'] >= item['share'] for item in numbers.values()))
            for account in (a, b):
                windows = current.windows[account.id]
                # An owner with reserve left always gets it (unless their account just refused).
                if (
                    not failed_over
                    and user_id == account.owner
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
            self.assertGreater(sum(member.get('failovers', 0) for member in tally.values()), 0, seed)


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

    async def test_a_new_account_joins_the_pool_at_once(self):
        await self.create_account(provider='codex', plan='plus', external_account_id='a', models=[{'id': 'gpt-6'}])
        before = await allowance_gate.decide(CAT, 'codex', 'gpt-6')
        # Owner idle for a day: 50 points plus a 25-point loan.
        self.assertEqual(before.windows['weekly']['capacity'], 75)
        added = await self.create_account(
            provider='codex', plan='plus', external_account_id='b', owner_user_id=OTHER.id, models=[{'id': 'gpt-6'}]
        )
        # Well within the 15 seconds the pool is otherwise kept for.
        after = await allowance_gate.decide(CAT, 'codex', 'gpt-6')
        self.assertEqual(after.windows['weekly']['capacity'], 150)
        self.assertEqual(round(after.windows['weekly']['share']), 50)
        from open_webui.models.provider_accounts import ProviderAccounts

        await ProviderAccounts.update(added.id, {'sharing': 'paused'})
        self.assertEqual((await allowance_gate.decide(CAT, 'codex', 'gpt-6')).windows['weekly']['capacity'], 75)
        await ProviderAccounts.update(added.id, {'sharing': 'shared'})
        self.assertEqual((await allowance_gate.decide(CAT, 'codex', 'gpt-6')).windows['weekly']['capacity'], 150)
        await ProviderAccounts.delete(added.id)
        self.assertEqual((await allowance_gate.decide(CAT, 'codex', 'gpt-6')).windows['weekly']['capacity'], 75)

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


CAT_USER = SimpleNamespace(id=CAT, role='user', name='Cat')
MODELS = [{'id': 'gpt-6', 'name': 'GPT-6'}, {'id': 'gpt-luna-6', 'name': 'GPT Luna'}]


def settings(**values):
    """Config.get as the server would answer it, with these settings."""
    stored = {'mologate.limits_mode': 'enforce', 'mologate.max_account_requests': 4} | values
    return AsyncMock(side_effect=lambda key, default=None: stored.get(key, default))


class EnforceCase(MeteringCase):
    """Phase 4: in enforce, requests are served as the engine decides.

    Two $20 OpenAI keys, A (the owner's) and B (another member's), each a weight-1 window; three
    members. Both owners idle for a day lend 25 points each: the pool is 150 units, 50 each.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        allowance_gate._cache.clear()
        account_slots._memory.clear()
        self.config_patch.stop()
        self.config_patch = patch('open_webui.models.config.Config.get', settings())
        self.config_patch.start()
        self.patches = [
            patch.object(allowance_gate, 'members', AsyncMock(return_value=3)),
            patch.object(proxy, 'auto_reset_enabled', AsyncMock(return_value=False)),
        ]
        for item in self.patches:
            item.start()
        self.a = await self.create_account(
            external_account_id='a', secret={'api_key': 'key-a', 'base_url': 'https://a.test'}, models=MODELS
        )
        self.b = await self.create_account(
            external_account_id='b',
            owner_user_id=OTHER.id,
            secret={'api_key': 'key-b', 'base_url': 'https://b.test'},
            models=MODELS,
        )
        self.now = int(__import__('time').time())

    async def asyncTearDown(self):
        await account_slots.flush()
        for item in self.patches:
            item.stop()
        await super().asyncTearDown()

    async def spent(self, account, user_id, dollars):
        await UsageEvents.insert(
            ts=self.now - 60,
            user_id=user_id,
            account_id=account.id,
            provider=account.provider,
            model_id='gpt-6',
            source='chat',
            tier='pool',
            status='ok',
            input_tokens=1000,
            cost=dollars,
        )

    async def ask(self, user, replies, model='gpt-6', stream=False, metadata=None):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)), state=SimpleNamespace())
        session = SimpleNamespace(post=AsyncMock(side_effect=replies))
        form = {'model': f'account:openai_api:{model}', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        if stream:
            form['stream'] = True
        with patch.object(proxy, 'get_session', AsyncMock(return_value=session)):
            answer = await proxy.account_completion(request, form, user, metadata=metadata)
            if stream:
                answer = await read_stream(answer.body_iterator)
            await usage_ledger.flush()
        keys = [call.kwargs['headers']['Authorization'].removeprefix('Bearer ') for call in session.post.call_args_list]
        models = [call.kwargs['json']['model'] for call in session.post.call_args_list]
        return answer, keys, models


class EnforceTests(EnforceCase):
    async def test_the_owner_is_served_by_their_own_account(self):
        answer, keys, _ = await self.ask(OWNER, [Response(chat_reply('Hi'))])
        self.assertEqual(keys, ['key-a'])
        [row] = await self.rows(UsageEvent)
        # Enforced: the tier is the decision's.
        self.assertEqual((row.tier, row.decision['tier'], row.decision['mode']), ('reserve', 'reserve', 'enforce'))
        self.assertNotIn('mologate', answer)

    async def test_past_the_share_the_reply_uses_the_worker_and_says_so(self):
        # Cat spent $12 through B: 60 units, past her share of 50.
        await self.spent(self.b, CAT, 12)
        answer, keys, models = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        # A has the most room for its size.
        self.assertEqual((keys, models), (['key-a'], ['gpt-luna-6']))
        self.assertRegex(
            answer['mologate']['notice'], r'^Your share is used up, so this reply uses GPT Luna\. Resets in '
        )
        rows = [row for row in await self.rows(UsageEvent) if row.ts >= self.now]
        self.assertEqual([(row.tier, row.model_id) for row in rows], [('worker', 'gpt-luna-6')])
        # Within the share, the model asked for.
        await self.spent(self.b, CAT, -4)
        # The pool's numbers are kept for 15 seconds.
        allowance_gate._cache.clear()
        _, _, models = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        self.assertEqual(models, ['gpt-6'])

    async def test_a_used_up_account_fails_over_within_the_tier(self):
        limited = Response({'error': {'code': 'rate_limit_exceeded'}}, status=429)
        answer, keys, _ = await self.ask(CAT_USER, [limited, Response(chat_reply('Hi'))])
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hi')
        self.assertEqual(sorted(keys), ['key-a', 'key-b'])
        rows = sorted(await self.rows(UsageEvent), key=lambda row: row.status)
        self.assertEqual([(row.status, row.tier) for row in rows], [('limited', 'pool'), ('ok', 'pool')])
        # Both slots were given back, the refused attempt's too.
        await account_slots.flush()
        self.assertEqual([await account_slots.active(None, key.id) for key in (self.a, self.b)], [0, 0])

    async def test_an_owner_whose_account_is_limited_goes_to_the_pool(self):
        limited = Response({'error': {'code': 'rate_limit_exceeded'}}, status=429)
        _, keys, _ = await self.ask(OWNER, [limited, Response(chat_reply('Hi'))])
        self.assertEqual(keys, ['key-a', 'key-b'])
        rows = sorted(await self.rows(UsageEvent), key=lambda row: row.status)
        self.assertEqual([(row.status, row.tier) for row in rows], [('limited', 'reserve'), ('ok', 'pool')])

    async def test_an_empty_pool_says_when_and_offers_a_worker_elsewhere(self):
        # Each key is used up to its owner's locked reserve.
        await self.spent(self.a, OTHER.id, 15)
        await self.spent(self.b, OWNER.id, 15)
        await self.create_account(
            provider='anthropic_api',
            owner_user_id=None,
            external_account_id='c',
            budget=None,
            models=[{'id': 'claude-haiku-5-5', 'name': 'Claude Haiku 5.5'}],
        )
        with self.assertRaises(HTTPException) as caught:
            await self.ask(CAT_USER, [])
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn('no room left for you', caught.exception.detail)
        self.assertIn('frees up in', caught.exception.detail)
        self.assertIn('Claude Haiku 5.5 (Anthropic)', caught.exception.detail)
        # Its owner still has their locked reserve.
        _, keys, _ = await self.ask(OWNER, [Response(chat_reply('Hi'))])
        self.assertEqual(keys, ['key-a'])

    async def test_a_full_account_hands_the_request_to_another_and_then_waits(self):
        self.config_patch.stop()
        self.config_patch = patch(
            'open_webui.models.config.Config.get', settings(**{'mologate.max_account_requests': 1})
        )
        self.config_patch.start()
        self.assertTrue(await account_slots.take(None, self.a.id, 'someone', 1))
        _, keys, _ = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        self.assertEqual(keys, ['key-b'])
        # Its slot was given back once the answer was.
        await account_slots.flush()
        self.assertEqual(await account_slots.active(None, self.b.id), 0)
        self.assertTrue(await account_slots.take(None, self.b.id, 'someone', 1))
        with patch.object(account_slots, 'WAIT_SECONDS', 0.6), self.assertRaises(HTTPException) as caught:
            await self.ask(CAT_USER, [])
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn('busy', caught.exception.detail)

    async def test_a_stream_holds_its_slot_until_it_ends(self):
        events = [
            {'choices': [{'index': 0, 'delta': {'content': 'Hi'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            '[DONE]',
        ]
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)), state=SimpleNamespace())
        session = SimpleNamespace(post=AsyncMock(return_value=Response(events=events)))
        form = {'model': 'account:openai_api:gpt-6', 'messages': [], 'stream': True}
        with patch.object(proxy, 'get_session', AsyncMock(return_value=session)):
            answer = await proxy.account_completion(request, form, OWNER)
            self.assertEqual(await account_slots.active(None, self.a.id), 1)
            await read_stream(answer.body_iterator)
        await account_slots.flush()
        self.assertEqual(await account_slots.active(None, self.a.id), 0)
        await usage_ledger.flush()

    async def test_a_broken_gate_lets_the_request_go_ahead(self):
        with patch.object(allowance_gate, 'decide', AsyncMock(side_effect=RuntimeError('broken'))):
            answer, keys, _ = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hi')
        self.assertEqual(len(keys), 1)

    async def test_an_override_extends_the_share_and_is_audited(self):
        from open_webui.models.limit_overrides import LimitOverrides
        from open_webui.routers import provider_accounts as routes

        await self.spent(self.b, CAT, 12)
        # 10 points of a 150-unit pool are 15 more units: 65 past 60 used, so credit tier.
        granted = await routes.grant_override(
            routes.OverrideForm(user_id=None, extra_points=10, reason='Release week'), user=ADMIN
        )
        allowance_gate._cache.clear()
        _, _, models = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        self.assertEqual(models, ['gpt-6'])
        row = max(await self.rows(UsageEvent), key=lambda row: row.ts)
        self.assertEqual((row.tier, row.decision['windows']['budget']['extra']), ('credit', 15))
        # Revoked: past the share again, and the grant stays on record.
        await routes.revoke_override(granted['id'], user=ADMIN)
        _, _, models = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        self.assertEqual(models, ['gpt-luna-6'])
        [entry] = await LimitOverrides.list()
        self.assertEqual(
            (entry['granted_by'], entry['revoked_by'], entry['reason']), (ADMIN.id, ADMIN.id, 'Release week')
        )
        # A member's own override counts only for them, and only until it ends.
        await LimitOverrides.grant(CAT, ADMIN.id, 5, self.now + 3600, 'Deadline')
        await LimitOverrides.grant(CAT, ADMIN.id, 7, self.now - 1, 'Over')
        self.assertEqual(await LimitOverrides.active_points(CAT), 5)
        self.assertEqual(await LimitOverrides.active_points(OWNER.id), 0)
        with self.assertRaises(HTTPException):
            await routes.grant_override(
                routes.OverrideForm(user_id=None, extra_points=5, until=self.now - 10, reason='Late'), user=ADMIN
            )

    async def test_the_member_sees_their_share_and_its_pace(self):
        from open_webui.utils.provider_limits import period_start

        # $9 through B in the last minutes: 45 of Cat's 50 units, at 9 units a minute.
        await self.spent(self.b, CAT, 9)
        standing = await allowance_gate.standing(CAT_USER, 'openai_api', 'gpt-6')
        window = standing['windows']['budget']
        self.assertEqual((standing['tier'], standing['mode'], standing['credits']), ('pool', 'enforce', 0))
        self.assertEqual((window['percent'], window['level'], window['minutes_left']), (90, 'low', 1))
        self.assertEqual(window['resets_at'], period_start('monthly', period_start('monthly', self.now) + 32 * 86400))
        # Past it: the worker, by name.
        await self.spent(self.b, CAT, 2)
        allowance_gate._cache.clear()
        standing = await allowance_gate.standing(CAT_USER, 'openai_api', 'gpt-6')
        self.assertEqual(
            (standing['tier'], standing['model_name'], standing['windows']['budget']['level']),
            ('worker', 'GPT Luna', 'empty'),
        )

    async def test_observe_serves_as_before(self):
        self.config_patch.stop()
        self.config_patch = patch(
            'open_webui.models.config.Config.get', settings(**{'mologate.limits_mode': 'observe'})
        )
        self.config_patch.start()
        await self.spent(self.b, CAT, 12)
        answer, keys, models = await self.ask(CAT_USER, [Response(chat_reply('Hi'))])
        # The model asked for, nothing said; the decision is recorded beside it.
        self.assertEqual((len(keys), models), (1, ['gpt-6']))
        self.assertNotIn('mologate', answer)
        row = next(row for row in await self.rows(UsageEvent) if row.ts >= self.now)
        self.assertEqual((row.tier, row.decision['tier'], row.decision['mode']), ('pool', 'worker', 'observe'))


class RouteTests(unittest.TestCase):
    def test_override_routes_are_for_admins(self):
        from open_webui.routers import provider_accounts as routes
        from open_webui.utils.auth import get_admin_user

        paths = [route for route in routes.router.routes if route.path.startswith('/overrides')]
        self.assertEqual(len(paths), 3)
        for route in paths:
            self.assertIn(get_admin_user, [dependency.call for dependency in route.dependant.dependencies])


class MigrationTests(unittest.TestCase):
    def test_private_accounts_become_shared(self):
        accounts = importlib.import_module('open_webui.migrations.versions.b1a7c9e4d205_add_provider_accounts')
        migration = importlib.import_module('open_webui.migrations.versions.e1b4c7a9d3f6_all_accounts_shared')
        self.assertEqual(migration.down_revision, 'd7a3f9c2e5b1')
        engine = create_engine('sqlite://')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            accounts.upgrade()
            for identifier, sharing in (('a', 'private'), ('b', 'shared'), ('c', 'paused')):
                connection.exec_driver_sql(
                    'INSERT INTO provider_account (id, owner_user_id, provider, plan, external_account_id, label, '
                    'secret, sharing, capacity_weight, allow_team_resets, status, models, created_at, updated_at) '
                    f"VALUES ('{identifier}', 'owner', 'codex', 'plus', '{identifier}', 'Account #{identifier}', 's', "
                    f"'{sharing}', 1, 0, 'ok', '[]', 0, 0)"
                )
            migration.upgrade()
            rows = dict(connection.exec_driver_sql('SELECT id, sharing FROM provider_account').all())
            # Paused stays paused: the owner's switch.
            self.assertEqual(rows, {'a': 'shared', 'b': 'shared', 'c': 'paused'})
            accounts.downgrade()
        engine.dispose()

    def test_override_migration_round_trip(self):
        from open_webui.models.limit_overrides import LimitOverride

        migration = importlib.import_module('open_webui.migrations.versions.a3f6c9d2b8e4_add_limit_override')
        self.assertEqual(migration.down_revision, 'e1b4c7a9d3f6')
        engine = create_engine('sqlite://')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            columns = {column['name'] for column in inspect(connection).get_columns('limit_override')}
            self.assertEqual(columns, set(LimitOverride.__table__.columns.keys()))
            migration.downgrade()
            self.assertNotIn('limit_override', inspect(connection).get_table_names())
        engine.dispose()

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
