"""Authenticated provider account management. Credentials are write-only."""

import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import IntegrityError

from open_webui.models.config import Config
from open_webui.models.provider_accounts import (
    AccountView,
    LimitResetEvents,
    ProviderAccounts,
    Sharing,
    open_secret,
    seal_secret,
)
from open_webui.models.users import Users
from open_webui.utils import allowance_gate, codex_accounts, credits, metering
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.provider_accounts import (
    TEAM,
    can_manage,
    key_fingerprint,
    require_accounts_enabled,
    verify_api_key,
)
from open_webui.utils.provider_accounts import LIMITS_REFRESH_SECONDS, refresh_account_limits
from open_webui.models.usage_events import UsageEvents
from open_webui.utils.provider_limits import budget_window, get_provider_limits, period_start, with_team_use
from open_webui.utils.rate_limit import RateLimiter


class CredentialRoute(APIRoute):
    """Validation must never echo a supplied credential or an entire request body."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, 'Check the account fields and try again.') from None

        return handle


log = logging.getLogger(__name__)
router = APIRouter(route_class=CredentialRoute, dependencies=[Depends(require_accounts_enabled)])
verification_limiter = RateLimiter(limit=10, window=300)


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    currency: Literal['USD'] = 'USD'


class ApiAccountForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['anthropic_api', 'openai_api', 'openrouter', 'zai']
    api_key: SecretStr
    plan: Literal['api', 'lite', 'pro', 'max'] = 'api'
    sharing: Sharing = 'shared'
    budget: Budget | None = None
    # Mologate: an admin's key that belongs to no member and serves the whole team.
    team: bool = False


class CodexAccountForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sharing: Sharing = 'shared'
    # A code on OpenAI's device page, or OpenAI's sign-in link with the callback address pasted back.
    method: Literal['device_code', 'link'] = 'device_code'
    team: bool = False


class CodexCallbackForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    callback_url: SecretStr = Field(max_length=8192)


class AccountUpdateForm(BaseModel):
    # Names are generated ('Account #' + 6 characters) and cannot be changed.
    model_config = ConfigDict(extra='forbid')
    sharing: Sharing | None = None
    budget: Budget | None = None
    capacity_weight: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)
    # Mologate: an admin corrects a Z.ai coding plan the member picked; other plans come from the provider.
    plan: Literal['lite', 'pro', 'max'] | None = None


class OverrideForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # Empty: the whole team.
    user_id: str | None = None
    extra_points: float = Field(gt=0, le=100, allow_inf_nan=False)
    until: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=500)


class MultipliersForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # {provider: {plan: multiplier}}
    multipliers: dict[str, dict[str, float]]


async def owned_account(account_id, user):
    account = await ProviderAccounts.get(account_id)
    if not account or not can_manage(account, user):
        raise HTTPException(404, 'Account not found.')
    return account


def require_team_admin(team: bool, user):
    if team and user.role != 'admin':
        raise HTTPException(403, 'Only an admin can connect a team account.')


async def check_rate(request, user):
    if await verification_limiter.is_limited(getattr(request.app.state, 'redis', None), 'provider-connect:' + user.id):
        raise HTTPException(429, 'Too many connection attempts. Try again in a few minutes.')


@router.get('/usage')
async def my_usage(user=Depends(get_verified_user)):
    """Mologate: the member's own metered use (docs/limits-and-accounts.md §8), read-only."""
    return await metering.member_summary(user.id)


@router.get('/metering')
async def metering_report(user=Depends(get_admin_user)):
    return await metering.report()


@router.post('/metering/run')
async def run_metering(request: Request, user=Depends(get_admin_user)):
    """Poll, measure and calibrate now instead of waiting for the next round."""
    await metering.poll(getattr(request.app.state, 'redis', None))
    await metering.flush()
    measured = await metering.process()
    fitted = await metering.calibrate()
    return {'measured': measured, 'calibrated': [f'{provider}:{window}' for provider, window in fitted]}


