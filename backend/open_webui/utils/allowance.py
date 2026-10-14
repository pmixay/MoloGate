"""Mologate: the allowance engine (docs/limits-and-accounts.md, section 7). Pure: no database, no clock.

``decide(request, state)`` says which tier a request belongs to, which account should serve it and
with which model:

1. **reserve:** the requester's own account, within the owner's half (or all of a private one).
2. **pool:** within the requester's share of the team pool, in every window.
3. **credit:** past the share, within the extra that credits and overrides give.
4. **worker:** past all of that while the pool still has room: cheap worker models only.
5. **empty:** no account of the provider has room for the pool.

``build_state`` turns what metering knows (each account window's reading, and every member's tokens
and outside use since the window last reset) into the engine's numbers. The live gate
(utils/allowance_gate.py) and the replay share it.

Units: one unit is one percent of a weight-1 window (section 5). A shared account gives the pool
``weight * (50 + loan)`` units per window; a team account, which has no owner, all of its 100.
"""

from dataclasses import dataclass, field

from open_webui.utils import calibration as fitting

RESERVE = 50.0
LOAN = 25.0
IDLE_SECONDS = 24 * 60 * 60
# One credit is 0.4 percentage points of the pool (decision 7).
CREDIT_SHARE = 0.004
# A $20 monthly budget is a weight-1 window: a dollar is 5 units.
BUDGET_UNIT_DOLLARS = 20.0
TIERS = ('reserve', 'pool', 'credit', 'worker', 'empty')
# Accounts that count towards the pool's size. Choosing an account also skips those cooling down.
POOLED_STATUSES = ('ok', 'cooldown')


@dataclass(frozen=True)
class Account:
    id: str
    provider: str
    owner: str | None
    sharing: str = 'shared'
    status: str = 'ok'
    weight: float = 1.0
    # Model ids it serves; None serves any (tests).
    models: frozenset | None = None
    # An API key's monthly budget in USD, if its owner set one.
    budget: float | None = None
    # The windows its use counts in: a subscription's 5-hour and weekly ones, an API key's budget.
    windows: tuple = ('5h', 'weekly')


@dataclass(frozen=True)
class Window:
    """One account window: what the provider reports, and how much of it is the owner's."""

    # u_a: percent of the window used, from the provider (or the ledger for a budget).
    used: float = 0.0
    # o_a: the owner's attributed use, Mologate and outside, in percent.
    owner_used: float = 0.0
    # No use by the owner in the last 24 hours (decision 3).
    owner_idle: bool = False
    # The window's size; a budget's comes from its amount. None: the account's weight.
    weight: float | None = None
    resets_at: int | None = None


@dataclass
class Request:
    user_id: str
    provider: str
    model: str
    # The account the chat used last, kept so the provider's prompt cache stays warm.
    previous_account: str | None = None
    # The provider's worker model, swapped in at tier 4.
    worker_model: str | None = None


@dataclass
class State:
    accounts: list
    # account id -> window name -> Window
    windows: dict
    members: int
    # The requester's pool use per window, in units.
    used: dict = field(default_factory=dict)
    # The requester's credit balance (phase 5) and active overrides, in pool points.
    credits: float = 0.0
    extra_points: float = 0.0
    # Model ids that count as worker models.
    worker_models: frozenset = frozenset()


@dataclass
class Decision:
    tier: str
    account_id: str | None
    model: str
    swapped: bool
    reason: str
    # Per window: pool capacity, the requester's share, extra, use and what is left.
    windows: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            'tier': self.tier,
            'account_id': self.account_id,
            'model': self.model,
            'swapped': self.swapped,
            'reason': self.reason,
            'windows': self.windows,
        }


def window_weight(account: Account, window: Window) -> float:
    return window.weight if window.weight is not None else account.weight or 1.0


def loan(account: Account, window: Window) -> float:
    """L_a: what an idle owner lends the pool from their unused reserve."""
    if account.owner is None or not window.owner_idle:
        return 0.0
    return min(LOAN, max(0.0, RESERVE - window.owner_used))


