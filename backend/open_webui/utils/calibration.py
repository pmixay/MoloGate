"""Mologate: metering math, pure functions (docs/limits-and-accounts.md, section 8).

Providers report how much of an account's window is used, in percent; Mologate knows how many
tokens it sent. This module ties the two together:

- ``build_intervals`` cuts an account window's readings into intervals of at least
  ``BLOCK_SECONDS``, each with the rise the provider reported. Long intervals keep the lag between
  a request and the provider's reading from mattering.
- ``fit`` learns, per provider and window, how many units a million tokens cost (``k``: uncached
  input, cached input, output and reasoning tokens as separate features), a scale per model, and
  each account's capacity weight. A unit is one percent of a weight-1 window.
- ``expected_percent`` and ``detect_outside`` say how much of a rise Mologate's own requests explain,
  and how much is the owner's use outside Mologate (decision 4).
"""

import math
import statistics

# Snapshot window ids, and the names metering stores them under.
WINDOWS = {'primary': '5h', 'secondary': 'weekly'}

# Readings come from several sources and can lag a little; only a bigger drop, or a reset time
# that moved on, means the window started over (as in utils/provider_limits.py).
RESET_DROP = 5
RESET_MOVE_SECONDS = 300
# Intervals span at least this long, and readings this recent wait: the ledger row of a request is
# written when its answer ends, and a provider's reading may lag behind it.
BLOCK_SECONDS = 15 * 60
SETTLE_SECONDS = 2 * 60
# A rise counts as outside use when it exceeds what Mologate's requests explain by this much.
MARGIN_POINTS = 1.0
MARGIN_SHARE = 0.2

# Features: uncached input, cached input, output (without reasoning) and reasoning tokens, in millions.
FEATURES = ('uncached_input', 'cached_input', 'output', 'reasoning')
# Their relative prices as most providers set them; the fit starts here and is pulled towards it
# when the data cannot tell the features apart.
PRIOR = (1.0, 0.1, 4.0, 4.0)
RIDGE = 0.01
ITERATIONS = 4
MIN_INTERVALS = 12
MIN_MODEL_INTERVALS = 8
MIN_ACCOUNT_INTERVALS = 6
MIN_SCALE = 0.1
# An interval is left out of the fit when it is this far from the rest: robust spreads, and at
# least this many points (providers round to whole percents).
TRIM_MADS = 2.5
TRIM_POINTS = 1.5
TRIM_ROUNDS = 2
# A fitted weight this far from the account's set weight means the set one is wrong.
WEIGHT_TOLERANCE = 0.25


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def started_over(previous: dict, reading: dict) -> bool:
    """Whether a window began a new period between two readings."""
    if reading['used_percent'] < previous['used_percent'] - RESET_DROP:
        return True
    old, new = previous.get('resets_at'), reading.get('resets_at')
    return bool(old and new and new > old + RESET_MOVE_SECONDS)


def keeps(previous: dict | None, reading: dict) -> bool:
    """Readings are kept only when they rise or start a new period: a lower one is a stale one."""
    return previous is None or started_over(previous, reading) or reading['used_percent'] > previous['used_percent']


def period_start(reading: dict) -> int | None:
    resets_at, minutes = reading.get('resets_at'), reading.get('window_minutes')
    if resets_at and minutes:
        return int(resets_at - minutes * 60)
    return None


def _interval(start: int, end: int, used_start: float, used_end: float, first: bool = False) -> dict:
    return {
        'start': int(start),
        'end': int(end),
        'used_start': used_start,
        'used_end': used_end,
        'rise': round(max(0.0, used_end - used_start), 4),
        'first': first,
    }


