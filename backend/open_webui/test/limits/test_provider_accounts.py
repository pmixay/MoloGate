"""Phase 1 regressions. Run with an isolated DATA_DIR and test database, never a live database."""

import asyncio
import contextlib
import importlib
import json
import os
import re
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, quote, urlparse

from aiohttp import web
from aiohttp.test_utils import TestServer
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from open_webui.internal.db import async_engine, get_async_db_context
from open_webui.models.provider_accounts import (
    AccountView,
    LimitResetEvent,
    LimitResetEvents,
    ProviderAccount,
    ProviderAccounts,
    ProviderLogin,
    open_secret,
    seal_secret,
)
from open_webui.routers import openai, provider_accounts as routes
from open_webui.utils import codex_accounts, codex_lb, provider_account_proxy as proxy
from open_webui.utils.codex_lb import CodexLBError
from open_webui.utils.provider_accounts import (
    TEAM,
    account_models,
    can_manage,
    catalog_model,
    key_fingerprint,
    can_use,
    choose_account,
    select_account,
    verify_api_key,
)
from open_webui.utils.provider_limits import (
    count_account_request,
    get_provider_limits,
    legacy_account_key,
    parse_rate_limit_headers,
    parse_zai_quota,
    record_provider_limits,
    record_provider_snapshot,
    snapshot_exhausted,
    split_reading,
    with_team_use,
)

OWNER = SimpleNamespace(id='owner', role='user', name='Owner')
OTHER = SimpleNamespace(id='other', role='user', name='Other')
ADMIN = SimpleNamespace(id='admin', role='admin', name='Admin')
GENERATED_NAME = re.compile(r'Account #[0-9A-Za-z]{6}')
CALLBACK = 'http://localhost:1455/auth/callback'


def account(**overrides):
    values = dict(
        id='test-account',
        owner_user_id=OWNER.id,
        provider='codex',
        plan='plus',
        external_account_id='external-id',
        secret=seal_secret({'lb_account_id': 'lb-test', 'lb_api_key': 'sk-clb-test', 'lb_api_key_id': 'key-test'}),
        label='Account #Ab3dE9',
        sharing='private',
        capacity_weight=1,
        budget=None,
        status='ok',
        last_error=None,
        models=[{'id': 'test-model', 'name': 'Test model'}],
        created_at=1,
        updated_at=1,
    )
    return SimpleNamespace(**(values | overrides))


def iso(seconds_from_now):
    return datetime.fromtimestamp(time.time() + seconds_from_now, timezone.utc).isoformat().replace('+00:00', 'Z')


def lb_summary(account_id, remaining=90, status='active', reset_in=3600, credits=0, plan='plus', refreshed=None):
    """An AccountSummary from codex-lb v1.24.0's GET /api/accounts."""
    return {
        'accountId': account_id,
        'chatgptAccountId': 'chatgpt-' + account_id,
        'email': account_id + '@example.com',
        'planType': plan,
        'status': status,
        'usage': {'primaryRemainingPercent': remaining, 'secondaryRemainingPercent': 80},
        'resetAtPrimary': None if reset_in is None else iso(reset_in),
        'resetAtSecondary': iso(7 * 86400),
        'windowMinutesPrimary': 300,
        'windowMinutesSecondary': 10080,
        'availableResetCredits': credits,
        'resetCreditNearestExpiresAt': None,
        'lastRefreshAt': refreshed or '2026-10-01T00:00:00Z',
    }


def codex_model(slug, name=None, **overrides):
    """A CodexModelEntry from codex-lb v1.24.0's GET /backend-api/codex/models."""
    return {
        'slug': slug,
        'display_name': name or slug,
        'description': '',
        'base_instructions': 'Long Codex instructions that Mologate never stores.',
        'default_reasoning_level': 'medium',
        'supported_reasoning_levels': [
            {'effort': 'low', 'description': 'Fast'},
            {'effort': 'medium', 'description': 'Balanced'},
            {'effort': 'ultra', 'description': 'Deepest'},
        ],
        'supported_in_api': True,
        'context_window': 272000,
        'input_modalities': ['text', 'image'],
        'visibility': 'list',
        'priority': 1,
    } | overrides


def coupon(identifier, expires_in, status='available'):
    return {
        'id': identifier,
        'resetType': 'codexRateLimits',
        'status': status,
        'expiresAt': None if expires_in is None else iso(expires_in),
    }


def coupons(*credits):
    return {
        'availableCount': len(credits),
        'nearestExpiresAt': None,
        'credits': [coupon(*credit) for credit in credits],
    }


def lb_error(status, code):
    return web.json_response({'error': {'code': code, 'message': 'Refused'}}, status=status)


