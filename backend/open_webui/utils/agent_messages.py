"""Mologate: agents in one chat message each other.

The main agent (the chat the user talks to, agent id ``main``) and the sub-agents it starts with
``delegate_task`` (agent id = delegation id, ``deleg_...``) reach each other with the
``send_message`` tool; ``list_agents`` shows who is around.

How a message is delivered depends on the recipient:

- Running: the message waits in the recipient chat's ``meta.agent_mail``. Its tool loop picks it up
  after the current tool call (``take_agent_messages``) and the model reads it as a user message.
- Idle, or done before it picked the message up: the message starts a new turn in the recipient's
  chat (``process_pending_internal_messages``). Only the main agent restarts a finished sub-agent,
  at most ``MAX_WAKES`` times, and the sub-agent's answer comes back to the main agent as a message.

Mail is kept in chat meta rather than in the chat history, so a browser saving an older copy of the
history can't bring delivered messages back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import Request
from open_webui.internal.db import get_async_db
from open_webui.models.chats import Chat, Chats
from open_webui.models.config import Config
from open_webui.models.users import UserModel
from open_webui.tasks import create_task, has_active_tasks
from sqlalchemy import select, update

log = logging.getLogger(__name__)

MAIN = 'main'
MAX_WAKES = 10
MAX_MESSAGE_CHARS = 20_000

# Chats with mail waiting for their running turn; spares the tool loop a database read otherwise.
_waiting: set[str] = set()


def _lock(chat_id: str) -> asyncio.Lock:
    from open_webui.utils.subagents import _parent_locks

    return _parent_locks.setdefault(chat_id, asyncio.Lock())


async def _get_meta(chat_id: str) -> dict | None:
    async with get_async_db() as db:
        row = (await db.execute(select(Chat.meta).where(Chat.id == chat_id))).first()
        return dict(row[0] or {}) if row else None


async def _change_meta(chat_id: str, change):
    """Apply ``change(meta)`` to a chat's meta and save it; returns what ``change`` returns."""
    async with get_async_db() as db:
        stmt = select(Chat.meta).where(Chat.id == chat_id)
        if db.bind.dialect.name == 'postgresql':
            stmt = stmt.with_for_update()
        row = (await db.execute(stmt)).first()
        if row is None:
            return None
        meta = dict(row[0] or {})
        result = change(meta)
        await db.execute(update(Chat).where(Chat.id == chat_id).values(meta=meta))
        await db.commit()
        return result


def agent_label(agent_id: str, meta: dict | None = None) -> str:
    if agent_id == MAIN:
        return 'the main agent'
    profile = (meta or {}).get('profile')
    return f'sub-agent {agent_id} ({profile})' if profile else f'sub-agent {agent_id}'


def _identify(chat_id: str, meta: dict) -> tuple[str, str]:
    """The (main chat id, agent id) of the agent working in ``chat_id``."""
    if meta.get('type') == 'subagent' and meta.get('parent_chat_id'):
        return meta['parent_chat_id'], meta.get('delegation_id') or chat_id
    return chat_id, MAIN


def _entry(sender: str, sender_meta: dict, sender_chat_id: str, body: str, *, reply: bool = False) -> dict:
    label = agent_label(sender, sender_meta)
    return {
        'id': f'am_{uuid4().hex[:12]}',
        'from': sender,
        'from_name': label,
        'from_chat_id': sender_chat_id,
        'content': f'[{"Reply" if reply else "Message"} from {label}]\n{body}',
        'timestamp': int(time.time()),
    }


def _message_text(message: dict | None) -> str:
    if not message:
        return ''
    content = message.get('content') or ''
    if isinstance(content, list):
        content = ''.join(
            str(part.get('text', '')) for part in content if isinstance(part, dict) and part.get('type') == 'text'
        )
    if content:
        return content
    return ''.join(
        str(part.get('text', ''))
        for item in message.get('output') or []
        if item.get('type') == 'message'
        for part in item.get('content') or []
        if part.get('type') == 'output_text'
    )


async def take_agent_messages(chat_id: str | None) -> list[dict]:
    """Output items for the mail a running turn should read before its next model call."""
    if not chat_id or chat_id not in _waiting:
        return []
    async with _lock(chat_id):
        _waiting.discard(chat_id)
        mail = await _change_meta(chat_id, lambda meta: meta.pop('agent_mail', None) or [])
    return [
        {
            'type': 'agent_message',
            'id': entry.get('id') or f'am_{uuid4().hex[:12]}',
            'from': entry.get('from'),
            'from_name': entry.get('from_name'),
            'from_chat_id': entry.get('from_chat_id'),
            'content': entry.get('content') or '',
            'status': 'completed',
        }
        for entry in mail or []
    ]


