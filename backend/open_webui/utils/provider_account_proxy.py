"""Route a provider model through an eligible account without exposing its credentials."""

import copy
import json
import logging
import math
import re
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from open_webui.models.provider_accounts import ProviderAccounts, open_secret
from open_webui.utils import account_slots, allowance_gate, api_key_limits, codex_lb
from open_webui.utils.codex_accounts import auto_reset, prepare_request, record_summary
from open_webui.utils.provider_accounts import (
    auto_reset_enabled,
    can_use,
    model_id as account_model_id,
    refresh_limits_soon,
    select_accounts,
    split_model_id,
)
from open_webui.utils.provider_errors import (
    DROPPED,
    ENDED,
    error_kind,
    error_message,
    failure_message,
    filter_message,
    length_message,
    provider_error,
    retry_seconds,
)
from open_webui.utils.provider_limits import (
    count_account_request,
    get_provider_limits,
    record_provider_limits,
    snapshot_exhausted,
    window_exhausted,
)
from open_webui.utils.session_pool import cleanup_response, get_client_timeout, get_session, stream_wrapper
from open_webui.utils.usage_ledger import Meter, responses_usage

log = logging.getLogger(__name__)
SESSION_ID = re.compile(r'[A-Za-z0-9:._-]{1,128}')
# Responses events sent before the model writes anything; a quota failure after them can still fail over.
PREAMBLE = ('response.created', 'response.in_progress', 'response.queued')
FAILED = ('error', 'response.failed')
FINISHED = ('response.completed', 'response.incomplete')
# What response_events yields for a provider's [DONE].
DONE = {'type': '[DONE]'}


class AccountExhausted(Exception):
    """This account's quota, or an API key's credit, is used up; another account may still serve."""

    def __init__(self, credit=False, retry_after=None, message=None):
        super().__init__()
        self.credit = credit
        self.retry_after = retry_after
        self.message = message
        # The account that was found used up (enforce, phase 4).
        self.account = None


def chat_session(request, form_data):
    """The chat id lets codex-lb keep one conversation's prompt cache together."""
    metadata = getattr(getattr(request, 'state', None), 'metadata', None) or form_data.get('metadata')
    value = metadata.get('chat_id') if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else None


async def error_body(response):
    """The error object of a failed response. Bodies are read for this only and never logged."""
    try:
        return provider_error(await response.json(content_type=None))
    except Exception:
        return None


def exhausted_by(account, status, error, headers=None):
    """AccountExhausted when the error is a used-up quota or credit, so the next account can serve."""
    error = error or {}
    # codex-lb: the key's only account is empty, paused or cannot serve this model.
    if account.provider == 'codex' and error.get('code') in codex_lb.UNAVAILABLE_CODES:
        return AccountExhausted(retry_after=retry_seconds(headers, error))
    kind = error_kind(status, error)
    if kind not in ('limit', 'credit'):
        return None
    return AccountExhausted(kind == 'credit', retry_seconds(headers, error), error_message(error))


async def reject(account, status, error, headers):
    """Raise for a failed request: quota and credit errors go to the next account, others to the member."""
    if status == 401:
        # Mologate: only a refused credential takes the account offline. A 403 answers this
        # request alone (moderation, model permissions), so one prompt cannot hide a shared key.
        await ProviderAccounts.update(
            account.id,
            {
                'status': 'needs_login',
                'last_error': 'Verify or reconnect this account.',
            },
        )
    elif exhausted := exhausted_by(account, status, error, headers):
        raise exhausted
    shown = status if status >= 400 else None
    kind = 'refusal' if error_kind(status, error) == 'refusal' else None
    raise HTTPException(shown or 502, failure_message(kind, error_message(error), status=shown))


def codex_payload(payload):
    allowed = {
        'model',
        'input',
        'instructions',
        'tools',
        'tool_choice',
        'parallel_tool_calls',
        'reasoning',
        'text',
        'include',
        'service_tier',
    }
    result = {key: value for key, value in payload.items() if key in allowed}
    result.update(stream=True, store=False)
    result.setdefault('instructions', 'You are a helpful assistant.')
    if isinstance(result.get('input'), str):
        result['input'] = [{'role': 'user', 'content': result['input']}]
    return result


