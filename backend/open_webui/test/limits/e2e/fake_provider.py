"""A fake OpenAI-compatible provider for testing Mologate's enforcement end to end.

Each API key can be told to answer 429, to take a while, and what a request costs. Every call is
logged, with how many requests each key was serving at once.
"""

import asyncio
import json
import time

from aiohttp import web

control = {}  # key -> {'mode': 'ok'|'429', 'delay': seconds, 'cost': dollars}
calls = []
active = {}
peak = {}


def key_of(request):
    return request.headers.get('Authorization', '').removeprefix('Bearer ').strip()


async def models(request):
    return web.json_response(
        {
            'data': [
                {'id': 'gpt-6'},
                {'id': 'gpt-luna-6'},
                {'id': 'claude-haiku-5-5'},
                {'id': 'claude-opus-5-5'},
            ]
        }
    )


async def chat(request):
    key = key_of(request)
    body = await request.json()
    settings = control.get(key, {})
    calls.append({'key': key, 'model': body.get('model'), 'stream': bool(body.get('stream')), 'at': time.time()})
    if settings.get('mode') == '429':
        return web.json_response(
            {'error': {'message': 'Rate limit reached', 'code': 'rate_limit_exceeded'}}, status=429
        )
    active[key] = active.get(key, 0) + 1
    peak[key] = max(peak.get(key, 0), active[key])
    try:
        await asyncio.sleep(settings.get('delay', 0))
        usage = {
            'prompt_tokens': 1000,
            'completion_tokens': 200,
            'total_tokens': 1200,
            'cost': settings.get('cost', 1.0),
        }
        text = f'Hello from {body.get("model")}'
        if not body.get('stream'):
            return web.json_response(
                {
                    'id': 'chatcmpl-fake',
                    'object': 'chat.completion',
                    'model': body.get('model'),
                    'choices': [
                        {'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}
                    ],
                    'usage': usage,
                }
            )
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        for chunk in (
            {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage},
        ):
            await response.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        await response.write(b'data: [DONE]\n\n')
        await response.write_eof()
        return response
    finally:
        active[key] -= 1


async def set_control(request):
    control.update(await request.json())
    return web.json_response(control)


async def get_calls(request):
    return web.json_response({'calls': calls, 'peak': peak})


async def reset_calls(request):
    calls.clear()
    peak.clear()
    return web.json_response({})


app = web.Application()
app.router.add_get('/v1/models', models)
app.router.add_post('/v1/chat/completions', chat)
app.router.add_post('/control', set_control)
app.router.add_get('/calls', get_calls)
app.router.add_post('/calls/reset', reset_calls)

if __name__ == '__main__':
    web.run_app(app, port=8199)
