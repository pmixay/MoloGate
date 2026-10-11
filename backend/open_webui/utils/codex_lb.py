"""Client for the internal codex-lb service that holds Mologate's ChatGPT accounts.

codex-lb keeps the OAuth tokens, refreshes them, reads limits and reset coupons, and sends
inference upstream as the official Codex client. Only Mologate reaches it, on a private network
with its dashboard login disabled. Request and response bodies, keys and tokens are never logged.
"""

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

from open_webui.utils.provider_limits import window_exhausted

log = logging.getLogger(__name__)

BASE_URL = os.environ.get('MOLOGATE_CODEX_LB_URL', 'http://codex-lb:2455').rstrip('/')
# codex-lb's fixed OAuth redirect. Members paste it back; it never loads in their browser.
CALLBACK_URL = 'http://localhost:1455/auth/callback'
ACCOUNTS_TTL = 5
MODELS_TTL = 5 * 60
SETTINGS_TTL = 10 * 60
# Without key auth codex-lb routes any request over the whole pool. v1.24.0 accepts retention
# overrides of 0 (keep forever) or at least 30 days of request logs and 45 days of usage history.
SETTINGS = {
    'apiKeyAuthEnabled': True,
    'requestLogRetentionOverrideDays': 30,
    'usageHistoryRetentionOverrideDays': 45,
}
EXHAUSTED_STATUSES = ('rate_limited', 'quota_exceeded')
REAUTH_STATUSES = ('reauth_required', 'deactivated')
# The proxy answers 503 with these codes when the key's account cannot take the request.
UNAVAILABLE_CODES = (
    'no_accounts',
    'quota_exhausted',
    'no_plan_support_for_model',
    'additional_quota_data_unavailable',
    'no_additional_quota_eligible_accounts',
)

_session: aiohttp.ClientSession | None = None
_session_loop = None
_accounts_cache: tuple[float, dict] | None = None
_models_cache: tuple[float, list] | None = None
_settings_checked: float | None = None


class CodexLBError(Exception):
    """codex-lb was unreachable (status 0) or refused a call. Never carries a response body."""

    def __init__(self, status: int = 0, code: str | None = None):
        super().__init__(f'codex-lb request failed ({status or "unreachable"}, {code or "no code"})')
        self.status = status
        self.code = code


def get_session() -> aiohttp.ClientSession:
    """One pooled session per event loop. Proxy variables are ignored: keys stay on the private network."""
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        _session = aiohttp.ClientSession(trust_env=False)
        _session_loop = loop
    return _session


async def close():
    global _session
    if _session is not None and not _session.closed and _session_loop is asyncio.get_running_loop():
        await _session.close()
    _session = None


async def request(method: str, path: str, *, timeout: float = 15, **kwargs):
    try:
        async with get_session().request(
            method,
            BASE_URL + path,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
            **kwargs,
        ) as response:
            body = await response.read()
            try:
                data = json.loads(body) if body else None
            except ValueError:
                data = None
            if response.status >= 300:
                error = data.get('error') if isinstance(data, dict) else None
                code = error.get('code') if isinstance(error, dict) else None
                raise CodexLBError(response.status, code if isinstance(code, str) else None)
            return data
    except (aiohttp.ClientError, TimeoutError):
        raise CodexLBError() from None


def _path(value: str) -> str:
    return quote(str(value), safe='')


def _settings_applied(current) -> bool:
    return isinstance(current, dict) and all(current.get(key) == value for key, value in SETTINGS.items())


async def ensure_settings():
    """Key auth on and bounded retention, checked again every few minutes. Raises CodexLBError otherwise."""
    global _settings_checked
    if _settings_checked is not None and time.monotonic() - _settings_checked < SETTINGS_TTL:
        return
    current = await request('GET', '/api/settings')
    if not _settings_applied(current):
        for attempt in range(2):
            try:
                current = await request('PUT', '/api/settings', json=SETTINGS)
                break
            except CodexLBError as error:
                # Another writer saved first; apply the change once more on top of it.
                if error.status != 409 or attempt:
                    raise
        if not isinstance(current, dict) or current.get('apiKeyAuthEnabled') is not True:
            raise CodexLBError(502, 'api_key_auth_disabled')
        log.info('codex-lb key authentication and retention configured.')
    _settings_checked = time.monotonic()


async def list_accounts(fresh: bool = False) -> dict[str, dict]:
    """Account summaries by codex-lb account id, at most a few seconds old unless fresh."""
    global _accounts_cache
    now = time.monotonic()
    if not fresh and _accounts_cache is not None and now - _accounts_cache[0] < ACCOUNTS_TTL:
        return _accounts_cache[1]
    data = await request('GET', '/api/accounts')
    accounts = {
        item['accountId']: item
        for item in (data or {}).get('accounts') or []
        if isinstance(item, dict) and isinstance(item.get('accountId'), str) and item['accountId']
    }
    _accounts_cache = (now, accounts)
    return accounts


async def models(fresh: bool = False) -> list[dict]:
    """The Codex catalog codex-lb syncs from OpenAI; models of extra sources are left out."""
    global _models_cache
    now = time.monotonic()
    if not fresh and _models_cache is not None and now - _models_cache[0] < MODELS_TTL:
        return _models_cache[1]
    data = await request('GET', '/api/models')
    result = [
        {'id': item['id'], 'name': item.get('name') or item['id']}
        for item in (data or {}).get('models') or []
        if isinstance(item, dict) and isinstance(item.get('id'), str) and not item.get('sourceOnly')
    ]
    if result:
        _models_cache = (now, result)
    return result


