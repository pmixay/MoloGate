"""Mologate: admin connections to a supported provider become team accounts.

Models then come from providers through accounts (account switching, limits per account, all use
counted as pool use) rather than from connection indices. A connection qualifies when it points at
a provider Mologate supports with a plain key: OpenAI, Anthropic, OpenRouter or Z.ai. Local
servers, Azure, and connections with custom headers or another sign-in stay plain connections.

An adopted connection leaves Admin -> Connections and appears in Provider accounts as a team
account (no owner; admins manage it). The model ids it served keep working: saved model settings,
presets, default and task models move to the new ids, and a request naming an old id is sent to
the new one. Adoption runs at startup and whenever an admin saves the connections.
"""

import asyncio
import logging

from fastapi import HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.access_grants import AccessGrant
from open_webui.models.config import Config
from open_webui.models.models import Model
from open_webui.models.provider_accounts import ProviderAccounts
from open_webui.utils.provider_accounts import catalog_model, key_fingerprint, model_id, verify_api_key
from sqlalchemy import update

log = logging.getLogger(__name__)

# Base URLs of supported providers. A Z.ai coding plan's tier is not reported: admins correct it.
PROVIDER_URLS = {
    'https://api.openai.com/v1': ('openai_api', 'api'),
    'https://api.anthropic.com/v1': ('anthropic_api', 'api'),
    'https://openrouter.ai/api/v1': ('openrouter', 'api'),
    'https://api.z.ai/api/paas/v4': ('zai', 'api'),
    'https://api.z.ai/api/coding/paas/v4': ('zai', 'lite'),
}
ALIASES_KEY = 'mologate.model_aliases'
# Settings that name one model, and settings that list models (comma-separated text or a list).
MODEL_KEYS = (
    'task.model.default',
    'task.model.external',
    'chat.context_compaction.model',
    'chat.tool_permissions.model',
    'chat.tool_permissions.fallback_model',
)
MODEL_LIST_KEYS = ('ui.default_models', 'ui.default_pinned_models', 'ui.model_order_list')

_lock = asyncio.Lock()


def adoptable(url: str, key: str, config: dict) -> tuple[str, str] | None:
    """The provider and plan a connection becomes, or None when it stays a connection."""
    if not key or not isinstance(url, str):
        return None
    target = PROVIDER_URLS.get(url.strip().rstrip('/'))
    if target is None:
        return None
    if config.get('azure') or config.get('provider') == 'azure' or config.get('api_type') == 'responses':
        return None
    if config.get('auth_type') not in (None, '', 'bearer') or config.get('headers'):
        return None
    return target


async def adopt_connections() -> int:
    """Turn qualifying admin connections into team accounts. Returns how many were adopted."""
    from open_webui.routers.openai import get_openai_runtime_config

    async with _lock:
        if await Config.get('mologate.limits_mode', 'observe') == 'off':
            return 0
        _, urls, keys, configs = await get_openai_runtime_config()
        aliases = dict(await Config.get(ALIASES_KEY, None) or {})
        accounts = await ProviderAccounts.list()
        adopted = []
        for index, url in enumerate(urls):
            key = keys[index] if index < len(keys) else ''
            config = configs.get(str(index), configs.get(url, {})) or {}
            target = adoptable(url, key, config)
            if target is None:
                continue
            provider, plan = target
            fingerprint = key_fingerprint(key)
            account = next(
                (item for item in accounts if item.provider == provider and item.external_account_id == fingerprint),
                None,
            )
            if account is not None and account.owner_user_id is not None:
                # A member connected the same key; their account keeps it and the connection stays.
                log.warning('Connection %s uses a key a member connected; it stays a connection.', index)
                continue
            if account is None:
                try:
                    secret, models = await verify_api_key(provider, key, plan)
                except HTTPException as error:
                    log.warning('Connection %s could not become a team account: %s', index, error.detail)
                    continue
                if chosen := config.get('model_ids'):
                    # The admin picked the connection's models; keep exactly those.
                    listed = {model['id']: model for model in models}
                    models = [listed.get(item) or catalog_model(item, item, {}) for item in chosen]
                account = await ProviderAccounts.insert(
                    owner_user_id=None,
                    provider=provider,
                    plan=plan,
                    external_account_id=fingerprint,
                    sharing='shared' if config.get('enable', True) else 'paused',
                    capacity_weight=1,
                    budget=None,
                    status='ok',
                    secret=secret,
                    models=models,
                )
                accounts.append(account)
            prefix = config.get('prefix_id')
            for model in account.models:
                aliases[f'{prefix}.{model["id"]}' if prefix else model['id']] = model_id(provider, model['id'])
            adopted.append(index)
        if not adopted:
            return 0
        kept = [index for index in range(len(urls)) if index not in adopted]
        await Config.upsert(
            {
                'openai.api_base_urls': [urls[index] for index in kept],
                'openai.api_keys': [keys[index] if index < len(keys) else '' for index in kept],
                # Per-connection settings are keyed by position (older ones by URL); renumber the rest.
                'openai.api_configs': {
                    **{urls[index]: configs[urls[index]] for index in kept if urls[index] in configs},
                    **{
                        str(position): configs[str(index)]
                        for position, index in enumerate(kept)
                        if str(index) in configs
                    },
                },
                ALIASES_KEY: aliases,
            }
        )
        await remap_model_references(aliases)
        log.info('Moved %d admin connection(s) into team accounts.', len(adopted))
        return len(adopted)


async def remap_model_references(aliases: dict):
    """Saved model settings, presets and model choices follow a model to its account id."""
    async with get_async_db_context() as db:
        for old, new in aliases.items():
            if await db.get(Model, new) is None and (row := await db.get(Model, old)) is not None:
                row.id = new
            await db.execute(update(Model).where(Model.base_model_id == old).values(base_model_id=new))
            await db.execute(
                update(AccessGrant)
                .where(AccessGrant.resource_type == 'model', AccessGrant.resource_id == old)
                .values(resource_id=new)
            )
        await db.commit()
    values = await Config.get_many(*MODEL_KEYS, *MODEL_LIST_KEYS)
    changes = {}
    for key in MODEL_KEYS:
        if isinstance(values.get(key), str) and values[key] in aliases:
            changes[key] = aliases[values[key]]
    for key in MODEL_LIST_KEYS:
        value = values.get(key)
        if isinstance(value, str) and value:
            mapped = ','.join(aliases.get(item.strip(), item.strip()) for item in value.split(','))
        elif isinstance(value, list):
            mapped = [aliases.get(item, item) if isinstance(item, str) else item for item in value]
        else:
            continue
        if mapped != value:
            changes[key] = mapped
    if changes:
        await Config.upsert(changes)


async def resolve_model_alias(value):
    """The account model id now serving an adopted connection's model id, else the id itself."""
    if not isinstance(value, str) or not value or value.startswith('account:'):
        return value
    aliases = await Config.get(ALIASES_KEY, None) or {}
    return aliases.get(value, value)