def sse_event(value):
    if value == '[DONE]':
        return DONE
    try:
        event = json.loads(value)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


async def response_events(response):
    """Parse SSE independent of network chunk boundaries."""
    data = []
    async for raw in stream_wrapper(response):
        line = raw.decode('utf-8', 'replace').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].lstrip())
        elif not line and data:
            event = sse_event('\n'.join(data))
            data = []
            if event is not None:
                yield event
    if data and (event := sse_event('\n'.join(data))) is not None:
        yield event


async def opening(events):
    """Events up to the first that is more than a preamble, read before the reply starts."""
    opened = []
    async for event in events:
        opened.append(event)
        if event.get('type') not in PREAMBLE:
            break
    return opened


async def replay(opened, events):
    for event in opened:
        yield event
    async for event in events:
        yield event


def chat_usage(usage):
    return {
        'prompt_tokens': usage.get('input_tokens', 0),
        'completion_tokens': usage.get('output_tokens', 0),
        'total_tokens': usage.get('total_tokens', usage.get('input_tokens', 0) + usage.get('output_tokens', 0)),
        'prompt_tokens_details': usage.get('input_tokens_details', {}),
        'completion_tokens_details': usage.get('output_tokens_details', {}),
    }


def refusal_text(items):
    """The model's own words where Responses output says it refuses."""
    return ''.join(
        part.get('refusal') or ''
        for item in items or []
        if isinstance(item, dict) and item.get('type') == 'message'
        for part in item.get('content') or []
        if isinstance(part, dict) and part.get('type') == 'refusal'
    )


# Mologate: a refusal turned into text keeps this mark, so the chat can still flag it (utils/refusals.py).
REFUSAL_MARK = {'refusal': True}


def error_chunk(kind, message):
    """The stream error chunk the chat shows beside any text already written."""
    return 'data: ' + json.dumps({'error': {'message': message, 'type': kind}}) + '\n\n'


def stream_failure(error, partial):
    kind = error_kind(None, error)
    return kind or 'provider_error', failure_message(kind, error_message(error), partial=partial)


def settle_chat(result):
    """A refusal becomes the reply's text; a filtered or cut-off reply with nothing in it becomes an error."""
    choices = result.get('choices') if isinstance(result, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return result
    choice = choices[0]
    message = choice.get('message') or {}
    if message.get('refusal') and not message.get('content'):
        message['content'] = message['refusal']
        result['mologate'] = REFUSAL_MARK
    if not message.get('content') and not message.get('tool_calls'):
        if choice.get('finish_reason') == 'content_filter':
            raise HTTPException(400, filter_message())
        if choice.get('finish_reason') == 'length':
            raise HTTPException(502, length_message(partial=False))
    return result


async def codex_chat_stream(response, model, events=None, on_end=None):
    """Responses SSE as Chat Completions chunks, closed with why the answer stopped when it did not finish.

    `on_end(usage, finished)` learns what the answer used once the stream ends (the usage ledger)."""
    identifier = 'chatcmpl-account'
    calls = {}
    partial = refused = finished = False
    usage = None
    note = ('interrupted', ENDED)

    def chunk(delta, finish_reason=None, usage=None, refusal=False):
        data = {
            'id': identifier,
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': model,
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}],
        }
        if usage is not None:
            data['usage'] = chat_usage(usage)
        if refusal:
            data['mologate'] = REFUSAL_MARK
        return 'data: ' + json.dumps(data) + '\n\n'

    try:
        try:
            async for event in events or response_events(response):
                kind = event.get('type')
                delta = None
                refusal = False
                if kind == 'response.created':
                    identifier = event.get('response', {}).get('id', identifier)
                elif kind == 'response.output_text.delta':
                    delta = {'content': event.get('delta', '')}
                elif kind in ('response.reasoning_summary_text.delta', 'response.reasoning_text.delta'):
                    delta = {'reasoning_content': event.get('delta', '')}
                elif kind == 'response.refusal.delta':
                    refused = refusal = True
                    delta = {'content': event.get('delta', '')}
                elif kind == 'response.output_item.done' and not refused:
                    # A refusal that arrived without deltas.
                    if text := refusal_text([event.get('item')]):
                        refused = refusal = True
                        delta = {'content': text}
                elif kind == 'response.output_item.added' and event.get('item', {}).get('type') == 'function_call':
                    item = event['item']
                    index = len(calls)
                    calls[item['id']] = index
                    delta = {
                        'tool_calls': [
                            {
                                'index': index,
                                'id': item['call_id'],
                                'type': 'function',
                                'function': {'name': item['name'], 'arguments': ''},
                            }
                        ]
                    }
                elif kind == 'response.function_call_arguments.delta' and event.get('item_id') in calls:
                    delta = {
                        'tool_calls': [
                            {
                                'index': calls[event['item_id']],
                                'function': {'arguments': event.get('delta', '')},
                            }
                        ]
                    }
                elif kind in FINISHED:
                    result = event.get('response') or {}
                    usage, finished = result.get('usage'), True
                    finish, note = 'tool_calls' if calls else 'stop', None
                    if kind == 'response.incomplete':
                        if (result.get('incomplete_details') or {}).get('reason') == 'content_filter':
                            finish, note = 'content_filter', ('content_filter', filter_message(partial))
                        else:
                            finish, note = 'length', ('length', length_message(partial))
                    yield chunk({}, finish, result.get('usage') or {})
                    break
                elif kind in FAILED:
                    note = stream_failure(provider_error(event), partial)
                    break
                if delta is not None:
                    partial = partial or any(delta.values())
                    yield chunk(delta, refusal=refusal)
        except Exception:
            note = ('interrupted', DROPPED)
        if note:
            yield error_chunk(*note)
        yield 'data: [DONE]\n\n'
    finally:
        await cleanup_response(response)
        if on_end is not None:
            on_end(usage, finished)


