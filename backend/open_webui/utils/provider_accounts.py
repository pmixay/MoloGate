"""Verification, visibility and deterministic account selection for Phase 1."""

import asyncio
import hashlib
import math
import re
import time

import aiohttp
from fastapi import HTTPException

from open_webui.models.config import Config
from open_webui.models.provider_accounts import ProviderAccounts, open_secret
from open_webui.utils.anthropic import ANTHROPIC_VERSION
from open_webui.utils.model_generations import is_current_generation

API_BASES = {
    'openai_api': 'https://api.openai.com/v1',
    'anthropic_api': 'https://api.anthropic.com/v1',
    'openrouter': 'https://openrouter.ai/api/v1',
    'zai': 'https://api.z.ai/api/paas/v4',
}
ZAI_CODING_BASE = 'https://api.z.ai/api/coding/paas/v4'
MODEL_PREFIX = 'account:'
# Mologate: what the effort selector, image check and context indicator read from a provider's
# /models entry (utils/reasoning_effort.py, image_input.py, context_window.py). Account models keep
# these, so they behave like models from a configured connection.
MODEL_DETAIL_KEYS = (
    'supported_reasoning_levels',
    'supported_reasoning_efforts',
    'reasoning_efforts',
    'default_reasoning_level',
    'default_reasoning_effort',
    'supported_parameters',
    'input_modalities',
    'architecture',
    'modalities',
    'supports_vision',
    'context_window',
    'context_length',
    'max_context_length',
    'max_model_len',
    'max_input_tokens',
    'input_token_limit',
    'inputTokenLimit',
    'top_provider',
)


# Mologate: models a key lists but chat cannot use (embeddings, speech, images, moderation, realtime).
UNUSABLE_MODEL_WORDS = (
    'embed',
    'whisper',
    'tts',
    'dall-e',
    'moderation',
    'realtime',
    'audio',
    'transcribe',
    'speech',
    'image',
    'video',
    'sora',
    'cogview',
    'cogvideo',
    'asr',
    'rerank',
    'computer-use',
)
# OpenAI's older generations and completion-only models.
OPENAI_OLD_MODELS = re.compile(
    r'^(gpt-3\.5|gpt-4($|-)|chatgpt-|babbage|davinci|text-|code-|o1-(mini|preview))|-instruct'
)
# OpenAI's dated copy of a model it also lists under its plain name, e.g. gpt-4o-2024-08-06.
# Other providers use date suffixes for newer releases (qwen3-...-2507), so this is OpenAI-only.
DATED_SNAPSHOT = re.compile(r'^(.+?)-(\d{4}-\d{2}-\d{2}|\d{8}|\d{4})$')
ZAI_QUOTA_URL = 'https://api.z.ai/api/monitor/usage/quota/limit'
OPENROUTER_KEY_URL = API_BASES['openrouter'] + '/key'
LIMITS_REFRESH_SECONDS = 60
_limits_refreshed: dict[str, float] = {}
_refresh_tasks: set = set()


async def require_accounts_enabled():
    if await Config.get('mologate.limits_mode', 'observe') == 'off':
        raise HTTPException(503, 'Provider accounts are disabled.')


# Mologate: how a team account (no owner) is asked for when connecting a ChatGPT account.
TEAM = 'team'


def is_team_account(account) -> bool:
    return account.owner_user_id is None


def can_manage(account, user):
    # Only admins manage a team account; it has no owner.
    return user.role == 'admin' or (account.owner_user_id is not None and account.owner_user_id == user.id)


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


def catalog_model(upstream_id: str, name, entry: dict) -> dict:
    """An account's model as stored: its id, name and the provider details features read."""
    model = {'id': upstream_id, 'name': name if isinstance(name, str) and name else upstream_id}
    details = {key: entry[key] for key in MODEL_DETAIL_KEYS if entry.get(key) is not None}
    if details:
        model['details'] = details
    return model


def split_model_id(value: str):
    parts = value.split(':', 2)
    if len(parts) != 3 or parts[0] != 'account' or not parts[2]:
        raise HTTPException(404, 'Model not found.')
    return parts[1], parts[2]


def eligible_accounts(accounts, provider, upstream_id, user):
    candidates = [
        account
        for account in accounts
        if account.provider == provider
        and can_use(account, user)
        and any(model['id'] == upstream_id for model in account.models)
    ]
    if not candidates:
        raise HTTPException(404, 'No connected account is available for this model.')
    # The requester's own accounts come first, then shared ones; quota rules come in later phases.
    return sorted(candidates, key=lambda account: (account.owner_user_id != user.id, account.created_at, account.id))


def choose_account(accounts, provider, upstream_id, user):
    return eligible_accounts(accounts, provider, upstream_id, user)[0]


