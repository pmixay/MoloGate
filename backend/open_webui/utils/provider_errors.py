"""What a provider's error or early stop means for a member, in words that are safe to show.

Providers report failures in a few shapes: OpenAI, OpenRouter and Z.ai send
``{"error": {"message", "type", "code", "metadata"}}``, Anthropic ``{"type": "error", "error": {...}}``,
codex-lb ``{"error": {"code", "message"}}``, and Responses streams ``error`` and ``response.failed`` events.
A message is shown as one capped line with anything that looks like a credential redacted, because
providers sometimes echo part of a key. Nothing here logs.
"""

import email.utils
import json
import math
import re
import time

MESSAGE_LIMIT = 500
# A wait longer than this is not a usable retry hint.
MAX_WAIT_SECONDS = 31 * 86400
REDACTED = '[redacted]'
# Control characters and invisible direction marks could hide or reorder text.
CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]')
SECRETS = (
    (re.compile(r'(?i)\bbearer\s+[^\s,;"\']+'), 'Bearer ' + REDACTED),
    (
        re.compile(
            r'(?i)((?:x-)?api[-_]?key|(?:access|refresh|id)[-_]?token|secret|password)'
            r'(["\']?\s*[:=]\s*["\']?)[^\s,;"\'&}]+'
        ),
        r'\1\2' + REDACTED,
    ),
    # OpenAI sk- and sk-proj-, Anthropic sk-ant-, OpenRouter sk-or-, codex-lb sk-clb-, also when masked.
    (re.compile(r'\b(?:sk|pk|rk)[-_][A-Za-z0-9_*.\-]{3,}[A-Za-z0-9_*]'), REDACTED),
    # JWTs, such as OAuth access tokens.
    (re.compile(r'\beyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]+)*'), REDACTED),
    # Z.ai keys: a hex id, a dot and the secret.
    (re.compile(r'\b[0-9A-Fa-f]{32}\.[A-Za-z0-9]{6,}'), REDACTED),
)
# Long runs of letters and digits (hex, base64) count as tokens; slugs with dashes do not.
TOKEN = re.compile(r'[A-Za-z0-9+/]{24,}={0,2}')

# Error codes and types for a used-up quota or rate limit. Z.ai uses numbers.
LIMIT_CODES = frozenset(
    {
        'rate_limit_exceeded',
        'rate_limit_error',
        'rate_limited',
        'usage_limit_reached',
        'usage_not_included',
        'quota_exceeded',
        'quota_exhausted',
        'too_many_requests',
        '1302',
        '1303',
        '1304',
        '1308',
        '1310',
    }
)
CREDIT_CODES = frozenset(
    {
        'insufficient_quota',
        'insufficient_credits',
        'insufficient_balance',
        'billing_hard_limit_reached',
        'billing_not_active',
        'payment_required',
        '1113',
    }
)
REFUSAL_CODES = frozenset(
    {'content_filter', 'content_policy_violation', 'moderation_blocked', 'flagged', 'refusal', '1301'}
)
CREDIT_TEXT = re.compile(
    r'(?i)insufficient[ _](credit|balance|funds|quota)|credit balance is too low|out of credits?'
    r'|exceeded your current quota|requires more credits|key limit exceeded'
)
REFUSAL_TEXT = re.compile(
    r'(?i)\bflagged\b|\bmoderation\b|content[ _](policy|filter|management)|safety (system|policy)|sensitive content'
)

LENGTH = 'The answer reached the output token limit and was cut off.'
LENGTH_EMPTY = (
    'The output token limit was used up before any answer was written. '
    'Allow more output tokens or lower the reasoning effort.'
)
DROPPED = 'The answer was interrupted: the connection to the provider was lost.'
ENDED = 'The answer was interrupted: the provider stream ended before completion.'


def _token(match):
    value = match.group()
    return REDACTED if re.search(r'\d', value) and re.search(r'[A-Za-z]', value) else value


def clean_message(value) -> str | None:
    """Provider text for members: one line, no controls, no credentials, at most MESSAGE_LIMIT long."""
    if not isinstance(value, str):
        return None
    text = CONTROL.sub('', value)
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    text = ' '.join(TOKEN.sub(_token, text).split())
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1].rstrip() + '…'
    return text or None