class FakeCodexLB:
    """codex-lb v1.24.0's management API and Codex proxy route, in memory."""

    def __init__(self):
        self.accounts = {}
        self.keys = {}
        self.flows = {}
        self.credits = {}
        self.settings = {
            'apiKeyAuthEnabled': False,
            'requestLogRetentionOverrideDays': None,
            'usageHistoryRetentionOverrideDays': None,
            'version': 1,
        }
        self.settings_locked = False
        self.ignore_assignment = False
        self.consume_error = None
        self.next_account = None
        self.refreshes = 0
        self.requests = []
        self.key_requests = []
        self.starts = []
        self.imports = []
        self.callbacks = []
        self.consumed = []
        self.deleted_accounts = []
        self.inference = []
        self.replies = {}
        # GET /backend-api/codex/models: the Codex catalog, with hidden and API-unsupported entries.
        self.catalog = [
            codex_model('test-model', 'Test model'),
            codex_model('hidden-model', 'Hidden', visibility='hide'),
            codex_model('internal-model', 'Internal', supported_in_api=False),
        ]
        self.app = web.Application(middlewares=[self.record])
        routes = self.app.router
        routes.add_get('/api/settings', self.get_settings)
        routes.add_put('/api/settings', self.put_settings)
        routes.add_get('/api/accounts', self.list_accounts)
        routes.add_post('/api/accounts/import', self.import_account)
        routes.add_delete('/api/accounts/{id}', self.delete_account)
        routes.add_put('/api/accounts/{id}/alias', self.alias)
        routes.add_get('/api/accounts/{id}/rate-limit-reset-credits', self.get_credits)
        routes.add_post('/api/accounts/{id}/rate-limit-reset-credits/consume', self.consume)
        routes.add_get('/backend-api/codex/models', self.codex_models)
        routes.add_post('/api/oauth/start', self.oauth_start)
        routes.add_get('/api/oauth/status', self.oauth_status)
        routes.add_post('/api/oauth/manual-callback', self.manual_callback)
        routes.add_post('/api/api-keys/', self.create_key)
        routes.add_delete('/api/api-keys/{id}', self.delete_key)
        routes.add_post('/backend-api/codex/responses', self.responses)

    @web.middleware
    async def record(self, request, handler):
        self.requests.append((request.method, request.path, {key.lower() for key in request.headers}))
        return await handler(request)

    def sign_in(self, flow_id, account_id):
        """OpenAI accepted the sign-in: codex-lb saves the account, in place when it already has it."""
        self.refreshes += 1
        refreshed = f'2026-10-11T00:00:{self.refreshes:02d}Z'
        self.flows[flow_id]['status'] = 'success'
        if account_id in self.accounts:
            self.accounts[account_id].update(status='active', lastRefreshAt=refreshed)
        else:
            self.accounts[account_id] = lb_summary(account_id, plan='pro', refreshed=refreshed)

    async def get_settings(self, request):
        return web.json_response(self.settings)

    async def put_settings(self, request):
        body = await request.json()
        if not self.settings_locked:
            self.settings.update({key: value for key, value in body.items() if key in self.settings})
            self.settings['version'] += 1
        return web.json_response(self.settings)

    async def list_accounts(self, request):
        return web.json_response({'accounts': list(self.accounts.values())})

    async def import_account(self, request):
        form = await request.post()
        raw = form['auth_json'].file.read()
        self.imports.append(raw)
        account_id = 'lb-' + json.loads(raw)['tokens']['account_id']
        self.accounts[account_id] = lb_summary(account_id, plan='pro')
        return web.json_response(
            {'accountId': account_id, 'email': 'x@example.com', 'planType': 'pro', 'status': 'active'}
        )

    async def delete_account(self, request):
        account_id = request.match_info['id']
        self.deleted_accounts.append((account_id, request.query.get('delete_history')))
        if self.accounts.pop(account_id, None) is None:
            return lb_error(404, 'account_not_found')
        return web.json_response({'status': 'deleted'})

    async def alias(self, request):
        self.accounts[request.match_info['id']]['alias'] = (await request.json())['alias']
        return web.json_response({'accountId': request.match_info['id']})

    async def get_credits(self, request):
        return web.json_response(self.credits.get(request.match_info['id']))

    async def consume(self, request):
        account_id = request.match_info['id']
        self.consumed.append((account_id, await request.json()))
        await asyncio.sleep(0.01)
        if self.consume_error:
            return lb_error(*self.consume_error)
        self.accounts[account_id].update(
            status='active', usage={'primaryRemainingPercent': 100, 'secondaryRemainingPercent': 100}
        )
        self.credits.pop(account_id, None)
        return web.json_response({'code': 'reset', 'windowsReset': 1, 'redeemedAt': iso(0)})

    async def codex_models(self, request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if self.settings['apiKeyAuthEnabled'] and not any(key['key'] == token for key in self.keys.values()):
            return lb_error(401, 'invalid_api_key')
        return web.json_response({'models': self.catalog, 'data': []})

    async def oauth_start(self, request):
        body = await request.json()
        self.starts.append(body)
        method = body.get('forceMethod')
        flow_id = f'flow-{len(self.flows) + 1}'
        if method == 'browser':
            state = 'state-' + flow_id
            self.flows[flow_id] = {'method': 'browser', 'status': 'pending', 'state': state}
            url = 'https://auth.openai.com/oauth/authorize?response_type=code'
            url += f'&redirect_uri={quote(CALLBACK, safe="")}&state={state}'
            return web.json_response(
                {'flowId': flow_id, 'method': 'browser', 'authorizationUrl': url, 'callbackUrl': CALLBACK}
            )
        if method == 'device':
            self.flows[flow_id] = {'method': 'device', 'status': 'pending'}
            return web.json_response(
                {
                    'flowId': flow_id,
                    'method': 'device',
                    'verificationUrl': 'https://auth.openai.com/codex/device',
                    'userCode': 'FAKE-CODE',
                    'deviceAuthId': 'device-auth',
                    'intervalSeconds': 5,
                    'expiresInSeconds': 900,
                }
            )
        # Like codex-lb: without a forced method no flow starts once accounts exist.
        return web.json_response({'method': 'browser'})

    async def oauth_status(self, request):
        flow = self.flows.get(request.query.get('flowId'))
        return web.json_response({'status': flow['status'] if flow else 'pending', 'errorMessage': None})

    async def manual_callback(self, request):
        body = await request.json()
        self.callbacks.append(body)
        flow = self.flows.get(body.get('flowId'))
        if (
            flow
            and flow['status'] == 'pending'
            and body['callbackUrl'] == f'{CALLBACK}?code=fake-code&state={flow["state"]}'
        ):
            self.sign_in(body['flowId'], self.next_account)
            return web.json_response({'status': 'success', 'errorMessage': None})
        return web.json_response({'status': 'error', 'errorMessage': 'Invalid OAuth callback'})

    async def create_key(self, request):
        body = await request.json()
        self.key_requests.append(body)
        assigned = body.get('assignedAccountIds') or []
        if any(account_id not in self.accounts for account_id in assigned):
            return lb_error(400, 'invalid_api_key_payload')
        if self.ignore_assignment:
            assigned = []
        key_id = f'key-{len(self.key_requests)}'
        self.keys[key_id] = {'key': 'sk-clb-' + key_id, 'accounts': assigned}
        return web.json_response(
            {
                'id': key_id,
                'name': body['name'],
                'key': 'sk-clb-' + key_id,
                'assignedAccountIds': assigned,
                'accountAssignmentScopeEnabled': bool(assigned),
            }
        )

    async def delete_key(self, request):
        if self.keys.pop(request.match_info['id'], None) is None:
            return lb_error(404, 'not_found')
        return web.Response(status=204)

    async def responses(self, request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        key = next((key for key in self.keys.values() if key['key'] == token), None)
        if not self.settings['apiKeyAuthEnabled'] or key is None:
            return lb_error(401, 'invalid_api_key')
        account_id = key['accounts'][0]
        headers = {name.lower(): value for name, value in request.headers.items()}
        self.inference.append({'account': account_id, 'headers': headers, 'body': await request.json()})
        if account_id in self.replies:
            return lb_error(*self.replies[account_id])
        result = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello'}]}]}
        events = [
            {'type': 'response.created', 'response': {'id': 'r1'}},
            {'type': 'response.completed', 'response': result},
        ]
        return web.Response(
            text=''.join('data: ' + json.dumps(event) + '\n\n' for event in events), content_type='text/event-stream'
        )


class PermissionTests(unittest.TestCase):
    def test_team_accounts_serve_everyone_and_only_admins_manage_them(self):
        team = account(owner_user_id=None, sharing='shared')
        self.assertTrue(can_use(team, OWNER))
        self.assertTrue(can_use(team, OTHER))
        self.assertFalse(can_manage(team, OWNER))
        self.assertTrue(can_manage(team, ADMIN))
        self.assertFalse(can_use(account(owner_user_id=None, sharing='paused'), ADMIN))

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

    def test_exhaustion_is_reported_never_assumed(self):
        now = time.time()
        empty = {'windows': [{'id': 'primary', 'used_percent': 100, 'resets_at': now + 60}]}
        self.assertTrue(snapshot_exhausted(empty, now))
        self.assertFalse(snapshot_exhausted(empty, now + 61))
        self.assertFalse(snapshot_exhausted({'windows': [{'id': 'primary', 'used_percent': 99}]}, now))
        self.assertFalse(snapshot_exhausted(None, now))
        # codex-lb's own verdict, or a used-up window that has not reset yet.
        self.assertTrue(codex_lb.exhausted(lb_summary('a', remaining=0)))
        self.assertTrue(codex_lb.exhausted(lb_summary('a', remaining=0, reset_in=None)))
        self.assertFalse(codex_lb.exhausted(lb_summary('a', remaining=0, reset_in=-60)))
        self.assertTrue(codex_lb.exhausted(lb_summary('a', remaining=50, status='rate_limited')))
        self.assertTrue(codex_lb.exhausted(lb_summary('a', remaining=50, status='quota_exceeded')))
        self.assertFalse(codex_lb.exhausted(lb_summary('a', remaining=1)))
        self.assertFalse(codex_lb.exhausted({'accountId': 'a', 'status': 'active'}))
        windows = {window['id']: window for window in codex_lb.windows(lb_summary('a', remaining=25.5))}
        self.assertEqual(windows['primary']['used_percent'], 74.5)
        self.assertEqual(windows['primary']['window_minutes'], 300)
        self.assertAlmostEqual(windows['primary']['resets_at'], time.time() + 3600, delta=5)
        self.assertEqual(windows['secondary']['window_minutes'], 10080)
        self.assertEqual(codex_lb.snapshot(lb_summary('a', credits=2))['reset_credits'], 2)

    def test_reset_coupons_soonest_expiry_first(self):
        listing = coupons(('later', 7200), ('never', None), ('soon', 600), ('expired', -60), ('spent', 60, 'redeemed'))
        ids = [credit['id'] for credit in codex_lb.available_credits(listing)]
        self.assertEqual(ids, ['soon', 'later', 'never'])
        self.assertEqual(codex_lb.available_credits({'availableCount': 0, 'credits': [coupon('x', 600)]}), [])
        self.assertEqual(codex_lb.available_credits(None), [])
        self.assertEqual(proxy.wait_text(30), '1 min')
        self.assertEqual(proxy.wait_text(3 * 3600 + 12 * 60), '3 h 12 min')
        self.assertEqual(proxy.wait_text(2 * 3600), '2 h')
        self.assertEqual(proxy.wait_text(50 * 3600), '2 d 2 h')

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
        resets = importlib.import_module('open_webui.migrations.versions.c3d9e7a5b1f0_add_limit_reset_event')
        self.assertEqual(resets.down_revision, migration.revision)
        engine = create_engine(target)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                resets.upgrade()
                names = inspect(connection).get_table_names()
                self.assertIn('provider_account', names)
                self.assertIn('provider_login', names)
                self.assertIn('limit_reset_event', names)
                resets.downgrade()
                self.assertNotIn('limit_reset_event', inspect(connection).get_table_names())
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


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This suite deliberately refuses the production engine.
        if 'provider-tests' not in str(async_engine.url):
            raise RuntimeError('Use an isolated provider-tests database.')
        async with async_engine.begin() as connection:
            for table in (ProviderLogin.__table__, ProviderAccount.__table__, LimitResetEvent.__table__):
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
                await connection.run_sync(table.create)
        self.config_patch = patch('open_webui.models.config.Config.get', AsyncMock(return_value='observe'))
        self.config_patch.start()

    async def asyncTearDown(self):
        self.config_patch.stop()
        await codex_accounts.shutdown()
        async with async_engine.begin() as connection:
            for table in (ProviderLogin.__table__, ProviderAccount.__table__, LimitResetEvent.__table__):
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
                    secret={'api_key': 'test-key', 'base_url': 'https://api.openai.com/v1'},
                    sharing='private',
                    capacity_weight=1,
                    budget={'amount': 20, 'currency': 'USD'},
                    status='ok',
                    models=[{'id': 'test-model'}],
                )
                | overrides
            )
        )


