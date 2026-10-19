"""ChatGPT accounts run through the internal codex-lb service.

codex-lb holds the OAuth tokens and sends inference as the official Codex client. Mologate keeps
who owns and shares each account, and one codex-lb API key per account that is assigned to that
account alone, so a request can never reach anyone else's account.
"""

import asyncio
import contextlib
import hmac
import logging
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from open_webui.env import DATA_DIR
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_accounts import (
    LimitResetEvents,
    ProviderAccount,
    ProviderAccounts,
    ProviderLogin,
    open_secret,
    seal_secret,
)
from open_webui.utils import codex_lb
from open_webui.utils.codex_lb import CodexLBError
from open_webui.utils.provider_accounts import TEAM
from open_webui.utils.provider_limits import record_provider_snapshot

log = logging.getLogger(__name__)

# Credentials of the retired per-account app-server, moved into codex-lb on first use.
ACCOUNT_ROOT = Path(DATA_DIR) / 'provider_accounts' / 'codex'
LOGIN_TTL = 15 * 60
# After a redemption, wait this long before spending another coupon on any account.
RESET_COOLDOWN = 10 * 60
RECONNECT = 'Reconnect this ChatGPT account.'
NEEDS_LOGIN = 'This ChatGPT account needs to be reconnected.'
ALREADY_CONNECTED = 'This ChatGPT account is already connected.'
BUSY = 'Another member is connecting a ChatGPT account right now. Try again in a few minutes.'
EXPIRED = 'Sign-in expired. Start a new connection.'
NOT_COMPLETED = 'Sign-in was not completed. Start a new connection.'
# codex-lb's code for a redemption -> the recorded outcome. Anything else may have spent a coupon.
RESET_OUTCOMES = {
    'reset': 'reset',
    'already_redeemed': 'reset',
    'nothing_to_reset': 'nothingToReset',
    'no_credit': 'noCredit',
}
# codex-lb refused before contacting OpenAI, so no coupon was spent.
RESET_REFUSALS = (
    'no_available_reset_credit',
    'account_not_reset_credit_applicable',
    'target_reset_credit_changed',
    'account_not_found',
)
_login_lock = asyncio.Lock()
_reset_lock = asyncio.Lock()


def unavailable():
    return HTTPException(502, 'The ChatGPT account service is unavailable. Try again shortly.')


async def _best_effort(call):
    try:
        await call
    except CodexLBError:
        log.debug('Optional codex-lb update failed.')


async def _forget_key(key_id):
    try:
        await codex_lb.delete_key(key_id)
    except CodexLBError:
        log.warning('Could not revoke a codex-lb key; it stays assigned to one account only.')


async def record_summary(redis, account_id: str, summary: dict | None):
    """Windows and coupons for the account card and the context indicator."""
    if summary:
        current = codex_lb.snapshot(summary)
        if current['windows']:
            await record_provider_snapshot(redis, account_id, current['windows'], current['reset_credits'])


async def sync_account(account, summary: dict | None, models: list | None = None):
    """Mirror codex-lb: a lost login waits for its owner, a recovered one serves again."""
    values = {}
    if summary is None or summary.get('status') in codex_lb.REAUTH_STATUSES:
        if account.status != 'needs_login':
            values.update(status='needs_login', last_error=RECONNECT)
    else:
        if account.status == 'needs_login':
            values.update(status='ok', last_error=None)
        if summary.get('planType') and summary['planType'] != account.plan:
            values['plan'] = summary['planType']
    if models and models != account.models:
        values['models'] = models
    if not values:
        return account
    updated = await ProviderAccounts.update(account.id, values) or account
    if 'plan' in values:
        # Mologate: a plan read only now still earns its connect credits (phase 5), once.
        from open_webui.utils import credits

        await credits.ensure_grant(updated)
    return updated


async def account_secret(account) -> dict:
    """The account's codex-lb binding; app-server credentials are imported the first time."""
    secret = open_secret(account.secret)
    if secret.get('lb_account_id'):
        return secret
    return await _migrate(account)


