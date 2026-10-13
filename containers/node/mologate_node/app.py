"""The node's HTTP API. Only Mologate calls it, with the node key; see docs/container-nodes.md."""

import asyncio
import hmac
import json
import logging

import aiohttp
from aiohttp import web
from docker.errors import APIError

from mologate_node.config import Settings
from mologate_node.manager import NodeError, SandboxManager, valid_context

log = logging.getLogger('mologate_node')

MANAGER = web.AppKey('manager', SandboxManager)
SETTINGS = web.AppKey('settings', Settings)

HOP_BY_HOP = frozenset(
    (
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'te',
        'trailers',
        'transfer-encoding',
        'upgrade',
        'host',
        'authorization',
    )
)
RESPONSE_STRIPPED = frozenset(('connection', 'keep-alive', 'transfer-encoding', 'content-length', 'server', 'date'))


def _json_error(status: int, code: str, detail: str) -> web.Response:
    return web.json_response({'error': code, 'detail': detail}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith('/v1/'):
        settings = request.app[SETTINGS]
        header = request.headers.get('Authorization', '')
        token = header[7:].strip() if header[:7].lower() == 'bearer ' else ''
        if not token or not hmac.compare_digest(token.encode(), settings.api_key.encode()):
            return _json_error(401, 'unauthorized', 'A valid node key is required.')
    try:
        return await handler(request)
    except NodeError as e:
        return _json_error(e.status, e.code, str(e))
    except APIError as e:
        log.warning('Docker refused %s %s: %s', request.method, request.path, e)
        return _json_error(502, 'docker_error', getattr(e, 'explanation', None) or str(e))


def _context(request: web.Request) -> str:
    context = request.match_info['context']
    if not valid_context(context) or context.startswith('spare:'):
        raise web.HTTPBadRequest(text=json.dumps({'error': 'bad_context'}), content_type='application/json')
    return context


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({'error': 'bad_json'}), content_type='application/json')
    return data if isinstance(data, dict) else {}


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({'status': 'ok'})


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[MANAGER].status())


async def openapi(request: web.Request) -> web.Response:
    return web.json_response(await request.app[MANAGER].openapi())