def build_intervals(readings: list[dict], last_end: int | None, now: float) -> list[dict]:
    """Intervals of an account window not measured yet.

    `readings` are the kept readings, oldest first, from the one the last interval ended at
    (`last_end`), or all of them when there is none. The first reading ever starts an interval at
    the start of its period: what was used before Mologate read the window is the owner's.
    """
    settled = []
    for reading in readings:
        if reading['observed_at'] > now - SETTLE_SECONDS:
            break
        # Workers keep readings independently, so a stale one can follow a higher one: within a
        # period the window never goes down.
        if settled and not started_over(settled[-1], reading):
            reading = {**reading, 'used_percent': max(reading['used_percent'], settled[-1]['used_percent'])}
        settled.append(reading)
    if not settled:
        return []
    intervals = []
    base = settled[0]
    if last_end is None:
        start = period_start(base)
        if start is not None and start < base['observed_at']:
            intervals.append(_interval(start, base['observed_at'], 0.0, base['used_percent'], first=True))
    block, previous = base, base
    for reading in settled[1:]:
        if started_over(previous, reading):
            if previous is not block:
                intervals.append(
                    _interval(
                        block['observed_at'], previous['observed_at'], block['used_percent'], previous['used_percent']
                    )
                )
            start = period_start(reading)
            if start is None or not previous['observed_at'] <= start <= reading['observed_at']:
                start = previous.get('resets_at')
                if not start or not previous['observed_at'] <= start <= reading['observed_at']:
                    start = previous['observed_at']
            intervals.append(_interval(start, reading['observed_at'], 0.0, reading['used_percent']))
            block = reading
        elif reading['observed_at'] - block['observed_at'] >= BLOCK_SECONDS:
            intervals.append(
                _interval(block['observed_at'], reading['observed_at'], block['used_percent'], reading['used_percent'])
            )
            block = reading
        previous = reading
    # Nothing changed for a while: close the interval at the last change.
    if previous is not block and now - previous['observed_at'] >= BLOCK_SECONDS:
        intervals.append(
            _interval(block['observed_at'], previous['observed_at'], block['used_percent'], previous['used_percent'])
        )
    return intervals


def features(tokens: dict) -> list[float]:
    """A request's (or a sum of requests') tokens as the fit's features, in millions."""
    input_tokens = _number(tokens.get('input_tokens')) or 0.0
    output_tokens = _number(tokens.get('output_tokens')) or 0.0
    cached = min(_number(tokens.get('cached_tokens')) or 0.0, input_tokens)
    reasoning = min(_number(tokens.get('reasoning_tokens')) or 0.0, output_tokens)
    # Cached input is part of the input, and reasoning part of the output, as providers count them.
    return [
        (input_tokens - cached) / 1e6,
        cached / 1e6,
        (output_tokens - reasoning) / 1e6,
        reasoning / 1e6,
    ]


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def scale_of(calibration: dict, model_id: str) -> float:
    """A model's scale; models without enough samples get the most expensive known (conservative)."""
    return (calibration.get('scales') or {}).get(model_id, calibration.get('default_scale') or 1.0)


def units(calibration: dict | None, model_id: str, tokens: dict) -> float | None:
    """Units a model's tokens used in a window, or None before calibration."""
    if not calibration or not calibration.get('k'):
        return None
    return scale_of(calibration, model_id) * _dot(calibration['k'], features(tokens))


def predicted_units(calibration: dict | None, tokens_by_model: dict) -> float | None:
    if not tokens_by_model:
        return 0.0
    if not calibration or not calibration.get('k'):
        return None
    return sum(units(calibration, model_id, tokens) for model_id, tokens in tokens_by_model.items())


def expected_percent(calibration: dict | None, tokens_by_model: dict, weight: float) -> float | None:
    """Percent of an account's window Mologate's requests explain, or None while unknown."""
    predicted = predicted_units(calibration, tokens_by_model)
    if predicted is None:
        return None
    return predicted / (weight or 1.0)