@router.get('/allowance')
async def allowance_report(days: int = 7, user=Depends(get_admin_user)):
    """Mologate: the allowance engine in observe mode (docs/limits-and-accounts.md, Phase 3): what it
    decided for each member's requests, and the same days decided again under the rules."""
    days = max(1, min(days, 30))
    return {
        'mode': await Config.get('mologate.limits_mode', 'observe'),
        'decided': await allowance_gate.summary(days),
        'replay': await allowance_gate.replay(days),
    }


@router.get('/credits')
async def my_credits(user=Depends(get_verified_user)):
    """Mologate: the member's credit balance, what earned it and how it was spent (phase 5)."""
    from open_webui.models.credits import Credits

    return {
        'balance': await Credits.balance(user.id),
        'grants': await Credits.grants(user.id),
        'entries': await Credits.entries(user.id),
        'multipliers': await credits.multipliers(),
        'per_dollar': credits.CREDITS_PER_DOLLAR,
    }


@router.put('/credits/multipliers')
async def set_multipliers(form: MultipliersForm, user=Depends(get_admin_user)):
    """Mologate: the plan multipliers credits are granted by (10 credits per 1x)."""
    table = {
        provider: {plan: value for plan, value in plans.items() if 0 <= value <= 1000}
        for provider, plans in form.multipliers.items()
        if provider in ('codex', 'zai')
    }
    await Config.upsert({'mologate.credit_multipliers': table})
    log.info('Credit multipliers changed by %s.', user.id)
    return await credits.multipliers()


@router.get('/overrides')
async def list_overrides(user=Depends(get_admin_user)):
    """Mologate: every override ever granted, newest first, revoked ones included (the audit log)."""
    from open_webui.models.limit_overrides import LimitOverrides

    names = {}
    result = []
    for item in await LimitOverrides.list():
        for key in ('user_id', 'granted_by', 'revoked_by'):
            if item[key] and item[key] not in names:
                found = await Users.get_user_by_id(item[key])
                names[item[key]] = found.name if found else 'Former member'
        result.append(
            item
            | {
                'user_name': names[item['user_id']] if item['user_id'] else 'Team',
                'granted_by_name': names[item['granted_by']],
                'revoked_by_name': names.get(item['revoked_by']),
            }
        )
    return {'data': result}


@router.post('/overrides')
async def grant_override(form: OverrideForm, user=Depends(get_admin_user)):
    """Mologate: extra pool points for a member, or for every member (phase 4)."""
    from open_webui.models.limit_overrides import LimitOverrides

    if form.user_id and not await Users.get_user_by_id(form.user_id):
        raise HTTPException(404, 'Member not found.')
    if form.until is not None and form.until <= time.time():
        raise HTTPException(400, 'Pick an end in the future.')
    granted = await LimitOverrides.grant(form.user_id, user.id, form.extra_points, form.until, form.reason.strip())
    log.info(
        'Limit override %s: %s extra points for %s, granted by %s.',
        granted['id'],
        form.extra_points,
        form.user_id or 'the team',
        user.id,
    )
    return granted


@router.delete('/overrides/{override_id}')
async def revoke_override(override_id: str, user=Depends(get_admin_user)):
    from open_webui.models.limit_overrides import LimitOverrides

    revoked = await LimitOverrides.revoke(override_id, user.id)
    if revoked is None:
        raise HTTPException(404, 'Override not found.')
    log.info('Limit override %s revoked by %s.', override_id, user.id)
    return revoked