async def chat_stream(response, model, events=None, on_end=None):
    """An OpenAI-compatible stream, with refusals as text and a closing note when the answer stops early."""
    partial = done = False
    finish = note = usage = None
    try:
        try:
            async for event in events or response_events(response):
                if event is DONE:
                    done = True
                    break
                if (error := provider_error(event)) is not None:
                    note = stream_failure(error, partial)
                    break
                for choice in event.get('choices') or []:
                    delta = choice.get('delta') if isinstance(choice, dict) else None
                    if not isinstance(delta, dict):
                        continue
                    if refusal := delta.pop('refusal', None):
                        delta['content'] = (delta.get('content') or '') + refusal
                        event['mologate'] = REFUSAL_MARK
                    partial = partial or any(
                        delta.get(key) for key in ('content', 'reasoning_content', 'reasoning', 'tool_calls')
                    )
                    finish = choice.get('finish_reason') or finish
                usage = event.get('usage') or usage
                yield 'data: ' + json.dumps(event) + '\n\n'
        except Exception:
            note = ('interrupted', DROPPED)
        if note is None:
            if finish == 'length':
                note = ('length', length_message(partial))
            elif finish == 'content_filter':
                note = ('content_filter', filter_message(partial))
            elif finish == 'error':
                note = stream_failure({}, partial)
            elif not finish and not done:
                note = ('interrupted', ENDED)
        if note:
            yield error_chunk(*note)
        yield 'data: [DONE]\n\n'
    finally:
        await cleanup_response(response)
        if on_end is not None:
            on_end(usage, bool(done or finish) and finish != 'error')


async def metered_passthrough(response, on_end):
    """A Responses stream passed through as it came, noting its final usage for the ledger."""
    usage = None
    try:
        async for line in stream_wrapper(response):
            usage = responses_usage(line) or usage
            yield line
    finally:
        on_end(usage, usage is not None)


