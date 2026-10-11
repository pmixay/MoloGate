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
from open_webui.utils.provider_accounts import auto_reset_enabled, select_accounts, split_model_id
from open_webui.utils.provider_limits import (
    count_account_request,
    get_provider_limits,
    record_provider_limits,
    snapshot_exhausted,
    window_exhausted,
)
from open_webui.utils.session_pool import cleanup_response, get_client_timeout, get_session, stream_wrapper

log = logging.getLogger(__name__)
SESSION_ID = re.compile(r'[A-Za-z0-9:._-]{1,128}')


class AccountExhausted(Exception):
    """The provider reports this account's quota as used up; another account may still serve."""


def chat_session(request, form_data):
    """The chat id lets codex-lb keep one conversation's prompt cache together."""
    metadata = getattr(getattr(request, 'state', None), 'metadata', None) or form_data.get('metadata')
    value = metadata.get('chat_id') if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else None


async def error_code(response):
    try:
        error = (await response.json(content_type=None)).get('error')
    except Exception:
        return None
    return error.get('code') if isinstance(error, dict) else None


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


async def response_events(response):
    """Parse SSE independent of network chunk boundaries."""
    data = []
    async for raw in stream_wrapper(response):
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].lstrip())
        elif not line and data:
            value = '\n'.join(data)
            data = []
            if value != '[DONE]':
                yield json.loads(value)
    if data:
        value = '\n'.join(data)
        if value != '[DONE]':
            yield json.loads(value)


def chat_usage(usage):
    return {
        'prompt_tokens': usage.get('input_tokens', 0),
        'completion_tokens': usage.get('output_tokens', 0),
        'total_tokens': usage.get('total_tokens', usage.get('input_tokens', 0) + usage.get('output_tokens', 0)),
        'prompt_tokens_details': usage.get('input_tokens_details', {}),
        'completion_tokens_details': usage.get('output_tokens_details', {}),
    }


async def codex_chat_stream(response, model):
    identifier = 'chatcmpl-account'
    calls = {}
    completed = False
    try:
        async for event in response_events(response):
            kind = event.get('type')
            delta = None
            if kind == 'response.created':
                identifier = event.get('response', {}).get('id', identifier)
            elif kind == 'response.output_text.delta':
                delta = {'content': event.get('delta', '')}
            elif kind in ('response.reasoning_summary_text.delta', 'response.reasoning_text.delta'):
                delta = {'reasoning_content': event.get('delta', '')}
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
            elif kind == 'response.completed':
                completed = True
                chunk = {
                    'id': identifier,
                    'object': 'chat.completion.chunk',
                    'model': model,
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}],
                    'usage': chat_usage(event.get('response', {}).get('usage') or {}),
                }
                yield 'data: ' + json.dumps(chunk) + '\n\n'
            elif kind in ('error', 'response.failed', 'response.incomplete'):
                yield (
                    'data: '
                    + json.dumps({'error': {'message': 'The provider could not finish this response.'}})
                    + '\n\n'
                )
                return
            if delta is not None:
                chunk = {
                    'id': identifier,
                    'object': 'chat.completion.chunk',
                    'created': int(time.time()),
                    'model': model,
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}],
                }
                yield 'data: ' + json.dumps(chunk) + '\n\n'
        if not completed:
            yield 'data: ' + json.dumps({'error': {'message': 'The provider stream ended before completion.'}}) + '\n\n'
        yield 'data: [DONE]\n\n'
    finally:
        await cleanup_response(response)


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


async def limit_error(redis, accounts, resets_enabled):
    now = time.time()
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
    message = 'Every account for this model has reached its usage limit.'
    if ready_at:
        message += f' The first one resets in {wait_text(min(ready_at) - now)}.'
    if not resets_enabled and any(account.provider == 'codex' for account in accounts):
        message += ' Automatic limit resets are turned off.'
    return HTTPException(429, message)


async def account_completion(request, form_data, user, endpoint='chat/completions'):
    # Always select and authorize again, including direct /openai routes and API-key requests.
    model = form_data.get('model', '')
    accounts = await select_accounts(model, user)
    if endpoint not in ('chat/completions', 'responses'):
        raise HTTPException(403, 'This account route only supports model inference.')
    redis = getattr(request.app.state, 'redis', None)
    exhausted = []
    # Mologate: an empty account hands the request to the next one that still has quota.
    for account in await order_by_headroom(redis, accounts):
        try:
            return await account_request(request, account, form_data, endpoint, user)
        except AccountExhausted:
            exhausted.append(account)
    # Only when every account is empty: redeem a reset coupon, unless a member turned this off.
    resets_enabled = await auto_reset_enabled()
    if resets_enabled:
        account = await auto_reset(exhausted, user.id, redis)
        if account is not None:
            try:
                return await account_request(request, account, form_data, endpoint, user)
            except AccountExhausted:
                pass
    raise await limit_error(redis, exhausted, resets_enabled)


async def account_request(request, account, form_data, endpoint, user):
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
        if response.status == 429:
            raise AccountExhausted()
        # codex-lb: the key's only account is empty, paused or cannot serve this model.
        if account.provider == 'codex' and response.status == 503:
            if await error_code(response) in codex_lb.UNAVAILABLE_CODES:
                raise AccountExhausted()
        if response.status >= 300:
            if response.status in (401, 403):
                await ProviderAccounts.update(
                    account.id,
                    {
                        'status': 'needs_login',
                        'last_error': 'Verify or reconnect this account.',
                    },
                )
            raise HTTPException(
                response.status if response.status >= 400 else 502,
                f'The provider rejected the request (HTTP {response.status}).',
            )
        if account.plan != 'api':
            # Mologate: who used the account decides how its next rise splits between owner and team.
            await count_account_request(
                getattr(request.app.state, 'redis', None), account.id, account.owner_user_id == user.id
            )
        if account.provider == 'codex':
            if wants_stream:
                streaming = True
                stream = (
                    codex_chat_stream(response, model) if endpoint == 'chat/completions' else stream_wrapper(response)
                )
                return StreamingResponse(stream, media_type='text/event-stream')
            async for event in response_events(response):
                if event.get('type') == 'response.completed':
                    result = event['response']
                    if endpoint == 'chat/completions':
                        from open_webui.routers.openai import convert_responses_result

                        result = convert_responses_result(result)
                        result['usage'] = chat_usage(result.get('usage') or {})
                    return result
                if event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                    break
            raise HTTPException(502, 'The provider did not complete this response.')
        if wants_stream and 'text/event-stream' in response.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(stream_wrapper(response), media_type='text/event-stream')
        return await response.json()
    except (HTTPException, AccountExhausted):
        raise
    except Exception:
        raise HTTPException(502, 'The provider request failed. Try again shortly.') from None
    finally:
        if not streaming:
            await cleanup_response(response)