async def list_agents(request: Request, user: UserModel, chat_id: str) -> dict:
    redis = request.app.state.redis
    meta = await _get_meta(chat_id) or {}
    root_chat_id, me = _identify(chat_id, meta)
    agents = [
        {
            'id': MAIN,
            'role': 'main agent: the conversation with the user, which started the sub-agents',
            'status': 'running' if await has_active_tasks(redis, root_chat_id) else 'idle',
        }
    ]
    for chat in await Chats.get_subagent_chats_by_parent_id(root_chat_id, user.id):
        sub_meta = chat['meta']
        agents.append(
            {
                'id': sub_meta.get('delegation_id') or chat['id'],
                'profile': sub_meta.get('profile') or 'general-purpose',
                'task': (sub_meta.get('task') or chat['title'] or '')[:300],
                'mode': sub_meta.get('mode') or 'foreground',
                'status': 'running' if await has_active_tasks(redis, chat['id']) else 'finished',
            }
        )
    return {'you': me, 'agents': agents}


async def send_agent_message(request: Request, user: UserModel, chat_id: str, to: str, message: str) -> str:
    message = (message or '').strip()
    if not message:
        return 'Error: message must not be empty.'
    if len(message) > MAX_MESSAGE_CHARS:
        message = f'{message[:MAX_MESSAGE_CHARS]}\n\n[message truncated]'

    meta = await _get_meta(chat_id)
    if meta is None:
        return 'Error: this chat no longer exists.'
    root_chat_id, me = _identify(chat_id, meta)
    target = (to or '').strip()
    if target.lower() == MAIN:
        target = MAIN
    if target == me:
        return 'Error: you cannot message yourself.'

    entry = _entry(me, meta, chat_id, message)
    if target == MAIN:
        # The main agent's settings for a new turn: the ones it delegated with.
        status = await deliver_to_main(request, user, root_chat_id, entry, run=meta.get('parent_run'))
        if status == 'queued':
            return 'Delivered to the main agent; it reads your message after its current step.'
        if status == 'started':
            return 'Delivered to the main agent; it started a new turn to read your message.'
        return status

    subagents = await Chats.get_subagent_chats_by_parent_id(root_chat_id, user.id)
    recipient = next((chat for chat in subagents if (chat['meta'].get('delegation_id') or chat['id']) == target), None)
    if not recipient:
        known = ', '.join([MAIN, *[chat['meta'].get('delegation_id') or chat['id'] for chat in subagents]])
        return f'Error: no agent "{to}" in this chat. Agents: {known}.'

    status = await deliver_to_subagent(request, user, recipient['id'], entry, may_wake=me == MAIN)
    name = agent_label(target, recipient['meta'])
    if status == 'queued':
        return f'Delivered to {name}; it reads your message after its current step.'
    if status == 'started':
        return (
            f'Delivered to {name}. It had finished, so it started again to handle your message; '
            'its answer comes back to you as a message.'
        )
    return status


async def deliver_to_main(request: Request, user: UserModel, root_chat_id: str, entry: dict, *, run: dict | None):
    """Returns 'queued' (read after the current step), 'started' (a new turn reads it) or an error."""
    from open_webui.utils.subagents import process_pending_internal_messages

    async with _lock(root_chat_id):
        running = await has_active_tasks(request.app.state.redis, root_chat_id)
        saved = await _change_meta(root_chat_id, lambda meta: meta.setdefault('agent_mail', []).append(entry) or True)
        if not saved:
            return 'Error: the main conversation no longer exists.'
        if running:
            _waiting.add(root_chat_id)
            return 'queued'

    if not (run or {}).get('model_id'):
        # Read with the main agent's next turn.
        return 'queued'

    async def start_turn():
        try:
            await process_pending_internal_messages(request, root_chat_id, user.id, run)
        except Exception:
            log.exception('Failed to start a turn for agent messages in chat %s', root_chat_id)

    asyncio.create_task(start_turn())
    return 'started'