class DatabaseTests(DatabaseCase):
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
        self.assertEqual(own_models[0]['provider_account_sharing'], 'private')
        await ProviderAccounts.update(original.id, {'sharing': 'shared'})
        self.assertEqual((await account_models(OTHER))[0]['provider_account_sharing'], 'shared')
        self.assertEqual((await select_account(own_models[0]['id'], OTHER)).id, original.id)
        await ProviderAccounts.update(original.id, {'sharing': 'private'})
        self.assertEqual((await account_models(OWNER))[0]['provider_account_sharing'], 'private')
        self.assertEqual(await account_models(OTHER), [])
        await ProviderAccounts.update(original.id, {'sharing': 'paused'})
        self.assertEqual(await account_models(OWNER), [])
        with self.assertRaises(HTTPException):
            await select_account(own_models[0]['id'], OWNER)
        await ProviderAccounts.delete(original.id)
        self.assertIsNone(await ProviderAccounts.get(original.id))

    async def test_accounts_get_generated_unique_names(self):
        first = await self.create_account()
        self.assertRegex(first.label, GENERATED_NAME)
        # A clash with an existing name draws again.
        names = iter([first.label, first.label, 'Account #Zz9Yy8'])
        with patch('open_webui.models.provider_accounts.generate_label', side_effect=lambda: next(names)):
            second = await self.create_account(external_account_id='second')
        self.assertEqual(second.label, 'Account #Zz9Yy8')

    async def test_catalog_sharing_aggregates_only_healthy_accounts_in_any_order(self):
        private = account()
        shared = account(id='shared', sharing='shared', owner_user_id=OTHER.id)
        for accounts in ([private, shared], [shared, private]):
            with patch.object(ProviderAccounts, 'list', AsyncMock(return_value=accounts)):
                for user in (None, OWNER, OTHER, ADMIN):
                    catalog = await account_models(user)
                    self.assertEqual(len(catalog), 1)
                    self.assertEqual(catalog[0]['provider_account_sharing'], 'shared')
                self.assertEqual(await account_models(SimpleNamespace(id='pending', role='pending')), [])
        for unavailable in (account(sharing='paused'), account(sharing='shared', status='needs_login')):
            with patch.object(ProviderAccounts, 'list', AsyncMock(return_value=[private, unavailable])):
                self.assertEqual((await account_models())[0]['provider_account_sharing'], 'private')
                self.assertEqual(await account_models(OTHER), [])

    async def test_catalog_and_chat_access_follow_sharing_despite_saved_model_acl(self):
        from open_webui.utils import models as model_utils

        original = await self.create_account(sharing='shared')
        catalog = await account_models()
        # A saved base-model override must not make a shared account private.
        catalog[0]['info'] = {'id': catalog[0]['id'], 'user_id': OWNER.id, 'access_grants': []}
        with (
            patch.object(model_utils, 'BYPASS_MODEL_ACCESS_CONTROL', False),
            patch.object(model_utils.Groups, 'get_groups_by_member_id', AsyncMock(return_value=[])),
            patch.object(model_utils.AccessGrants, 'get_accessible_resource_ids', AsyncMock(return_value=[])),
        ):
            self.assertEqual(await model_utils.get_filtered_models(catalog, OTHER), catalog)
            await model_utils.check_model_access(OTHER, catalog[0])
            await ProviderAccounts.update(original.id, {'sharing': 'private'})
            # Even an old public catalog and stale grants cannot keep credentials shared.
            catalog[0]['info']['access_grants'] = [
                {'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}
            ]
            self.assertEqual(await model_utils.get_filtered_models(catalog, OTHER), [])
            with self.assertRaises(HTTPException):
                await model_utils.check_model_access(OTHER, catalog[0])

    async def test_off_switch(self):
        with patch('open_webui.models.config.Config.get', AsyncMock(return_value='off')):
            self.assertEqual(await account_models(OWNER), [])
            with self.assertRaises(HTTPException) as error:
                await select_account('account:codex:test-model', OWNER)
            self.assertEqual(error.exception.status_code, 503)

    async def test_only_a_refused_key_takes_a_shared_account_offline(self):
        shared = await self.create_account(sharing='shared')
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        form = {'model': 'account:openai_api:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        # A 403 answers one request (a moderation flag, say); a 401 means the key itself is refused.
        for status, expected in ((403, 'ok'), (401, 'needs_login')):
            session = SimpleNamespace(post=AsyncMock(return_value=Response(status=status)))
            with patch.object(proxy, 'get_session', AsyncMock(return_value=session)):
                with self.assertRaises(HTTPException) as refused:
                    await proxy.account_completion(request, form, OTHER)
            self.assertEqual(refused.exception.status_code, status)
            self.assertEqual((await ProviderAccounts.get(shared.id)).status, expected)

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

    async def test_reset_coupon_count_survives_header_updates(self):
        await codex_accounts.record_summary(None, 'coupon-count', lb_summary('a', credits=2))
        await record_provider_limits(None, 'coupon-count', {'x-codex-primary-used-percent': '40'})
        snapshot = await get_provider_limits(None, 'coupon-count')
        self.assertEqual(snapshot['windows'][0]['used_percent'], 40)
        self.assertEqual(snapshot['reset_credits'], 2)


class CodexLBTests(DatabaseCase):
    """ChatGPT accounts against a fake codex-lb speaking v1.24.0's HTTP shapes."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake = FakeCodexLB()
        self.server = TestServer(self.fake.app, host='127.0.0.1')
        await self.server.start_server()
        self.temporary = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(codex_lb, 'BASE_URL', str(self.server.make_url('')).rstrip('/')),
            patch.object(codex_lb, '_accounts_cache', None),
            patch.object(codex_lb, '_models_cache', None),
            patch.object(codex_lb, '_settings_checked', None),
            patch.object(codex_accounts, 'ACCOUNT_ROOT', Path(self.temporary.name) / 'codex'),
            patch.object(codex_accounts, '_login_lock', asyncio.Lock()),
            patch.object(codex_accounts, '_reset_lock', asyncio.Lock()),
        ]
        for active in self.patches:
            active.start()

    async def asyncTearDown(self):
        # codex-lb treats an empty assignment as the whole pool: every key names exactly one account.
        self.assertTrue(all(len(body.get('assignedAccountIds') or []) == 1 for body in self.fake.key_requests))
        await codex_lb.close()
        for active in reversed(self.patches):
            active.stop()
        await self.server.close()
        self.temporary.cleanup()
        await super().asyncTearDown()

    def fresh(self):
        """Look past codex-lb's short in-process caches."""
        codex_lb._accounts_cache = None
        codex_lb._models_cache = None

    async def connected(self, lb_id='lb-one', owner=OWNER.id, sharing='private', **summary):
        self.fake.accounts[lb_id] = lb_summary(lb_id, **summary)
        key = await codex_lb.create_key('test', lb_id)
        return await ProviderAccounts.insert(
            owner_user_id=owner,
            provider='codex',
            plan='plus',
            external_account_id=lb_id,
            sharing=sharing,
            capacity_weight=1,
            budget=None,
            status='ok',
            models=[{'id': 'test-model', 'name': 'Test model'}],
            secret={'lb_account_id': lb_id, 'lb_api_key': key['key'], 'lb_api_key_id': key['id']},
        )

    def request(self, chat_id=None):
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None)),
            state=SimpleNamespace(metadata={'chat_id': chat_id} if chat_id else {}),
        )

    def form(self):
        return {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}

    async def test_inference_needs_key_auth_and_reaches_codex_lb_as_a_codex_client(self):
        saved = await self.connected()
        # codex-lb would route a keyless request over every account: no traffic until key auth is on.
        self.fake.settings_locked = True
        with self.assertRaises(HTTPException) as refused:
            await proxy.account_completion(self.request(), self.form(), OWNER)
        self.assertEqual(refused.exception.status_code, 502)
        self.assertEqual(self.fake.inference, [])
        self.fake.settings_locked = False
        answer = await proxy.account_completion(self.request('chat-1'), self.form(), OWNER)
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hello')
        settings = self.fake.settings
        self.assertTrue(settings['apiKeyAuthEnabled'])
        self.assertEqual(
            (settings['requestLogRetentionOverrideDays'], settings['usageHistoryRetentionOverrideDays']), (30, 45)
        )
        sent = self.fake.inference[0]
        self.assertEqual(sent['account'], 'lb-one')
        self.assertEqual(sent['headers']['authorization'], 'Bearer ' + open_secret(saved.secret)['lb_api_key'])
        self.assertEqual(sent['headers']['session_id'], 'chat-1')
        for header in ('originator', 'chatgpt-account-id', 'openai-beta', 'origin'):
            self.assertNotIn(header, sent['headers'])
        self.assertEqual(
            (sent['body']['model'], sent['body']['store'], sent['body']['stream']), ('test-model', False, True)
        )
        self.assertEqual(sent['body']['input'][0]['role'], 'user')
        # The account card and context indicator show codex-lb's windows.
        self.assertEqual((await get_provider_limits(None, saved.id))['windows'][0]['used_percent'], 10)
        # Settings are not written again while they stay confirmed.
        writes = [request for request in self.fake.requests if request[:2] == ('PUT', '/api/settings')]
        await proxy.account_completion(self.request(), self.form(), OWNER)
        self.assertEqual([r for r in self.fake.requests if r[:2] == ('PUT', '/api/settings')], writes)
        # Management calls never carry browser headers.
        self.assertFalse(any({'origin', 'sec-fetch-site'} & headers for _, _, headers in self.fake.requests))

    async def test_account_models_carry_provider_details_like_connection_models(self):
        from open_webui.utils.context_window import apply_context_windows
        from open_webui.utils.image_input import apply_image_input_support
        from open_webui.utils.reasoning_effort import apply_reasoning_effort_options

        self.fake.catalog.append(codex_model('text-model', 'Text only', input_modalities=['text'], context_window=1000))
        shared = await self.connected('lb-details', sharing='shared')
        # A request brings the catalog in: new models and their details arrive without opening Settings.
        await proxy.account_completion(self.request(), self.form(), OWNER)
        stored = (await ProviderAccounts.get(shared.id)).models
        self.assertEqual([model['id'] for model in stored], ['test-model', 'text-model'])
        self.assertNotIn('base_instructions', stored[0]['details'])
        listing = await account_models(OTHER)
        for apply in (apply_reasoning_effort_options, apply_image_input_support, apply_context_windows):
            apply(listing)
        models = {model['id']: model for model in listing}
        # Hidden and Codex-internal models are not offered.
        self.assertEqual(set(models), {'account:codex:test-model', 'account:codex:text-model'})
        full, text = models['account:codex:test-model'], models['account:codex:text-model']
        self.assertEqual(full['connection_type'], 'external')
        self.assertEqual(
            [level['value'] for level in full['reasoning_effort_options']['levels']], ['low', 'medium', 'ultra']
        )
        self.assertEqual(full['reasoning_effort_options']['default'], 'medium')
        self.assertEqual((full['reads_images'], full['context_window']), (True, 272000))
        self.assertEqual((text['reads_images'], text['context_window']), (False, 1000))

    async def test_team_use_of_a_shared_account_is_split_from_the_owners(self):
        shared = await self.connected('lb-team', owner=OTHER.id, sharing='shared', remaining=90)

        async def used_up_to(remaining):
            self.fake.accounts['lb-team']['usage']['primaryRemainingPercent'] = remaining
            self.fresh()
            await codex_accounts.record_summary(None, shared.id, (await codex_lb.list_accounts())['lb-team'])
            limits = await with_team_use(None, shared.id, await get_provider_limits(None, shared.id))
            return [(window['used_percent'], window['team_percent']) for window in limits['windows']]

        # OWNER is the team here: the account belongs to OTHER.
        await proxy.account_completion(self.request(), self.form(), OWNER)
        self.assertEqual(await used_up_to(80), [(20, 10), (20, 0)])
        # The owner's own request, and a rise with no request at all, are the owner's.
        self.fresh()
        await proxy.account_completion(self.request(), self.form(), OTHER)
        self.assertEqual(await used_up_to(70), [(30, 10), (20, 0)])
        self.assertEqual(await used_up_to(65), [(35, 10), (20, 0)])

    async def test_empty_limited_or_refused_account_hands_request_to_the_next(self):
        own = await self.connected('lb-empty', remaining=0)
        for order, lb_id in enumerate(('lb-limited', 'lb-refused', 'lb-serving')):
            shared = await self.connected(lb_id, owner=OTHER.id, sharing='shared')
            # Shared accounts are tried oldest first.
            await ProviderAccounts.update(shared.id, {'created_at': order})
        self.fake.replies['lb-limited'] = (429, 'usage_limit_reached')
        self.fake.replies['lb-refused'] = (503, 'no_accounts')
        with patch.object(proxy, 'auto_reset', AsyncMock()) as reset:
            answer = await proxy.account_completion(self.request(), self.form(), OWNER)
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hello')
        # The empty account is skipped without a request; the others answer for themselves.
        self.assertEqual([sent['account'] for sent in self.fake.inference], ['lb-limited', 'lb-refused', 'lb-serving'])
        reset.assert_not_awaited()
        self.assertTrue(snapshot_exhausted(await get_provider_limits(None, own.id)))

    async def test_lost_login_needs_the_owner_and_recovers(self):
        saved = await self.connected('lb-sync')
        self.fake.accounts['lb-sync']['status'] = 'reauth_required'
        with self.assertRaises(HTTPException) as lost:
            await proxy.account_completion(self.request(), self.form(), OWNER)
        self.assertEqual(lost.exception.status_code, 401)
        saved = await ProviderAccounts.get(saved.id)
        self.assertEqual((saved.status, saved.last_error), ('needs_login', 'Reconnect this ChatGPT account.'))
        self.assertEqual(self.fake.inference, [])
        # Once codex-lb has the login again, the account list brings it back with new models and plan.
        self.fake.accounts['lb-sync'].update(status='active', planType='pro')
        self.fake.catalog.append(codex_model('new-model', 'New model'))
        self.fresh()
        await codex_accounts.sync_accounts()
        saved = await ProviderAccounts.get(saved.id)
        self.assertEqual((saved.status, saved.last_error, saved.plan), ('ok', None, 'pro'))
        self.assertEqual([model['id'] for model in saved.models], ['test-model', 'new-model'])
        self.assertEqual((await codex_accounts.verify(saved)).status, 'ok')
        self.fake.accounts.pop('lb-sync')
        with self.assertRaises(HTTPException) as gone:
            await codex_accounts.verify(saved)
        self.assertEqual(gone.exception.status_code, 401)
        self.assertEqual((await ProviderAccounts.get(saved.id)).status, 'needs_login')

    async def test_auto_reset_spends_soonest_coupon_once_under_concurrency(self):
        first = await self.connected('lb-first', remaining=0, sharing='shared')
        second = await self.connected('lb-second', remaining=0, owner=OTHER.id, sharing='shared')
        self.fake.credits['lb-first'] = coupons(('first-coupon', 7200))
        self.fake.credits['lb-second'] = coupons(('second-later', 9000), ('second-coupon', 600))
        # Two requests find every account empty at once; only one coupon may be spent.
        results = await asyncio.gather(
            codex_accounts.auto_reset([first, second], OWNER.id),
            codex_accounts.auto_reset([first, second], OTHER.id),
        )
        self.assertEqual([result.id for result in results], [second.id, second.id])
        self.assertEqual(len(self.fake.consumed), 1)
        lb_id, body = self.fake.consumed[0]
        self.assertEqual(lb_id, 'lb-second')
        async with get_async_db_context() as db:
            events = list((await db.execute(select(LimitResetEvent))).scalars())
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].outcome, events[0].credit_id), ('reset', 'second-coupon'))
        self.assertEqual(body, {'redeemRequestId': events[0].idempotency_key})
        self.assertIn(events[0].triggered_by, (OWNER.id, OTHER.id))
        self.assertEqual(list(await LimitResetEvents.last_resets()), [second.id])
        self.assertEqual((await get_provider_limits(None, second.id))['windows'][0]['used_percent'], 0)

    async def test_auto_reset_respects_cooldown_and_sends_once(self):
        empty = await self.connected('lb-empty', remaining=0)
        self.fake.credits['lb-empty'] = coupons(('coupon', 600))
        # codex-lb refused before reaching OpenAI: nothing was spent, so no cooldown.
        self.fake.consume_error = (409, 'no_available_reset_credit')
        self.assertIsNone(await codex_accounts.auto_reset([empty], OWNER.id))
        self.assertEqual([lb_id for lb_id, _ in self.fake.consumed], ['lb-empty'])
        self.assertFalse(await LimitResetEvents.recent('codex', codex_accounts.RESET_COOLDOWN))
        # A lost answer may have spent the coupon: it is never sent again, and it blocks the next one.
        consume = AsyncMock(side_effect=CodexLBError())
        with patch.object(codex_lb, 'consume_reset_credit', consume):
            self.assertIsNone(await codex_accounts.auto_reset([empty], OWNER.id))
            consume.assert_awaited_once()
            self.assertEqual(consume.await_args.args[0], 'lb-empty')
            self.assertIsNone(await codex_accounts.auto_reset([empty], OWNER.id))
            consume.assert_awaited_once()
        async with get_async_db_context() as db:
            outcomes = [event.outcome for event in (await db.execute(select(LimitResetEvent))).scalars()]
        self.assertEqual(sorted(outcomes), ['error', 'refused'])
        self.assertTrue(await LimitResetEvents.recent('codex', codex_accounts.RESET_COOLDOWN))
        self.assertFalse(await LimitResetEvents.recent('codex', 0))

    async def test_admin_team_sign_in_creates_an_account_without_owner(self):
        self.fake.next_account = 'lb-team'
        identifier = await codex_accounts.start_login(ADMIN, TEAM, method='link')
        status = await codex_accounts.login_status(identifier, ADMIN)
        state = parse_qs(urlparse(status['auth_url']).query)['state'][0]
        done = await codex_accounts.submit_callback(identifier, ADMIN, f'{CALLBACK}?code=fake-code&state={state}')
        saved = await ProviderAccounts.get(done['account_id'])
        self.assertEqual((saved.owner_user_id, saved.sharing), (None, 'shared'))
        self.assertTrue(can_use(saved, OTHER))
        # All of a team account's use is team use.
        await proxy.account_completion(self.request(), self.form(), ADMIN)
        self.fake.accounts['lb-team']['usage']['primaryRemainingPercent'] = 70
        self.fresh()
        await codex_accounts.record_summary(None, saved.id, (await codex_lb.list_accounts())['lb-team'])
        limits = await with_team_use(None, saved.id, await get_provider_limits(None, saved.id))
        self.assertEqual(limits['windows'][0]['team_percent'], 20)

    async def test_link_login_sends_only_code_and_state_of_its_own_flow(self):
        self.fake.next_account = 'lb-link'
        identifier = await codex_accounts.start_login(OWNER, 'shared', method='link')
        status = await codex_accounts.login_status(identifier, OWNER)
        self.assertEqual(
            set(status),
            {
                'id',
                'status',
                'account_id',
                'expires_at',
                'error',
                'method',
                'user_code',
                'verification_url',
                'auth_url',
            },
        )
        self.assertEqual((status['status'], status['method'], status['user_code']), ('pending', 'link', None))
        self.assertTrue(status['auth_url'].startswith('https://auth.openai.com/oauth/authorize?'))
        # One ChatGPT connection at a time across the server, whichever the method.
        for method in ('link', 'device_code'):
            with self.assertRaises(HTTPException) as busy:
                await codex_accounts.start_login(OTHER, 'private', method=method)
            self.assertEqual(busy.exception.status_code, 409)
            self.assertIn('Another member', busy.exception.detail)
        self.assertEqual(await codex_accounts.start_login(OWNER, 'private', method='link'), identifier)
        state = parse_qs(urlparse(status['auth_url']).query)['state'][0]
        pasted = f'{CALLBACK}?code=fake-code&state={state}'
        with self.assertRaises(HTTPException) as foreign:
            await codex_accounts.submit_callback(identifier, OTHER, pasted)
        self.assertEqual(foreign.exception.status_code, 404)
        for wrong in (
            pasted.replace(state, 'other'),
            pasted.replace(state, 'f%C3%BCnf'),
            'https://example.com/',
            f'{CALLBACK}?state={state}',
        ):
            with self.assertRaises(HTTPException) as rejected:
                await codex_accounts.submit_callback(identifier, OWNER, wrong)
            self.assertEqual(rejected.exception.status_code, 400)
        self.assertEqual(self.fake.callbacks, [])
        # The pasted host and extra parameters are dropped: codex-lb gets a canonical callback.
        done = await codex_accounts.submit_callback(
            identifier, OWNER, f'  http://evil.example:9/auth/callback?code=fake-code&state={state}&next=x '
        )
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(self.fake.callbacks, [{'callbackUrl': pasted, 'flowId': 'flow-1'}])
        saved = await ProviderAccounts.get(done['account_id'])
        self.assertEqual(
            (saved.owner_user_id, saved.sharing, saved.external_account_id, saved.plan, saved.status),
            (OWNER.id, 'shared', 'lb-link', 'pro', 'ok'),
        )
        self.assertRegex(saved.label, GENERATED_NAME)
        self.assertEqual(self.fake.accounts['lb-link']['alias'], saved.label)
        self.assertEqual(saved.models, [catalog_model('test-model', 'Test model', codex_model('test-model'))])
        secret = open_secret(saved.secret)
        self.assertEqual(
            self.fake.keys[secret['lb_api_key_id']], {'key': secret['lb_api_key'], 'accounts': ['lb-link']}
        )
        with self.assertRaises(HTTPException) as finished:
            await codex_accounts.submit_callback(identifier, OWNER, pasted)
        self.assertEqual(finished.exception.status_code, 409)
        # The slot is free again; a pending sign-in can be cancelled by its owner.
        second = await codex_accounts.start_login(OTHER, 'private', method='link')
        self.assertTrue(await codex_accounts.cancel_login(second, OTHER))
        cancelled = await codex_accounts.login_status(second, OTHER)
        self.assertEqual((cancelled['status'], cancelled['error']), ('error', 'Sign-in cancelled.'))
        # A sign-in OpenAI turned down ends the attempt.
        third = await codex_accounts.start_login(OTHER, 'private', method='link')
        rejected = await codex_accounts.submit_callback(third, OTHER, f'{CALLBACK}?code=wrong&state=state-flow-3')
        self.assertEqual(rejected['status'], 'error')

    async def test_device_login_maps_the_new_account_and_refuses_another_owner(self):
        identifier = await codex_accounts.start_login(OWNER, 'private')
        status = await codex_accounts.login_status(identifier, OWNER)
        self.assertEqual(
            (status['status'], status['method'], status['user_code'], status['verification_url'], status['auth_url']),
            ('pending', 'device_code', 'FAKE-CODE', 'https://auth.openai.com/codex/device', None),
        )
        with self.assertRaises(HTTPException):
            await codex_accounts.login_status(identifier, OTHER)
        async with get_async_db_context() as db:
            reserved = (await db.get(ProviderLogin, identifier)).label
        self.assertRegex(reserved, GENERATED_NAME)
        self.fake.sign_in('flow-1', 'lb-device')
        status = await codex_accounts.login_status(identifier, OWNER)
        self.assertEqual((status['status'], status['user_code']), ('completed', None))
        saved = await ProviderAccounts.get(status['account_id'])
        self.assertEqual((saved.owner_user_id, saved.sharing, saved.label), (OWNER.id, 'private', reserved))
        self.assertIsNotNone(await get_provider_limits(None, saved.id))
        # Another member signing in to the same account cannot take it over.
        other = await codex_accounts.start_login(OTHER, 'shared')
        self.fake.sign_in('flow-2', 'lb-device')
        refused = await codex_accounts.login_status(other, OTHER)
        self.assertEqual((refused['status'], refused['error']), ('error', 'This ChatGPT account is already connected.'))
        self.assertEqual(len(await ProviderAccounts.list()), 1)
        # Its owner reconnects a lost login: the same account serves again, with its existing key.
        await ProviderAccounts.update(
            saved.id, {'status': 'needs_login', 'last_error': 'Reconnect this ChatGPT account.'}
        )
        keys = dict(self.fake.keys)
        again = await codex_accounts.start_login(OWNER, 'private')
        self.fake.sign_in('flow-3', 'lb-device')
        reconnected = await codex_accounts.login_status(again, OWNER)
        self.assertEqual((reconnected['status'], reconnected['account_id']), ('completed', saved.id))
        self.assertEqual((await ProviderAccounts.get(saved.id)).status, 'ok')
        self.assertEqual(self.fake.keys, keys)
        # A sign-in that failed at OpenAI ends the attempt.
        failed = await codex_accounts.start_login(OWNER, 'private')
        self.fake.flows['flow-4']['status'] = 'error'
        self.assertEqual((await codex_accounts.login_status(failed, OWNER))['status'], 'error')
        # codex-lb skips the flow once accounts exist unless the method is forced.
        self.assertEqual([body['forceMethod'] for body in self.fake.starts], ['device'] * 4)

    async def test_keys_are_always_assigned_to_exactly_one_account(self):
        for missing in ('', '  ', None):
            with self.assertRaises(ValueError):
                await codex_lb.create_key('test', missing)
        self.assertEqual(self.fake.key_requests, [])
        # A codex-lb that dropped the assignment: the key is revoked at once and never used.
        self.fake.accounts['lb-x'] = lb_summary('lb-x')
        self.fake.ignore_assignment = True
        with self.assertRaises(CodexLBError):
            await codex_lb.create_key('test', 'lb-x')
        self.assertEqual(self.fake.keys, {})

    async def test_legacy_app_server_account_moves_into_codex_lb_once(self):
        home_id = str(uuid.uuid4())
        home = codex_accounts.ACCOUNT_ROOT / home_id
        home.mkdir(parents=True)
        tokens = {'account_id': 'legacy', 'access_token': 'a', 'refresh_token': 'r', 'id_token': 'i'}
        (home / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': None, 'tokens': tokens, 'last_refresh': iso(0)}))
        raw = (home / 'auth.json').read_bytes()
        legacy = await ProviderAccounts.insert(
            owner_user_id=OWNER.id,
            provider='codex',
            plan='plus',
            external_account_id='legacy',
            sharing='shared',
            capacity_weight=1,
            budget=None,
            status='ok',
            models=[{'id': 'test-model', 'name': 'Test model'}],
            secret={'home_id': home_id},
        )
        # codex-lb is down: nothing is lost, and the account list still works.
        with patch.object(codex_lb, 'BASE_URL', 'http://127.0.0.1:9'):
            await codex_accounts.sync_accounts()
            with self.assertRaises(HTTPException) as down:
                await codex_accounts.account_secret(legacy)
        self.assertEqual(down.exception.status_code, 502)
        self.assertEqual(open_secret((await ProviderAccounts.get(legacy.id)).secret), {'home_id': home_id})
        self.assertTrue((home / 'auth.json').exists())
        # The first listing with codex-lb up moves it.
        await codex_accounts.sync_accounts()
        self.assertEqual(self.fake.imports, [raw])
        saved = await ProviderAccounts.get(legacy.id)
        secret = open_secret(saved.secret)
        self.assertEqual(set(secret), {'lb_account_id', 'lb_api_key', 'lb_api_key_id'})
        self.assertEqual((secret['lb_account_id'], saved.external_account_id), ('lb-legacy', 'lb-legacy'))
        self.assertEqual((saved.plan, saved.status, saved.sharing), ('pro', 'ok', 'shared'))
        self.assertEqual(self.fake.keys[secret['lb_api_key_id']]['accounts'], ['lb-legacy'])
        self.assertFalse(home.exists())
        await codex_accounts.account_secret(saved)
        self.assertEqual(len(self.fake.imports), 1)

    async def test_disconnect_revokes_key_and_removes_account(self):
        saved = await self.connected('lb-gone')
        await codex_accounts.disconnect(saved)
        self.assertEqual(self.fake.keys, {})
        self.assertEqual(self.fake.deleted_accounts, [('lb-gone', 'true')])
        self.assertNotIn('lb-gone', self.fake.accounts)
        # Deleting a member never waits on codex-lb.
        other = await self.connected('lb-other', owner=OTHER.id)
        with patch.object(codex_lb, 'BASE_URL', 'http://127.0.0.1:9'):
            await codex_accounts.delete_user_accounts(OTHER.id)
        self.assertIsNone(await ProviderAccounts.get(other.id))


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

    def test_only_admins_connect_team_accounts_which_stay_shared(self):
        form = {'provider': 'openrouter', 'api_key': 'test-secret', 'budget': {'amount': 20}, 'team': True}
        team = account(owner_user_id=None, provider='openrouter', plan='api', sharing='shared')
        insert = AsyncMock(return_value=team)
        verified = ({'api_key': 'test-secret', 'base_url': 'https://openrouter.ai/api/v1'}, [{'id': 'model'}])
        with (
            patch.object(routes, 'verify_api_key', AsyncMock(return_value=verified)),
            patch.object(ProviderAccounts, 'insert', insert),
            patch.object(routes.codex_accounts, 'start_login', AsyncMock()) as start,
        ):
            self.assertEqual(self.client.post('/accounts', json=form).status_code, 403)
            self.assertEqual(self.client.post('/accounts/codex', json={'team': True}).status_code, 403)
            start.assert_not_awaited()
            insert.assert_not_awaited()
            self.app.dependency_overrides[routes.get_verified_user] = lambda: ADMIN
            self.assertEqual(self.client.post('/accounts', json=form).status_code, 200)
        self.assertEqual(
            (insert.await_args.kwargs['owner_user_id'], insert.await_args.kwargs['sharing']), (None, 'shared')
        )
        # A team account has no owner to keep it private.
        with (
            patch.object(ProviderAccounts, 'get', AsyncMock(return_value=team)),
            patch.object(ProviderAccounts, 'update', AsyncMock(return_value=team)) as update,
        ):
            self.assertEqual(self.client.patch('/accounts/id', json={'sharing': 'private'}).status_code, 400)
            self.assertEqual(self.client.patch('/accounts/id', json={'sharing': 'paused'}).status_code, 200)
        update.assert_awaited_once_with('id', {'sharing': 'paused'})

    def test_only_an_admin_corrects_a_member_chosen_plan(self):
        zai = account(provider='zai', plan='lite')
        with (
            patch.object(ProviderAccounts, 'get', AsyncMock(return_value=zai)),
            patch.object(ProviderAccounts, 'update', AsyncMock(return_value=zai)) as update,
        ):
            # The owner cannot raise their own plan; an admin can correct it.
            self.assertEqual(self.client.patch('/accounts/id', json={'plan': 'max'}).status_code, 403)
            self.app.dependency_overrides[routes.get_verified_user] = lambda: ADMIN
            self.assertEqual(self.client.patch('/accounts/id', json={'plan': 'max'}).status_code, 200)
            update.assert_awaited_once_with('id', {'plan': 'max'})
            self.assertEqual(self.client.patch('/accounts/id', json={'plan': 'enterprise'}).status_code, 422)
        # ChatGPT and API-key plans come from the provider.
        for fixed in (account(), account(provider='openrouter', plan='api')):
            with patch.object(ProviderAccounts, 'get', AsyncMock(return_value=fixed)):
                self.assertEqual(self.client.patch('/accounts/id', json={'plan': 'pro'}).status_code, 400)

    def test_any_member_switches_auto_reset_for_everyone(self):
        stored = {}

        async def get(key, default=None):
            return stored.get(key, default)

        async def upsert(values):
            stored.update(values)

        with (
            patch.object(routes.Config, 'get', AsyncMock(side_effect=get)),
            patch.object(routes.Config, 'upsert', AsyncMock(side_effect=upsert)),
        ):
            self.assertTrue(self.client.get('/accounts/auto-reset').json()['enabled'])
            response = self.client.put('/accounts/auto-reset', json={'enabled': False})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['enabled'], False)
            self.assertEqual(response.json()['changed_by'], 'Owner')
            self.assertIs(stored['mologate.auto_reset'], False)
            self.assertEqual(
                self.client.put('/accounts/auto-reset', json={'enabled': True, 'by': 'other'}).status_code, 422
            )

    def test_budget_required_and_no_owner_spoofing(self):
        form = {'provider': 'openai_api', 'api_key': 'test-secret'}
        self.assertEqual(self.client.post('/accounts', json=form).status_code, 400)
        self.assertEqual(self.client.post('/accounts', json=form | {'owner_user_id': 'other'}).status_code, 422)

    def test_members_cannot_name_accounts_or_switch_resets_per_account(self):
        form = {'provider': 'openai_api', 'api_key': 'test-secret', 'budget': {'amount': 20}}
        start = AsyncMock(return_value='login')
        with (
            patch.object(ProviderAccounts, 'get', AsyncMock(return_value=account())),
            patch.object(ProviderAccounts, 'update', AsyncMock(return_value=account())) as update,
            patch.object(routes, 'verify_api_key', AsyncMock()) as verify,
            patch.object(routes.codex_accounts, 'start_login', start),
        ):
            for method, path, body in [
                ('POST', '/accounts', form | {'label': 'Mine'}),
                ('POST', '/accounts/codex', {'label': 'Mine', 'sharing': 'private'}),
                ('PATCH', '/accounts/id', {'label': 'Mine'}),
                ('PATCH', '/accounts/id', {'allow_team_resets': False}),
            ]:
                self.assertEqual(self.client.request(method, path, json=body).status_code, 422)
            verify.assert_not_awaited()
            start.assert_not_awaited()
            update.assert_not_awaited()


