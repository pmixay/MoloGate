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
from unittest.mock import AsyncMock, MagicMock, patch

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
    codex_exhausted,
    codex_reset_credits,
    get_provider_limits,
    legacy_account_key,
    parse_rate_limit_headers,
    parse_zai_quota,
    record_codex_snapshot,
    record_provider_limits,
    record_provider_snapshot,
    snapshot_exhausted,
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


def codex_limits(used=10, allowed=None, credits=None, count=None):
    """An `account/rateLimits/read` result in the pinned app-server's shape."""
    summary = None
    if credits is not None or count is not None:
        summary = {'availableCount': len(credits or []) if count is None else count, 'credits': credits}
    return {
        'ordinaryUsageAllowed': allowed,
        'rateLimits': {
            'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': int(time.time()) + 3600}
        },
        'rateLimitResetCredits': summary,
    }


def coupon(identifier, expires_in, status='available'):
    expires_at = None if expires_in is None else int(time.time()) + expires_in
    return {'id': identifier, 'resetType': 'codexRateLimits', 'status': status, 'expiresAt': expires_at}


LINK_APP_SERVER = """
import http.server, json, os, sys, threading, urllib.parse
from pathlib import Path

lock = threading.Lock()


def send(message):
    with lock:
        print(json.dumps(message), flush=True)


class Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # Like the real loopback server: only this login's state and code, nothing else.
        if query == {'code': ['fake-code'], 'state': ['fake-state']}:
            tokens = {'account_id': 'link-chatgpt-id', 'access_token': 'fake-token'}
            Path(os.environ['CODEX_HOME'], 'auth.json').write_text(json.dumps({'tokens': tokens}))
            self.send_response(302)
            self.send_header('Location', '/success')
            self.end_headers()
            send({'method': 'account/login/completed', 'params': {'loginId': 'link-1', 'success': True}})
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, *args):
        pass


for raw in sys.stdin:
    request = json.loads(raw)
    if 'id' not in request:
        continue
    method, result = request['method'], {}
    if method == 'account/login/start' and request['params'].get('type') == 'chatgpt':
        server = http.server.HTTPServer(('127.0.0.1', 0), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        redirect = urllib.parse.quote(f'http://127.0.0.1:{server.server_port}/auth/callback', safe='')
        url = f'https://auth.openai.com/oauth/authorize?response_type=code&redirect_uri={redirect}&state=fake-state'
        result = {'type': 'chatgpt', 'loginId': 'link-1', 'authUrl': url}
    elif method == 'account/read':
        result = {'account': {'type': 'chatgpt', 'planType': 'pro'}}
    elif method == 'model/list':
        result = {'data': [{'id': 'test-model', 'model': 'test-model', 'displayName': 'Test'}]}
    elif method == 'account/rateLimits/read':
        result = {'rateLimits': {'primary': {'usedPercent': 10}}}
    send({'id': request['id'], 'result': result})
"""


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

    def test_exhaustion_is_reported_never_assumed(self):
        now = time.time()
        empty = {'windows': [{'id': 'primary', 'used_percent': 100, 'resets_at': now + 60}]}
        self.assertTrue(snapshot_exhausted(empty, now))
        self.assertFalse(snapshot_exhausted(empty, now + 61))
        self.assertFalse(snapshot_exhausted({'windows': [{'id': 'primary', 'used_percent': 99}]}, now))
        self.assertFalse(snapshot_exhausted(None, now))
        # The backend's permission wins over percentages in both directions.
        self.assertTrue(codex_exhausted(codex_limits(used=10, allowed=False)))
        self.assertFalse(codex_exhausted(codex_limits(used=100, allowed=True)))
        self.assertTrue(codex_exhausted(codex_limits(used=100)))
        self.assertFalse(codex_exhausted(codex_limits(used=90)))
        self.assertFalse(codex_exhausted({}))
        reached = codex_limits(used=50)
        reached['rateLimits']['rateLimitReachedType'] = 'rate_limit_reached'
        self.assertTrue(codex_exhausted(reached))

    def test_reset_coupons_soonest_expiry_first(self):
        credits = [
            coupon('later', 7200),
            coupon('never', None),
            coupon('soon', 600),
            coupon('expired', -60),
            coupon('spent', 60, status='redeemed'),
        ]
        ids = [credit['id'] for credit in codex_reset_credits(codex_limits(credits=credits))]
        self.assertEqual(ids, ['soon', 'later', 'never'])
        # Only a count: the backend picks the coupon itself.
        self.assertEqual(codex_reset_credits(codex_limits(count=2)), [{'id': None, 'expiresAt': None}])
        self.assertEqual(codex_reset_credits(codex_limits(count=0)), [])
        self.assertEqual(codex_reset_credits(codex_limits(credits=[])), [])
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


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_auto_reset_spends_soonest_coupon_once_under_concurrency(self):
        first = account(id='first', sharing='shared')
        second = account(id='second', sharing='shared', owner_user_id=OTHER.id)
        limits = {
            'first': codex_limits(used=100, credits=[coupon('first-coupon', 7200)]),
            'second': codex_limits(used=100, credits=[coupon('second-coupon', 600)]),
        }

        async def consume(account, key, credit_id):
            await asyncio.sleep(0.01)
            limits[account.id] = codex_limits(used=0)
            return 'reset'

        refresh = AsyncMock(side_effect=lambda account: ({'access_token': 'token'}, limits[account.id]))
        with (
            patch.object(codex_accounts, 'refresh_account', refresh),
            patch.object(codex_accounts, 'consume_reset_credit', AsyncMock(side_effect=consume)) as redeem,
        ):
            # Two requests find every account empty at once; only one coupon may be spent.
            results = await asyncio.gather(
                codex_accounts.auto_reset([first, second], OWNER.id),
                codex_accounts.auto_reset([first, second], OTHER.id),
            )
        self.assertEqual([result.id for result in results], ['second', 'second'])
        redeem.assert_awaited_once()
        self.assertEqual(redeem.await_args.args[2], 'second-coupon')
        async with get_async_db_context() as db:
            events = list((await db.execute(select(LimitResetEvent))).scalars())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, 'reset')
        self.assertEqual(events[0].idempotency_key, redeem.await_args.args[1])
        self.assertIn(events[0].triggered_by, (OWNER.id, OTHER.id))
        self.assertEqual(list(await LimitResetEvents.last_resets()), ['second'])
        self.assertEqual((await get_provider_limits(None, 'second'))['windows'][0]['used_percent'], 0)

    async def test_auto_reset_respects_opt_out_cooldown_and_retries_with_same_key(self):
        opted_out = account(id='opted-out', allow_team_resets=False)
        empty = account(id='still-empty')
        limits = codex_limits(used=100, credits=[coupon('coupon', 600)])
        consume = AsyncMock(side_effect=[RuntimeError('connection lost'), 'reset'])
        with (
            patch.object(codex_accounts, 'refresh_account', AsyncMock(return_value=({}, limits))),
            patch.object(codex_accounts, 'consume_reset_credit', consume),
        ):
            self.assertIsNone(await codex_accounts.auto_reset([opted_out], OWNER.id))
            consume.assert_not_awaited()
            # Redeemed, yet the account still reports empty: no further coupon inside the cooldown.
            self.assertIsNone(await codex_accounts.auto_reset([opted_out, empty], OWNER.id))
            self.assertEqual(consume.await_count, 2)
            first_try, retry = consume.await_args_list
            self.assertEqual(first_try.args[0].id, 'still-empty')
            self.assertEqual(first_try.args[1], retry.args[1])
            self.assertIsNone(await codex_accounts.auto_reset([empty], OWNER.id))
            self.assertEqual(consume.await_count, 2)
        self.assertTrue(await LimitResetEvents.recent('codex', codex_accounts.RESET_COOLDOWN))
        self.assertFalse(await LimitResetEvents.recent('codex', 0))

    async def test_reset_coupon_count_survives_header_updates(self):
        await record_codex_snapshot(None, 'coupon-count', codex_limits(credits=[coupon('a', 600), coupon('b', 900)]))
        await record_provider_limits(None, 'coupon-count', {'x-codex-primary-used-percent': '40'})
        snapshot = await get_provider_limits(None, 'coupon-count')
        self.assertEqual(snapshot['windows'][0]['used_percent'], 40)
        self.assertEqual(snapshot['reset_credits'], 2)

    async def test_link_login_accepts_only_its_own_pasted_callback(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'fake-codex'
            binary.write_text('#!' + sys.executable + '\n' + LINK_APP_SERVER)
            binary.chmod(0o700)
            with (
                patch.dict(os.environ, {'MOLOGATE_CODEX_BINARY': str(binary)}),
                patch.object(codex_accounts, 'ACCOUNT_ROOT', Path(temporary) / 'accounts'),
            ):
                identifier = await codex_accounts.start_login(OWNER, 'Link ChatGPT', 'shared', method='link')
                status = await codex_accounts.login_status(identifier, OWNER)
                self.assertEqual(status['method'], 'link')
                self.assertTrue(status['auth_url'].startswith('https://auth.openai.com/oauth/authorize?'))
                self.assertIsNone(status['user_code'])
                # Every link sign-in returns to the same loopback port, so a second one must wait.
                with self.assertRaises(HTTPException) as busy:
                    await codex_accounts.start_login(OTHER, 'Second', 'private', method='link')
                self.assertEqual(busy.exception.status_code, 409)
                pasted = 'http://127.0.0.1:1455/auth/callback?code=fake-code&state=fake-state'
                with self.assertRaises(HTTPException) as foreign:
                    await codex_accounts.submit_callback(identifier, OTHER, pasted)
                self.assertEqual(foreign.exception.status_code, 404)
                for wrong in (
                    pasted.replace('state=fake-state', 'state=other'),
                    pasted.replace('state=fake-state', 'state=f%C3%BCnf'),
                    'https://example.com/',
                ):
                    with self.assertRaises(HTTPException) as rejected:
                        await codex_accounts.submit_callback(identifier, OWNER, wrong)
                    self.assertEqual(rejected.exception.status_code, 400)
                # The pasted host and extra parameters are ignored: only code and state reach app-server.
                done = await codex_accounts.submit_callback(
                    identifier, OWNER, '  http://evil.example:9/auth/callback?code=fake-code&state=fake-state&next=x '
                )
                self.assertEqual(done['status'], 'completed')
                saved = await ProviderAccounts.get(done['account_id'])
                self.assertEqual(
                    (saved.external_account_id, saved.plan, saved.sharing), ('link-chatgpt-id', 'pro', 'shared')
                )
                with self.assertRaises(HTTPException) as finished:
                    await codex_accounts.submit_callback(identifier, OWNER, pasted)
                self.assertEqual(finished.exception.status_code, 409)
                # The port is free again; a pending sign-in can be cancelled by its owner.
                second = await codex_accounts.start_login(OTHER, 'Second', 'private', method='link')
                self.assertTrue(await codex_accounts.cancel_login(second, OTHER))
                cancelled = await codex_accounts.login_status(second, OTHER)
                self.assertEqual((cancelled['status'], cancelled['error']), ('error', 'Sign-in cancelled.'))

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
    elif method == 'account/rateLimitResetCredit/consume':
        expected = {'idempotencyKey': 'attempt-1', 'creditId': 'credit-1'}
        result = {'outcome': 'reset' if request['params'] == expected else 'noCredit'}
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
                outcome = await codex_accounts.consume_reset_credit(saved, 'attempt-1', 'credit-1')
                self.assertEqual(outcome, 'reset')
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
        form = {'provider': 'openai_api', 'label': 'test', 'api_key': 'test-secret'}
        self.assertEqual(self.client.post('/accounts', json=form).status_code, 400)
        self.assertEqual(self.client.post('/accounts', json=form | {'owner_user_id': 'other'}).status_code, 422)


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

    async def json(self):
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
            patch.object(proxy, 'select_accounts', AsyncMock(return_value=[account()])),
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

    def codex_patches(self, accounts, limits, session, resets_enabled=True, reset=None):
        def refresh(account):
            return {'access_token': 'token-' + account.id, 'account_id': account.id}, limits[account.id]

        return (
            patch.object(proxy, 'select_accounts', AsyncMock(return_value=accounts)),
            patch.object(proxy, 'refresh_account', AsyncMock(side_effect=refresh)),
            patch.object(proxy, 'get_session', AsyncMock(return_value=session)),
            patch.object(proxy, 'auto_reset_enabled', AsyncMock(return_value=resets_enabled)),
            patch.object(proxy, 'auto_reset', reset or AsyncMock(return_value=None)),
        )

    def completed(self):
        result = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello'}]}]}
        return Response(events=[{'type': 'response.completed', 'response': result}])

    async def test_empty_account_hands_request_to_next_account(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        own = account(id='own-empty')
        shared = account(id='shared-with-quota', owner_user_id=OTHER.id, sharing='shared')
        limits = {'own-empty': codex_limits(used=100), 'shared-with-quota': codex_limits(used=20)}
        session = SimpleNamespace(post=AsyncMock(return_value=self.completed()))
        patches = self.codex_patches([own, shared], limits, session)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as reset:
            form = {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
            answer = await proxy.account_completion(request, form, OWNER)
        self.assertEqual(answer['choices'][0]['message']['content'], 'Hello')
        session.post.assert_awaited_once()
        self.assertEqual(session.post.call_args.kwargs['headers']['ChatGPT-Account-Id'], 'shared-with-quota')
        reset.assert_not_awaited()
        # Next time the known-empty account is tried last.
        ordered = await proxy.order_by_headroom(None, [own, shared])
        self.assertEqual([item.id for item in ordered], ['shared-with-quota', 'own-empty'])

    async def test_rate_limited_account_fails_over_with_the_full_conversation(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        first = account(id='limited-first')
        second = account(id='limited-second', owner_user_id=OTHER.id, sharing='shared')
        limits = {'limited-first': codex_limits(), 'limited-second': codex_limits()}
        limited = Response(status=429)
        session = SimpleNamespace(post=AsyncMock(side_effect=[limited, self.completed()]))
        patches = self.codex_patches([first, second], limits, session)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            form = {'model': 'account:codex:test-model', 'messages': [{'role': 'user', 'content': 'Hi'}]}
            await proxy.account_completion(request, form, OWNER)
        first_call, second_call = session.post.call_args_list
        self.assertEqual(second_call.kwargs['headers']['ChatGPT-Account-Id'], 'limited-second')
        # The Responses conversion consumes messages; the retry must still send the conversation.
        self.assertEqual(first_call.kwargs['json']['input'], second_call.kwargs['json']['input'])
        self.assertTrue(second_call.kwargs['json']['input'])
        self.assertEqual(form['messages'], [{'role': 'user', 'content': 'Hi'}])
        self.assertTrue(limited.closed)

    async def test_every_account_empty_redeems_a_reset_then_serves(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        only = account(id='refilled')
        limits = {'refilled': codex_limits(used=100)}

        async def redeem(accounts, user_id, redis):
            limits['refilled'] = codex_limits(used=0)
            return accounts[0]

        session = SimpleNamespace(post=AsyncMock(return_value=self.completed()))
        patches = self.codex_patches([only], limits, session, reset=AsyncMock(side_effect=redeem))
        with patches[0], patches[1], patches[2], patches[3], patches[4] as reset:
            await proxy.account_completion(request, {'model': 'account:codex:test-model', 'messages': []}, OWNER)
        reset.assert_awaited_once_with([only], OWNER.id, None)
        session.post.assert_awaited_once()

    async def test_every_account_empty_with_resets_off_reports_the_wait(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        only = account(id='empty-no-resets')
        session = SimpleNamespace(post=AsyncMock())
        patches = self.codex_patches([only], {only.id: codex_limits(used=100)}, session, resets_enabled=False)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as reset:
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