@router.get('')
async def list_accounts(request: Request, user=Depends(get_verified_user)):
    # ChatGPT accounts pick up status, plan, new models and limits from codex-lb (cached).
    await codex_accounts.sync_accounts(getattr(request.app.state, 'redis', None))
    result = []
    last_resets = await LimitResetEvents.last_resets()
    redis = getattr(request.app.state, 'redis', None)
    visible = [
        account for account in await ProviderAccounts.list() if can_manage(account, user) or account.sharing == 'shared'
    ]
    # Z.ai coding-plan quotas and OpenRouter key limits are not in answer headers; read them when a minute old.
    await asyncio.gather(
        *(refresh_account_limits(account, redis, max_age=LIMITS_REFRESH_SECONDS) for account in visible)
    )
    names = {}
    for account in visible:
        managed = can_manage(account, user)
        owner = await Users.get_user_by_id(account.owner_user_id) if account.owner_user_id else None
        # Each window also says how much of it the rest of the team used (the owner used the rest).
        limits = await with_team_use(redis, account.id, await get_provider_limits(redis, account.id))
        usage = await member_usage(account, limits, names)
        item = AccountView.model_validate(account).model_dump()
        item.update(
            owner_name='Team' if account.owner_user_id is None else owner.name if owner else 'Former member',
            can_manage=managed,
            limits=with_ledger_budget(account, limits, usage),
            usage=usage,
            last_reset_at=last_resets.get(account.id),
        )
        result.append(item)
    return {'data': result}


async def member_usage(account, limits: dict | None, names: dict) -> dict:
    """Each member's requests, tokens and cost on the account from the usage ledger: this month for
    an API key, the current weekly window (or the last 7 days) for a subscription."""
    now = time.time()
    if account.plan == 'api':
        period, since = 'month', period_start('monthly', now)
    else:
        weekly = next((window for window in (limits or {}).get('windows', []) if window.get('id') == 'secondary'), {})
        resets_at, minutes = weekly.get('resets_at'), weekly.get('window_minutes')
        period, since = 'week', int(now - 7 * 86400)
        if resets_at and minutes and resets_at - minutes * 60 <= now:
            since = int(resets_at - minutes * 60)
    members = await UsageEvents.by_member(account.id, since)
    for member in members:
        if member['user_id'] not in names:
            found = await Users.get_user_by_id(member['user_id'])
            names[member['user_id']] = found.name if found else 'Former member'
        member['name'] = names[member['user_id']]
    members.sort(key=lambda member: -(member['input_tokens'] + member['output_tokens']))
    return {'period': period, 'since': since, 'members': members}


def with_ledger_budget(account, limits: dict | None, usage: dict) -> dict | None:
    """An API key's monthly budget against this month's spend in the ledger. OpenAI, Anthropic and
    Z.ai keys cannot read their own spending; OpenRouter's comes from its key (provider_limits)."""
    amount = (account.budget or {}).get('amount')
    windows = (limits or {}).get('windows', [])
    if account.plan != 'api' or not amount or usage['period'] != 'month' or any(w['id'] == 'budget' for w in windows):
        return limits
    spent = sum(member['cost'] or 0 for member in usage['members'])
    window = budget_window(amount, spent, time.time(), 'ledger')
    window['unpriced'] = sum(member['unpriced'] for member in usage['members'])
    return {**(limits or {'updated_at': int(time.time())}), 'windows': [*windows, window]}


@router.post('', response_model=AccountView)
async def create_api_account(request: Request, form: ApiAccountForm, user=Depends(get_verified_user)):
    await check_rate(request, user)
    require_team_admin(form.team, user)
    if form.provider != 'zai' and form.plan != 'api':
        raise HTTPException(400, 'Select the API plan for this provider.')
    if form.plan == 'api' and form.budget is None:
        raise HTTPException(400, 'Set a monthly budget for this API-key account.')
    key = form.api_key.get_secret_value().strip()
    secret, models = await verify_api_key(form.provider, key, form.plan)
    try:
        account = await ProviderAccounts.insert(
            owner_user_id=None if form.team else user.id,
            provider=form.provider,
            plan=form.plan,
            external_account_id=key_fingerprint(key),
            sharing='shared' if form.team else form.sharing,
            capacity_weight=1,
            budget=form.budget.model_dump() if form.budget else None,
            status='ok',
            secret=secret,
            models=models,
        )
    except IntegrityError:
        raise HTTPException(409, 'This provider key is already connected.') from None
    await refresh_account_limits(account, getattr(request.app.state, 'redis', None))
    # Mologate: a Z.ai coding plan earns its credits now; an API key earns them as it is spent.
    await credits.ensure_grant(account)
    return account


