"""Phase 5 regressions: credits for accounts added to the pool, earned, spent and forfeited.

Run with an isolated DATA_DIR and test database, never a live database (see test_provider_accounts).
"""

import importlib
import time
import unittest
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from open_webui.models.credits import CreditEntry, CreditGrant, Credits
from open_webui.models.provider_accounts import ProviderAccounts
from open_webui.models.usage_events import UsageEvent
from open_webui.utils import allowance_gate, credits
from test_allowance import CAT, CAT_USER, EnforceCase, settings
from test_provider_accounts import ADMIN, OTHER, OWNER, Response, chat_reply


class CreditTests(EnforceCase):
    """Reuses the enforce setup: keys A (the owner's) and B (another member's), three members."""

    async def test_a_subscription_earns_once_per_provider_account(self):
        plus = await self.create_account(provider='codex', plan='plus', budget=None, external_account_id='chatgpt-1')
        self.assertEqual(await credits.ensure_grant(plus), 10)
        self.assertIsNone(await credits.ensure_grant(plus))
        self.assertEqual(await credits.balance(OWNER.id), 10)
        # Disconnected: what is left of it goes. Re-added, by anyone: nothing new (decision 8).
        self.assertEqual(await credits.forfeit(plus), 10)
        await ProviderAccounts.delete(plus.id)
        again = await self.create_account(
            provider='codex', plan='plus', budget=None, external_account_id='chatgpt-1', owner_user_id=OTHER.id
        )
        self.assertIsNone(await credits.ensure_grant(again))
        self.assertEqual((await credits.balance(OWNER.id), await credits.balance(OTHER.id)), (0, 0))

    async def test_multipliers_by_plan(self):
        pro = await self.create_account(provider='codex', plan='pro', budget=None, external_account_id='p')
        coding = await self.create_account(provider='zai', plan='max', budget=None, external_account_id='z')
        team = await self.create_account(
            provider='codex', plan='plus', budget=None, external_account_id='t', owner_user_id=None
        )
        unknown = await self.create_account(provider='codex', plan='unknown', budget=None, external_account_id='u')
        self.assertEqual(await credits.ensure_grant(pro), 200)
        self.assertEqual(await credits.ensure_grant(coding), 200)
        # A team account has no owner; an unknown plan waits until the plan is read.
        self.assertIsNone(await credits.ensure_grant(team))
        self.assertIsNone(await credits.ensure_grant(unknown))
        # An admin's table wins over the defaults.
        self.config_patch.stop()
        stored = {'mologate.credit_multipliers': {'codex': {'unknown': 2}}}
        self.config_patch = patch('open_webui.models.config.Config.get', settings(**stored))
        self.config_patch.start()
        self.assertEqual(await credits.ensure_grant(unknown), 20)
        self.assertEqual((await credits.multipliers())['codex']['plus'], 1)

    async def test_forfeit_takes_only_what_is_left(self):
        plus = await self.create_account(provider='codex', plan='plus', budget=None, external_account_id='c')
        await credits.ensure_grant(plus)
        self.assertEqual(await Credits.spend(OWNER.id, 4), 4)
        # Spending never goes below zero.
        self.assertEqual(await Credits.spend(OWNER.id, 100), 6)
        await Credits.adjust(OWNER.id, 3)
        self.assertEqual(await credits.forfeit(plus), 3)
        self.assertEqual(await credits.balance(OWNER.id), 0)

    async def test_disconnecting_forfeits(self):
        from open_webui.routers import provider_accounts as routes

        plus = await self.create_account(provider='zai', plan='lite', budget=None, external_account_id='zl')
        await credits.ensure_grant(plus)
        await routes.delete_account(plus.id, user=OWNER)
        self.assertEqual(await credits.balance(OWNER.id), 0)
        reasons = sorted(row.reason for row in await self.rows(CreditEntry))
        self.assertEqual(reasons, ['forfeit', 'grant'])

    async def test_an_api_key_earns_from_the_money_spent_through_it(self):
        # Cat spends $2 through Bob's key B: B's owner earns a credit.
        reply = chat_reply('Hi') | {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 2.0}}
        _, keys, _ = await self.ask(CAT_USER, [Response(reply)])
        expected = {'key-a': OWNER.id, 'key-b': OTHER.id}[keys[0]]
        self.assertEqual(await credits.balance(expected), 1.0)
        self.assertEqual(await credits.balance(CAT), 0)
        [grant] = await self.rows(CreditGrant)
        self.assertEqual((grant.plan, grant.amount, grant.user_id), ('api', 1.0, expected))
        # Free models cost nothing and earn nothing; a team key earns nobody anything.
        free = chat_reply('Hi') | {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 0}}
        await self.ask(CAT_USER, [Response(free)])
        self.assertEqual(await credits.balance(expected), 1.0)
        team = await self.create_account(external_account_id='team-key', owner_user_id=None)
        self.assertEqual(await credits.earn(team, 5.0), 0)

    async def test_past_the_share_credits_pay_for_big_models(self):
        # Cat used 60 units of her 50; 50 credits give her 0.2 x 150 = 30 more.
        await self.spent(self.b, CAT, 12)
        await Credits.adjust(CAT, 50)
        reply = chat_reply('Hi') | {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 1.0}}
        _, _, models = await self.ask(CAT_USER, [Response(reply)])
        self.assertEqual(models, ['gpt-6'])
        row = max(await self.rows(UsageEvent), key=lambda row: (row.ts, row.created_at))
        # $1 is 5 units of the budget window; a credit is 0.4% of its 150 units.
        self.assertEqual(row.tier, 'credit')
        self.assertAlmostEqual(row.credits_charged, 5 / (0.004 * 150), places=4)
        self.assertAlmostEqual(await credits.balance(CAT), 50 - 5 / 0.6, places=4)
        standing = await allowance_gate.standing(CAT_USER, 'openai_api', 'gpt-6')
        self.assertAlmostEqual(standing['credits'], 50 - 5 / 0.6, places=2)

    async def test_the_member_reads_their_credits(self):
        from open_webui.routers import provider_accounts as routes

        plus = await self.create_account(provider='codex', plan='plus', budget=None, external_account_id='m')
        await credits.ensure_grant(plus)
        view = await routes.my_credits(user=OWNER)
        self.assertEqual((view['balance'], view['grants'][0]['amount'], view['per_dollar']), (10, 10, 0.5))
        with patch('open_webui.models.config.Config.upsert', AsyncMock()) as upsert:
            await routes.set_multipliers(
                routes.MultipliersForm(multipliers={'zai': {'pro': 4}, 'openai_api': {'api': 9}}), user=ADMIN
            )
        # API keys earn by spending, not by a plan.
        upsert.assert_awaited_once_with({'mologate.credit_multipliers': {'zai': {'pro': 4}}})


class MigrationTests(unittest.TestCase):
    def test_credit_migration_round_trip(self):
        migration = importlib.import_module('open_webui.migrations.versions.b6d1e8f3a2c7_add_credits')
        self.assertEqual(migration.down_revision, 'a3f6c9d2b8e4')
        engine = create_engine('sqlite://')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            for model in (CreditGrant, CreditEntry):
                columns = {column['name'] for column in inspect(connection).get_columns(model.__tablename__)}
                self.assertEqual(columns, set(model.__table__.columns.keys()))
            migration.downgrade()
            self.assertEqual({'credit_grant', 'credit_entry'} & set(inspect(connection).get_table_names()), set())
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
