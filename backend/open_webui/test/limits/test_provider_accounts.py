"""Phase 1 regressions. Run with an isolated DATA_DIR and test database, never a live database."""

import asyncio
import importlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from open_webui.internal.db import async_engine, get_async_db_context
from open_webui.models.provider_accounts import (
    AccountView,
    ProviderAccount,
    ProviderAccounts,
    ProviderLogin,
    open_secret,
    seal_secret,
)
from open_webui.routers import openai, provider_accounts as routes
from open_webui.utils import codex_accounts, provider_account_proxy as proxy
from open_webui.utils.provider_accounts import (
    account_models,
    can_manage,
    can_use,
    choose_account,
    model_id,
    select_account,
    verify_api_key,
)
from open_webui.utils.provider_limits import (
    get_provider_limits,
    legacy_account_key,
    parse_rate_limit_headers,
    parse_zai_quota,
    record_provider_limits,
)

OWNER = SimpleNamespace(id='owner', role='user', name='Owner')
OTHER = SimpleNamespace(id='other', role='user', name='Other')
ADMIN = SimpleNamespace(id='admin', role='admin', name='Admin')


def account(**overrides):
    values = dict(
        id='test-account',
        owner_user_id=OWNER.id,
        provider='codex',
        plan='plus',
        external_account_id='external-id',
        secret=seal_secret({'home_id': str(uuid.uuid4())}),
        label='My account',
        sharing='private',
        capacity_weight=1,
        budget=None,
        allow_team_resets=True,
        status='ok',
        last_error=None,
        models=[{'id': 'test-model', 'name': 'Test model'}],
        created_at=1,
        updated_at=1,
    )
    return SimpleNamespace(**(values | overrides))