async def select_accounts(value: str, user):
    await require_accounts_enabled()
    provider, upstream_id = split_model_id(value)
    return eligible_accounts(await ProviderAccounts.list(), provider, upstream_id, user)


async def select_account(value: str, user):
    return (await select_accounts(value, user))[0]


async def auto_reset_enabled() -> bool:
    """Any approved member can turn automatic coupon resets off for the whole team."""
    return await Config.get('mologate.auto_reset', True) is not False


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
            catalog[value] = {
                'id': value,
                'name': model.get('name') or model['id'],
                'object': 'model',
                'created': 0,
                'owned_by': 'openai',
                # Like a configured connection's model: the provider's entry under 'openai', so
                # effort levels, image input and the context window come from the provider.
                'openai': {**(model.get('details') or {}), 'id': model['id']},
                'connection_type': 'external',
                'provider_account_provider': account.provider,
                # Mologate: every account is shared with the team; paused ones are not listed.
                'provider_account_sharing': 'shared',
                'upstream_model_id': model['id'],
                # Older generations leave the selectors but still answer (utils/model_generations.py).
                **({} if is_current_generation(model['id']) else {'info': {'meta': {'hidden': True}}}),
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
                    if isinstance(item, dict) and isinstance(item.get('id'), str) and usable_model(provider, item):
                        models.append(catalog_model(item['id'], item.get('display_name') or item.get('name'), item))
                if provider != 'anthropic_api' or not data.get('has_more') or not data.get('last_id'):
                    break
                params = {'after_id': data['last_id']}
        except HTTPException:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise HTTPException(502, 'The provider could not be verified. Check the key and try again.') from None
    models = list({item['id']: item for item in models}.values())
    if provider == 'openai_api':
        models = without_dated_copies(models)
    if not models:
        raise HTTPException(400, 'The provider returned no chat models for this key.')
    return {'api_key': key, 'base_url': base}, models


def usable_model(provider: str, entry: dict) -> bool:
    """A model a member can chat with today: not a tool model, not retired, not an older generation."""
    model = entry['id'].lower()
    if any(word in model for word in UNUSABLE_MODEL_WORDS):
        return False
    if provider == 'openai_api' and OPENAI_OLD_MODELS.search(model):
        return False
    if provider == 'openrouter':
        architecture = entry.get('architecture') if isinstance(entry.get('architecture'), dict) else {}
        inputs = architecture.get('input_modalities')
        outputs = architecture.get('output_modalities')
        if isinstance(outputs, list) and outputs and 'text' not in outputs:
            return False
        if isinstance(inputs, list) and inputs and 'text' not in inputs:
            return False
        # OpenRouter dates the models it is retiring.
        if entry.get('expiration_date'):
            return False
    return True


def without_dated_copies(models: list[dict]) -> list[dict]:
    names = {model['id'] for model in models}
    return [model for model in models if not ((match := DATED_SNAPSHOT.match(model['id'])) and match.group(1) in names)]


def reports_limits(account) -> bool:
    """Z.ai coding plans and OpenRouter keys report limits only through their own endpoints."""
    return (account.provider == 'zai' and account.plan != 'api') or account.provider == 'openrouter'


async def refresh_account_limits(account, redis, max_age: float = 0):
    """Read a Z.ai coding plan's 5-hour and weekly quota, or an OpenRouter key's credit limit and
    this month's spend. Best effort: missing data never blocks an otherwise valid key, and unknown
    quota is never zero usage. With max_age, a reading that recent is kept."""
    if not reports_limits(account):
        return
    now = time.monotonic()
    if max_age and now - _limits_refreshed.get(account.id, -math.inf) < max_age:
        return
    _limits_refreshed[account.id] = now
    from open_webui.utils.provider_limits import parse_openrouter_key, parse_zai_quota, record_provider_snapshot

    key = (open_secret(account.secret) or {}).get('api_key')
    if not key:
        return
    if account.provider == 'openrouter':
        url, headers = OPENROUTER_KEY_URL, {'Authorization': f'Bearer {key}'}
    else:
        # Z.ai's monitor endpoints take the key itself, without "Bearer" (as its own usage plugin sends it).
        url, headers = ZAI_QUOTA_URL, {'Authorization': key, 'Accept-Language': 'en-US,en'}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    return
                result = await response.json()
        if account.provider == 'openrouter':
            windows = parse_openrouter_key(result.get('data') or {}, account.budget)
            if windows:
                await record_provider_snapshot(redis, account.id, windows, source='key')
        elif windows := parse_zai_quota(result):
            await record_provider_snapshot(redis, account.id, windows)
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
        pass


def refresh_limits_soon(account, redis):
    """Their answers carry no such limits: read them again in the background after use."""
    if not reports_limits(account):
        return
    task = asyncio.create_task(refresh_account_limits(account, redis, max_age=LIMITS_REFRESH_SECONDS))
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)
