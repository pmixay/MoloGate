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
from open_webui.utils import codex_lb
from open_webui.utils.codex_accounts import auto_reset, prepare_request, record_summary
from open_webui.utils.provider_accounts import (
    auto_reset_enabled,
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


class CodexToolCalls:
    """The function calls of a Codex Responses stream, as Chat Completions tool calls.

    The ChatGPT backend doesn't always stream a call the way the Responses docs describe: its
    arguments may come as `response.function_call_arguments.delta` (by `item_id`), as
    `response.output_tool_call.delta` (by `call_id`, without `output_item.added`), or only whole, in
    `response.function_call_arguments.done` or `response.output_item.done`. A call is found by any of
    its ids, and the `.done` arguments are the final ones; the part not sent yet is sent then. Without
    that, a call reached the agent with no arguments ("search_web() missing ... 'query'").
    """

    def __init__(self):
        self.aliases = {}
        self.calls = []

    def __bool__(self):
        return bool(self.calls)

    @staticmethod
    def _aliases(event, item):
        output_index = event.get('output_index')
        aliases = (event.get('item_id'), item.get('id'), event.get('call_id'), item.get('call_id'))
        return [alias for alias in aliases if alias] + ([('output', output_index)] if output_index is not None else [])

    def _call(self, event, item, deltas):
        """The call an event belongs to, opened (and announced) the first time one is seen."""
        aliases = self._aliases(event, item)
        call = next((self.calls[self.aliases[alias]] for alias in aliases if alias in self.aliases), None)
        if call is None:
            call = {
                'index': len(self.calls),
                'id': item.get('call_id') or event.get('call_id') or item.get('id') or event.get('item_id'),
                'name': item.get('name') or event.get('name') or '',
                'arguments': '',
                'sent': '',
            }
            self.calls.append(call)
            deltas.append(
                {
                    'tool_calls': [
                        {
                            'index': call['index'],
                            'id': call['id'],
                            'type': 'function',
                            'function': {'name': call['name'], 'arguments': ''},
                        }
                    ]
                }
            )
        elif not call['name'] and (name := item.get('name') or event.get('name')):
            # Opened by an event without the name: the agent needs it to run the call.
            call['name'] = name
            deltas.append({'tool_calls': [{'index': call['index'], 'function': {'name': name}}]})
        for alias in aliases:
            self.aliases.setdefault(alias, call['index'])
        return call

    def _sync(self, call, deltas, final=False):
        """Send what the call's best-known arguments add to what was sent already."""
        arguments, sent = call['arguments'], call['sent']
        if arguments.startswith(sent):
            if len(arguments) > len(sent):
                deltas.append(
                    {'tool_calls': [{'index': call['index'], 'function': {'arguments': arguments[len(sent) :]}}]}
                )
                call['sent'] = arguments
        elif final:
            log.warning('Codex sent arguments for a tool call that differ from the ones it streamed.')

    def feed(self, event) -> list[dict]:
        """The Chat Completions deltas an event adds."""
        kind, deltas = event.get('type'), []
        item = event.get('item') if isinstance(event.get('item'), dict) else {}
        if kind in ('response.output_item.added', 'response.output_item.done'):
            if item.get('type') != 'function_call':
                return deltas
            call = self._call(event, item, deltas)
            if isinstance(item.get('arguments'), str) and item['arguments']:
                call['arguments'] = item['arguments']
            if kind == 'response.output_item.done':
                self._sync(call, deltas, final=True)
        elif kind == 'response.function_call_arguments.delta':
            call = self._call(event, item, deltas)
            call['arguments'] += str(event.get('delta') or '')
            self._sync(call, deltas)
        elif kind == 'response.output_tool_call.delta':
            # Held back until the call is done: these may repeat what came before.
            call = self._call(event, item, deltas)
            call['arguments'] += str(event.get('arguments') or event.get('delta') or '')
        elif kind == 'response.function_call_arguments.done':
            call = self._call(event, item, deltas)
            if isinstance(event.get('arguments'), str):
                call['arguments'] = event['arguments']
            self._sync(call, deltas, final=True)
        return deltas

    def finish(self) -> list[dict]:
        """What is still owed when the stream ends: arguments of calls that never said they were done."""
        deltas = []
        for call in self.calls:
            self._sync(call, deltas, final=True)
        return deltas

    def items(self) -> list[dict]:
        """The calls as Responses `function_call` output items."""
        return [
            {
                'type': 'function_call',
                'call_id': call['id'],
                'name': call['name'],
                'arguments': call['arguments'] or '{}',
            }
            for call in self.calls
        ]


async def codex_chat_stream(response, model, events=None, on_end=None):
    """Responses SSE as Chat Completions chunks, closed with why the answer stopped when it did not finish.

    `on_end(usage, finished)` learns what the answer used once the stream ends (the usage ledger)."""
    identifier = 'chatcmpl-account'
    calls = CodexToolCalls()
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
                for call_delta in calls.feed(event):
                    partial = True
                    yield chunk(call_delta)
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
                elif kind in FINISHED:
                    for call_delta in calls.finish():
                        yield chunk(call_delta)
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
    calls = CodexToolCalls()
    try:
        async for event in response_events(response):
            kind = event.get('type')
            calls.feed(event)
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
        output = result.get('output') or []
        if calls and not any(isinstance(item, dict) and item.get('type') == 'function_call' for item in output):
            # The backend's final response can leave out what it streamed; the calls come from the stream.
            result = {**result, 'output': [*output, *calls.items()]}
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
    redis = getattr(request.app.state, 'redis', None)
    # Mologate: every attempt leaves a usage ledger row (utils/usage_ledger.py).
    metadata = metadata or getattr(getattr(request, 'state', None), 'metadata', None)
    meter = Meter(request, user, metadata, split_model_id(model)[1])

    async def attempt(account):
        written = meter.rows
        try:
            return await account_request(request, account, form_data, endpoint, user, meter)
        except AccountExhausted:
            meter.record(account, 'limited')
            raise
        except HTTPException:
            # Unless the attempt already left its row (a refused answer, with what it used).
            if meter.rows == written:
                meter.record(account, 'error')
            raise

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


async def account_request(request, account, form_data, endpoint, user, meter=None):
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
                return StreamingResponse(metered_passthrough(response, ended), media_type='text/event-stream')
            # Read up to the first real event: a used-up quota there still goes to the next account.
            events = response_events(response)
            opened = await opening(events)
            if opened and (exhausted := exhausted_by(account, None, provider_error(opened[-1]), response.headers)):
                raise exhausted
            stream = codex_chat_stream if account.provider == 'codex' else chat_stream
            streaming = True
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
            await cleanup_response(response)