async def codex_result(response, account, endpoint):
    """A whole Responses stream as one result. Text already written survives a failure; without any, it is raised."""
    from open_webui.routers.openai import convert_responses_result

    written, last, dropped, refused = [], {}, False, False
    try:
        async for event in response_events(response):
            kind = event.get('type')
            if kind in ('response.output_text.delta', 'response.refusal.delta'):
                written.append(event.get('delta') or '')
                refused = refused or kind == 'response.refusal.delta'
            elif kind in FINISHED + FAILED:
                last = event
                break
    except Exception:
        dropped = True
    kind, result, text = last.get('type'), last.get('response') or {}, ''.join(written)
    if kind in FINISHED:
        if endpoint != 'chat/completions':
            return result
        converted = convert_responses_result(result)
        choice = converted['choices'][0]
        refusal = refusal_text(result.get('output'))
        choice['message']['content'] = choice['message']['content'] or text or refusal
        if refused or refusal:
            converted['mologate'] = REFUSAL_MARK
        if kind == 'response.incomplete':
            reason = (result.get('incomplete_details') or {}).get('reason')
            choice['finish_reason'] = 'content_filter' if reason == 'content_filter' else 'length'
        converted['usage'] = chat_usage(result.get('usage') or {})
        return settle_chat(converted)
    error = provider_error(last) if kind in FAILED else None
    if error is not None:
        # Nothing has reached the member yet, so a used-up account can still hand the request on.
        if exhausted := exhausted_by(account, None, error, response.headers):
            raise exhausted
        failure, message = stream_failure(error, bool(text))
    else:
        failure, message = 'interrupted', DROPPED if dropped else ENDED
    if not text or endpoint != 'chat/completions':
        raise HTTPException(400 if failure == 'refusal' else 502, message)
    partial = convert_responses_result(
        {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}
    )
    partial['choices'][0]['finish_reason'] = 'error'
    partial['usage'] = chat_usage(result.get('usage') or {})
    # The chat shows the error beside the partial answer.
    partial['error'] = {'message': message, 'type': failure}
    return partial


async def order_by_headroom(redis, accounts):
    """Accounts known to be empty go last; otherwise the owner-first order is kept."""
    now = time.time()
    empty = {account.id: snapshot_exhausted(await get_provider_limits(redis, account.id), now) for account in accounts}
    return sorted(accounts, key=lambda account: empty[account.id])