class UploadMigrationTests(unittest.TestCase):
    def test_existing_installs_send_uploads_as_full_text_once(self):
        migration = importlib.import_module('open_webui.migrations.versions.d8e2b6c4a9f3_send_uploads_as_full_text')
        self.assertEqual(migration.down_revision, 'c3d9e7a5b1f0')
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE config (key TEXT PRIMARY KEY, value JSON)')
            connection.exec_driver_sql(
                "INSERT INTO config VALUES ('rag.bypass_embedding_and_retrieval', 'false'), ('other', 'false')"
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
            rows = dict(connection.exec_driver_sql('SELECT key, value FROM config').all())
        engine.dispose()
        self.assertEqual(json.loads(rows['rag.bypass_embedding_and_retrieval']), True)
        self.assertEqual(json.loads(rows['other']), False)


class UsageSplitTests(unittest.IsolatedAsyncioTestCase):
    def reading(self, state, used, resets_at=1000, window_id='primary'):
        split_reading(state, [{'id': window_id, 'used_percent': used, 'resets_at': resets_at}])
        return state['windows'][window_id]['team']

    def requests(self, state, owner=0, team=0, window_id='primary'):
        state['pending'][window_id] = {'owner': owner, 'team': team}

    def test_rises_are_shared_by_requests_and_unexplained_use_is_the_owners(self):
        state = {'windows': {}, 'pending': {}}
        # What was used before the first reading is the owner's, even with requests pending.
        self.requests(state, team=3)
        self.assertEqual(self.reading(state, 30), 0)
        self.assertEqual(state['pending'], {})
        self.requests(state, owner=1, team=3)
        self.assertEqual(self.reading(state, 38), 6)
        # No visible rise yet: the requests wait for the reading that shows them.
        self.requests(state, team=2)
        self.assertEqual(self.reading(state, 38), 6)
        self.assertEqual(self.reading(state, 37), 6)
        self.assertEqual(state['pending']['primary'], {'owner': 0, 'team': 2})
        self.assertEqual(self.reading(state, 40), 8)
        # A rise with no request from Mologate is the owner's use elsewhere.
        self.assertEqual(self.reading(state, 45), 8)
        self.assertEqual(state['windows']['primary']['used'], 45)

    def test_a_window_that_starts_over_starts_the_split_over(self):
        state = {'windows': {}, 'pending': {}}
        self.reading(state, 50)
        self.requests(state, team=1)
        self.assertEqual(self.reading(state, 60), 10)
        # The reset time moved on: the new window counts from zero.
        self.requests(state, team=1)
        self.assertEqual(self.reading(state, 4, resets_at=1000 + 18000), 4)
        # A reset coupon empties the window without moving its reset time.
        self.requests(state, owner=1)
        self.assertEqual(self.reading(state, 30, resets_at=18000 + 1000), 4)
        self.assertEqual(self.reading(state, 0, resets_at=18000 + 1000), 0)

    async def test_requests_and_readings_are_kept_per_account_and_window(self):
        account_id = 'split-' + uuid.uuid4().hex
        await record_provider_snapshot(
            None,
            account_id,
            [
                {'id': 'primary', 'used_percent': 10, 'resets_at': 1000},
                {'id': 'secondary', 'used_percent': 40, 'resets_at': 9000},
            ],
        )
        await count_account_request(None, account_id, by_owner=False)
        # Only the 5-hour window moved; the weekly one keeps the request until it does.
        await record_provider_snapshot(
            None,
            account_id,
            [
                {'id': 'primary', 'used_percent': 12, 'resets_at': 1000},
                {'id': 'secondary', 'used_percent': 40, 'resets_at': 9000},
            ],
        )
        await record_provider_snapshot(
            None,
            account_id,
            [
                {'id': 'primary', 'used_percent': 12, 'resets_at': 1000},
                {'id': 'secondary', 'used_percent': 41, 'resets_at': 9000},
                {'id': 'requests', 'used_percent': 50, 'limit': 10, 'remaining': 5},
            ],
        )
        limits = await with_team_use(None, account_id, await get_provider_limits(None, account_id))
        self.assertEqual([window.get('team_percent') for window in limits['windows']], [2, 1, None])
        self.assertIsNone(await with_team_use(None, account_id + '-none', None))


class AccountNameMigrationTests(unittest.TestCase):
    def test_existing_accounts_get_generated_names_and_lose_the_reset_switch(self):
        migration = importlib.import_module(
            'open_webui.migrations.versions.e5a3c1f8d2b7_generated_names_and_global_resets'
        )
        self.assertEqual(migration.down_revision, 'd8e2b6c4a9f3')
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            connection.exec_driver_sql(
                'CREATE TABLE provider_account '
                '(id TEXT PRIMARY KEY, label TEXT NOT NULL, allow_team_resets BOOLEAN NOT NULL)'
            )
            connection.exec_driver_sql(
                'INSERT INTO provider_account VALUES '
                "('a', 'My ChatGPT', 0), ('b', 'My ChatGPT', 1), ('c', 'Work key', 1)"
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            rows = dict(connection.exec_driver_sql('SELECT id, label FROM provider_account').all())
            columns = [row[1] for row in connection.exec_driver_sql('PRAGMA table_info(provider_account)').all()]
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            restored = connection.exec_driver_sql('SELECT allow_team_resets FROM provider_account').all()
        engine.dispose()
        self.assertEqual(columns, ['id', 'label'])
        self.assertEqual(restored, [(1,), (1,), (1,)])
        self.assertEqual(set(rows), {'a', 'b', 'c'})
        for label in rows.values():
            self.assertRegex(label, GENERATED_NAME)
            self.assertEqual(len(label), len('Account #') + 6)
        self.assertEqual(len(set(rows.values())), 3)


class ConnectionAdoptionTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from open_webui.models.access_grants import AccessGrant
        from open_webui.models.models import Model

        self.tables = (Model.__table__, AccessGrant.__table__)
        async with async_engine.begin() as connection:
            for table in self.tables:
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
                await connection.run_sync(table.create)

    async def asyncTearDown(self):
        async with async_engine.begin() as connection:
            for table in self.tables:
                await connection.run_sync(lambda conn, table=table: table.drop(conn, checkfirst=True))
        await super().asyncTearDown()

    async def test_supported_connections_become_team_accounts_and_old_ids_follow(self):
        from open_webui.models.models import Model
        from open_webui.utils import connection_accounts

        stored = {
            'mologate.limits_mode': 'observe',
            'openai.enable': True,
            'openai.api_base_urls': [
                'https://openrouter.ai/api/v1/',
                'http://localhost:11434/v1',
                'https://api.z.ai/api/coding/paas/v4',
                'https://openrouter.ai/api/v1',
            ],
            'openai.api_keys': ['router-key', 'local-key', 'zai-key', 'member-key'],
            'openai.api_configs': {'0': {'prefix_id': 'or'}, '1': {'tags': ['local']}, '2': {'enable': False}},
            'task.model.default': 'glm-5.3',
            'ui.default_models': 'or.vendor/model,local-model',
            'ui.model_order_list': ['glm-5.3', 'local-model'],
        }
        await self.create_account(provider='openrouter', external_account_id=key_fingerprint('member-key'))

        async def verify(provider, key, plan):
            listed = {
                'openrouter': [catalog_model('vendor/model', 'Vendor: Model', {'context_length': 1000})],
                'zai': [catalog_model('glm-5.3', 'GLM-5.3', {})],
            }
            return {'api_key': key, 'base_url': 'https://provider.example'}, listed[provider]

        now = int(time.time())
        async with get_async_db_context() as db:
            meta = {'capabilities': {'vision': False}}
            db.add(
                Model(id='glm-5.3', user_id=ADMIN.id, name='GLM', params={}, meta=meta, created_at=now, updated_at=now)
            )
            db.add(
                Model(
                    id='team-preset',
                    user_id=ADMIN.id,
                    base_model_id='or.vendor/model',
                    name='Preset',
                    params={},
                    meta={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.commit()
        with (
            patch(
                'open_webui.models.config.Config.get',
                AsyncMock(side_effect=lambda key, default=None: stored.get(key, default)),
            ),
            patch(
                'open_webui.models.config.Config.get_many',
                AsyncMock(side_effect=lambda *keys: {key: stored[key] for key in keys if key in stored}),
            ),
            patch('open_webui.models.config.Config.upsert', AsyncMock(side_effect=stored.update)),
            patch.object(connection_accounts, 'verify_api_key', AsyncMock(side_effect=verify)),
        ):
            self.assertEqual(await connection_accounts.adopt_connections(), 2)
            # Already adopted: nothing more to do.
            self.assertEqual(await connection_accounts.adopt_connections(), 0)
            self.assertEqual(
                await connection_accounts.resolve_model_alias('or.vendor/model'), 'account:openrouter:vendor/model'
            )
            self.assertEqual(await connection_accounts.resolve_model_alias('local-model'), 'local-model')
        team = [item for item in await ProviderAccounts.list() if item.owner_user_id is None]
        self.assertEqual(
            sorted((item.provider, item.plan, item.sharing) for item in team),
            [('openrouter', 'api', 'shared'), ('zai', 'lite', 'paused')],
        )
        # The local server, and the key a member already connected, stay connections, renumbered.
        self.assertEqual(stored['openai.api_base_urls'], ['http://localhost:11434/v1', 'https://openrouter.ai/api/v1'])
        self.assertEqual(stored['openai.api_keys'], ['local-key', 'member-key'])
        self.assertEqual(stored['openai.api_configs'], {'0': {'tags': ['local']}})
        self.assertEqual(stored['task.model.default'], 'account:zai:glm-5.3')
        self.assertEqual(stored['ui.default_models'], 'account:openrouter:vendor/model,local-model')
        self.assertEqual(stored['ui.model_order_list'], ['account:zai:glm-5.3', 'local-model'])
        async with get_async_db_context() as db:
            self.assertIsNone(await db.get(Model, 'glm-5.3'))
            self.assertEqual((await db.get(Model, 'account:zai:glm-5.3')).meta, meta)
            self.assertEqual((await db.get(Model, 'team-preset')).base_model_id, 'account:openrouter:vendor/model')

    async def test_old_model_ids_reach_the_team_account(self):
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(OPENAI_MODELS={})))
        aliases = {'or.vendor/model': 'account:openrouter:vendor/model'}
        with (
            patch(
                'open_webui.models.config.Config.get',
                AsyncMock(
                    side_effect=lambda key, default=None: aliases if key == 'mologate.model_aliases' else default
                ),
            ),
            patch.object(openai, 'account_completion', AsyncMock(return_value={'ok': True})) as route,
            patch.object(openai.Models, 'get_model_by_id', AsyncMock(return_value=None)),
        ):
            payload = {'model': 'or.vendor/model', 'messages': []}
            self.assertEqual(await openai.generate_chat_completion(request, payload, OWNER), {'ok': True})
        self.assertEqual(route.await_args.args[1]['model'], 'account:openrouter:vendor/model')


class TeamAccountMigrationTests(unittest.TestCase):
    def test_accounts_may_have_no_owner_and_downgrade_removes_team_accounts(self):
        migration = importlib.import_module('open_webui.migrations.versions.f7c2a9d4b8e1_team_provider_accounts')
        self.assertEqual(migration.down_revision, 'e5a3c1f8d2b7')
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            connection.exec_driver_sql(
                'CREATE TABLE provider_account (id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL)'
            )
            connection.exec_driver_sql("INSERT INTO provider_account VALUES ('member', 'owner')")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            connection.exec_driver_sql("INSERT INTO provider_account VALUES ('team', NULL)")
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            rows = connection.exec_driver_sql('SELECT id FROM provider_account').all()
        engine.dispose()
        self.assertEqual(rows, [('member',)])


class EnvOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_set_in_the_environment_win_at_every_start(self):
        from open_webui import config

        stored = {'mologate.limits_mode': 'off', 'mologate.auto_reset': True}
        upsert = AsyncMock(side_effect=stored.update)
        defaults = {**config.DEFAULT_CONFIG, 'mologate.limits_mode': 'observe', 'mologate.auto_reset': False}
        with (
            patch.object(config, 'DEFAULT_CONFIG', defaults),
            patch.object(config.Config, 'get', AsyncMock(side_effect=lambda key, default=None: stored.get(key))),
            patch.object(config.Config, 'upsert', upsert),
        ):
            # Unset in the environment: the stored values (a member's auto-reset choice) stay.
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('MOLOGATE_LIMITS_MODE', None)
                os.environ.pop('MOLOGATE_AUTO_RESET', None)
                await config.apply_env_overrides()
            upsert.assert_not_awaited()
            with patch.dict(os.environ, {'MOLOGATE_LIMITS_MODE': 'observe', 'MOLOGATE_AUTO_RESET': 'false'}):
                await config.apply_env_overrides()
        self.assertEqual((stored['mologate.limits_mode'], stored['mologate.auto_reset']), ('observe', False))
        self.assertEqual(stored['mologate.auto_reset_changed']['by'], 'Server settings')


class SignupTests(unittest.IsolatedAsyncioTestCase):
    async def test_members_cannot_register_themselves(self):
        from open_webui.routers import auths

        form = auths.SignupForm(name='New member', email='new@example.com', password='Password-123!')
        with (
            patch.object(auths.Users, 'has_users', AsyncMock(return_value=True)),
            patch.object(auths, 'WEBUI_AUTH', True),
            patch.object(auths.Config, 'get', AsyncMock(return_value=True)),
        ):
            # Even a stored "sign-ups enabled" value cannot reopen registration.
            with self.assertRaises(HTTPException) as error:
                await auths.signup(SimpleNamespace(), SimpleNamespace(), form, db=None)
        self.assertEqual(error.exception.status_code, 403)

    async def test_admin_settings_keep_sign_up_off(self):
        from open_webui.routers import auths

        form = auths.AdminConfig.model_construct(
            ENABLE_SIGNUP=True,
            DEFAULT_USER_ROLE='pending',
            CHANNEL_MODEL_RESPONSE_MODE='thread',
            JWT_EXPIRES_IN='-1',
            DEFAULT_INTERFACE_SETTINGS=None,
            FOLDER_MAX_FILE_COUNT=None,
            AUTOMATION_MAX_COUNT=None,
            AUTOMATION_MIN_INTERVAL=None,
        )
        upsert = AsyncMock()
        with (
            patch.object(auths.Config, 'upsert', upsert),
            patch.object(auths, 'get_config_values', AsyncMock(return_value={})),
        ):
            await auths.update_admin_config(SimpleNamespace(), form, user=ADMIN)
        self.assertIs(upsert.await_args.args[0]['ui.enable_signup'], False)


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

    async def json(self, **kwargs):
        return self.data

    def close(self):
        self.closed = True

    async def iter_chunks(self):
        raw = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\r\n\r\n' for event in self.events).encode()
        for index in range(0, len(raw), 3):
            yield raw[index : index + 3], False


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_model_acl_update_redirects_to_account_sharing(self):
        from open_webui.routers.models import ModelAccessGrantsForm, update_model_access_by_id

        with self.assertRaises(HTTPException) as error:
            await update_model_access_by_id(
                SimpleNamespace(),
                ModelAccessGrantsForm(id='account:codex:test-model', access_grants=[]),
                user=ADMIN,
                db=None,
            )
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn('Provider accounts', error.exception.detail)

    async def test_admin_overrides_on_an_account_model_keep_it_usable(self):
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))

        def saved(model_id, base_model_id=None):
            # What Admin -> Models saves: the admin's row, with no access grants for anyone else.
            return SimpleNamespace(
                id=model_id,
                base_model_id=base_model_id,
                user_id=ADMIN.id,
                access_grants=[],
                params=SimpleNamespace(model_dump=lambda: {'temperature': 0.2}),
            )

        rows = {'account:codex:test-model': saved('account:codex:test-model')}
        rows['team-preset'] = saved('team-preset', base_model_id='account:codex:test-model')
        with (
            patch.object(openai, 'account_completion', AsyncMock(return_value={'ok': True})) as route,
            patch.object(openai, 'check_model_access', AsyncMock()) as access,
            patch.object(openai.Models, 'get_model_by_id', AsyncMock(side_effect=rows.get)),
        ):
            payload = {'model': 'account:codex:test-model', 'messages': []}
            self.assertEqual(await openai.generate_chat_completion(request, payload, OWNER), {'ok': True})
            access.assert_not_awaited()
            self.assertEqual(route.await_args.args[1]['temperature'], 0.2)
            # A preset built on the account model still checks its own access.
            await openai.generate_chat_completion(request, {'model': 'team-preset', 'messages': []}, OWNER)
            access.assert_awaited_once()
            self.assertEqual(route.await_args.args[1]['model'], 'account:codex:test-model')

    async def test_model_lists_leave_out_old_and_unusable_models(self):
        from open_webui.utils.provider_accounts import usable_model, without_dated_copies

        listed = [
            'gpt-5',
            'gpt-4.1',
            'gpt-4o',
            'gpt-4o-2024-08-06',
            'o3',
            'o3-2025-04-16',
            'gpt-4',
            'gpt-4-turbo',
            'gpt-3.5-turbo',
            'gpt-3.5-turbo-instruct',
            'babbage-002',
            'text-embedding-3-small',
            'omni-moderation-latest',
            'gpt-4o-realtime-preview',
            'gpt-4o-transcribe',
            'gpt-image-1',
            'tts-1',
            'whisper-1',
            'computer-use-preview',
        ]
        kept = without_dated_copies([{'id': model} for model in listed if usable_model('openai_api', {'id': model})])
        self.assertEqual([model['id'] for model in kept], ['gpt-5', 'gpt-4.1', 'gpt-4o', 'o3'])
        # OpenRouter: text chat models stay, image generators and retiring models go.
        text = {
            'id': 'qwen/qwen3-235b-a22b-2507',
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
        }
        painter = {
            'id': 'vendor/painter',
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['image']},
        }
        retiring = {**text, 'id': 'vendor/old', 'expiration_date': '2026-11-01'}
        self.assertEqual(
            [usable_model('openrouter', entry) for entry in (text, painter, retiring)], [True, False, False]
        )
        self.assertTrue(usable_model('openrouter', {'id': 'meta-llama/llama-3.3-70b-instruct'}))
        self.assertFalse(usable_model('zai', {'id': 'cogview-4'}))

    async def test_zai_quota_is_read_with_the_bare_key_and_kept_current(self):
        from open_webui.utils import provider_accounts

        seen = []
        reset = int(time.time() + 600) * 1000

        def get(url, **kwargs):
            seen.append((url, kwargs['headers']))
            self.assertFalse(kwargs['allow_redirects'])
            limits = [{'type': 'TOKENS_LIMIT', 'unit': 3, 'number': 5, 'percentage': 37, 'nextResetTime': reset}]
            return Response({'code': 200, 'msg': 'ok', 'data': {'limits': limits}})

        session = SimpleNamespace(get=get)
        manager = Response()
        manager.__class__ = type('SessionContext', (Response,), {'__aenter__': AsyncMock(return_value=session)})
        coding = account(
            id='zai-' + uuid.uuid4().hex,
            provider='zai',
            plan='lite',
            secret=seal_secret({'api_key': 'zai-test-key', 'base_url': 'https://api.z.ai/api/coding/paas/v4'}),
        )
        with patch('open_webui.utils.provider_accounts.aiohttp.ClientSession', return_value=manager):
            await provider_accounts.refresh_zai_limits(coding, None, max_age=60)
            # A reading less than a minute old is kept.
            await provider_accounts.refresh_zai_limits(coding, None, max_age=60)
        self.assertEqual(
            seen, [(provider_accounts.ZAI_QUOTA_URL, {'Authorization': 'zai-test-key', 'Accept-Language': 'en-US,en'})]
        )
        self.assertEqual((await get_provider_limits(None, coding.id))['windows'][0]['used_percent'], 37)
        self.assertEqual(parse_zai_quota({'success': False, 'code': 200, 'data': {'limits': []}}), [])

    async def test_api_key_models_keep_provider_details(self):
        entry = {
            'id': 'vendor/model',
            'name': 'Vendor: Model',
            'context_length': 200000,
            'architecture': {'input_modalities': ['text']},
            'supported_parameters': ['reasoning', 'tools'],
            'pricing': {'prompt': '0.000001'},
        }
        self.assertEqual(
            catalog_model(entry['id'], entry['name'], entry),
            {
                'id': 'vendor/model',
                'name': 'Vendor: Model',
                'details': {
                    'supported_parameters': ['reasoning', 'tools'],
                    'architecture': {'input_modalities': ['text']},
                    'context_length': 200000,
                },
            },
        )
        self.assertEqual(catalog_model('bare', None, {'id': 'bare'}), {'id': 'bare', 'name': 'bare'})

    async def test_audit_body_logging_redacts_keys_and_device_codes(self):
        from starlette.requests import Request
        from open_webui.utils.audit import AuditContext, AuditLevel, AuditLoggingMiddleware

        middleware = AuditLoggingMiddleware(AsyncMock(), audit_level=AuditLevel.REQUEST_RESPONSE)
        middleware._get_authenticated_user = AsyncMock(return_value=None)
        middleware.audit_logger = MagicMock()
        request = Request(
            {
                'type': 'http',
                'method': 'POST',
                'path': '/api/v1/provider-accounts/codex',
                'query_string': b'',
                'headers': [],
                'server': ('localhost', 80),
            }
        )
        context = AuditContext(4096)
        context.add_request_chunk(b'{"api_key":"never-log-this-key"}')
        context.add_response_chunk(b'{"user_code":"never-log-this-code"}')
        await middleware._log_audit_entry(request, context)
        entry = middleware.audit_logger.write.call_args.args[0]
        self.assertEqual(entry.request_object, '[provider account body redacted]')
        self.assertEqual(entry.response_object, '[provider account body redacted]')

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

    def codex_patches(self, accounts, summaries, session, resets_enabled=True, reset=None):
        async def prepare(account):
            return 'key-' + account.id, summaries[account.id]

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(proxy, 'select_accounts', AsyncMock(return_value=accounts)))
        stack.enter_context(patch.object(proxy, 'prepare_request', AsyncMock(side_effect=prepare)))
        stack.enter_context(patch.object(proxy.codex_lb, 'ensure_settings', AsyncMock()))
        stack.enter_context(patch.object(proxy.codex_lb, 'get_session', MagicMock(return_value=session)))
        stack.enter_context(patch.object(proxy, 'auto_reset_enabled', AsyncMock(return_value=resets_enabled)))
        return stack, stack.enter_context(patch.object(proxy, 'auto_reset', reset or AsyncMock(return_value=None)))

    def completed(self):
        result = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello'}]}]}
        return Response(events=[{'type': 'response.completed', 'response': result}])

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
        stack, _ = self.codex_patches([account()], {'test-account': lb_summary('lb-test')}, session)
        with stack:
            answer = await proxy.account_completion(
                request, {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}, OWNER
            )
            self.assertEqual(answer['choices'][0]['message']['content'], 'Hello')
            self.assertEqual(answer['usage']['prompt_tokens'], 12)
            sent = session.post.call_args
            self.assertEqual(sent.args[0], codex_lb.BASE_URL + '/backend-api/codex/responses')
            self.assertFalse(sent.kwargs['json']['store'])
            self.assertTrue(sent.kwargs['json']['stream'])
            self.assertEqual(
                sent.kwargs['headers'], {'Authorization': 'Bearer key-test-account', 'Accept': 'text/event-stream'}
            )
            self.assertTrue(upstream.closed)
            session.post.side_effect = RuntimeError('secret-token')
            with self.assertRaises(HTTPException) as error:
                await proxy.account_completion(request, {'model': 'account:codex:test-model'}, OWNER)
            self.assertNotIn('secret-token', error.exception.detail)

    async def test_empty_account_hands_request_to_next_account(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        own = account(id='own-empty')
        shared = account(id='shared-with-quota', owner_user_id=OTHER.id, sharing='shared')
        summaries = {'own-empty': lb_summary('a', remaining=0), 'shared-with-quota': lb_summary('b', remaining=80)}
        session = SimpleNamespace(post=AsyncMock(return_value=self.completed()))
        stack, reset = self.codex_patches([own, shared], summaries, session)
        with stack:
            form = {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
            answer = await proxy.account_completion(request, form, OWNER)
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hello')
        session.post.assert_awaited_once()
        self.assertEqual(session.post.call_args.kwargs['headers']['Authorization'], 'Bearer key-shared-with-quota')
        reset.assert_not_awaited()
        # Next time the known-empty account is tried last.
        ordered = await proxy.order_by_headroom(None, [own, shared])
        self.assertEqual([item.id for item in ordered], ['shared-with-quota', 'own-empty'])

    async def test_rate_limited_account_fails_over_with_the_full_conversation(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        first = account(id='limited-first')
        second = account(id='limited-second', owner_user_id=OTHER.id, sharing='shared')
        summaries = {'limited-first': lb_summary('a'), 'limited-second': lb_summary('b')}
        limited = Response(status=429)
        session = SimpleNamespace(post=AsyncMock(side_effect=[limited, self.completed()]))
        stack, _ = self.codex_patches([first, second], summaries, session)
        with stack:
            form = {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
            await proxy.account_completion(request, form, OWNER)
        first_call, second_call = session.post.call_args_list
        self.assertEqual(second_call.kwargs['headers']['Authorization'], 'Bearer key-limited-second')
        # The Responses conversion consumes messages; the retry must still send the conversation.
        self.assertEqual(first_call.kwargs['json']['input'], second_call.kwargs['json']['input'])
        self.assertTrue(second_call.kwargs['json']['input'])
        self.assertEqual(form['messages'], [{'role': 'user', 'content': 'Hi'}])
        self.assertTrue(limited.closed)

    async def test_every_account_empty_redeems_a_reset_then_serves(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        only = account(id='refilled')
        summaries = {'refilled': lb_summary('a', remaining=0)}

        async def redeem(accounts, user_id, redis):
            summaries['refilled'] = lb_summary('a', remaining=100)
            return accounts[0]

        session = SimpleNamespace(post=AsyncMock(return_value=self.completed()))
        stack, reset = self.codex_patches([only], summaries, session, reset=AsyncMock(side_effect=redeem))
        with stack:
            await proxy.account_completion(request, {'model': 'account:codex:test-model', 'messages': []}, OWNER)
        reset.assert_awaited_once_with([only], OWNER.id, None)
        session.post.assert_awaited_once()

    async def test_every_account_empty_with_resets_off_reports_the_wait(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        only = account(id='empty-no-resets')
        session = SimpleNamespace(post=AsyncMock())
        stack, reset = self.codex_patches(
            [only], {only.id: lb_summary('a', remaining=0)}, session, resets_enabled=False
        )
        with stack:
            with self.assertRaises(HTTPException) as error:
                await proxy.account_completion(request, {'model': 'account:codex:test-model', 'messages': []}, OWNER)
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn('resets in 1 h.', error.exception.detail)
        self.assertIn('turned off', error.exception.detail)
        reset.assert_not_awaited()
        session.post.assert_not_awaited()

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