async def deliver_to_subagent(request: Request, user: UserModel, chat_id: str, entry: dict, *, may_wake: bool):
    async with _lock(chat_id):
        running = await has_active_tasks(request.app.state.redis, chat_id)
        meta = await _get_meta(chat_id)
        if meta is None:
            return 'Error: that sub-agent no longer exists.'
        name = agent_label(meta.get('delegation_id') or chat_id, meta)
        if not running:
            if not may_wake:
                return f'Error: {name} has finished; only the main agent can restart it. Message "main" instead.'
            if int(meta.get('wakes') or 0) >= MAX_WAKES:
                return f'Error: {name} has already been restarted {MAX_WAKES} times; start a new sub-agent instead.'
        await _change_meta(chat_id, lambda meta: meta.setdefault('agent_mail', []).append(entry))
        if running:
            _waiting.add(chat_id)
            return 'queued'
        await _wake(request, user, chat_id)
        return 'started'


async def _wake(request: Request, user: UserModel, chat_id: str) -> None:
    """Start a finished sub-agent on its mail. The caller holds the chat's lock and found it idle."""
    from open_webui.utils import agent_tasks

    def wake(meta: dict) -> dict:
        meta['wakes'] = int(meta.get('wakes') or 0) + 1
        # Mologate: it runs again, so the Tasks panel lists it as running (utils/agent_tasks.py).
        meta.update(status='running', started_at=int(time.time()), finished_at=None, error=None, cancelled_by=None)
        meta.pop('task_cleared', None)
        return dict(meta)

    meta = await _change_meta(chat_id, wake)
    if not meta or not meta.get('run'):
        return
    delegation_id = meta.get('delegation_id') or chat_id

    async def emit(status: str):
        await agent_tasks.emit_task_changed(
            user.id, meta['parent_chat_id'], chat_id, task_type='subagent', status=status, handle=delegation_id
        )

    async def finish(status: str, error: str | None, reply: str):
        def change(row_meta: dict):
            if row_meta.get('status') in agent_tasks.FINISHED:
                return False  # stopped from the Tasks panel or with cancel_task
            row_meta.update(status=status, finished_at=int(time.time()), error=error)
            if reply:
                row_meta['result_preview'] = agent_tasks.preview(reply)
            return True

        try:
            if await agent_tasks.update_task_meta(chat_id, change):
                await emit(status)
        except Exception:
            log.exception('Could not record how sub-agent %s ended', delegation_id)

    # Its live output is kept under its task's id, like a sub-agent's first run (utils/subagents.py).
    stream_id = str(uuid4())

    async def run():
        from open_webui.utils.subagents import process_pending_internal_messages

        reply = ''
        status, error = 'completed', None
        try:
            max_iterations = int(await Config.get('subagents.max_iterations', 30) or 30)
            message_id = await process_pending_internal_messages(
                request,
                chat_id,
                user.id,
                meta['run'],
                internal=True,
                max_iterations=max_iterations,
                owned=True,
                stream_id=stream_id,
            )
            if not message_id:
                await finish(status, None, '')
                return
            message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            error = agent_tasks.error_text((message or {}).get('error'))
            reply = _message_text(message) or ''
            if error:
                status = 'failed'
                reply = f'{reply}\n\n[It stopped with an error: {error}]'.strip()
        except Exception as exc:
            log.exception('Sub-agent %s failed to handle its messages', delegation_id)
            status, error = 'failed', str(exc)
            reply = f'[It failed to handle the message: {exc}]'

        await finish(status, error, reply)
        entry = _entry(delegation_id, meta, chat_id, reply or '(no answer)', reply=True)
        await deliver_to_main(request, user, meta['parent_chat_id'], entry, run=meta.get('parent_run'))
        schedule_leftover_messages(request, user, chat_id)

    await create_task(request.app.state.redis, run(), id=chat_id, task_id=stream_id)
    await emit('running')


def schedule_leftover_messages(request: Request, user: UserModel, chat_id: str) -> None:
    """Called as a sub-agent's run ends: mail it got after its last step starts another run."""
    asyncio.create_task(_deliver_leftovers(request, user, chat_id))


async def _deliver_leftovers(request: Request, user: UserModel, chat_id: str) -> None:
    redis = request.app.state.redis
    try:
        # The ending run still holds the chat's slot for a moment.
        while await has_active_tasks(redis, chat_id):
            await asyncio.sleep(0.5)
        async with _lock(chat_id):
            if await has_active_tasks(redis, chat_id):
                return  # Another run started; it reads the mail.
            meta = await _get_meta(chat_id) or {}
            if not meta.get('agent_mail'):
                return
            if int(meta.get('wakes') or 0) >= MAX_WAKES:
                await _change_meta(chat_id, lambda meta: meta.pop('agent_mail', None))
                log.warning('Dropped messages to sub-agent chat %s: restart limit reached', chat_id)
                return
            await _wake(request, user, chat_id)
    except Exception:
        log.exception('Failed to deliver leftover agent messages for chat %s', chat_id)