class PermissionTests(unittest.TestCase):
    def test_private_paused_pending_and_admin_boundaries(self):
        private = account()
        self.assertTrue(can_use(private, OWNER))
        self.assertFalse(can_use(private, OTHER))
        self.assertFalse(can_use(private, ADMIN))
        self.assertTrue(can_manage(private, ADMIN))
        self.assertFalse(can_manage(private, OTHER))
        self.assertTrue(can_use(account(sharing='shared'), OTHER))
        self.assertFalse(can_use(account(sharing='paused'), OWNER))
        self.assertFalse(can_use(account(status='needs_login'), OWNER))
        self.assertFalse(can_use(account(sharing='shared'), SimpleNamespace(id='pending', role='pending')))

    def test_owner_first_and_catalog_constrained_selection(self):
        shared = account(id='a', owner_user_id='other', sharing='shared')
        owned = account(id='b', created_at=2)
        self.assertEqual(choose_account([shared, owned], 'codex', 'test-model', OWNER).id, 'b')
        self.assertEqual(choose_account([owned, shared], 'codex', 'test-model', OWNER).id, 'b')
        for provider, model in [('codex', 'missing'), ('openai_api', 'test-model')]:
            with self.assertRaises(HTTPException):
                choose_account([owned], provider, model, OWNER)

    def test_encryption_and_public_response(self):
        secret = {'api_key': 'private-test-key', 'refresh_token': 'private-refresh'}
        sealed = seal_secret(secret)
        self.assertNotIn(secret['api_key'], sealed)
        self.assertEqual(open_secret(sealed), secret)
        view = AccountView.model_validate(account(secret=sealed)).model_dump()
        self.assertNotIn('secret', view)
        self.assertNotIn('external_account_id', view)

    def test_limits_headers_and_stable_legacy_identity(self):
        now = 1800000000
        headers = {
            'X-Codex-Primary-Used-Percent': '25',
            'x-codex-primary-reset-after-seconds': '60',
            'x-codex-primary-window-minutes': '300',
            'x-ratelimit-limit-tokens': '1000',
            'x-ratelimit-remaining-tokens': '500',
            'x-ratelimit-reset-tokens': '1m30s',
            'anthropic-ratelimit-requests-limit': '100',
            'anthropic-ratelimit-requests-remaining': '70',
            'anthropic-ratelimit-requests-reset': '2027-01-15T08:00:00Z',
        }
        windows = {w['id']: w for w in parse_rate_limit_headers(headers, now)}
        self.assertEqual(windows['primary']['used_percent'], 25)
        self.assertEqual(windows['primary']['resets_at'], now + 60)
        self.assertEqual(windows['tokens']['resets_at'], now + 90)
        self.assertAlmostEqual(windows['requests']['used_percent'], 30)
        self.assertEqual(parse_rate_limit_headers({'x-codex-primary-used-percent': 'NaN'}), [])
        self.assertEqual(
            legacy_account_key('https://provider/v1/', 'key'), legacy_account_key('https://provider/v1', 'key')
        )
        self.assertNotEqual(
            legacy_account_key('https://provider/v1', 'key'), legacy_account_key('https://provider/v1', 'other')
        )

    def test_append_only_migration_round_trip(self):
        migration = importlib.import_module('open_webui.migrations.versions.b1a7c9e4d205_add_provider_accounts')
        self.assertEqual(migration.down_revision, 'a6d4c8e2f1b3')
        target = os.environ.get('TEST_MIGRATION_DATABASE_URL', 'sqlite://')
        if target != 'sqlite://' and 'provider-tests' not in target:
            raise RuntimeError('Use an isolated provider-tests database.')
        engine = create_engine(target)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                names = inspect(connection).get_table_names()
                self.assertIn('provider_account', names)
                self.assertIn('provider_login', names)
                migration.downgrade()
                self.assertNotIn('provider_account', inspect(connection).get_table_names())
        engine.dispose()

    def test_zai_quota_observed_windows_and_unknowns(self):
        reset = int(time.time() + 600) * 1000
        result = {
            'success': True,
            'data': {
                'limits': [
                    {'type': 'CREDIT_LIMIT', 'unit': 3, 'number': 5, 'percentage': 0, 'nextResetTime': reset},
                    {'type': 'CREDIT_LIMIT', 'unit': 6, 'number': 1, 'percentage': 20, 'nextResetTime': reset},
                    {'type': 'TIME_LIMIT', 'unit': 9, 'number': 1, 'percentage': 30},
                ]
            },
        }
        windows = parse_zai_quota(result)
        self.assertEqual([w['window_minutes'] for w in windows], [300, 10080])
        self.assertEqual(windows[0]['used_percent'], 0)
        self.assertEqual(windows[0]['resets_at'], reset // 1000)
        self.assertEqual(parse_zai_quota({'success': True, 'data': {}}), [])
        self.assertEqual(parse_zai_quota({'success': False, 'data': result['data']}), [])


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This suite deliberately refuses the production engine.
        if 'provider-tests' not in str(async_engine.url):
            raise RuntimeError('Use an isolated provider-tests database.')
        async with async_engine.begin() as connection:
            for table in (ProviderLogin.__table__, ProviderAccount.__table__):
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
                await connection.run_sync(table.create)
        self.config_patch = patch('open_webui.models.config.Config.get', AsyncMock(return_value='observe'))
        self.config_patch.start()

    async def asyncTearDown(self):
        self.config_patch.stop()
        await codex_accounts.shutdown()
        async with async_engine.begin() as connection:
            for table in (ProviderLogin.__table__, ProviderAccount.__table__):
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
        await async_engine.dispose()

    async def create_account(self, **overrides):
        return await ProviderAccounts.insert(
            **(
                dict(
                    owner_user_id=OWNER.id,
                    provider='openai_api',
                    plan='api',
                    external_account_id='fingerprint',
                    label='Example',
                    secret={'api_key': 'test-key', 'base_url': 'https://api.openai.com/v1'},
                    sharing='private',
                    capacity_weight=1,
                    budget={'amount': 20, 'currency': 'USD'},
                    allow_team_resets=True,
                    status='ok',
                    models=[{'id': 'test-model'}],
                )
                | overrides
            )
        )

    async def test_storage_deduplication_and_dynamic_visibility(self):
        original = await self.create_account()
        with self.assertRaises(IntegrityError):
            await self.create_account(owner_user_id=OTHER.id)
        reloaded = await ProviderAccounts.get(original.id)
        self.assertEqual(reloaded.budget, {'amount': 20, 'currency': 'USD'})
        self.assertNotIn('test-key', reloaded.secret)
        self.assertEqual(await account_models(OTHER), [])
        self.assertEqual(await account_models(ADMIN), [])
        own_models = await account_models(OWNER)
        self.assertEqual(own_models[0]['id'], 'account:openai_api:test-model')
        await ProviderAccounts.update(original.id, {'sharing': 'shared'})
        self.assertEqual((await select_account(own_models[0]['id'], OTHER)).id, original.id)
        await ProviderAccounts.update(original.id, {'sharing': 'paused'})
        with self.assertRaises(HTTPException):
            await select_account(own_models[0]['id'], OWNER)
        await ProviderAccounts.delete(original.id)
        self.assertIsNone(await ProviderAccounts.get(original.id))

    async def test_off_switch(self):
        with patch('open_webui.models.config.Config.get', AsyncMock(return_value='off')):
            self.assertEqual(await account_models(OWNER), [])
            with self.assertRaises(HTTPException) as error:
                await select_account('account:codex:test-model', OWNER)
            self.assertEqual(error.exception.status_code, 503)

    async def test_user_deletion_revokes_shared_accounts(self):
        await self.create_account(sharing='shared')
        await codex_accounts.delete_user_accounts(OWNER.id)
        self.assertEqual(await ProviderAccounts.list(), [])

    async def test_limit_storage_with_redis_failure(self):
        redis = SimpleNamespace(set=AsyncMock(side_effect=OSError), get=AsyncMock(side_effect=OSError))
        await record_provider_limits(redis, 'stable-account', {'x-codex-primary-used-percent': '15'})
        snapshot = await get_provider_limits(redis, 'stable-account')
        self.assertEqual(snapshot['windows'][0]['used_percent'], 15)
        self.assertIsNone(await get_provider_limits(redis, 'other-account'))

    async def test_real_appserver_protocol_device_login_with_fake_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'fake-codex'
            binary.write_text(
                """#!"""
                + sys.executable
                + """
import json, os, sys
from pathlib import Path
for raw in sys.stdin:
    request = json.loads(raw)
    if 'id' not in request: continue
    method = request['method']
    result = {}
    if method == 'account/login/start':
        Path(os.environ['CODEX_HOME'], 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'fake-chatgpt-id', 'access_token': 'fake-token'}}))
        result = {'loginId': 'login-1', 'userCode': 'FAKE-CODE', 'verificationUrl': 'https://auth.openai.com/codex/device'}
    elif method == 'account/read': result = {'account': {'type': 'chatgpt', 'planType': 'plus'}}
    elif method == 'model/list': result = {'data': [{'id': 'test-model', 'model': 'test-model', 'displayName': 'Test'}]}
    elif method == 'account/rateLimits/read': result = {'rateLimits': {'primary': {'usedPercent': 10}}}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
    if method == 'account/login/start':
        print(json.dumps({'method': 'account/login/completed', 'params': {'loginId': 'login-1', 'success': True}}), flush=True)
"""
            )
            binary.chmod(0o700)
            with (
                patch.dict(os.environ, {'MOLOGATE_CODEX_BINARY': str(binary)}),
                patch.object(codex_accounts, 'ACCOUNT_ROOT', Path(temporary) / 'accounts'),
            ):
                identifier = await codex_accounts.start_login(OWNER, 'Test ChatGPT', 'private')
                task = codex_accounts._tasks.get(identifier)
                if task:
                    await asyncio.wait_for(asyncio.shield(task), 10)
                status = await codex_accounts.login_status(identifier, OWNER)
                self.assertEqual(status['status'], 'completed')
                self.assertNotIn('user_code', status)
                with self.assertRaises(HTTPException):
                    await codex_accounts.login_status(identifier, OTHER)
                saved = await ProviderAccounts.get(status['account_id'])
                self.assertEqual(saved.external_account_id, 'fake-chatgpt-id')
                home = codex_accounts.account_home(identifier)
                self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((home / 'auth.json').stat().st_mode), 0o600)
                tokens, _ = await codex_accounts.refresh_account(saved)
                self.assertEqual(tokens['account_id'], 'fake-chatgpt-id')
                # A second login to the same provider account must not register another owner.
                duplicate = await codex_accounts.start_login(OTHER, 'Duplicate', 'shared')
                duplicate_task = codex_accounts._tasks.get(duplicate)
                if duplicate_task:
                    await asyncio.wait_for(asyncio.shield(duplicate_task), 10)
                self.assertEqual((await codex_accounts.login_status(duplicate, OTHER))['status'], 'error')
                self.assertEqual(len(await ProviderAccounts.list()), 1)
                await codex_accounts.disconnect(saved)
                self.assertFalse(home.exists())


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.state.redis = None
        self.app.include_router(routes.router, prefix='/accounts')
        self.app.dependency_overrides[routes.get_verified_user] = lambda: OWNER
        self.app.dependency_overrides[routes.require_accounts_enabled] = lambda: None
        self.client = TestClient(self.app)

    def test_validation_never_echoes_secrets(self):
        response = self.client.post(
            '/accounts', json={'api_key': 'never-echo-this-secret', 'label': {'secret': 'never-echo-this-secret'}}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('never-echo', response.text)

    def test_other_member_cannot_edit_delete_or_verify(self):
        with patch.object(ProviderAccounts, 'get', AsyncMock(return_value=account(owner_user_id='other'))):
            for method, path, body in [
                ('PATCH', '/accounts/id', {'sharing': 'shared'}),
                ('DELETE', '/accounts/id', None),
                ('POST', '/accounts/id/verify', None),
            ]:
                self.assertEqual(self.client.request(method, path, json=body).status_code, 404)

    def test_admin_can_manage_but_response_never_contains_credentials(self):
        self.app.dependency_overrides[routes.get_verified_user] = lambda: ADMIN
        with (
            patch.object(ProviderAccounts, 'get', AsyncMock(return_value=account())),
            patch.object(ProviderAccounts, 'update', AsyncMock(return_value=account())),
        ):
            result = self.client.patch('/accounts/id', json={'sharing': 'paused'})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('secret', result.json())
            self.assertNotIn('external_account_id', result.json())

    def test_budget_required_and_no_owner_spoofing(self):
        form = {'provider': 'openai_api', 'label': 'test', 'api_key': 'test-secret'}
        self.assertEqual(self.client.post('/accounts', json=form).status_code, 400)
        self.assertEqual(self.client.post('/accounts', json=form | {'owner_user_id': 'other'}).status_code, 422)


class Response:
    def __init__(self, data=None, status=200, events=None):
        self.status = status
        self.data = data or {}
        self.headers = {'Content-Type': 'text/event-stream' if events is not None else 'application/json'}
        self.closed = False
        self.content = self
        self.events = events or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()

    async def json(self):
        return self.data

    def close(self):
        self.closed = True

    async def iter_chunks(self):
        raw = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\r\n\r\n' for event in self.events).encode()
        for index in range(0, len(raw), 3):
            yield raw[index : index + 3], False


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_public_catalog_does_not_verify_key(self):
        seen = []

        def get(url, **kwargs):
            seen.append(url)
            self.assertFalse(kwargs['allow_redirects'])
            return Response(status=401) if url.endswith('/key') else Response({'data': [{'id': 'model'}]})

        session = SimpleNamespace(get=get)
        manager = Response()
        manager.__class__ = type('SessionContext', (Response,), {'__aenter__': AsyncMock(return_value=session)})
        with patch('open_webui.utils.provider_accounts.aiohttp.ClientSession', return_value=manager):
            with self.assertRaises(HTTPException):
                await verify_api_key('openrouter', 'invalid-key')
        self.assertEqual(seen, ['https://openrouter.ai/api/v1/key'])

    async def test_codex_stream_unicode_tools_and_usage(self):
        events = [
            {'type': 'response.created', 'response': {'id': 'response-1'}},
            {'type': 'response.output_text.delta', 'delta': 'Hello 世界'},
            {
                'type': 'response.output_item.added',
                'item': {'type': 'function_call', 'id': 'item-1', 'call_id': 'call-1', 'name': 'lookup'},
            },
            {'type': 'response.function_call_arguments.delta', 'item_id': 'item-1', 'delta': '{"q":"test"}'},
            {
                'type': 'response.completed',
                'response': {'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}},
            },
        ]
        response = Response(events=events)
        chunks = [chunk async for chunk in proxy.codex_chat_stream(response, 'account:codex:test-model')]
        data = [json.loads(chunk[6:]) for chunk in chunks if chunk != 'data: [DONE]\n\n']
        self.assertEqual(data[0]['choices'][0]['delta']['content'], 'Hello 世界')
        self.assertEqual(data[1]['choices'][0]['delta']['tool_calls'][0]['id'], 'call-1')
        self.assertEqual(data[-1]['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(data[-1]['usage']['prompt_tokens'], 10)
        self.assertTrue(response.closed)

    async def test_interrupted_stream_reports_error_and_closes(self):
        response = Response(events=[{'type': 'response.output_text.delta', 'delta': 'partial'}])
        result = ''.join([chunk async for chunk in proxy.codex_chat_stream(response, 'test')])
        self.assertIn('ended before completion', result)
        self.assertTrue(response.closed)

    async def test_non_streaming_codex_and_secret_safe_failure(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        result = {
            'id': 'response-1',
            'model': 'test-model',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello'}]}],
            'usage': {'input_tokens': 12, 'output_tokens': 3},
        }
        upstream = Response(events=[{'type': 'response.completed', 'response': result}])
        session = SimpleNamespace(post=AsyncMock(return_value=upstream))
        with (
            patch.object(proxy, 'select_account', AsyncMock(return_value=account())),
            patch.object(
                proxy,
                'refresh_account',
                AsyncMock(return_value=({'access_token': 'secret-token', 'account_id': 'id'}, {})),
            ),
            patch.object(proxy, 'get_session', AsyncMock(return_value=session)),
        ):
            answer = await proxy.account_completion(
                request, {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}, OWNER
            )
            self.assertEqual(answer['choices'][0]['message']['content'], 'Hello')
            self.assertEqual(answer['usage']['prompt_tokens'], 12)
            sent = session.post.call_args.kwargs
            self.assertFalse(sent['json']['store'])
            self.assertTrue(sent['json']['stream'])
            self.assertEqual(sent['headers']['ChatGPT-Account-Id'], 'id')
            self.assertTrue(upstream.closed)
            session.post.side_effect = RuntimeError('secret-token')
            with self.assertRaises(HTTPException) as error:
                await proxy.account_completion(request, {'model': 'account:codex:test-model'}, OWNER)
            self.assertNotIn('secret-token', error.exception.detail)

    async def test_chat_responses_and_proxy_use_account_route(self):
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()), method='POST')
        payload = {'model': 'account:codex:test-model', 'messages': []}
        request.body = AsyncMock(return_value=json.dumps(payload).encode())
        with (
            patch.object(openai, 'account_completion', AsyncMock(return_value={'ok': True})) as route,
            patch.object(openai.Models, 'get_model_by_id', AsyncMock(return_value=None)),
            patch.object(openai, 'ENABLE_OPENAI_API_PASSTHROUGH', True),
        ):
            self.assertEqual(await openai.generate_chat_completion(request, payload, OWNER), {'ok': True})
            self.assertEqual(
                await openai.responses(request, openai.ResponsesForm(model=payload['model'], input='Hi'), OWNER),
                {'ok': True},
            )
            self.assertEqual(await openai.proxy('responses', request, OWNER), {'ok': True})
            self.assertEqual(route.await_count, 3)


if __name__ == '__main__':
    unittest.main()