def lock(account: Account, window: Window) -> float:
    """The reserve still locked for the owner, in percent."""
    if account.owner is None:
        return 0.0
    if account.sharing != 'shared':
        return 100.0
    return max(0.0, RESERVE - window.owner_used - loan(account, window))


def pool_part(account: Account, window: Window) -> float:
    """Percent of the window the pool owns: half plus the loan, or all of a team account."""
    if account.owner is None:
        return 100.0
    return RESERVE + loan(account, window)


def in_pool(account: Account) -> bool:
    return account.sharing == 'shared' and account.status in POOLED_STATUSES


def pool_room(account: Account, windows: dict) -> float:
    """Percent the pool may still use of an account: the smallest room across its windows."""
    if not windows:
        return 100.0
    return min(100.0 - lock(account, window) - window.used for window in windows.values())


def owner_room(account: Account, windows: dict) -> float:
    """Percent the owner may still use of their own account in every window, within the reserve
    (or all of a private account)."""
    if not windows:
        return 100.0
    rooms = []
    for window in windows.values():
        room = 100.0 - window.used
        if account.sharing == 'shared':
            room = min(room, RESERVE - window.owner_used)
        rooms.append(room)
    return min(rooms)


def serves(account: Account, model: str) -> bool:
    return account.models is None or model in account.models


def credits_for(units: float, weekly_capacity: float) -> float:
    """Credits charged for pool use past the share, using the weekly window (section 7.3)."""
    return units / (CREDIT_SHARE * weekly_capacity) if weekly_capacity > 0 else 0.0


def pool_numbers(state: State, provider: str) -> dict:
    """Per window: the pool's capacity C, the requester's share C/N, their extra and use (units)."""
    accounts = [account for account in state.accounts if account.provider == provider]
    names = sorted({name for account in accounts for name in state.windows.get(account.id, {})})
    members = max(1, state.members)
    numbers = {}
    for name in names:
        capacity = 0.0
        for account in accounts:
            window = state.windows.get(account.id, {}).get(name)
            if window is not None and in_pool(account):
                capacity += window_weight(account, window) * pool_part(account, window)
        share = capacity / members
        extra = (state.credits * CREDIT_SHARE + state.extra_points / 100) * capacity
        used = state.used.get(name, 0.0)
        numbers[name] = {
            'capacity': round(capacity, 4),
            'share': round(share, 4),
            'extra': round(extra, 4),
            'used': round(used, 4),
            'left': round(share + extra - used, 4),
        }
    return numbers


def _pick(candidates: list, previous: str | None, room) -> Account | None:
    """The chat's previous account when it qualifies, else the one with the most room for its size."""
    if not candidates:
        return None
    for account in candidates:
        if account.id == previous:
            return account
    return max(candidates, key=lambda account: (room(account) * (account.weight or 1.0), account.id))


def decide(request: Request, state: State) -> Decision:
    accounts = [account for account in state.accounts if account.provider == request.provider]

    def windows_of(account: Account) -> dict:
        windows = state.windows.get(account.id, {})
        if account.owner == request.user_id:
            # The owner's request is use: an idle owner's loan stops now (decision 3).
            windows = {name: _active(window) for name, window in windows.items()}
        return windows

    numbers = pool_numbers(state, request.provider)

    # 1. Reserve: the requester's own account, within their half (or all of a private one).
    own = [
        account
        for account in accounts
        if account.owner == request.user_id
        and account.status == 'ok'
        and account.sharing != 'paused'
        and serves(account, request.model)
        and owner_room(account, windows_of(account)) > 0
    ]
    if account := _pick(own, request.previous_account, lambda item: owner_room(item, windows_of(item))):
        return Decision('reserve', account.id, request.model, False, 'Your own account.', numbers)

    # Accounts the pool may still use: shared and healthy (one cooling down counts towards the
    # pool's size but is not chosen). An owner past their half draws on the pool like anyone,
    # their own account included.
    open_accounts = [
        account
        for account in accounts
        if account.sharing == 'shared' and account.status == 'ok' and pool_room(account, windows_of(account)) > 0
    ]

    def room(account: Account) -> float:
        return pool_room(account, windows_of(account))

    within_share = all(item['used'] < item['share'] for item in numbers.values())
    within_extra = all(item['used'] < item['share'] + item['extra'] for item in numbers.values())
    for_model = [account for account in open_accounts if serves(account, request.model)]

    # 2. Pool, and 3. credit: any model.
    if within_share and (account := _pick(for_model, request.previous_account, room)):
        return Decision('pool', account.id, request.model, False, 'Within your share of the team pool.', numbers)
    if within_extra and (account := _pick(for_model, request.previous_account, room)):
        return Decision('credit', account.id, request.model, False, 'Past your share: uses your credits.', numbers)

    # 4. Worker: past the share, or no room left for the model asked for.
    worker = request.model if request.model in state.worker_models else request.worker_model
    if worker and (
        account := _pick(
            [account for account in open_accounts if serves(account, worker)], request.previous_account, room
        )
    ):
        swapped = worker != request.model
        reason = (
            f'Your share of the pool is used up, so this reply uses {worker}.'
            if not within_extra
            else f'No account has room for {request.model}, so this reply uses {worker}.'
        )
        return Decision('worker', account.id, worker, swapped, reason if swapped else 'A worker model.', numbers)

    # 5. Empty.
    return Decision('empty', None, request.model, False, 'Every shared account of this provider is used up.', numbers)