def detect_outside(rise: float, expected: float | None) -> float | None:
    """Percent of a rise used outside Mologate: 0 when Mologate explains it, None while unknown."""
    if expected is None:
        return None
    excess = rise - expected
    return round(excess, 4) if excess >= max(MARGIN_POINTS, MARGIN_SHARE * expected) else 0.0


def nnls_ridge(rows: list[list[float]], targets: list[float], prior: list[float], ridge: float) -> list[float]:
    """Least squares with non-negative coefficients, pulled towards `prior` by `ridge`.

    Minimizes |A k - y|^2 + lam |k - prior|^2 with lam = ridge * trace(A'A) / p, by projected
    coordinate descent on the normal equations; the problems here have a handful of unknowns.
    """
    size = len(prior)
    gram = [[sum(row[i] * row[j] for row in rows) for j in range(size)] for i in range(size)]
    trace = sum(gram[i][i] for i in range(size))
    if trace <= 0:
        return [max(0.0, value) for value in prior]
    lam = ridge * trace / size
    rhs = [sum(row[i] * target for row, target in zip(rows, targets)) + lam * prior[i] for i in range(size)]
    for i in range(size):
        gram[i][i] += lam
    coefficients = [max(0.0, value) for value in prior]
    for _ in range(2000):
        change = 0.0
        for j in range(size):
            gradient = rhs[j] - _dot(gram[j], coefficients)
            value = max(0.0, coefficients[j] + gradient / gram[j][j])
            change = max(change, abs(value - coefficients[j]))
            coefficients[j] = value
        if change <= 1e-12 * (1 + max(coefficients)):
            break
    return coefficients


def _fit(training: list[dict]) -> dict | None:
    counts: dict[str, int] = {}
    for interval in training:
        for model_id in interval['features']:
            counts[model_id] = counts.get(model_id, 0) + 1
    own = sorted(model_id for model_id, count in counts.items() if count >= MIN_MODEL_INTERVALS)
    groups = own + (['*'] if len(own) < len(counts) else [])
    grouped = []
    for interval in training:
        sums = {group: [0.0] * len(FEATURES) for group in groups}
        for model_id, tokens in interval['features'].items():
            group = model_id if model_id in own else '*'
            sums[group] = [a + b for a, b in zip(sums[group], features(tokens))]
        grouped.append(sums)
    targets = [interval['rise'] * interval['weight'] for interval in training]
    scales = {group: 1.0 for group in groups}
    k = None
    for _ in range(ITERATIONS):
        rows = [
            [sum(scales[group] * sums[group][f] for group in groups) for f in range(len(FEATURES))] for sums in grouped
        ]
        along = [_dot(PRIOR, row) for row in rows]
        denominator = sum(value * value for value in along)
        if denominator <= 0:
            return None
        kappa = sum(value * target for value, target in zip(along, targets)) / denominator
        if kappa <= 0:
            return None
        k = nnls_ridge(rows, targets, [kappa * value for value in PRIOR], RIDGE)
        if len(groups) < 2:
            break
        contributions = [[_dot(k, sums[group]) for group in groups] for sums in grouped]
        fitted = nnls_ridge(contributions, targets, [1.0] * len(groups), RIDGE)
        fitted = [max(MIN_SCALE, value) for value in fitted]
        base = sum(sum(row) for row in contributions)
        total = sum(_dot(fitted, row) for row in contributions)
        if base <= 0 or total <= 0:
            break
        # Scales average 1 over the traffic, so k stays in units per million tokens of it.
        factor = total / base
        scales = {group: value / factor for group, value in zip(groups, fitted)}
        k = [value * factor for value in k]
    model_scales = {model_id: round(scales[model_id], 6) for model_id in own}
    return {
        'k': [round(value, 6) for value in k],
        'scales': model_scales,
        'default_scale': round(max(scales.values()), 6),
        'samples': len(training),
        'models': counts,
    }


def predict(calibration: dict, interval: dict) -> float:
    return predicted_units(calibration, interval['features']) or 0.0