async def _migrate(account) -> dict:
    async with _login_lock:
        current = await ProviderAccounts.get(account.id)
        if current is None:
            raise HTTPException(404, 'Account not found.')
        secret = open_secret(current.secret)
        if secret.get('lb_account_id'):
            return secret
        try:
            home = ACCOUNT_ROOT / str(uuid.UUID(secret['home_id']))
            raw = (home / 'auth.json').read_bytes()
        except (KeyError, ValueError, OSError):
            await ProviderAccounts.update(account.id, {'status': 'needs_login', 'last_error': RECONNECT})
            raise HTTPException(401, NEEDS_LOGIN) from None
        try:
            imported = await codex_lb.import_auth(raw)
        except CodexLBError as error:
            if error.status in (400, 409, 422):
                # codex-lb rejected these credentials; the files stay until the owner reconnects.
                await ProviderAccounts.update(account.id, {'status': 'needs_login', 'last_error': RECONNECT})
                raise HTTPException(401, NEEDS_LOGIN) from None
            raise unavailable() from None
        lb_id = imported['accountId']
        try:
            key = await codex_lb.create_key('mologate-' + account.id, lb_id)
        except CodexLBError:
            raise unavailable() from None
        binding = {'lb_account_id': lb_id, 'lb_api_key': key['key'], 'lb_api_key_id': key['id']}
        try:
            await ProviderAccounts.update(
                account.id,
                {
                    'secret': seal_secret(binding),
                    'external_account_id': lb_id,
                    'plan': imported.get('planType') or current.plan,
                },
            )
        except IntegrityError:
            await _forget_key(key['id'])
            await ProviderAccounts.update(account.id, {'status': 'needs_login', 'last_error': ALREADY_CONNECTED})
            raise HTTPException(409, ALREADY_CONNECTED) from None
        await _best_effort(codex_lb.set_alias(lb_id, current.label))
        shutil.rmtree(home, ignore_errors=True)
        log.info('Moved ChatGPT account %s into codex-lb.', account.id)
        return binding


async def prepare_request(account) -> tuple[str, dict]:
    """The account's key and codex-lb's current view of it. Raises when the account cannot serve."""
    binding = await account_secret(account)
    try:
        accounts = await codex_lb.list_accounts()
        if binding['lb_account_id'] not in accounts:
            accounts = await codex_lb.list_accounts(fresh=True)
    except CodexLBError:
        raise unavailable() from None
    summary = accounts.get(binding['lb_account_id'])
    catalog = None
    with contextlib.suppress(CodexLBError):
        # Cached for a few minutes: new models and their details arrive without opening Settings.
        catalog = await codex_lb.models(binding['lb_api_key'])
    if (await sync_account(account, summary, catalog)).status != 'ok':
        raise HTTPException(401, NEEDS_LOGIN)
    return binding['lb_api_key'], summary


async def verify(account, redis=None):
    """Read status, plan, models and limits again, past every cache."""
    binding = await account_secret(account)
    try:
        summary = (await codex_lb.list_accounts(fresh=True)).get(binding['lb_account_id'])
        catalog = await codex_lb.models(binding['lb_api_key'], fresh=True)
    except CodexLBError:
        raise unavailable() from None
    account = await sync_account(await ProviderAccounts.get(account.id) or account, summary, catalog)
    if account.status != 'ok':
        raise HTTPException(401, NEEDS_LOGIN)
    await record_summary(redis, account.id, summary)
    return account


async def sync_accounts(redis=None):
    """Bring every ChatGPT account up to date from codex-lb's cached view, so new models appear."""
    accounts = [account for account in await ProviderAccounts.list() if account.provider == 'codex']
    if not accounts:
        return
    bindings = {}
    try:
        summaries = await codex_lb.list_accounts()
        for account in accounts:
            with contextlib.suppress(HTTPException):
                bindings[account.id] = await account_secret(account)
        if any(binding['lb_account_id'] not in summaries for binding in bindings.values()):
            summaries = await codex_lb.list_accounts(fresh=True)
        # Any account's key reads the same catalog.
        catalog = await codex_lb.models(next(iter(bindings.values()))['lb_api_key']) if bindings else None
    except CodexLBError:
        log.debug('codex-lb is unavailable; ChatGPT accounts keep their last known state.')
        return
    for account in accounts:
        if binding := bindings.get(account.id):
            summary = summaries.get(binding['lb_account_id'])
            await sync_account(account, summary, catalog)
            await record_summary(redis, account.id, summary)


async def _get_login(identifier):
    async with get_async_db_context() as db:
        return await db.get(ProviderLogin, identifier)


async def _owned_login(identifier, user):
    attempt = await _get_login(identifier)
    if not attempt or attempt.owner_user_id != user.id:
        raise HTTPException(404, 'Connection attempt not found.')
    return attempt


async def _pending_logins(now):
    async with get_async_db_context() as db:
        result = await db.execute(
            select(ProviderLogin).where(ProviderLogin.status == 'pending', ProviderLogin.expires_at > now)
        )
        return list(result.scalars())


async def _update_login(identifier, **values):
    async with get_async_db_context() as db:
        attempt = await db.get(ProviderLogin, identifier)
        if attempt:
            for key, value in values.items():
                setattr(attempt, key, value)
            attempt.updated_at = int(time.time())
            await db.commit()