def _active(window: Window) -> Window:
    return Window(
        used=window.used,
        owner_used=window.owner_used,
        owner_idle=False,
        weight=window.weight,
        resets_at=window.resets_at,
    )


# ------------------------------------------------------------------------------------------ state


@dataclass
class Period:
    """What metering knows of one account window since it last reset."""

    # The provider's used percent; None while unknown (unknown is never empty).
    used: float | None = None
    resets_at: int | None = None
    # user id -> model id -> token counts
    tokens: dict = field(default_factory=dict)
    # user id -> USD (API keys)
    cost: dict = field(default_factory=dict)
    # user id -> units used outside Mologate (the owner's; '' for a team account)
    outside: dict = field(default_factory=dict)


def _sum_tokens(by_model: dict) -> dict:
    total = {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0, 'reasoning_tokens': 0}
    for tokens in by_model.values():
        for key in total:
            total[key] += tokens.get(key) or 0
    return total


def _weighted(by_model: dict) -> float:
    return sum(fitting._dot(fitting.PRIOR, fitting.features(tokens)) for tokens in by_model.values())


def member_units(account: Account, name: str, period: Period, calibration: dict | None) -> dict:
    """Each member's units on one account window in its current period.

    A budget's units come from cost (5 per dollar). Subscription windows use the provider's
    calibration (section 8); before there is one, the account's own rise (less outside use) is
    shared out by weighted tokens, so the members' units add up to what the provider reported.
    """
    if name == 'budget':
        return {user_id: cost * 100 / BUDGET_UNIT_DOLLARS for user_id, cost in period.cost.items()}
    if calibration and calibration.get('k'):
        return {
            user_id: sum(fitting.units(calibration, model_id, tokens) or 0.0 for model_id, tokens in by_model.items())
            for user_id, by_model in period.tokens.items()
        }
    weighted = {user_id: _weighted(by_model) for user_id, by_model in period.tokens.items()}
    total = sum(weighted.values())
    if not total or period.used is None:
        return {user_id: 0.0 for user_id in period.tokens}
    observed = max(0.0, period.used * (account.weight or 1.0) - sum(period.outside.values()))
    return {user_id: observed * value / total for user_id, value in weighted.items()}


