"""Phase 6 regressions: personal API keys, their limits, and their use in the owner's share.

Run with an isolated DATA_DIR and test database, never a live database (see test_provider_accounts).
"""

import importlib
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine

from open_webui.models.api_keys import HASH_PREFIX, PersonalKeys
from open_webui.models.usage_events import UsageEvent
from open_webui.models.users import ApiKey
from open_webui.routers import api_keys as routes
from open_webui.utils import account_slots, allowance_gate, api_key_limits, usage_ledger
from open_webui.utils import auth
from test_allowance import CAT, CAT_USER, EnforceCase, settings
from test_provider_accounts import ADMIN, OWNER, Response, chat_reply, read_stream

AUTH_CONFIG = {
    'auth.enable_api_keys': True,
    'user.permissions': {'features': {'api_keys': True}},
    'auth.api_key.endpoint_restrictions': False,
}
PEOPLE = {CAT: CAT_USER, OWNER.id: OWNER, ADMIN.id: ADMIN}


def http_request(path='/api/chat/completions'):
    return SimpleNamespace(
        scope={'path': path}, state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(redis=None))
    )


class KeyTests(EnforceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        account_slots._memory.clear()
        api_key_limits._limiters.clear()
        self.key_patches = [
            patch('open_webui.models.config.Config.get_many', AsyncMock(return_value=AUTH_CONFIG)),
            patch.object(auth, 'has_permission', AsyncMock(return_value=True)),
            patch.object(auth.Users, 'get_user_by_id', AsyncMock(side_effect=lambda user_id: PEOPLE.get(user_id))),
            patch.object(auth.Users, 'update_last_active_by_id', AsyncMock()),
        ]
        for item in self.key_patches:
            item.start()

    async def asyncTearDown(self):
        for item in self.key_patches:
            item.stop()
        await super().asyncTearDown()

    async def make(self, user=CAT_USER, name='Laptop', **limits):
        with patch.object(routes, 'has_permission', AsyncMock(return_value=True)):
            made = await routes.create_key(routes.KeyForm(name=name, limits=routes.KeyLimits(**limits)), user=user)
        return made['key'], made['secret']

    async def test_a_key_is_shown_once_and_stored_as_a_hash(self):
        key, secret = await self.make()
        self.assertTrue(secret.startswith('sk-'))
        [row] = await self.rows(ApiKey)
        self.assertTrue(row.key.startswith(HASH_PREFIX))
        self.assertNotIn(secret, str(row.key) + str(row.data))
        listed = await routes.key_views(CAT_USER)
        self.assertEqual(
            [(item['name'], item['hint']) for item in listed], [('Laptop', secret[:5] + '…' + secret[-4:])]
        )
        self.assertNotIn(secret, str(listed))
        # Several per member.
        await self.make(name='CI')
        self.assertEqual(len(await routes.key_views(CAT_USER)), 2)

    async def test_auth_carries_the_key_and_refuses_revoked_and_expired_ones(self):
        key, secret = await self.make(rpm=30)
        request = http_request()
        user = await auth.get_current_user_by_api_key(request, secret)
        self.assertEqual(
            (user.id, request.state.api_key_id, request.state.api_key_limits), (CAT, key['id'], {'rpm': 30})
        )
        with self.assertRaises(HTTPException) as caught:
            await auth.get_current_user_by_api_key(http_request(), 'sk-not-a-key')
        self.assertEqual(caught.exception.status_code, 401)
        [row] = await self.rows(ApiKey)
        self.assertIsNone(await PersonalKeys.find(row.key))
        await routes.revoke_key(key['id'], user=CAT_USER)
        with self.assertRaises(HTTPException) as caught:
            await auth.get_current_user_by_api_key(http_request(), secret)
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (401, 'This API key was revoked.'))
        expiring, secret = await self.make(name='Short')
        await PersonalKeys.update(CAT, expiring['id'], {'expires_at': int(time.time()) - 1})
        with self.assertRaises(HTTPException) as caught:
            await auth.get_current_user_by_api_key(http_request(), secret)
        self.assertIn('expired', caught.exception.detail)

    async def test_the_default_key_keeps_working_beside_personal_ones(self):
        await self.make()
        await auth.Users.update_user_api_key_by_id(CAT, 'sk-legacy-default-key')
        user = await auth.get_current_user_by_api_key(http_request(), 'sk-legacy-default-key')
        self.assertEqual(user.id, CAT)
        names = sorted(item['name'] for item in await routes.key_views(CAT_USER))
        self.assertEqual(names, ['Default key', 'Laptop'])
        # Making a new default key or deleting it leaves personal keys alone.
        await auth.Users.update_user_api_key_by_id(CAT, 'sk-another-default-key')
        await auth.Users.delete_user_api_key_by_id(CAT)
        self.assertEqual([item['name'] for item in await routes.key_views(CAT_USER)], ['Laptop'])

    async def test_a_members_key_reaches_only_the_gated_endpoints(self):
        _, secret = await self.make()
        for path in ('/api/chat/completions', '/api/v1/chat/completions', '/api/models', '/api/v1/models/model/limits'):
            await auth.get_current_user_by_api_key(http_request(path), secret)
        for path in (
            '/openai/chat/completions',
            '/api/v1/api-keys',
            '/api/v1/auths/api_key',
            '/api/v1/models/create',
            '/api/v1/provider-accounts',
            '/api/v1/chats/new',
        ):
            with self.assertRaises(HTTPException) as caught:
                await auth.get_current_user_by_api_key(http_request(path), secret)
            self.assertEqual(caught.exception.status_code, 403, path)
        # An admin's key keeps the whole API.
        _, admin_secret = await self.make(user=ADMIN, name='Ops')
        await auth.get_current_user_by_api_key(http_request('/api/v1/auths/api_key'), admin_secret)

    async def test_requests_a_minute(self):
        _, secret = await self.make(rpm=2)
        for _ in range(2):
            await auth.get_current_user_by_api_key(http_request(), secret)
        with self.assertRaises(HTTPException) as caught:
            await auth.get_current_user_by_api_key(http_request(), secret)
        self.assertEqual(caught.exception.status_code, 429)

    async def test_models_and_efforts(self):
        key, secret = await self.make(models=['account:openai_api:gpt-luna-6'], efforts=['low', 'medium'])
        request = http_request()
        await auth.get_current_user_by_api_key(request, secret)
        api_key_limits.check_request(request, 'account:openai_api:gpt-luna-6', {'reasoning_effort': 'low'})
        with self.assertRaises(HTTPException):
            api_key_limits.check_request(request, 'account:openai_api:gpt-6', {})
        with self.assertRaises(HTTPException):
            api_key_limits.check_request(request, 'account:openai_api:gpt-luna-6', {'reasoning': {'effort': 'high'}})

    async def test_limits_never_go_beyond_the_owner(self):
        with self.assertRaises(HTTPException):
            await self.make(concurrency=50)
        with self.assertRaises(HTTPException):
            await self.make(efforts=['ultra'])
        key, _ = await self.make(concurrency=2, daily_points=5, models=['b', 'a', 'a'])
        self.assertEqual(key['limits'], {'concurrency': 2, 'daily_points': 5.0, 'models': ['a', 'b']})

    async def ask_with(self, secret, replies, stream=False):
        request = http_request()
        await auth.get_current_user_by_api_key(request, secret)
        session = SimpleNamespace(post=AsyncMock(side_effect=replies))
        form = {'model': 'account:openai_api:gpt-6', 'messages': [{'role': 'user', 'content': 'Hi'}], 'stream': stream}
        request.state.auth_type = 'api_key'
        with patch('open_webui.utils.provider_account_proxy.get_session', AsyncMock(return_value=session)):
            from open_webui.utils import provider_account_proxy as proxy

            return await proxy.account_completion(request, form, CAT_USER)

    async def test_its_use_is_the_owners_and_names_the_key(self):
        key, secret = await self.make()
        reply = chat_reply('Hi') | {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 2.0}}
        await self.ask_with(secret, [Response(reply)])
        await usage_ledger.flush()
        [row] = await self.rows(UsageEvent)
        # $2 of a $20 budget window: 10 units, in Cat's share.
        self.assertEqual((row.user_id, row.api_key_id, row.source, row.est_units), (CAT, key['id'], 'api_key', 10))
        allowance_gate._cache.clear()
        standing = await allowance_gate.standing(CAT_USER, 'openai_api', 'gpt-6')
        self.assertEqual(standing['windows']['budget']['used'], 10)
        [listed] = await routes.key_views(CAT_USER)
        self.assertEqual(listed['usage']['week']['requests'], 1)
        self.assertGreater(listed['usage']['points_today']['openai_api'], 0)

    async def test_daily_pool_points(self):
        # The pool is 150 units; 6 points are 9 units, so a $2 request (10 units) uses them up.
        _, secret = await self.make(daily_points=6)
        reply = chat_reply('Hi') | {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 2.0}}
        await self.ask_with(secret, [Response(reply)])
        await usage_ledger.flush()
        allowance_gate._cache.clear()
        with self.assertRaises(HTTPException) as caught:
            await self.ask_with(secret, [Response(reply)])
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn('6 points', caught.exception.detail)

    async def test_requests_at_once(self):
        _, secret = await self.make(concurrency=1)
        events = [{'choices': [{'index': 0, 'delta': {'content': 'Hi'}, 'finish_reason': 'stop'}]}, '[DONE]']
        streaming = await self.ask_with(secret, [Response(events=events)], stream=True)
        with self.assertRaises(HTTPException) as caught:
            await self.ask_with(secret, [Response(chat_reply('Hi'))])
        self.assertEqual(caught.exception.status_code, 429)
        await read_stream(streaming.body_iterator)
        await account_slots.flush()
        # Free again once the stream is done.
        await self.ask_with(secret, [Response(chat_reply('Hi'))])
        await usage_ledger.flush()

    async def test_without_a_limit_a_key_runs_as_much_as_its_owner_in_enforce(self):
        _, secret = await self.make()
        self.config_patch.stop()
        self.config_patch = patch(
            'open_webui.models.config.Config.get', settings(**{'mologate.max_concurrent_chats': 1})
        )
        self.config_patch.start()
        events = [{'choices': [{'index': 0, 'delta': {'content': 'Hi'}, 'finish_reason': 'stop'}]}, '[DONE]']
        streaming = await self.ask_with(secret, [Response(events=events)], stream=True)
        with self.assertRaises(HTTPException):
            await self.ask_with(secret, [Response(chat_reply('Hi'))])
        await read_stream(streaming.body_iterator)
        await usage_ledger.flush()


class MigrationTests(unittest.TestCase):
    def test_api_keys_turn_on_for_existing_installs(self):
        migration = importlib.import_module('open_webui.migrations.versions.d2f8b5c1e9a4_personal_api_keys_on')
        self.assertEqual(migration.down_revision, 'b6d1e8f3a2c7')
        engine = create_engine('sqlite://')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.exec_driver_sql('CREATE TABLE config (key TEXT PRIMARY KEY, value JSON)')
            connection.exec_driver_sql("INSERT INTO config VALUES ('auth.enable_api_keys', 'false')")
            connection.exec_driver_sql(
                'INSERT INTO config VALUES (\'user.permissions\', \'{"features": {"api_keys": false, "notes": true}}\')'
            )
            migration.upgrade()
            rows = dict(connection.exec_driver_sql('SELECT key, value FROM config').all())
            self.assertEqual(rows['auth.enable_api_keys'], 'true')
            self.assertIn('"api_keys": true', rows['user.permissions'])
            self.assertIn('"notes": true', rows['user.permissions'])
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
