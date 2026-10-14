"""Mologate: a member's personal API keys (docs/limits-and-accounts.md §11, phase 6).

Several named keys per member, each with optional limits that never go beyond its owner's. A key is
shown once, when it is made, and stored only as a hash. Revoking a key keeps its record, so the
ledger's rows still name it. Keys themselves cannot reach these routes (utils/api_key_limits.py).
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from open_webui.models.api_keys import PersonalKeys, view
from open_webui.models.config import Config
from open_webui.models.usage_events import UsageEvents
from open_webui.utils import api_key_limits
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)
router = APIRouter()

MAX_KEYS = 20


class KeyLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rpm: int | None = Field(default=None, ge=1, le=api_key_limits.MAX_RPM)
    concurrency: int | None = Field(default=None, ge=1, le=100)
    models: list[str] | None = Field(default=None, max_length=200)
    efforts: list[str] | None = None
    daily_points: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False)


class KeyForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    limits: KeyLimits = KeyLimits()
    # Days from now; empty: until revoked.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyUpdateForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=80)
    limits: KeyLimits | None = None


async def allowed_user(user=Depends(get_verified_user)):
    if not await Config.get('auth.enable_api_keys') or (
        user.role != 'admin'
        and not await has_permission(user.id, 'features.api_keys', await Config.get('user.permissions'))
    ):
        raise HTTPException(403, 'API keys are not turned on for your account.')
    return user


async def key_views(user) -> list[dict]:
    """The member's keys with their use: today and the last 7 days, and today's pool points."""
    from open_webui.models.provider_accounts import ProviderAccounts
    from open_webui.utils.provider_limits import period_start

    rows = await PersonalKeys.list(user.id)
    ids = [row.id for row in rows]
    now = time.time()
    today = await UsageEvents.key_usage(ids, period_start('daily', now))
    week = await UsageEvents.key_usage(ids, int(now - 7 * 86400))
    providers = sorted({account.provider for account in await ProviderAccounts.list()})
    result = []
    for row in rows:
        item = view(row)
        points = {}
        if today.get(row.id):
            for provider in providers:
                used = await api_key_limits.points_today(row.id, user.id, provider, now)
                if used:
                    points[provider] = round(used, 2)
        item['usage'] = {'today': today.get(row.id), 'week': week.get(row.id), 'points_today': points}
        result.append(item)
    return result


@router.get('')
async def list_keys(user=Depends(allowed_user)):
    from open_webui.utils.chat_slots import chat_limit

    return {
        'data': await key_views(user),
        # What a key's limits can be: never beyond its owner.
        'bounds': {
            'concurrency': await chat_limit(),
            'rpm': api_key_limits.MAX_RPM,
            'efforts': list(api_key_limits.EFFORTS),
            'endpoints': sorted(api_key_limits.KEY_ENDPOINTS),
        },
    }


@router.post('')
async def create_key(form: KeyForm, user=Depends(allowed_user)):
    rows = await PersonalKeys.list(user.id)
    if sum(1 for row in rows if not (row.data or {}).get('revoked_at')) >= MAX_KEYS:
        raise HTTPException(400, f'You can have up to {MAX_KEYS} keys. Revoke one first.')
    limits = await api_key_limits.clean_limits(form.limits.model_dump(exclude_none=True), user)
    expires_at = int(time.time()) + form.expires_in_days * 86400 if form.expires_in_days else None
    item, key = await PersonalKeys.create(user.id, form.name.strip(), limits, expires_at)
    log.info('API key %s made by user %s.', item['id'], user.id)
    # The only time the key itself is returned.
    return {'key': item, 'secret': key}


@router.patch('/{key_id}')
async def update_key(key_id: str, form: KeyUpdateForm, user=Depends(allowed_user)):
    values = {}
    if form.name is not None:
        values['name'] = form.name.strip()
    if form.limits is not None:
        values['limits'] = await api_key_limits.clean_limits(form.limits.model_dump(exclude_none=True), user)
    row = await PersonalKeys.update(user.id, key_id, values)
    if row is None:
        raise HTTPException(404, 'Key not found.')
    return view(row)


@router.delete('/{key_id}')
async def revoke_key(key_id: str, user=Depends(allowed_user)):
    row = await PersonalKeys.revoke(user.id, key_id)
    if row is None:
        raise HTTPException(404, 'Key not found.')
    log.info('API key %s revoked by user %s.', key_id, user.id)
    return view(row)