async def _finish(identifier, status, error=None, account_id=None):
    """End a pending attempt once. Flow details are dropped; only the method stays."""
    async with get_async_db_context() as db:
        attempt = await db.get(ProviderLogin, identifier)
        if attempt and attempt.status == 'pending':
            method = open_secret(attempt.secret).get('method')
            attempt.status, attempt.error, attempt.account_id = status, error, account_id
            attempt.secret = seal_secret({'method': method})
            attempt.updated_at = int(time.time())
            await db.commit()


def _flow_details(method, started):
    """Only OpenAI's own pages, returning to codex-lb's fixed callback, are shown to members."""
    flow_id = started.get('flowId')
    if not isinstance(flow_id, str) or not flow_id:
        raise ValueError('Missing flow.')
    if method == 'link':
        url = started.get('authorizationUrl') or ''
        auth = urlparse(url)
        query = parse_qs(auth.query)
        if (
            started.get('method') != 'browser'
            or auth.scheme != 'https'
            or auth.hostname != 'auth.openai.com'
            or query.get('redirect_uri') != [codex_lb.CALLBACK_URL]
            or not query.get('state')
        ):
            raise ValueError('Unexpected authorization URL.')
        return {'method': 'link', 'flowId': flow_id, 'authUrl': url, 'state': query['state'][0]}
    verification = started.get('verificationUrl') or ''
    parsed = urlparse(verification)
    if (
        started.get('method') != 'device'
        or parsed.scheme != 'https'
        or parsed.hostname not in ('auth.openai.com', 'chatgpt.com')
        or not started.get('userCode')
    ):
        raise ValueError('Unexpected device authorization.')
    return {
        'method': 'device_code',
        'flowId': flow_id,
        'verificationUrl': verification,
        'userCode': started['userCode'],
    }


async def start_login(user, sharing, redis=None, method='device_code'):
    async with _login_lock:
        now = int(time.time())
        for attempt in await _pending_logins(now):
            # A sign-in nobody is watching may have finished; settle it before turning others away.
            with contextlib.suppress(CodexLBError, HTTPException):
                await _settle(attempt.id, redis)
        pending = await _pending_logins(now)
        if own := next((attempt for attempt in pending if attempt.owner_user_id == user.id), None):
            return own.id
        # codex-lb runs one device flow at a time, and finding the new account relies on it.
        if pending:
            raise HTTPException(409, BUSY)
        identifier = str(uuid.uuid4())
        # The account's generated name, reserved while the sign-in is pending.
        label = await ProviderAccounts.unique_label(include_logins=True)
        async with get_async_db_context() as db:
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
        # Workers do not share the lock: of attempts saved at the same moment, only the first goes on.
        first = min(await _pending_logins(now), key=lambda attempt: (attempt.created_at, attempt.id))
        if first.id != identifier:
            await _delete_login(identifier)
            raise HTTPException(409, BUSY)
        try:
            # Signing in again to an existing codex-lb account updates it in place; this tells them apart.
            known = {
                account_id: summary.get('lastRefreshAt')
                for account_id, summary in (await codex_lb.list_accounts(fresh=True)).items()
            }
            started = await codex_lb.start_oauth('browser' if method == 'link' else 'device')
            details = _flow_details(method, started)
        except (CodexLBError, ValueError):
            await _delete_login(identifier)
            raise unavailable() from None
        values = {'secret': seal_secret(details | {'known': known})}
        lifetime = started.get('expiresInSeconds')
        if isinstance(lifetime, int) and 0 < lifetime < LOGIN_TTL:
            values['expires_at'] = now + lifetime
        await _update_login(identifier, **values)
    return identifier


async def _delete_login(identifier):
    async with get_async_db_context() as db:
        await db.execute(delete(ProviderLogin).where(ProviderLogin.id == identifier))
        await db.commit()


async def _settle(identifier, redis=None):
    """Follow codex-lb's flow. The caller holds the login lock; codex-lb errors propagate."""
    attempt = await _get_login(identifier)
    if attempt is None or attempt.status != 'pending':
        return
    if attempt.expires_at < time.time():
        await _finish(identifier, 'error', EXPIRED)
        return
    flow_id = open_secret(attempt.secret).get('flowId')
    if not flow_id:
        return
    status = await codex_lb.oauth_status(flow_id)
    if status == 'success':
        await _finalize(attempt, redis)
    elif status == 'error':
        await _finish(identifier, 'error', NOT_COMPLETED)


