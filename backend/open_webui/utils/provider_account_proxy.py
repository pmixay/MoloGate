"""Route a provider model through an eligible account without exposing its credentials."""

import json
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from open_webui.models.provider_accounts import ProviderAccounts, open_secret
from open_webui.utils.codex_accounts import refresh_account
from open_webui.utils.provider_accounts import select_account, split_model_id
from open_webui.utils.provider_limits import parse_codex_rate_limits, record_provider_limits, record_provider_snapshot
from open_webui.utils.session_pool import cleanup_response, get_client_timeout, get_session, stream_wrapper


async def record_codex_limits(request, account_id, result):
    windows = parse_codex_rate_limits(result)
    if windows:
        await record_provider_snapshot(getattr(request.app.state, 'redis', None), account_id, windows)


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


async def account_completion(request, form_data, user, endpoint='chat/completions'):
    # Always select and authorize again, including direct /openai routes and API-key requests.
    model = form_data.get('model', '')
    account = await select_account(model, user)
    _, upstream_id = split_model_id(model)
    payload = {key: value for key, value in form_data.items() if key not in ('metadata', 'model')}
    payload['model'] = upstream_id
    wants_stream = bool(payload.get('stream'))
    if endpoint not in ('chat/completions', 'responses'):
        raise HTTPException(403, 'This account route only supports model inference.')
    headers = {}
    if account.provider == 'codex':
        from open_webui.routers.openai import convert_to_responses_payload

        tokens, limits = await refresh_account(account)
        await record_codex_limits(request, account.id, limits)
        headers = {
            'Authorization': 'Bearer ' + tokens['access_token'],
            'ChatGPT-Account-Id': tokens['account_id'],
            'originator': 'mologate',
            'OpenAI-Beta': 'responses=experimental',
        }
        payload = codex_payload(convert_to_responses_payload(payload) if endpoint == 'chat/completions' else payload)
        url = 'https://chatgpt.com/backend-api/codex/responses'
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
        session = await get_session()
        response = await session.post(
            url,
            json=payload,
            headers=headers,
            allow_redirects=False,
            timeout=get_client_timeout(stream=bool(payload.get('stream'))),
        )
        await record_provider_limits(getattr(request.app.state, 'redis', None), account.id, response.headers)
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
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'The provider request failed. Try again shortly.') from None
    finally:
        if not streaming:
            await cleanup_response(response)