def build_state(
    user_id: str,
    provider: str,
    accounts: list,
    periods: dict,
    owner_active: dict,
    members: int,
    calibrations: dict | None = None,
    credits: float = 0.0,
    extra_points: float = 0.0,
    worker_models: frozenset = frozenset(),
) -> State:
    """The engine's numbers for one requester.

    `periods` is {account id: {window: Period}}; `owner_active` says which accounts their owner used
    in the last 24 hours (in Mologate or outside it).
    """
    calibrations = calibrations or {}
    windows: dict = {}
    used: dict = {}
    for account in accounts:
        if account.provider != provider:
            continue
        for name, period in periods.get(account.id, {}).items():
            weight = account.weight or 1.0
            if name == 'budget':
                if not account.budget:
                    continue
                weight = account.budget / BUDGET_UNIT_DOLLARS
            units = member_units(account, name, period, calibrations.get((provider, name)))
            if name == 'budget':
                percent = sum(period.cost.values()) * 100 / account.budget
            else:
                percent = period.used if period.used is not None else 0.0
            owner_units = 0.0
            if account.owner is not None:
                owner_units = units.get(account.owner, 0.0) + period.outside.get(account.owner, 0.0)
            owner_used = min(owner_units / weight, percent) if account.owner is not None else 0.0
            window = Window(
                used=percent,
                owner_used=owner_used,
                owner_idle=account.owner is not None and not owner_active.get(account.id, False),
                weight=weight if name == 'budget' else None,
                resets_at=period.resets_at,
            )
            windows.setdefault(account.id, {})[name] = window
            # The requester's pool use: their use of anyone else's account, and their own shared
            # account's use past the reserve, outside use included (decision 4).
            if account.owner == user_id:
                if account.sharing == 'shared':
                    used[name] = used.get(name, 0.0) + max(0.0, owner_used - RESERVE) * weight
            else:
                used[name] = used.get(name, 0.0) + units.get(user_id, 0.0)
    return State(
        accounts=[account for account in accounts if account.provider == provider],
        windows=windows,
        members=members,
        used=used,
        credits=credits,
        extra_points=extra_points,
        worker_models=worker_models,
    )


# ----------------------------------------------------------------------------------------- replay


def _independent_room(decision: Decision, request: Request, state: State) -> bool:
    """Whether the pool had room the decision should have given: a member within their share sent
    to the worker tier or refused while an account could serve their model, or anyone refused
    while an account could serve them at all."""
    accounts = [account for account in state.accounts if account.provider == request.provider]

    def open_for(model: str | None) -> bool:
        if not model:
            return False
        for account in accounts:
            windows = state.windows.get(account.id, {})
            if account.owner == request.user_id:
                windows = {name: _active(window) for name, window in windows.items()}
            if (
                account.sharing == 'shared'
                and account.status == 'ok'
                and serves(account, model)
                and pool_room(account, windows) > 0
            ):
                return True
        return False

    numbers = pool_numbers(state, request.provider)
    within_share = all(item['used'] < item['share'] for item in numbers.values())
    if decision.tier in ('worker', 'empty') and within_share and open_for(request.model):
        return True
    worker = request.model if request.model in state.worker_models else request.worker_model
    return decision.tier == 'empty' and (open_for(request.model) or open_for(worker))


