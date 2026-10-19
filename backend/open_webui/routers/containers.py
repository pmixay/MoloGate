"""Mologate: each chat's container, as the ⋯ menu and admins see it (docs/container-nodes.md)."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.utils import containers
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.chat_id import is_saved_chat_id

router = APIRouter()

_warmups: dict[str, float] = {}


async def _chat_context(chat_id: str, user) -> str:
    if not is_saved_chat_id(chat_id):
        raise HTTPException(status_code=400, detail='Save the chat first.')
    if user.role != 'admin' and not await Chats.is_chat_owner(chat_id, user.id):
        raise HTTPException(status_code=404, detail='Chat not found.')
    return f'chat:{await containers.root_chat_id(chat_id)}'


async def _require_enabled() -> None:
    if not await containers.containers_enabled():
        raise HTTPException(status_code=404, detail='Chat containers are not set up.')


@router.get('/chats/{chat_id}')
async def get_chat_container(chat_id: str, user=Depends(get_verified_user)):
    await _require_enabled()
    return await containers.describe(await _chat_context(chat_id, user))


@router.get('/limits')
async def get_container_limits(user=Depends(get_verified_user)):
    """The size of a chat container, for the ⋯ menu of a chat that has none yet."""
    await _require_enabled()
    nodes = await containers.get_nodes()
    statuses = await containers.node_statuses(nodes)
    node = containers.pick_node(nodes, statuses) or next((node for node in nodes if statuses.get(node['id'])), None)
    if node is None:
        raise HTTPException(status_code=503, detail='No container node is available right now.')
    return statuses[node['id']].get('limits') or {}


class NetworkForm(BaseModel):
    enabled: bool


@router.post('/chats/{chat_id}/network')
async def set_chat_network(chat_id: str, form_data: NetworkForm, user=Depends(get_verified_user)):
    """Switch the container's internet access, mid-chat too: it applies at once."""
    await _require_enabled()
    context = await _chat_context(chat_id, user)
    try:
        await containers.placement(context, user_id=user.id, network=form_data.enabled)
        await containers.set_network(context, form_data.enabled)
    except containers.NodeUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except containers.NodeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await containers.describe(context)


# GitHub credentials and device-login process stay inside this chat's sandbox.
async def _github(chat_id: str, user, operation: str):
    from open_webui.utils.github import github

    await _require_enabled()
    context = await _chat_context(chat_id, user)
    return await github(context, user.id, operation)


@router.get('/chats/{chat_id}/github')
async def get_chat_github(chat_id: str, user=Depends(get_verified_user)):
    return await _github(chat_id, user, 'status')


@router.post('/chats/{chat_id}/github/connect')
async def connect_chat_github(chat_id: str, user=Depends(get_verified_user)):
    return await _github(chat_id, user, 'connect')


@router.post('/chats/{chat_id}/github/poll')
async def poll_chat_github(chat_id: str, user=Depends(get_verified_user)):
    return await _github(chat_id, user, 'poll')


@router.delete('/chats/{chat_id}/github')
async def disconnect_chat_github(chat_id: str, user=Depends(get_verified_user)):
    return await _github(chat_id, user, 'disconnect')


class WarmupForm(BaseModel):
    chat_id: str | None = None


@router.post('/warmup')
async def warm_up(form_data: WarmupForm, user=Depends(get_verified_user)):
    """The user started typing: wake the chat's container, or ready a spare for a new chat."""
    if not await containers.containers_enabled():
        return {'ok': False}
    context = None
    if form_data.chat_id and is_saved_chat_id(form_data.chat_id):
        context = await _chat_context(form_data.chat_id, user)
    key = context or f'new:{user.id}'
    now = time.monotonic()
    if now - _warmups.get(key, 0) < containers.WARMUP_INTERVAL:
        return {'ok': True, 'skipped': True}
    if len(_warmups) > 2000:
        _warmups.clear()
    _warmups[key] = now
    asyncio.create_task(containers.warm_up(context, user.id))
    return {'ok': True}


# -- admin --------------------------------------------------------------------------------------


class NodeForm(BaseModel):
    id: str
    name: str | None = None
    url: str
    # Empty keeps the node's current key.
    key: str | None = None
    enabled: bool = True


class NodesForm(BaseModel):
    enable: bool = True
    nodes: list[NodeForm]


async def _nodes_response() -> dict:
    nodes = await containers.get_nodes(include_disabled=True)
    statuses = await containers.node_statuses(nodes, fresh=True)
    return {
        'enable': bool(await Config.get('containers.enable', True)),
        'nodes': [
            {
                **containers.public_node(node),
                'status': statuses.get(node['id']),
                'score': containers.node_score(statuses[node['id']]) if statuses.get(node['id']) else None,
            }
            for node in nodes
        ],
    }


@router.get('/nodes')
async def get_nodes(user=Depends(get_admin_user)):
    return await _nodes_response()


@router.post('/nodes')
async def set_nodes(request: Request, form_data: NodesForm, user=Depends(get_admin_user)):
    current = {node['id']: node for node in await containers.get_nodes(include_disabled=True)}
    nodes = []
    for node in form_data.nodes:
        key = node.key if node.key else current.get(node.id, {}).get('key', '')
        if not key:
            raise HTTPException(status_code=400, detail=f'Node {node.id} needs its key.')
        nodes.append({**node.model_dump(), 'key': key})
    await Config.upsert({'containers.enable': form_data.enable, 'containers.nodes': containers.parse_nodes(nodes)})
    await containers.sync_connection()
    return await _nodes_response()