async def list_sandboxes(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    return web.json_response([manager.info(sb) for sb in manager.store.all() if not sb.spare])


async def create_spare(request: web.Request) -> web.Response:
    return web.json_response(await request.app[MANAGER].create_spare())


async def get_sandbox(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    sandbox = manager.store.get(_context(request))
    if sandbox is None:
        return _json_error(404, 'not_found', 'No container for this chat.')
    return web.json_response(manager.info(sandbox))


async def ensure(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    context = _context(request)
    data = await _body(request)
    network = data.get('network')
    start = data.get('start', True) is not False
    if data.get('wait') is False:
        # Warm-up: answer at once and start in the background.
        async def run():
            try:
                await manager.ensure(context, network=network if isinstance(network, bool) else None, start=start)
            except Exception as e:
                log.info('Warm-up of %s failed: %s', context, e)

        asyncio.create_task(run())
        return web.json_response({'context': context, 'state': 'starting'}, status=202)
    sandbox = await manager.ensure(context, network=network if isinstance(network, bool) else None, start=start)
    return web.json_response(manager.info(sandbox))


async def touch(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    context = _context(request)
    if manager.store.get(context) is None:
        return _json_error(404, 'not_found', 'No container for this chat.')
    manager.touch(context)
    return web.json_response({'ok': True})


async def set_network(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    data = await _body(request)
    if not isinstance(data.get('enabled'), bool):
        return _json_error(400, 'bad_request', '"enabled" must be true or false.')
    sandbox = await manager.set_network(_context(request), data['enabled'])
    return web.json_response(manager.info(sandbox))


async def pause(request: web.Request) -> web.Response:
    await request.app[MANAGER].pause(_context(request))
    return web.json_response({'ok': True})


async def stop(request: web.Request) -> web.Response:
    await request.app[MANAGER].stop(_context(request))
    return web.json_response({'ok': True})


async def delete(request: web.Request) -> web.Response:
    deleted = await request.app[MANAGER].delete(_context(request))
    return web.json_response({'deleted': deleted})


async def put_file(request: web.Request) -> web.Response:
    path = request.query.get('path', '')
    result = await request.app[MANAGER].put_file(_context(request), path, request.content)
    return web.json_response(result)


async def export(request: web.Request) -> web.StreamResponse:
    chunks = await request.app[MANAGER].export(_context(request))
    response = web.StreamResponse(headers={'Content-Type': 'application/x-tar'})
    await response.prepare(request)
    async for chunk in chunks:
        await response.write(chunk)
    await response.write_eof()
    return response


async def import_(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    network = request.query.get('network', '1') not in {'0', 'false'}
    sandbox = await manager.import_(_context(request), request.content, network)
    return web.json_response(manager.info(sandbox))


async def snapshot(request: web.Request) -> web.Response:
    result = await request.app[MANAGER].snapshot(_context(request), request.match_info['snap_id'])
    return web.json_response(result)


async def changes(request: web.Request) -> web.Response:
    result = await request.app[MANAGER].changes(_context(request), request.match_info['snap_id'])
    return web.json_response(result)


async def proxy(request: web.Request) -> web.StreamResponse:
    """Forward a request to the container's Open Terminal, starting or waking the container first."""
    manager = request.app[MANAGER]
    context = _context(request)
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return await _proxy_websocket(request, manager, context)

    sandbox = await manager.ensure(context)
    path = request.match_info['path']
    url = f'http://sandbox/{path}'
    if request.query_string:
        url += f'?{request.query_string}'
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers['Authorization'] = f'Bearer {sandbox.api_key}'

    manager.begin(context)
    try:
        session = manager.session_for(sandbox)
        async with session.request(
            request.method,
            url,
            headers=headers,
            data=request.content if request.can_read_body else None,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status,
                headers={k: v for k, v in upstream.headers.items() if k.lower() not in RESPONSE_STRIPPED},
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                manager.touch(context)
            await response.write_eof()
            return response
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
        return _json_error(502, 'sandbox_unreachable', f'The container did not answer: {e}')
    finally:
        manager.end(context)


async def _proxy_websocket(request: web.Request, manager: SandboxManager, context: str) -> web.StreamResponse:
    sandbox = await manager.ensure(context)
    client = web.WebSocketResponse(heartbeat=30)
    await client.prepare(request)
    path = request.match_info['path']
    url = f'http://sandbox/{path}'
    if request.query_string:
        url += f'?{request.query_string}'

    session = manager.session_for(sandbox)
    try:
        async with session.ws_connect(url, heartbeat=30) as upstream:
            # Open Terminal authenticates sockets with their first message. Whatever the caller sent
            # as its own auth message is replaced by this container's key.
            await upstream.send_str(json.dumps({'type': 'auth', 'token': sandbox.api_key}))
            first = True

            async def client_to_upstream():
                nonlocal first
                async for message in client:
                    manager.touch(context)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        if first:
                            first = False
                            try:
                                if json.loads(message.data).get('type') == 'auth':
                                    continue
                            except (ValueError, AttributeError):
                                pass
                        await upstream.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        first = False
                        await upstream.send_bytes(message.data)
                    else:
                        break

            async def upstream_to_client():
                async for message in upstream:
                    manager.touch(context)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await client.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await client.send_bytes(message.data)
                    else:
                        break

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except aiohttp.ClientError as e:
        log.info('Terminal socket for %s failed: %s', context, e)
    finally:
        await client.close()
    return client


def create_app(settings: Settings, manager: SandboxManager | None = None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=1024**2)
    app[SETTINGS] = settings
    app[MANAGER] = manager or SandboxManager(settings)

    async def on_startup(app: web.Application) -> None:
        await app[MANAGER].start()

    async def on_cleanup(app: web.Application) -> None:
        await app[MANAGER].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    sandbox = '/v1/sandboxes/{context}'
    app.add_routes(
        [
            web.get('/healthz', healthz),
            web.get('/v1/status', status),
            web.get('/v1/openapi.json', openapi),
            web.get('/v1/sandboxes', list_sandboxes),
            web.post('/v1/spares', create_spare),
            web.get(sandbox, get_sandbox),
            web.delete(sandbox, delete),
            web.post(f'{sandbox}/ensure', ensure),
            web.post(f'{sandbox}/touch', touch),
            web.post(f'{sandbox}/network', set_network),
            web.post(f'{sandbox}/pause', pause),
            web.post(f'{sandbox}/stop', stop),
            web.put(f'{sandbox}/files', put_file),
            web.get(f'{sandbox}/export', export),
            web.put(f'{sandbox}/import', import_),
            web.post(f'{sandbox}/snapshots/{{snap_id}}', snapshot),
            web.get(f'{sandbox}/snapshots/{{snap_id}}/changes', changes),
            web.route('*', f'{sandbox}/t/{{path:.*}}', proxy),
        ]
    )
    return app