def replay(
    events: list[dict],
    accounts: list,
    members: int,
    calibrations: dict | None = None,
    workers: dict | None = None,
) -> dict:
    """Decide every past request again, in order, as the rules would have.

    Events, oldest first: ``reading`` (account_id, window, used, resets_at, window_minutes),
    ``request`` (user_id, account_id, provider, model_id, tokens, cost, chat_id, request_id,
    status), ``history`` (a request from before the replay: counted, not decided) and ``outside``
    (account_id, window, user_id, units). Requests still go to the account that actually served
    them: like observe mode, the replay tells what the rules would have decided.
    `workers` is {provider: (worker model, frozenset of worker model ids)}.
    """
    from open_webui.utils.provider_limits import period_start

    workers = workers or {}
    by_id = {account.id: account for account in accounts}
    # (account id, window) -> its period's start, rows (ts, kind, user, model, tokens, cost or units) and totals
    windows: dict = {}
    periods: dict = {}
    readings: dict = {}
    owner_seen: dict = {}
    last_account: dict = {}
    decided: set = set()
    report = {'requests': 0, 'by_member': {}, 'blocked_with_room': 0, 'same_account': 0, 'decisions': []}

    def total(entry: dict, row: tuple) -> None:
        _, kind, user_id, model_id, tokens, amount = row
        period = entry['period']
        if kind == 'outside':
            period.outside[user_id] = period.outside.get(user_id, 0.0) + (amount or 0.0)
            return
        by_model = period.tokens.setdefault(user_id, {}).setdefault(model_id, {})
        for name, value in tokens.items():
            by_model[name] = by_model.get(name, 0) + (value or 0)
        if amount is not None:
            period.cost[user_id] = period.cost.get(user_id, 0.0) + amount

    def window(account_id: str, name: str, ts: int) -> dict:
        key = (account_id, name)
        entry = windows.get(key)
        if entry is None:
            entry = windows[key] = {'start': None, 'rows': [], 'period': Period()}
            periods.setdefault(account_id, {})[name] = entry['period']
        if name == 'budget' and entry['start'] != period_start('monthly', ts):
            restart(account_id, name, period_start('monthly', ts))
        return entry

    def restart(account_id: str, name: str, start: int) -> None:
        """A new period from `start`: what came before it no longer counts."""
        entry = window(account_id, name, start) if name != 'budget' else windows[(account_id, name)]
        old = entry['period']
        entry['start'] = start
        entry['rows'] = [row for row in entry['rows'] if row[0] >= start]
        entry['period'] = Period(used=old.used, resets_at=old.resets_at)
        periods.setdefault(account_id, {})[name] = entry['period']
        for row in entry['rows']:
            total(entry, row)

    def add(account_id: str, name: str, row: tuple) -> None:
        entry = window(account_id, name, row[0])
        if entry['start'] is not None and row[0] < entry['start']:
            return
        entry['rows'].append(row)
        total(entry, row)

    for event in events:
        kind, ts = event['kind'], event['ts']
        account = by_id.get(event.get('account_id'))
        if kind == 'reading':
            if account is None:
                continue
            key = (account.id, event['window'])
            reading = {
                'used_percent': event['used'],
                'resets_at': event.get('resets_at'),
                'window_minutes': event.get('window_minutes'),
            }
            previous = readings.get(key)
            start = fitting.period_start(reading)
            if previous is None or fitting.started_over(previous, reading):
                if start is None or start > ts:
                    start = previous.get('resets_at') if previous else None
                    if not start or start > ts:
                        start = ts if previous is not None else None
                if start is not None:
                    restart(account.id, event['window'], start)
            else:
                reading['used_percent'] = max(reading['used_percent'], previous['used_percent'])
            readings[key] = reading
            entry = window(account.id, event['window'], ts)
            entry['period'].used = reading['used_percent']
            entry['period'].resets_at = reading['resets_at']
        elif kind == 'outside':
            if account is None or not event.get('window'):
                continue
            user_id = event.get('user_id') or ''
            add(account.id, event['window'], (ts, 'outside', user_id, None, {}, event.get('units') or 0.0))
            if account.owner and user_id == account.owner:
                owner_seen[account.id] = ts
        elif kind in ('request', 'history'):
            provider = event['provider']
            key = (event['user_id'], event.get('request_id') or id(event))
            if kind == 'request' and key not in decided:
                decided.add(key)
                worker_model, worker_models = workers.get(provider, (None, frozenset()))
                request = Request(
                    user_id=event['user_id'],
                    provider=provider,
                    model=event['model_id'],
                    previous_account=last_account.get(event.get('chat_id')) if event.get('chat_id') else None,
                    worker_model=worker_model,
                )
                active = {account_id: seen >= ts - IDLE_SECONDS for account_id, seen in owner_seen.items()}
                state = build_state(
                    request.user_id,
                    provider,
                    accounts,
                    periods,
                    active,
                    members,
                    calibrations,
                    worker_models=worker_models,
                )
                decision = decide(request, state)
                report['requests'] += 1
                member = report['by_member'].setdefault(request.user_id, {tier: 0 for tier in TIERS} | {'swapped': 0})
                member[decision.tier] += 1
                member['swapped'] += decision.swapped
                report['blocked_with_room'] += _independent_room(decision, request, state)
                report['same_account'] += decision.account_id == event.get('account_id')
                report['decisions'].append((ts, request.user_id, decision.tier, decision.account_id, decision.model))
            if account is None or event.get('status') not in ('ok', 'error'):
                continue
            for name in account.windows:
                add(
                    account.id,
                    name,
                    (ts, 'request', event['user_id'], event['model_id'], event.get('tokens') or {}, event.get('cost')),
                )
            if event.get('chat_id'):
                last_account[event['chat_id']] = account.id
            if account.owner == event['user_id']:
                owner_seen[account.id] = ts
    return report
