"""Managed Codex login and refresh. App-server never receives chat prompts or tool requests."""

import asyncio
import base64
import contextlib
import fcntl
import hmac
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import delete, select

from open_webui.env import DATA_DIR
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_accounts import (
    LimitResetEvents,
    ProviderAccounts,
    ProviderLogin,
    open_secret,
    seal_secret,
)

log = logging.getLogger(__name__)

ACCOUNT_ROOT = Path(DATA_DIR) / 'provider_accounts' / 'codex'
LOGIN_TTL = 15 * 60
# After a redemption, wait this long before spending another coupon on any account.
RESET_COOLDOWN = 10 * 60
_tasks: dict[str, asyncio.Task] = {}
_login_lock = asyncio.Lock()
_reset_lock = asyncio.Lock()


def account_home(identifier: str) -> Path:
    # Only generated UUIDs can select directories; never accept a client path.
    identifier = str(uuid.UUID(identifier))
    ACCOUNT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    ACCOUNT_ROOT.chmod(0o700)
    home = ACCOUNT_ROOT / identifier
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    return home


class AppServer:
    def __init__(self, identifier: str):
        self.home = account_home(identifier)
        self.sequence = 0
        self.waiters = {}
        self.notifications = asyncio.Queue()
        self.process = None
        self.reader = None
        self.lock = None

    async def __aenter__(self):
        self.lock = (self.home / '.mologate.lock').open('a')
        try:
            async with asyncio.timeout(30):
                while True:
                    try:
                        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.1)
        except BaseException as error:
            self.lock.close()
            if isinstance(error, TimeoutError):
                raise HTTPException(409, 'This account is busy. Try again shortly.') from None
            raise
        # Do not inherit the web server's database passwords, provider keys, or host login.
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if key in os.environ}
        env['CODEX_HOME'] = str(self.home)
        env['RUST_LOG'] = 'off'
        try:
            self.process = await asyncio.create_subprocess_exec(
                os.environ.get('MOLOGATE_CODEX_BINARY', 'codex'),
                'app-server',
                '--listen',
                'stdio://',
                '-c',
                'cli_auth_credentials_store="file"',
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.home,
                env=env,
                limit=4 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self._read())
            await self.call(
                'initialize',
                {
                    'clientInfo': {'name': 'mologate', 'title': 'MoloGate', 'version': '1.0'},
                    'capabilities': {'experimentalApi': True},
                },
            )
            await self._send({'method': 'initialized', 'params': {}})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if 'id' in message and 'method' not in message:
                    waiter = self.waiters.get(message['id'])
                    if waiter and not waiter.done():
                        if 'error' in message:
                            waiter.set_exception(RuntimeError('Codex could not complete the account operation.'))
                        else:
                            waiter.set_result(message.get('result', {}))
                elif 'id' in message:
                    # This bridge only manages authentication and catalogs, never tools.
                    await self._send(
                        {'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported operation'}}
                    )
                elif message.get('method') == 'account/login/completed':
                    await self.notifications.put(message.get('params', {}))
        except (ValueError, OSError, asyncio.CancelledError):
            pass
        finally:
            for waiter in self.waiters.values():
                if not waiter.done():
                    waiter.set_exception(RuntimeError('Codex account service disconnected.'))

    async def call(self, method, params=None, timeout=45):
        self.sequence += 1
        identifier = self.sequence
        waiter = asyncio.get_running_loop().create_future()
        self.waiters[identifier] = waiter
        try:
            await self._send({'id': identifier, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self.waiters.pop(identifier, None)

    async def __aexit__(self, *args):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.lock and not self.lock.closed:
            self.lock.close()


def read_credentials(identifier: str):
    path = account_home(identifier) / 'auth.json'
    path.chmod(0o600)
    tokens = json.loads(path.read_text()).get('tokens') or {}
    if not tokens.get('access_token') or not tokens.get('account_id'):
        raise ValueError('Codex account credentials are incomplete.')
    return tokens


async def _save_login(identifier: str, **values):
    async with get_async_db_context() as db:
        attempt = await db.get(ProviderLogin, identifier)
        if attempt:
            for key, value in values.items():
                setattr(attempt, key, value)
            attempt.updated_at = int(time.time())
            await db.commit()


def _link_details(started):
    """Only OpenAI's own sign-in page, returning to app-server's loopback callback, is accepted."""
    auth = urlparse(started['authUrl'])
    query = parse_qs(auth.query)
    callback = urlparse((query.get('redirect_uri') or [''])[0])
    if auth.scheme != 'https' or auth.hostname != 'auth.openai.com' or not query.get('state'):
        raise RuntimeError('Unexpected authorization URL.')
    if callback.scheme != 'http' or callback.hostname not in ('127.0.0.1', 'localhost') or not callback.port:
        raise RuntimeError('Unexpected sign-in callback.')
    return {
        'method': 'link',
        'loginId': started['loginId'],
        'authUrl': started['authUrl'],
        'state': query['state'][0],
        'callbackPort': callback.port,
        'callbackPath': callback.path or '/auth/callback',
    }


async def _complete_login(identifier, owner_id, label, sharing, ready, redis=None, method='device_code'):
    keep_home = False
    try:
        async with AppServer(identifier) as server:
            if method == 'link':
                # The member signs in through OpenAI's page and pastes back the loopback address.
                started = _link_details(await server.call('account/login/start', {'type': 'chatgpt'}))
            else:
                started = await server.call('account/login/start', {'type': 'chatgptDeviceCode'})
                # Only OpenAI's device authorization page is shown to the user.
                parsed = urlparse(started['verificationUrl'])
                if parsed.scheme != 'https' or parsed.hostname not in ('auth.openai.com', 'chatgpt.com'):
                    raise RuntimeError('Unexpected device authorization URL.')
                started = {'method': 'device_code', **started}
            await _save_login(identifier, secret=seal_secret({'home_id': identifier, **started}))
            if not ready.done():
                ready.set_result(True)
            async with asyncio.timeout(LOGIN_TTL):
                while True:
                    completed = await server.notifications.get()
                    if completed.get('loginId') == started['loginId']:
                        if not completed.get('success'):
                            raise RuntimeError('Sign-in was not completed.')
                        break
            details = await server.call('account/read', {'refreshToken': False})
            if (details.get('account') or {}).get('type') != 'chatgpt':
                raise RuntimeError('Connect a ChatGPT account.')
            tokens = read_credentials(identifier)
            catalog = await server.call('model/list', {'includeHidden': False})
            models = [
                {'id': item.get('model') or item['id'], 'name': item.get('displayName') or item['id']}
                for item in catalog.get('data', [])
            ]
            if not models:
                raise RuntimeError('Codex returned no models.')
            account = await ProviderAccounts.insert(
                owner_user_id=owner_id,
                provider='codex',
                plan=details['account'].get('planType') or 'unknown',
                external_account_id=tokens['account_id'],
                label=label,
                sharing=sharing,
                capacity_weight=1,
                budget=None,
                allow_team_resets=True,
                status='ok',
                models=models,
                secret={'home_id': identifier},
            )
            keep_home = True
            await _save_login(identifier, status='completed', account_id=account.id, secret=seal_secret({}))
            from open_webui.utils.provider_limits import record_codex_snapshot

            with contextlib.suppress(RuntimeError, TimeoutError):
                await record_codex_snapshot(redis, account.id, await server.call('account/rateLimits/read'))
    except (Exception, asyncio.CancelledError) as error:
        message = 'Sign-in expired or failed. Start a new connection.'
        if isinstance(error, IntegrityError):
            message = 'This ChatGPT account is already connected.'
        await _save_login(identifier, status='error', error=message, secret=seal_secret({}))
        if not ready.done():
            ready.set_exception(HTTPException(502, message))
    finally:
        if not keep_home:
            shutil.rmtree(account_home(identifier), ignore_errors=True)
        _tasks.pop(identifier, None)


async def start_login(user, label, sharing, redis=None, method='device_code'):
    if not shutil.which(os.environ.get('MOLOGATE_CODEX_BINARY', 'codex')):
        raise HTTPException(503, 'The Codex account service is not installed.')
    identifier = str(uuid.uuid4())
    now = int(time.time())
    # Bound long-lived login subprocesses on the small VPS; pending attempts survive worker routing.
    async with _login_lock, get_async_db_context() as db:
        pending = list(
            (
                await db.execute(
                    select(ProviderLogin).where(ProviderLogin.status == 'pending', ProviderLogin.expires_at > now)
                )
            ).scalars()
        )
        if any(attempt.owner_user_id == user.id for attempt in pending):
            return next(attempt.id for attempt in pending if attempt.owner_user_id == user.id)
        if len(pending) >= 4:
            raise HTTPException(429, 'Several members are connecting accounts. Try again shortly.')
        # Every link sign-in returns to the same loopback port; a second one would cancel the first.
        if method == 'link' and any(open_secret(attempt.secret).get('method') == 'link' for attempt in pending):
            raise HTTPException(
                409, 'Another member is signing in with a link. Try again in a few minutes, or sign in with a code.'
            )
        db.add(
            ProviderLogin(
                id=identifier,
                owner_user_id=user.id,
                label=label,
                sharing=sharing,
                secret=seal_secret({'method': method}),
                status='pending',
                expires_at=now + LOGIN_TTL,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()
    ready = asyncio.get_running_loop().create_future()
    _tasks[identifier] = asyncio.create_task(_complete_login(identifier, user.id, label, sharing, ready, redis, method))
    try:
        await asyncio.wait_for(ready, 60)
    except TimeoutError:
        _tasks[identifier].cancel()
        raise HTTPException(504, 'The Codex login service timed out. Try again.') from None
    return identifier


async def login_status(identifier, user):
    async with get_async_db_context() as db:
        attempt = await db.get(ProviderLogin, identifier)
        if not attempt or attempt.owner_user_id != user.id:
            raise HTTPException(404, 'Connection attempt not found.')
        status = attempt.status
        if status == 'pending' and attempt.expires_at < time.time():
            status = 'error'
        result = {
            'id': attempt.id,
            'status': status,
            'account_id': attempt.account_id,
            'expires_at': attempt.expires_at,
            'error': attempt.error,
        }
        if status == 'pending':
            secret = open_secret(attempt.secret)
            result.update(
                method=secret.get('method', 'device_code'),
                user_code=secret.get('userCode'),
                verification_url=secret.get('verificationUrl'),
                auth_url=secret.get('authUrl'),
            )
        return result


def callback_params(value: str) -> dict:
    """Only the OAuth result travels on; the pasted host, port and path are never used."""
    value = value.strip()
    query = urlparse(value).query if '?' in value else value.lstrip('?')
    params = {
        key: values[0]
        for key, values in parse_qs(query).items()
        if key in ('code', 'state', 'error', 'error_description') and values
    }
    if not params.get('state') or not (params.get('code') or params.get('error')):
        raise HTTPException(
            400,
            'Paste the full address your browser opened after signing in. '
            'It starts with http://127.0.0.1:1455/auth/callback.',
        )
    return params


async def submit_callback(identifier, user, value: str):
    async with get_async_db_context() as db:
        attempt = await db.get(ProviderLogin, identifier)
        if not attempt or attempt.owner_user_id != user.id:
            raise HTTPException(404, 'Connection attempt not found.')
        status, expires_at, secret = attempt.status, attempt.expires_at, open_secret(attempt.secret)
    if status != 'pending' or expires_at < time.time() or secret.get('method') != 'link':
        raise HTTPException(409, 'This sign-in is no longer waiting. Start a new connection.')
    if not secret.get('state'):
        raise HTTPException(409, 'The sign-in link is still being prepared. Try again in a moment.')
    params = callback_params(value)
    if not hmac.compare_digest(params['state'].encode(), secret['state'].encode()):
        raise HTTPException(400, 'This address belongs to a different sign-in. Use the address from the latest link.')
    # app-server holds the PKCE verifier and exchanges the code itself at its loopback callback.
    url = f'http://127.0.0.1:{int(secret["callbackPort"])}{secret["callbackPath"]}?{urlencode(params)}'
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(url, allow_redirects=False) as response:
                accepted = response.status < 400
    except (aiohttp.ClientError, TimeoutError):
        raise HTTPException(502, 'The sign-in could not be finished. Start a new connection.') from None
    if not accepted:
        raise HTTPException(400, 'OpenAI did not accept this sign-in. Start a new connection and try again.')
    if task := _tasks.get(identifier):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), 30)
    return await login_status(identifier, user)


async def cancel_login(identifier, user):
    async with get_async_db_context() as db:
        attempt = await db.get(ProviderLogin, identifier)
        if not attempt or attempt.owner_user_id != user.id:
            raise HTTPException(404, 'Connection attempt not found.')
        pending = attempt.status == 'pending'
    if task := _tasks.get(identifier):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if pending:
        await _save_login(identifier, status='error', error='Sign-in cancelled.', secret=seal_secret({}))
    return True


def token_needs_refresh(tokens):
    try:
        value = tokens['access_token'].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        return float(claims['exp']) < time.time() + 300
    except (KeyError, IndexError, ValueError, TypeError):
        return True


async def refresh_account(account):
    identifier = open_secret(account.secret)['home_id']
    try:
        async with AppServer(identifier) as server:
            try:
                refresh = token_needs_refresh(read_credentials(identifier))
            except (OSError, ValueError):
                refresh = True
            details = await server.call('account/read', {'refreshToken': refresh})
            if (details.get('account') or {}).get('type') != 'chatgpt':
                raise ValueError('Login required.')
            tokens = read_credentials(identifier)
            if tokens['account_id'] != account.external_account_id:
                raise ValueError('Account identity changed.')
            return tokens, await server.call('account/rateLimits/read')
    except HTTPException:
        raise
    except (OSError, ValueError):
        await ProviderAccounts.update(
            account.id, {'status': 'needs_login', 'last_error': 'Reconnect this ChatGPT account.'}
        )
        raise HTTPException(401, 'This ChatGPT account needs to be reconnected.') from None
    except (RuntimeError, TimeoutError):
        raise HTTPException(502, 'The Codex account service is unavailable. Try again shortly.') from None


async def consume_reset_credit(account, idempotency_key: str, credit_id: str | None = None) -> str:
    params = {'idempotencyKey': idempotency_key}
    if credit_id:
        params['creditId'] = credit_id
    async with AppServer(open_secret(account.secret)['home_id']) as server:
        result = await server.call('account/rateLimitResetCredit/consume', params)
    return result.get('outcome') or 'unknown'


async def auto_reset(accounts, triggered_by: str | None, redis=None):
    """Redeem one reset coupon once every eligible account is empty. Returns a usable account."""
    from open_webui.utils.provider_limits import codex_exhausted, codex_reset_credits, record_codex_snapshot

    accounts = [account for account in accounts if account.provider == 'codex' and account.allow_team_resets]
    if not accounts:
        return None
    async with _reset_lock:
        coupons = []
        for account in accounts:
            # Read again under the lock: a concurrent request may already have refilled an account.
            try:
                _, limits = await refresh_account(account)
            except HTTPException:
                continue
            await record_codex_snapshot(redis, account.id, limits)
            if not codex_exhausted(limits):
                return account
            coupons += [(credit, account) for credit in codex_reset_credits(limits)]
        # The database also guards workers that do not share this lock.
        if not coupons or await LimitResetEvents.recent('codex', RESET_COOLDOWN):
            return None
        # Spend the coupon that would expire first.
        credit, account = min(coupons, key=lambda coupon: coupon[0].get('expiresAt') or float('inf'))
        event = await LimitResetEvents.start(account, triggered_by, credit['id'])
        outcome = 'error'
        try:
            for attempt in range(2):
                try:
                    # The same idempotency key makes a retried attempt redeem at most one coupon.
                    outcome = await consume_reset_credit(account, event.idempotency_key, credit['id'])
                    break
                except (HTTPException, OSError, RuntimeError, TimeoutError):
                    if attempt:
                        log.warning('Automatic limit reset failed for account %s.', account.id)
        finally:
            await LimitResetEvents.finish(event.id, outcome)
        log.info('Automatic limit reset on account %s: %s.', account.id, outcome)
        if outcome not in ('reset', 'nothingToReset'):
            return None
        try:
            _, limits = await refresh_account(account)
        except HTTPException:
            return None
        await record_codex_snapshot(redis, account.id, limits)
        return None if codex_exhausted(limits) else account


async def disconnect(account):
    identifier = open_secret(account.secret)['home_id']
    # Local deletion remains possible when login has expired or the binary is unavailable.
    try:
        async with AppServer(identifier) as server:
            await server.call('account/logout')
    except (OSError, RuntimeError, TimeoutError):
        pass
    shutil.rmtree(account_home(identifier), ignore_errors=True)


async def delete_user_accounts(user_id):
    """Revoke credentials and pending logins when the owner is deleted."""
    from open_webui.models.provider_accounts import ProviderAccount

    async with get_async_db_context() as db:
        accounts = list(
            (await db.execute(select(ProviderAccount).where(ProviderAccount.owner_user_id == user_id))).scalars()
        )
        attempts = list(
            (await db.execute(select(ProviderLogin).where(ProviderLogin.owner_user_id == user_id))).scalars()
        )
        for attempt in attempts:
            if task := _tasks.get(attempt.id):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await db.execute(delete(ProviderLogin).where(ProviderLogin.owner_user_id == user_id))
        await db.execute(delete(ProviderAccount).where(ProviderAccount.owner_user_id == user_id))
        await db.commit()
    for account in accounts:
        if account.provider == 'codex':
            shutil.rmtree(account_home(open_secret(account.secret)['home_id']), ignore_errors=True)
    for attempt in attempts:
        shutil.rmtree(account_home(attempt.id), ignore_errors=True)


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