def wait_text(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f'{days} d {hours} h'
    if not hours:
        return f'{minutes} min'
    return f'{hours} h {minutes} min' if minutes else f'{hours} h'


async def ready_at(redis, accounts, failures=None):
    """When the first of these used-up accounts can serve again, from its limits or its 429."""
    now = time.time()
    failures = failures or {}
    times = []
    for account in accounts:
        windows = [
            window
            for window in ((await get_provider_limits(redis, account.id)) or {}).get('windows', [])
            if window_exhausted(window, now)
        ]
        if windows and all(window.get('resets_at') for window in windows):
            times.append(max(window['resets_at'] for window in windows))
        elif account.id in failures and failures[account.id].retry_after:
            times.append(now + failures[account.id].retry_after)
    return int(min(times)) if times else None


async def limit_error(redis, accounts, resets_enabled, failures=None):
    now = time.time()
    failures = failures or {}
    ready_at = []
    for account in accounts:
        windows = [
            window
            for window in ((await get_provider_limits(redis, account.id)) or {}).get('windows', [])
            if window_exhausted(window, now)
        ]
        # An account is usable again once every one of its empty windows has reset.
        if windows and all(window.get('resets_at') for window in windows):
            ready_at.append(max(window['resets_at'] for window in windows))
        elif account.id in failures and failures[account.id].retry_after:
            ready_at.append(now + failures[account.id].retry_after)
    failed = [failures[account.id] for account in accounts if account.id in failures]
    detail = next((failure.message for failure in reversed(failed) if failure.message), None)
    if failed and len(failed) == len(accounts) and all(failure.credit for failure in failed):
        status = 402
        message = (
            'The API key for this model is out of credit.'
            if len(accounts) == 1
            else 'Every API key for this model is out of credit.'
        )
        message += ' Add credit with the provider or pick another model.'
    else:
        status = 429
        message = 'Every account for this model has reached its usage limit.'
        if ready_at:
            message += f' The first one resets in {wait_text(min(ready_at) - now)}.'
            detail = None
    if detail:
        message += f' The provider says: {detail}'
    if not resets_enabled and any(account.provider == 'codex' for account in accounts):
        message += ' Automatic limit resets are turned off.'
    return HTTPException(status, message)


async def account_completion(request, form_data, user, endpoint='chat/completions', metadata=None):
    # Always select and authorize again, including direct /openai routes and API-key requests.
    model = form_data.get('model', '')
    accounts = await select_accounts(model, user)
    if endpoint not in ('chat/completions', 'responses'):
        raise HTTPException(403, 'This account route only supports model inference.')
    # Mologate: a personal API key's own limits (phase 6, utils/api_key_limits.py): its daily points
    # of the pool and how many requests it runs at once. Its models and efforts are checked where
    # the request comes in (main.chat_completion), against the id it asked for.
    await api_key_limits.check_daily_points(request, user, split_model_id(model)[0])
    enforce = await allowance_gate.limits_mode() == 'enforce'
    slot = await api_key_limits.acquire(request, user, enforce)
    try:
        result = await serve_account_model(request, form_data, user, endpoint, metadata, accounts, enforce)
    except BaseException:
        if slot is not None:
            slot.release()
        raise
    return api_key_limits.hold_until_done(result, slot)


async def serve_account_model(request, form_data, user, endpoint, metadata, accounts, enforce):
    model = form_data.get('model', '')
    redis = getattr(request.app.state, 'redis', None)
    # Mologate: every attempt leaves a usage ledger row (utils/usage_ledger.py).
    metadata = metadata or getattr(getattr(request, 'state', None), 'metadata', None)
    provider, upstream_id = split_model_id(model)
    meter = Meter(request, user, metadata, upstream_id)
    # Mologate: in enforce the allowance engine decides first and the request is served as decided
    # (phase 4); a decision that cannot be made lets it go ahead as in observe.
    if enforce:
        chat_id = (metadata or {}).get('chat_id') if isinstance(metadata, dict) else None
        decision = await allowance_gate.decide_now(user.id, provider, upstream_id, chat_id)
        if decision is not None:
            return await enforced_completion(
                request, form_data, user, endpoint, metadata, meter, decision, accounts, redis
            )
    # Mologate: the allowance engine decides every request; observe records it (phase 3).
    meter.decision = allowance_gate.observe(user, provider, upstream_id, metadata)

    async def attempt(account):
        written = meter.rows
        # Mologate: counted against the account's cap; observe never waits (utils/account_slots.py).
        slot = (await account_slots.acquire(redis, [account.id], enforce=False) or (None, None))[1]
        try:
            return await account_request(request, account, form_data, endpoint, user, meter, slot)
        except AccountExhausted:
            meter.record(account, 'limited')
            raise
        except HTTPException:
            # Unless the attempt already left its row (a refused answer, with what it used).
            if meter.rows == written:
                meter.record(account, 'error')
            raise
        finally:
            # A failed attempt may end before its answer could free the slot; freeing twice is harmless.
            if slot is not None and not slot.streaming:
                slot.release()

    exhausted = []
    failures = {}
    # Mologate: an empty account hands the request to the next one that still has quota.
    for account in await order_by_headroom(redis, accounts):
        try:
            return await attempt(account)
        except AccountExhausted as error:
            exhausted.append(account)
            failures[account.id] = error
    # Only when every account is empty: redeem a reset coupon, unless a member turned this off.
    resets_enabled = await auto_reset_enabled()
    if resets_enabled:
        account = await auto_reset(exhausted, user.id, redis)
        if account is not None:
            try:
                return await attempt(account)
            except AccountExhausted as error:
                failures[account.id] = error
    raise await limit_error(redis, exhausted, resets_enabled, failures)


async def tell_member(metadata, text, user_id):
    """A status line under the chat's reply, kept with the chat. API clients read it from the answer."""
    if not isinstance(metadata, dict) or not (metadata.get('chat_id') and metadata.get('message_id')):
        return
    try:
        from open_webui.socket.main import get_event_emitter

        emitter = await get_event_emitter({**metadata, 'user_id': metadata.get('user_id') or user_id})
        if emitter:
            await emitter({'type': 'status', 'data': {'action': 'limits', 'description': text, 'done': True}})
    except Exception as e:
        log.debug('Could not show a limits notice: %s', e)


def with_notice(result, text):
    """The notice on the answer itself, for clients that are not the chat page."""
    if isinstance(result, dict):
        result.setdefault('mologate', {})
        if isinstance(result['mologate'], dict):
            result['mologate']['notice'] = text
    elif isinstance(result, StreamingResponse):
        result.headers['X-Mologate-Notice'] = text.encode('ascii', 'replace').decode('ascii')
    return result


async def enforced_completion(request, form_data, user, endpoint, metadata, meter, decision, accounts, redis):
    """Serve the request as the allowance engine decided (phase 4).

    The decided account first, then the other accounts of the same tier; an account that turns out
    to be used up or rate-limited is left out and the request decided again, so it moves on within
    its tier, or to the next one when none is left. Past the share the reply uses the provider's
    worker model, and the member is told. When the pool has no room, a limit error says when it
    frees up and which worker on another provider still has room.
    """
    provider, asked = split_model_id(form_data.get('model', ''))
    rows = {row.id: row for row in await ProviderAccounts.list() if row.provider == provider and can_use(row, user)}
    for row in accounts:
        rows.setdefault(row.id, row)
    chat_id = metadata.get('chat_id') if isinstance(metadata, dict) else None
    excluded, exhausted, failures = set(), [], {}
    reset_tried = False
    while decision is not None:
        if decision.tier != 'empty':
            usable = [rows[account_id] for account_id in decision.candidates if account_id in rows]
            if usable:
                try:
                    account, answer = await serve_decided(
                        request, form_data, user, endpoint, meter, decision, usable, redis, asked
                    )
                except AccountExhausted as error:
                    excluded.add(error.account.id)
                    exhausted.append(error.account)
                    failures[error.account.id] = error
                else:
                    if decision.swapped:
                        text = allowance_gate.swap_notice(decision, asked, list(rows.values()))
                        await tell_member(metadata, text, user.id)
                        answer = with_notice(answer, text)
                    return answer
            else:
                # Accounts this member cannot use (paused or signed out since the pool was read).
                excluded.update(decision.candidates)
        else:
            # Tier 5. A reset coupon can refill an account only when the provider itself found it
            # used up; the rules' own limits are not the provider's to lift.
            candidates = [row for row in exhausted if row.provider == 'codex']
            if reset_tried or not candidates or not await auto_reset_enabled():
                break
            reset_tried = True
            account = await auto_reset(candidates, user.id, redis)
            if account is None:
                break
            # Its new reading counts at once.
            allowance_gate._cache.pop(provider, None)
            excluded.discard(account.id)
        decision = await allowance_gate.decide_now(user.id, provider, asked, chat_id, frozenset(excluded))
    resets_enabled = await auto_reset_enabled()
    if decision is None or (failures and all(failure.credit for failure in failures.values())):
        if not exhausted:
            raise HTTPException(429, 'Every account for this model has reached its usage limit.')
        raise await limit_error(redis, exhausted, resets_enabled, failures)
    if not decision.resets_at and exhausted:
        # The provider's own reset times, when the rules know none.
        decision.resets_at = await ready_at(redis, exhausted, failures)
    raise HTTPException(429, await allowance_gate.empty_error(user, provider, decision))


async def serve_decided(request, form_data, user, endpoint, meter, decision, usable, redis, asked):
    """One attempt on the first of the tier's accounts with a free slot. Returns (account, answer)."""
    try:
        held = await account_slots.acquire(redis, [row.id for row in await order_by_headroom(redis, usable)], True)
    except account_slots.Busy:
        raise HTTPException(
            429, 'Every account that can serve you is busy with other requests. Try again in a minute.'
        ) from None
    account = next((row for row in usable if held and row.id == held[0]), usable[0])
    slot = held[1] if held else None
    form = form_data
    if decision.model != asked:
        form = {**form_data, 'model': account_model_id(account.provider, decision.model)}
    meter.model_id = decision.model
    meter.tier = decision.tier
    meter.decision = decision.as_dict() | {'mode': 'enforce'}
    written = meter.rows
    try:
        return account, await account_request(request, account, form, endpoint, user, meter, slot)
    except AccountExhausted as error:
        meter.record(account, 'limited')
        error.account = account
        raise
    except HTTPException:
        if meter.rows == written:
            meter.record(account, 'error')
        raise
    finally:
        # A stream frees its slot when it ends; anything else is done with it now.
        if slot is not None and not slot.streaming:
            slot.release()


async def account_request(request, account, form_data, endpoint, user, meter=None, slot=None):
    model = form_data.get('model', '')
    _, upstream_id = split_model_id(model)
    # Each attempt gets its own copy: the Responses conversion consumes the messages.
    payload = copy.deepcopy({key: value for key, value in form_data.items() if key not in ('metadata', 'model')})
    payload['model'] = upstream_id
    wants_stream = bool(payload.get('stream'))
    headers = {}
    if account.provider == 'codex':
        from open_webui.routers.openai import convert_to_responses_payload

        # Mologate: codex-lb sends the request as the official Codex client and refreshes tokens.
        try:
            # Without key auth codex-lb would route over every account; never send traffic then.
            await codex_lb.ensure_settings()
        except codex_lb.CodexLBError:
            log.warning('codex-lb key authentication could not be confirmed; ChatGPT requests are refused.')
            raise HTTPException(502, 'The ChatGPT account service is unavailable. Try again shortly.') from None
        key, summary = await prepare_request(account)
        await record_summary(getattr(request.app.state, 'redis', None), account.id, summary)
        if summary.get('status') == 'paused' or codex_lb.exhausted(summary):
            raise AccountExhausted()
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'text/event-stream'}
        if session_id := chat_session(request, form_data):
            headers['session_id'] = session_id
        payload = codex_payload(convert_to_responses_payload(payload) if endpoint == 'chat/completions' else payload)
        url = codex_lb.BASE_URL + '/backend-api/codex/responses'
    else:
        secret = open_secret(account.secret)
        headers['Authorization'] = 'Bearer ' + secret['api_key']
        if account.provider == 'anthropic_api':
            from open_webui.utils.anthropic import ANTHROPIC_VERSION

            headers.update({'x-api-key': secret['api_key'], 'anthropic-version': ANTHROPIC_VERSION})
        url = secret['base_url'] + '/' + endpoint
    response = None
    streaming = False

    def ended(usage, finished):
        if slot is not None:
            slot.release()
        if meter is not None:
            meter.record(account, 'ok' if finished else 'error', usage)

    try:
        session = codex_lb.get_session() if account.provider == 'codex' else await get_session()
        response = await session.post(
            url,
            json=payload,
            headers=headers,
            allow_redirects=False,
            timeout=get_client_timeout(stream=bool(payload.get('stream'))),
        )
        await record_provider_limits(getattr(request.app.state, 'redis', None), account.id, response.headers)
        if response.status >= 300:
            await reject(account, response.status, await error_body(response), response.headers)
        if account.plan != 'api':
            # Mologate: who used the account decides how its next rise splits between owner and team.
            await count_account_request(
                getattr(request.app.state, 'redis', None), account.id, account.owner_user_id == user.id
            )
        # Z.ai coding plans and OpenRouter keys report their limits only through their own endpoints.
        refresh_limits_soon(account, getattr(request.app.state, 'redis', None))
        if account.provider == 'codex' and not wants_stream:
            result = await codex_result(response, account, endpoint)
            ended(result.get('usage'), 'error' not in result)
            return result
        if wants_stream and (
            account.provider == 'codex' or 'text/event-stream' in response.headers.get('Content-Type', '')
        ):
            if endpoint != 'chat/completions':
                streaming = True
                if slot is not None:
                    slot.streaming = True
                return StreamingResponse(metered_passthrough(response, ended), media_type='text/event-stream')
            # Read up to the first real event: a used-up quota there still goes to the next account.
            events = response_events(response)
            opened = await opening(events)
            if opened and (exhausted := exhausted_by(account, None, provider_error(opened[-1]), response.headers)):
                raise exhausted
            stream = codex_chat_stream if account.provider == 'codex' else chat_stream
            streaming = True
            if slot is not None:
                slot.streaming = True
            return StreamingResponse(
                stream(response, model, replay(opened, events), on_end=ended), media_type='text/event-stream'
            )
        result = await response.json()
        error = provider_error(result)
        if error is not None and not (isinstance(result, dict) and (result.get('choices') or result.get('output'))):
            await reject(account, response.status, error, response.headers)
        usage = result.get('usage') if isinstance(result, dict) else None
        try:
            result = settle_chat(result) if endpoint == 'chat/completions' else result
        except HTTPException:
            ended(usage, False)
            raise
        ended(usage, True)
        return result
    except (HTTPException, AccountExhausted):
        raise
    except Exception:
        raise HTTPException(502, 'The provider request failed. Try again shortly.') from None
    finally:
        if not streaming:
            # Mologate: the account's slot is free once the answer is (a stream frees it when it ends).
            if slot is not None:
                slot.release()
            await cleanup_response(response)