def provider_error(data) -> dict | None:
    """The error object in a provider body, stream chunk or Responses event, if it holds one."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None
    # response.failed carries the error on the response.
    if isinstance(data.get('response'), dict) and data['response'].get('error'):
        data = data['response']
    error = data.get('error')
    if isinstance(error, dict):
        return error
    if isinstance(error, str) and error.strip():
        return {'message': error}
    # A Responses stream error event is itself the error.
    return data if data.get('type') == 'error' else None


def error_message(error) -> str | None:
    """The cleaned message, with the upstream provider's own words where OpenRouter wraps them."""
    if not isinstance(error, dict):
        return None
    message = error.get('message') if isinstance(error.get('message'), str) else None
    metadata = error.get('metadata')
    if isinstance(metadata, dict) and isinstance(metadata.get('raw'), str):
        try:
            inner = provider_error(json.loads(metadata['raw']))
        except ValueError:
            inner = None
        detail = inner.get('message') if inner else None
        if isinstance(detail, str) and detail.strip() and detail != message:
            message = f'{message}: {detail}' if message else detail
    return clean_message(message)


def error_kind(status, error) -> str | None:
    """'credit', 'limit' or 'refusal' when the status or error says so; None for any other failure."""
    error = error if isinstance(error, dict) else {}
    codes = {str(error[key]).lower() for key in ('code', 'type') if error.get(key) is not None}
    text = error.get('message') if isinstance(error.get('message'), str) else ''
    if status == 402 or codes & CREDIT_CODES or CREDIT_TEXT.search(text):
        return 'credit'
    if status == 429 or codes & LIMIT_CODES:
        return 'limit'
    if codes & REFUSAL_CODES or (status in (None, 400, 403, 451) and REFUSAL_TEXT.search(text)):
        return 'refusal'
    return None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def retry_seconds(headers, error=None, now=None) -> float | None:
    """How long the provider asks to wait: retry headers first, then a usage-limit error's reset time."""
    now = time.time() if now is None else now
    headers = {str(key).lower(): value for key, value in (headers or {}).items()}
    error = error if isinstance(error, dict) else {}
    seconds = None
    if (value := _number(headers.get('retry-after-ms'))) is not None:
        seconds = value / 1000
    elif headers.get('retry-after'):
        seconds = _number(headers['retry-after'])
        if seconds is None:
            try:
                seconds = email.utils.parsedate_to_datetime(headers['retry-after']).timestamp() - now
            except (TypeError, ValueError):
                seconds = None
    elif (value := _number(error.get('resets_in_seconds'))) is not None:
        seconds = value
    elif (value := _number(error.get('resets_at'))) is not None:
        seconds = (value / 1000 if value > 1e12 else value) - now
    return seconds if seconds is not None and 0 < seconds <= MAX_WAIT_SECONDS else None


def refusal_message(message=None) -> str:
    return 'The model declined to answer' + (f': {message}' if message else '.')


def filter_message(partial=False) -> str:
    if partial:
        return "The provider's content filter stopped this answer."
    return refusal_message("the provider's content filter stopped this response.")


def length_message(partial=True) -> str:
    return LENGTH if partial else LENGTH_EMPTY


def failure_message(kind, message, status=None, partial=False) -> str:
    """The member-facing text for a failed request (with its HTTP status) or a stream that ended in an error."""
    if kind == 'refusal':
        return refusal_message(message)
    if kind == 'credit':
        lead = 'The API key ran out of credit'
    elif kind == 'limit':
        lead = 'The account reached its usage limit'
    elif status is not None:
        lead = 'The provider could not answer' if status >= 500 else 'The provider rejected the request'
        lead += f' (HTTP {status})'
    else:
        lead = 'The provider stopped this answer with an error' if partial else 'The provider could not answer'
    if kind in ('credit', 'limit') and partial:
        lead += ' before the answer was complete'
    return f'{lead}: {message}' if message else lead + '.'