async def _finalize(attempt, redis=None):
    """codex-lb saved the sign-in: map the account it created, or refreshed, to this member."""
    known = open_secret(attempt.secret).get('known') or {}
    accounts = await codex_lb.list_accounts(fresh=True)
    mapped = {
        account.external_account_id: account for account in await ProviderAccounts.list() if account.provider == 'codex'
    }
    found = [account_id for account_id in accounts if account_id not in known and account_id not in mapped]
    if not found:
        found = [
            account_id
            for account_id in accounts
            if account_id in known and accounts[account_id].get('lastRefreshAt') != known[account_id]
        ]
    if len(found) != 1:
        await _finish(attempt.id, 'error', 'Mologate could not tell which ChatGPT account signed in. Try again.')
        return
    lb_id = found[0]
    summary = accounts[lb_id]
    # A team sign-in (an admin's) creates an account that belongs to no member and serves everyone.
    team = attempt.sharing == TEAM
    owner = None if team else attempt.owner_user_id
    if existing := mapped.get(lb_id):
        if existing.owner_user_id != owner:
            await _finish(attempt.id, 'error', ALREADY_CONNECTED)
            return
        values = {'status': 'ok', 'last_error': None}
        if summary.get('planType'):
            values['plan'] = summary['planType']
        updated = await ProviderAccounts.update(existing.id, values)
        await record_summary(redis, existing.id, summary)
        # Mologate: a plan read only now still earns the account its connect credits, once (phase 5).
        from open_webui.utils import credits

        await credits.ensure_grant(updated or existing)
        await _finish(attempt.id, 'completed', account_id=existing.id)
        log.info('ChatGPT account %s reconnected.', existing.id)
        return
    key = await codex_lb.create_key('mologate-' + attempt.id, lb_id)
    try:
        # The catalog is read with the new key, the way every request will reach codex-lb.
        catalog = await codex_lb.models(key['key'])
        if not catalog:
            raise CodexLBError(502, 'no_models')
    except CodexLBError:
        await _forget_key(key['id'])
        raise
    try:
        account = await ProviderAccounts.insert(
            owner_user_id=owner,
            provider='codex',
            plan=summary.get('planType') or 'unknown',
            external_account_id=lb_id,
            label=attempt.label,
            sharing='shared' if team else attempt.sharing,
            capacity_weight=1,
            budget=None,
            status='ok',
            models=catalog,
            secret={'lb_account_id': lb_id, 'lb_api_key': key['key'], 'lb_api_key_id': key['id']},
        )
    except IntegrityError:
        await _forget_key(key['id'])
        await _finish(attempt.id, 'error', ALREADY_CONNECTED)
        return
    await _best_effort(codex_lb.set_alias(lb_id, account.label))
    await record_summary(redis, account.id, summary)
    # Mologate: credits for adding the account to the pool, once ever per ChatGPT account (phase 5).
    from open_webui.utils import credits

    await credits.ensure_grant(account)
    await _finish(attempt.id, 'completed', account_id=account.id)
    log.info('ChatGPT account %s connected by user %s.', account.id, attempt.owner_user_id)


def _login_view(attempt):
    secret = open_secret(attempt.secret)
    pending = attempt.status == 'pending'
    return {
        'id': attempt.id,
        'status': attempt.status,
        'account_id': attempt.account_id,
        'expires_at': attempt.expires_at,
        'error': attempt.error,
        'method': secret.get('method', 'device_code'),
        'user_code': secret.get('userCode') if pending else None,
        'verification_url': secret.get('verificationUrl') if pending else None,
        'auth_url': secret.get('authUrl') if pending else None,
    }


async def login_status(identifier, user, redis=None):
    attempt = await _owned_login(identifier, user)
    if attempt.status == 'pending':
        async with _login_lock:
            try:
                await _settle(identifier, redis)
            except CodexLBError:
                raise unavailable() from None
        attempt = await _owned_login(identifier, user)
    return _login_view(attempt)


def callback_params(value: str) -> tuple[str, str]:
    value = value.strip()
    query = parse_qs(urlparse(value).query if '?' in value else value.lstrip('?'))
    code, state = (query.get('code') or [''])[0], (query.get('state') or [''])[0]
    if query.get('error') and not code:
        raise HTTPException(400, 'OpenAI did not accept this sign-in. Start a new connection and try again.')
    if not code or not state:
        raise HTTPException(
            400,
            'Paste the full address your browser opened after signing in. '
            'It starts with http://localhost:1455/auth/callback.',
        )
    return code, state