async def import_auth(raw: bytes) -> dict:
    form = aiohttp.FormData()
    form.add_field('auth_json', raw, filename='auth.json', content_type='application/json')
    result = await request('POST', '/api/accounts/import', data=form, timeout=60)
    if not isinstance(result, dict) or not isinstance(result.get('accountId'), str) or not result['accountId']:
        raise CodexLBError(502, 'invalid_import_response')
    return result


async def create_key(name: str, account_id: str) -> dict:
    """A key that routes to this one account only. codex-lb treats an empty assignment as the whole pool."""
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError('A codex-lb key must be assigned to exactly one account.')
    result = await request('POST', '/api/api-keys/', json={'name': name[:128], 'assignedAccountIds': [account_id]})
    if not isinstance(result, dict) or not result.get('id'):
        raise CodexLBError(502, 'invalid_key_response')
    if (
        not isinstance(result.get('key'), str)
        or not result['key']
        or result.get('assignedAccountIds') != [account_id]
        or result.get('accountAssignmentScopeEnabled') is False
    ):
        # Never keep a key that could reach other members' accounts.
        await delete_key(result['id'])
        raise CodexLBError(502, 'key_not_scoped')
    return result


async def delete_key(key_id: str):
    try:
        await request('DELETE', f'/api/api-keys/{_path(key_id)}')
    except CodexLBError as error:
        if error.status != 404:
            raise


async def delete_account(account_id: str):
    try:
        await request('DELETE', f'/api/accounts/{_path(account_id)}', params={'delete_history': 'true'})
    except CodexLBError as error:
        if error.status != 404:
            raise


async def set_alias(account_id: str, alias: str):
    await request('PUT', f'/api/accounts/{_path(account_id)}/alias', json={'alias': alias[:255]})


async def start_oauth(method: str) -> dict:
    # Without an explicit method codex-lb skips the flow once any account exists.
    result = await request('POST', '/api/oauth/start', json={'forceMethod': method}, timeout=30)
    if not isinstance(result, dict):
        raise CodexLBError(502, 'invalid_oauth_response')
    return result


async def oauth_status(flow_id: str) -> str:
    result = await request('GET', '/api/oauth/status', params={'flowId': flow_id})
    return (result or {}).get('status') or 'pending'


async def manual_callback(callback_url: str, flow_id: str) -> dict:
    result = await request(
        'POST', '/api/oauth/manual-callback', json={'callbackUrl': callback_url, 'flowId': flow_id}, timeout=60
    )
    return result if isinstance(result, dict) else {}


async def reset_credits(account_id: str) -> dict | None:
    """codex-lb's latest coupon listing for the account, or None when it has none it could redeem."""
    result = await request('GET', f'/api/accounts/{_path(account_id)}/rate-limit-reset-credits')
    return result if isinstance(result, dict) else None


async def consume_reset_credit(account_id: str, redeem_request_id: str) -> dict:
    """Redeem the soonest-expiring coupon. Not idempotent upstream, so callers send it once."""
    result = await request(
        'POST',
        f'/api/accounts/{_path(account_id)}/rate-limit-reset-credits/consume',
        json={'redeemRequestId': redeem_request_id},
        timeout=90,
    )
    return result if isinstance(result, dict) else {}


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def epoch(value) -> int | None:
    """codex-lb sends ISO 8601 times; numbers are accepted as epoch seconds or milliseconds."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())
    number = _number(value)
    if number is None:
        return None
    return int(number / 1000 if number > 1e12 else number)


def windows(summary: dict) -> list[dict]:
    """The 5-hour (primary) and weekly (secondary) windows in Mologate's snapshot shape."""
    usage = summary.get('usage') or {}
    result = []
    for name in ('primary', 'secondary'):
        remaining = _number(usage.get(f'{name}RemainingPercent'))
        if remaining is None:
            continue
        minutes = _number(summary.get(f'windowMinutes{name.title()}'))
        result.append(
            {
                'id': name,
                'used_percent': max(0.0, min(100.0, 100 - remaining)),
                'window_minutes': int(minutes) if minutes else None,
                'resets_at': epoch(summary.get(f'resetAt{name.title()}')),
            }
        )
    return result


def snapshot(summary: dict) -> dict:
    credits = _number(summary.get('availableResetCredits'))
    return {'windows': windows(summary), 'reset_credits': int(credits) if credits is not None else None}


def exhausted(summary: dict, now: float | None = None) -> bool:
    """Empty when codex-lb says so, or a window is used up and has not reset yet."""
    if summary.get('status') in EXHAUSTED_STATUSES:
        return True
    now = time.time() if now is None else now
    return any(window_exhausted(window, now) for window in windows(summary))


def available_credits(listing: dict | None, now: float | None = None) -> list[dict]:
    """Unexpired coupons, soonest expiry first; a coupon without an expiry sorts last."""
    if not listing or (_number(listing.get('availableCount')) or 0) <= 0:
        return []
    now = time.time() if now is None else now
    credits = []
    for credit in listing.get('credits') or []:
        if not isinstance(credit, dict) or credit.get('status') != 'available':
            continue
        expires_at = epoch(credit.get('expiresAt'))
        if expires_at is None or expires_at > now:
            credits.append({'id': credit.get('id'), 'expires_at': expires_at})
    return sorted(credits, key=lambda credit: math.inf if credit['expires_at'] is None else credit['expires_at'])
