"""Mologate: credits for accounts added to the pool (docs/limits-and-accounts.md, decision 7, phase 5).

- A subscription (ChatGPT, a Z.ai coding plan) earns its owner 10 x the plan's multiplier when it is
  connected, once ever per provider account (decision 8). The multipliers are an admin-editable
  table (``mologate.credit_multipliers``); a plan without one earns nothing until an admin adds it.
- An API key earns its owner 0.5 credits for every dollar anyone spends through it, as the ledger
  records the spending. Free keys and free models cost nothing and so earn nothing.
- Team accounts have no owner and earn nobody credits.
- Disconnecting an account forfeits what is left of its credits.
- Credits are spent in tier 3, using the weekly window (or an API key's budget): a request past the
  share costs ``allowance.credits_for(its units, the window's capacity)``.
"""

import logging

from open_webui.models.config import Config
from open_webui.models.credits import Credits
from open_webui.utils import allowance
from open_webui.utils import calibration as fitting

log = logging.getLogger(__name__)

CREDITS_PER_MULTIPLIER = 10.0
TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cached_tokens', 'reasoning_tokens')
# 10 credits per $20, the same rate as a 1x plan.
CREDITS_PER_DOLLAR = 0.5
# The provider's base paid tier is 1x. ChatGPT Pro comes as 5x (Pro Lite) and 20x; Z.ai's coding
# plans are sized against Lite.
DEFAULT_MULTIPLIERS = {
    'codex': {'plus': 1, 'business': 1, 'team': 1, 'enterprise': 1, 'edu': 1, 'prolite': 5, 'pro': 20},
    'zai': {'lite': 1, 'pro': 5, 'max': 20},
}


async def multipliers() -> dict:
    """The plan multipliers: the defaults, with an admin's table over them."""
    stored = await Config.get('mologate.credit_multipliers', None)
    table = {provider: dict(plans) for provider, plans in DEFAULT_MULTIPLIERS.items()}
    if isinstance(stored, dict):
        for provider, plans in stored.items():
            if isinstance(plans, dict):
                table.setdefault(provider, {}).update(
                    {
                        plan: value
                        for plan, value in plans.items()
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
                    }
                )
    return table


async def ensure_grant(account) -> float | None:
    """Grant a member's subscription its credits, once ever for this provider account. Returns the
    credits granted, or None (a team account, an API key, a plan without a multiplier, or granted
    before)."""
    try:
        if account.owner_user_id is None or account.plan == 'api':
            return None
        multiplier = (await multipliers()).get(account.provider, {}).get(account.plan)
        if not multiplier:
            return None
        amount = CREDITS_PER_MULTIPLIER * multiplier
        if await Credits.grant(
            account.owner_user_id, account.provider, account.external_account_id, account.plan, amount
        ):
            log.info('Granted %s credits for account %s.', amount, account.id)
            return amount
    except Exception as e:
        log.warning('Could not grant credits for account %s: %s', account.id, e)
    return None


async def earn(account, cost: float | None, usage_event_id: str | None = None) -> float:
    """An API key's owner earns 0.5 credits per dollar spent through it, by anyone."""
    if account.plan != 'api' or account.owner_user_id is None or not cost or cost <= 0:
        return 0.0
    amount = round(cost * CREDITS_PER_DOLLAR, 6)
    await Credits.earn(account.owner_user_id, account.provider, account.external_account_id, amount, usage_event_id)
    return amount


async def forfeit(account) -> float:
    """Forfeit the unspent credits from this account's grant when it is disconnected."""
    if account.owner_user_id is None:
        return 0.0
    try:
        amount = await Credits.forfeit(account.owner_user_id, account.provider, account.external_account_id)
        if amount:
            log.info('Forfeited %s credits for account %s.', amount, account.id)
        return amount
    except Exception as e:
        log.warning('Could not forfeit credits for account %s: %s', account.id, e)
        return 0.0


async def balance(user_id: str) -> float:
    return await Credits.balance(user_id)


async def window_units(account, model_id: str, numbers: dict, cost) -> tuple[str | None, float]:
    """A served request's units in its account's weekly window, or an API key's budget window (its
    only one): the window credits are measured in."""
    from open_webui.utils import allowance_gate, metering

    engine_account = allowance_gate.to_account(account)
    name = 'weekly' if 'weekly' in engine_account.windows else 'budget' if 'budget' in engine_account.windows else None
    if name is None:
        return None, 0.0
    tokens = {key: numbers.get(key) or 0 for key in TOKEN_KEYS}
    if name == 'budget':
        return name, (cost or 0.0) * 100 / allowance.BUDGET_UNIT_DOLLARS
    calibration = (await metering.calibrations()).get((account.provider, name))
    units = fitting.units(calibration, model_id, tokens)
    if units is None:
        # Before calibration: the request's part of the account's rise, by weighted tokens.
        snapshot = await allowance_gate.pool_snapshot(account.provider)
        period = (snapshot['periods'].get(account.id) or {}).get(name)
        units = allowance.request_units(engine_account, period, tokens)
    return name, units


def request_credits(decision: dict | None, name: str | None, units: float) -> float:
    """What a request served in tier 3 costs in credits: its units, over 0.4% of that window's pool."""
    if not isinstance(decision, dict) or decision.get('tier') != 'credit' or not name:
        return 0.0
    capacity = ((decision.get('windows') or {}).get(name) or {}).get('capacity')
    return round(allowance.credits_for(units, capacity), 6) if capacity else 0.0