async def submit_callback(identifier, user, value: str, redis=None):
    attempt = await _owned_login(identifier, user)
    secret = open_secret(attempt.secret)
    if attempt.status != 'pending' or attempt.expires_at < time.time() or secret.get('method') != 'link':
        raise HTTPException(409, 'This sign-in is no longer waiting. Start a new connection.')
    if not secret.get('state') or not secret.get('flowId'):
        raise HTTPException(409, 'The sign-in link is still being prepared. Try again in a moment.')
    code, state = callback_params(value)
    if not hmac.compare_digest(state.encode(), secret['state'].encode()):
        raise HTTPException(400, 'This address belongs to a different sign-in. Use the address from the latest link.')
    # codex-lb holds the PKCE verifier. Only the code and state travel on, never the pasted host.
    callback = codex_lb.CALLBACK_URL + '?' + urlencode({'code': code, 'state': state})
    try:
        result = await codex_lb.manual_callback(callback, secret['flowId'])
        async with _login_lock:
            if result.get('status') == 'success':
                await _settle(identifier, redis)
            else:
                await _finish(identifier, 'error', 'OpenAI did not accept this sign-in. Start a new connection.')
    except CodexLBError:
        raise unavailable() from None
    return await login_status(identifier, user, redis)


async def cancel_login(identifier, user):
    await _owned_login(identifier, user)
    # codex-lb lets its own flow expire.
    await _finish(identifier, 'error', 'Sign-in cancelled.')
    return True


async def manual_reset(account, triggered_by: str, redis=None):
    """Explicitly redeem a coupon on this account only, never during inference."""
    if account.provider != 'codex':
        raise HTTPException(400, 'Only ChatGPT accounts support limit resets.')
    async with _reset_lock:
        # Re-read past the cache: a concurrent manual request may already have refilled it.
        try:
            lb_id = (await account_secret(account))['lb_account_id']
            summary = (await codex_lb.list_accounts(fresh=True)).get(lb_id)
        except (HTTPException, CodexLBError):
            return None
        if summary is None or summary.get('status') in ('paused', *codex_lb.REAUTH_STATUSES):
            return None
        await record_summary(redis, account.id, summary)
        if not codex_lb.exhausted(summary):
            return account
        try:
            coupons = codex_lb.available_credits(await codex_lb.reset_credits(lb_id))
        except CodexLBError:
            return None
        # Retain the conservative cross-worker cooldown and single-send audit trail.
        if not coupons or await LimitResetEvents.recent('codex', RESET_COOLDOWN):
            return None
        credit = coupons[0]
        event = await LimitResetEvents.start(account, triggered_by, credit['id'])
        outcome = 'error'
        try:
            # Sent once: a redemption that may have reached OpenAI is never repeated.
            result = await codex_lb.consume_reset_credit(lb_id, event.idempotency_key)
            outcome = RESET_OUTCOMES.get(result.get('code'), 'error')
        except CodexLBError as error:
            if error.code in RESET_REFUSALS:
                outcome = 'refused'
        finally:
            await LimitResetEvents.finish(event.id, outcome)
        log.info('Manual limit reset on account %s: %s.', account.id, outcome)
        if outcome not in ('reset', 'nothingToReset'):
            return None
        try:
            summary = (await codex_lb.list_accounts(fresh=True)).get(lb_id)
        except CodexLBError:
            return None
        await record_summary(redis, account.id, summary)
        return None if summary is None or codex_lb.exhausted(summary) else account


async def disconnect(account):
    """Revoke the key and remove the account from codex-lb. Never blocks local deletion."""
    secret = open_secret(account.secret)
    if secret.get('home_id'):
        with contextlib.suppress(ValueError):
            shutil.rmtree(ACCOUNT_ROOT / str(uuid.UUID(secret['home_id'])), ignore_errors=True)
    if secret.get('lb_api_key_id'):
        await _forget_key(secret['lb_api_key_id'])
    if secret.get('lb_account_id'):
        try:
            await codex_lb.delete_account(secret['lb_account_id'])
        except CodexLBError:
            log.warning('Could not remove account %s from codex-lb; its key no longer reaches it.', account.id)


async def delete_user_accounts(user_id):
    """Revoke credentials and pending logins when the owner is deleted."""
    async with get_async_db_context() as db:
        accounts = list(
            (await db.execute(select(ProviderAccount).where(ProviderAccount.owner_user_id == user_id))).scalars()
        )
        await db.execute(delete(ProviderLogin).where(ProviderLogin.owner_user_id == user_id))
        await db.execute(delete(ProviderAccount).where(ProviderAccount.owner_user_id == user_id))
        await db.commit()
    for account in accounts:
        if account.provider == 'codex':
            await disconnect(account)


async def shutdown():
    await codex_lb.close()