def _outliers(calibration: dict, intervals: list[dict]) -> set[int]:
    """Intervals the fit cannot explain, usually use outside Mologate: dropped from the refit."""
    residuals = [interval['rise'] * interval['weight'] - predict(calibration, interval) for interval in intervals]
    if len(residuals) < 3:
        return set()
    middle = statistics.median(residuals)
    spread = statistics.median(abs(value - middle) for value in residuals) * 1.4826
    return {
        index
        for index, (interval, value) in enumerate(zip(intervals, residuals))
        if abs(value - middle) > max(TRIM_MADS * spread, TRIM_POINTS * interval['weight'])
    }


def fit(intervals: list[dict], anchors: set[str] | None = None) -> dict | None:
    """Fit one provider window's units per token from its intervals.

    Each interval holds `account_id`, `rise` (percent), `weight` (the account's capacity weight),
    `requests`, `features` ({model: tokens}) and `first`. Only intervals with Mologate traffic count,
    and not an account's first, which includes use from before Mologate. `anchors` are accounts on
    the provider's base plan (weight 1 by definition): when they have enough intervals, k comes from
    them alone and every other account's capacity weight is fitted against them.
    """
    usable = [
        interval
        for interval in intervals
        if interval['requests'] > 0 and interval['features'] and not interval['first']
    ]
    anchored = [interval for interval in usable if anchors and interval['account_id'] in anchors]
    training = anchored if len(anchored) >= MIN_INTERVALS else usable
    if len(training) < MIN_INTERVALS:
        return None
    calibration = _fit(training)
    if calibration is None:
        return None
    # Use outside Mologate only adds to a rise; a first fit skewed by it still shows the worst of
    # it, and a second round the rest.
    kept = training
    for _ in range(TRIM_ROUNDS):
        dropped = _outliers(calibration, kept)
        if not dropped or len(kept) - len(dropped) < MIN_INTERVALS:
            break
        kept = [interval for index, interval in enumerate(kept) if index not in dropped]
        calibration = _fit(kept) or calibration
    calibration['trimmed'] = len(training) - len(kept)
    calibration['anchored'] = training is anchored

    # Per account: what the fit predicts against what the provider reported, and the weight that
    # would make them match.
    accounts: dict[str, dict] = {}
    for account_id in sorted({interval['account_id'] for interval in usable}):
        own = [interval for interval in usable if interval['account_id'] == account_id]
        unexplained = _outliers(calibration, own) if len(own) >= MIN_ACCOUNT_INTERVALS else set()
        kept = [interval for index, interval in enumerate(own) if index not in unexplained]
        predicted = sum(predict(calibration, interval) for interval in kept)
        observed = sum(interval['rise'] for interval in kept)
        weight = kept[-1]['weight'] if kept else 1.0
        entry = {
            'intervals': len(own),
            'trimmed': len(unexplained),
            'observed_percent': round(observed, 3),
            'predicted_percent': round(predicted / weight, 3),
            'weight': weight,
        }
        if observed > 0:
            entry['error'] = round(abs(predicted / weight - observed) / observed, 4)
            if len(kept) >= MIN_ACCOUNT_INTERVALS:
                entry['fitted_weight'] = round(predicted / observed, 4)
                # A Pro account left at weight 1 is off by its size, not by the fit: an admin sets
                # its weight (Settings → Provider accounts), and until then it stays out of the error.
                entry['weight_mismatch'] = abs(entry['fitted_weight'] / weight - 1) > WEIGHT_TOLERANCE
        accounts[account_id] = entry
    calibration['accounts'] = accounts
    errors = [
        (entry['error'], entry['observed_percent'])
        for entry in accounts.values()
        if 'error' in entry and not entry.get('weight_mismatch')
    ]
    total = sum(observed for _, observed in errors)
    calibration['error'] = round(sum(error * observed for error, observed in errors) / total, 4) if total else None
    return calibration
