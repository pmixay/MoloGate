"""Verification, visibility and deterministic account selection for Phase 1."""

import hashlib

import aiohttp
from fastapi import HTTPException

from open_webui.models.config import Config
from open_webui.models.provider_accounts import ProviderAccounts, open_secret
from open_webui.utils.anthropic import ANTHROPIC_VERSION

API_BASES = {
    'openai_api': 'https://api.openai.com/v1',
    'anthropic_api': 'https://api.anthropic.com/v1',
    'openrouter': 'https://openrouter.ai/api/v1',
    'zai': 'https://api.z.ai/api/paas/v4',
}
ZAI_CODING_BASE = 'https://api.z.ai/api/coding/paas/v4'
MODEL_PREFIX = 'account:'


async def require_accounts_enabled():
    if await Config.get('mologate.limits_mode', 'observe') == 'off':
        raise HTTPException(503, 'Provider accounts are disabled.')


def can_manage(account, user):
    return user.role == 'admin' or account.owner_user_id == user.id


def can_use(account, user):
    return (
        user is not None
        and user.role in ('user', 'admin')
        and account.status == 'ok'
        and account.sharing != 'paused'
        and (account.owner_user_id == user.id or account.sharing == 'shared')
    )


def model_id(provider: str, upstream_id: str):
    return f'{MODEL_PREFIX}{provider}:{upstream_id}'


def split_model_id(value: str):
    parts = value.split(':', 2)
    if len(parts) != 3 or parts[0] != 'account' or not parts[2]:
        raise HTTPException(404, 'Model not found.')
    return parts[1], parts[2]


def choose_account(accounts, provider, upstream_id, user):
    candidates = [
        account
        for account in accounts
        if account.provider == provider
        and can_use(account, user)
        and any(model['id'] == upstream_id for model in account.models)
    ]
    if not candidates:
        raise HTTPException(404, 'No connected account is available for this model.')
    # Phase 1 selects credentials, without applying the later quota/credit rules.
    return min(candidates, key=lambda account: (account.owner_user_id != user.id, account.created_at, account.id))


async def select_account(value: str, user):
    await require_accounts_enabled()
    provider, upstream_id = split_model_id(value)
    return choose_account(await ProviderAccounts.list(), provider, upstream_id, user)


async def account_models(user=None):
    if await Config.get('mologate.limits_mode', 'observe') == 'off':
        return []
    catalog = {}
    for account in await ProviderAccounts.list():
        if account.status != 'ok' or account.sharing == 'paused':
            continue
        if user is not None and not can_use(account, user):
            continue
        for model in account.models:
            value = model_id(account.provider, model['id'])
            # Visibility belongs to the pool, not whichever account is listed last.
            shared = account.sharing == 'shared' or catalog.get(value, {}).get('provider_account_sharing') == 'shared'
            catalog[value] = {
                'id': value,
                'name': model.get('name') or model['id'],
                'object': 'model',
                'created': 0,
                'owned_by': 'openai',
                'provider_account_provider': account.provider,
                'provider_account_sharing': 'shared' if shared else 'private',
                'upstream_model_id': model['id'],
            }
    return list(catalog.values())


def key_fingerprint(key: str):
    return hashlib.sha256(key.encode()).hexdigest()


async def verify_api_key(provider: str, key: str, plan: str = 'api'):
    """Only fixed provider origins receive keys. No redirects or caller-controlled URLs."""
    if not key or len(key) > 16384 or any(character.isspace() for character in key):
        raise HTTPException(400, 'Enter a valid API key.')
    base = ZAI_CODING_BASE if provider == 'zai' and plan != 'api' else API_BASES[provider]
    headers = {'Authorization': f'Bearer {key}'}
    if provider == 'anthropic_api':
        headers = {'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION}
    models = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            # /models is public on OpenRouter; it cannot verify a credential.
            if provider == 'openrouter':
                async with session.get(base + '/key', headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        raise HTTPException(400, f'Provider verification failed (HTTP {response.status}).')
                    key_info = (await response.json()).get('data') or {}
                    if key_info.get('is_management_key') or key_info.get('is_provisioning_key'):
                        raise HTTPException(400, 'Use an inference API key, not a management key.')
            params = {}
            for _ in range(20):
                async with session.get(
                    base + '/models', headers=headers, params=params, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        raise HTTPException(400, f'Provider verification failed (HTTP {response.status}).')
                    data = await response.json()
                for item in data.get('data', []):
                    if isinstance(item, dict) and isinstance(item.get('id'), str):
                        if not any(word in item['id'].lower() for word in ('embedding', 'whisper', 'tts', 'dall-e')):
                            models.append({'id': item['id'], 'name': item.get('display_name') or item['id']})
                if provider != 'anthropic_api' or not data.get('has_more') or not data.get('last_id'):
                    break
                params = {'after_id': data['last_id']}
        except HTTPException:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise HTTPException(502, 'The provider could not be verified. Check the key and try again.') from None
    if not models:
        raise HTTPException(400, 'The provider returned no chat models for this key.')
    return {'api_key': key, 'base_url': base}, list({item['id']: item for item in models}.values())


async def refresh_zai_limits(account, redis):
    """Best-effort observation; missing quota data never blocks an otherwise valid key."""
    if account.provider != 'zai' or account.plan == 'api':
        return
    from open_webui.utils.provider_limits import parse_zai_quota, record_provider_snapshot

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                'https://api.z.ai/api/monitor/usage/quota/limit',
                headers={'Authorization': 'Bearer ' + open_secret(account.secret)['api_key']},
                allow_redirects=False,
            ) as response:
                if response.status == 200:
                    windows = parse_zai_quota(await response.json())
                    if windows:
                        await record_provider_snapshot(redis, account.id, windows)
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
        pass
