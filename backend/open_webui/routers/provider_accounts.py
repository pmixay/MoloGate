"""Authenticated provider account management. Credentials are write-only."""

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
from open_webui.utils import codex_accounts
from open_webui.utils.auth import get_verified_user
from open_webui.utils.provider_accounts import (
    auto_reset_enabled,
    can_manage,
    key_fingerprint,
    require_accounts_enabled,
    verify_api_key,
)
from open_webui.utils.provider_accounts import refresh_zai_limits
from open_webui.utils.provider_limits import get_provider_limits, with_team_use
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
    sharing: Sharing = 'private'
    budget: Budget | None = None


class CodexAccountForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sharing: Sharing = 'private'
    # A code on OpenAI's device page, or OpenAI's sign-in link with the callback address pasted back.
    method: Literal['device_code', 'link'] = 'device_code'


class CodexCallbackForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    callback_url: SecretStr = Field(max_length=8192)


class AccountUpdateForm(BaseModel):
    # Names are generated ('Account #' + 6 characters) and cannot be changed.
    model_config = ConfigDict(extra='forbid')
    sharing: Sharing | None = None
    budget: Budget | None = None
    capacity_weight: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)


class AutoResetForm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


async def owned_account(account_id, user):
    account = await ProviderAccounts.get(account_id)
    if not account or not can_manage(account, user):
        raise HTTPException(404, 'Account not found.')
    return account


async def check_rate(request, user):
    if await verification_limiter.is_limited(getattr(request.app.state, 'redis', None), 'provider-connect:' + user.id):
        raise HTTPException(429, 'Too many connection attempts. Try again in a few minutes.')


async def auto_reset_view():
    changed = await Config.get('mologate.auto_reset_changed', None)
    changed = changed if isinstance(changed, dict) else {}
    return {'enabled': await auto_reset_enabled(), 'changed_by': changed.get('by'), 'changed_at': changed.get('at')}


@router.get('/auto-reset')
async def get_auto_reset(user=Depends(get_verified_user)):
    return await auto_reset_view()


@router.put('/auto-reset')
async def set_auto_reset(form: AutoResetForm, user=Depends(get_verified_user)):
    # The roadmap lets any approved member switch automatic resets off, or back on, for everyone.
    await Config.upsert(
        {
            'mologate.auto_reset': form.enabled,
            'mologate.auto_reset_changed': {'by': user.name, 'by_id': user.id, 'at': int(time.time())},
        }
    )
    log.info('Automatic limit resets turned %s by user %s.', 'on' if form.enabled else 'off', user.id)
    return await auto_reset_view()


@router.get('')
async def list_accounts(request: Request, user=Depends(get_verified_user)):
    # ChatGPT accounts pick up status, plan, new models and limits from codex-lb (cached).
    await codex_accounts.sync_accounts(getattr(request.app.state, 'redis', None))
    result = []
    last_resets = await LimitResetEvents.last_resets()
    for account in await ProviderAccounts.list():
        managed = can_manage(account, user)
        if not managed and account.sharing != 'shared':
            continue
        owner = await Users.get_user_by_id(account.owner_user_id)
        redis = getattr(request.app.state, 'redis', None)
        # Each window also says how much of it the rest of the team used (the owner used the rest).
        limits = await with_team_use(redis, account.id, await get_provider_limits(redis, account.id))
        item = AccountView.model_validate(account).model_dump()
        item.update(
            owner_name=owner.name if owner else 'Former member',
            can_manage=managed,
            limits=limits,
            last_auto_reset_at=last_resets.get(account.id),
        )
        result.append(item)
    return {'data': result}


@router.post('', response_model=AccountView)
async def create_api_account(request: Request, form: ApiAccountForm, user=Depends(get_verified_user)):
    await check_rate(request, user)
    if form.provider != 'zai' and form.plan != 'api':
        raise HTTPException(400, 'Select the API plan for this provider.')
    if form.plan == 'api' and form.budget is None:
        raise HTTPException(400, 'Set a monthly budget for this API-key account.')
    key = form.api_key.get_secret_value().strip()
    secret, models = await verify_api_key(form.provider, key, form.plan)
    try:
        account = await ProviderAccounts.insert(
            owner_user_id=user.id,
            provider=form.provider,
            plan=form.plan,
            external_account_id=key_fingerprint(key),
            sharing=form.sharing,
            capacity_weight=1,
            budget=form.budget.model_dump() if form.budget else None,
            status='ok',
            secret=secret,
            models=models,
        )
    except IntegrityError:
        raise HTTPException(409, 'This provider key is already connected.') from None
    await refresh_zai_limits(account, getattr(request.app.state, 'redis', None))
    return account


@router.post('/codex')
async def connect_codex(request: Request, form: CodexAccountForm, user=Depends(get_verified_user)):
    await check_rate(request, user)
    redis = getattr(request.app.state, 'redis', None)
    identifier = await codex_accounts.start_login(user, form.sharing, redis, form.method)
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
async def update_account(account_id: str, form: AccountUpdateForm, user=Depends(get_verified_user)):
    account = await owned_account(account_id, user)
    values = form.model_dump(exclude_unset=True)
    if any(value is None for key, value in values.items() if key != 'budget'):
        raise HTTPException(400, 'Account settings cannot be empty.')
    if account.plan == 'api' and 'budget' in values and values['budget'] is None:
        raise HTTPException(400, 'API-key accounts need a monthly budget.')
    return await ProviderAccounts.update(account_id, values)


@router.post('/{account_id}/verify', response_model=AccountView)
async def verify_account(account_id: str, request: Request, user=Depends(get_verified_user)):
    account = await owned_account(account_id, user)
    await check_rate(request, user)
    if account.provider == 'codex':
        return await codex_accounts.verify(account, getattr(request.app.state, 'redis', None))
    secret, models = await verify_api_key(account.provider, open_secret(account.secret)['api_key'], account.plan)
    await refresh_zai_limits(account, getattr(request.app.state, 'redis', None))
    return await ProviderAccounts.update(
        account.id, {'status': 'ok', 'last_error': None, 'models': models, 'secret': seal_secret(secret)}
    )


@router.delete('/{account_id}')
async def delete_account(account_id: str, user=Depends(get_verified_user)):
    account = await owned_account(account_id, user)
    if account.provider == 'codex':
        await codex_accounts.disconnect(account)
    await ProviderAccounts.delete(account.id)
    return True