@router.post('/codex')
async def connect_codex(request: Request, form: CodexAccountForm, user=Depends(get_verified_user)):
    await check_rate(request, user)
    require_team_admin(form.team, user)
    redis = getattr(request.app.state, 'redis', None)
    identifier = await codex_accounts.start_login(user, TEAM if form.team else form.sharing, redis, form.method)
    return await codex_accounts.login_status(identifier, user, redis)


@router.get('/codex/logins/{identifier}')
async def codex_login_status(identifier: str, request: Request, user=Depends(get_verified_user)):
    return await codex_accounts.login_status(identifier, user, getattr(request.app.state, 'redis', None))


@router.post('/codex/logins/{identifier}/callback')
async def codex_login_callback(
    identifier: str, request: Request, form: CodexCallbackForm, user=Depends(get_verified_user)
):
    return await codex_accounts.submit_callback(
        identifier, user, form.callback_url.get_secret_value(), getattr(request.app.state, 'redis', None)
    )


@router.delete('/codex/logins/{identifier}')
async def cancel_codex_login(identifier: str, user=Depends(get_verified_user)):
    return await codex_accounts.cancel_login(identifier, user)


@router.patch('/{account_id}', response_model=AccountView)
async def update_account(account_id: str, form: AccountUpdateForm, request: Request, user=Depends(get_verified_user)):
    account = await owned_account(account_id, user)
    values = form.model_dump(exclude_unset=True)
    if any(value is None for key, value in values.items() if key != 'budget'):
        raise HTTPException(400, 'Account settings cannot be empty.')
    if account.plan == 'api' and 'budget' in values and values['budget'] is None:
        raise HTTPException(400, 'API-key accounts need a monthly budget.')
    if 'plan' in values:
        if user.role != 'admin':
            raise HTTPException(403, 'Only an admin can correct a plan.')
        if account.provider != 'zai' or account.plan == 'api':
            raise HTTPException(400, 'This plan comes from the provider and cannot be changed.')
    updated = await ProviderAccounts.update(account_id, values)
    if updated and 'plan' in values:
        # Mologate: a corrected plan grants credits only if the account never had them (phase 5).
        await credits.ensure_grant(updated)
    if updated and 'budget' in values:
        # An OpenRouter key's spend is shown against the new budget straight away.
        await refresh_account_limits(updated, getattr(request.app.state, 'redis', None))
    return updated


@router.post('/{account_id}/verify', response_model=AccountView)
async def verify_account(account_id: str, request: Request, user=Depends(get_verified_user)):
    account = await owned_account(account_id, user)
    await check_rate(request, user)
    if account.provider == 'codex':
        return await codex_accounts.verify(account, getattr(request.app.state, 'redis', None))
    secret, models = await verify_api_key(account.provider, open_secret(account.secret)['api_key'], account.plan)
    await refresh_account_limits(account, getattr(request.app.state, 'redis', None))
    return await ProviderAccounts.update(
        account.id, {'status': 'ok', 'last_error': None, 'models': models, 'secret': seal_secret(secret)}
    )


@router.delete('/{account_id}')
async def delete_account(account_id: str, user=Depends(get_verified_user)):
    account = await owned_account(account_id, user)
    if account.provider == 'codex':
        await codex_accounts.disconnect(account)
    # Mologate: the account's unspent credits go with it (phase 5).
    await credits.forfeit(account)
    await ProviderAccounts.delete(account.id)
    return True


@router.post('/{account_id}/reset')
async def reset_account(account_id: str, request: Request, user=Depends(get_verified_user)):
    """Only the account owner or an admin may spend its reset coupon."""
    account = await owned_account(account_id, user)
    result = await codex_accounts.manual_reset(account, user.id, getattr(request.app.state, 'redis', None))
    if result is None:
        raise HTTPException(409, 'No reset was confirmed. Check the account limits before trying again.')
    allowance_gate._cache.pop(account.provider, None)
    return {'status': 'ok'}
