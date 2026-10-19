from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import timedelta
from uuid import uuid4

from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from open_webui.internal.db import get_async_db
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import Chat, ChatForm, Chats
from open_webui.models.config import Config
from open_webui.models.users import UserModel, Users
from open_webui.tasks import create_task, has_active_tasks
from open_webui.utils import agent_tasks, task_limits
from open_webui.utils.auth import create_token
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import get_message_list
from open_webui.utils.model_ids import find_model_id
from open_webui.utils.subagent_profiles import get_profiles, resolve_profile
from open_webui.utils.tool_context import shorten
from sqlalchemy import select
from starlette.datastructures import Headers

log = logging.getLogger(__name__)

DEFAULT_SUBAGENT_SYSTEM_PROMPT = """You are a sub-agent working on a specific task assigned by the lead agent.

You have full access to the workspace — you can read, write, edit files, and run commands.
Focus exclusively on your assigned task. Do NOT work on anything outside your scope.

When done, end with a clear summary:
- What you did
- What files you changed (if any)
- Any issues or open questions
"""

# Mologate: how a sub-agent reaches the other agents of its chat (utils/agent_messages.py).
AGENT_MESSAGING_PROMPT = """## Other agents
Your agent id is {agent_id}. The main agent started you and may message you while you work; its
messages appear in your conversation. Use send_message to reach the main agent ("main") or another
running sub-agent by its id, for example to report something it needs before you finish or to ask
a question you cannot answer yourself. list_agents shows who is around. Your final answer still goes
back to the main agent on its own."""

MUTATING_MEMORY_TOOLS = {
    'add_memory',
    'delete_memory',
    'replace_memory_content',
    'update_memory',
}

_background_active: set[str] = set()
_background_lock = asyncio.Lock()
_foreground_semaphore: asyncio.Semaphore | None = None
_parent_locks: dict[str, asyncio.Lock] = {}
# Mologate: the sub-agents running per main chat, and the background ones per member, for the task
# limits (utils/task_limits.py). In-process, like the caps above.
_chat_running: dict[str, set[str]] = {}
_user_background: dict[str, set[str]] = {}
# Results waiting for a background sub-agent to finish, kept so they aren't garbage-collected.
_deliveries: set[asyncio.Task] = set()


def _build_request(source: Request, user_id: str, *, internal: bool) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.0'},
        'method': 'POST',
        'path': '/api/v1/subagents/internal',
        'query_string': b'',
        'headers': Headers({}).raw,
        'client': ('127.0.0.1', 0),
        'server': ('127.0.0.1', 80),
        'scheme': 'http',
        'app': source.app,
    }
    request = Request(scope)
    token = create_token(
        data={'id': user_id, 'typ': 'subagent'},
        expires_delta=timedelta(hours=1),
    )
    request.state.token = HTTPAuthorizationCredentials(scheme='Bearer', credentials=token)
    request.state.enable_api_keys = False
    if internal:
        request.state.internal = True
    return request


def _app_request(app) -> Request:
    """Mologate: a request that only carries the app, for work that outlives the request that started it."""
    return Request(
        {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'method': 'POST',
            'path': '/api/v1/tasks/internal',
            'query_string': b'',
            'headers': Headers({}).raw,
            'client': ('127.0.0.1', 0),
            'server': ('127.0.0.1', 80),
            'scheme': 'http',
            'app': app,
        }
    )


def _count(members: dict[str, set[str]], key: str) -> int:
    return len(members.get(key) or ())


def _discard(members: dict[str, set[str]], key: str, delegation_id: str) -> None:
    held = members.get(key)
    if held is not None:
        held.discard(delegation_id)
        if not held:
            members.pop(key, None)


def _background_refusal(user_id: str, *, max_async: int, per_user: int) -> str | None:
    """Why one more background sub-agent can't start now; the caller holds ``_background_lock``."""
    if _count(_user_background, user_id) >= per_user:
        return (
            f'Error: {per_user} of your background tasks are already running, the most one member can have. '
            'Wait for one to finish, or stop one with cancel_task.'
        )
    if max_async != -1 and len(_background_active) >= max_async:
        return (
            f'Error: Async subagent capacity reached ({max_async} running). '
            'Wait for one to finish or increase subagents.max_async.'
        )
    return None


def _is_pending_result(meta: dict) -> bool:
    """A background sub-agent's result, or (Mologate) another task's ('task', see post_task_result),
    not answered yet."""
    if meta.get('internal') is not True:
        return False
    if meta.get('type') == 'subagent':
        return meta.get('status') in (None, 'pending')
    # A task result's ``status`` is the task's own; it waits for an answer until it has one.
    return meta.get('type') == 'task'


def _is_pending_agent_message(meta: dict) -> bool:
    # Mologate: a message one agent sent another (utils/agent_messages.py), not yet delivered.
    return (
        meta.get('internal') is True and meta.get('type') == 'agent_message' and meta.get('status') in (None, 'pending')
    )


def _latest_assistant_id(messages: dict, current_id: str | None) -> str | None:
    """Where a new turn continues from: the current message when it's an answer, else the latest answer."""
    current = messages.get(current_id) if current_id else None
    if current and current.get('role') == 'assistant':
        return current_id
    answers = [message for message in messages.values() if message.get('role') == 'assistant']
    return max(answers, key=lambda message: message.get('timestamp', 0)).get('id') if answers else current_id


async def process_pending_internal_messages(
    source_request: Request,
    parent_chat_id: str,
    user_id: str,
    run: dict,
    *,
    internal: bool = False,
    max_iterations: int | None = None,
    owned: bool = False,
    stream_id: str | None = None,
) -> str | None:
    """Start a turn in ``parent_chat_id`` for its waiting internal messages (background sub-agent
    results, task results, timers, agent messages). Returns the new assistant message id, or None
    when nothing was waiting. Mologate: ``internal`` runs the turn as a sub-agent and waits for it to
    finish (waking a sub-agent); ``owned`` means the caller's task already holds the chat's running
    slot; ``stream_id`` is that task's id, under which the turn's live output is kept."""
    lock = _parent_locks.setdefault(parent_chat_id, asyncio.Lock())
    while not owned and await has_active_tasks(source_request.app.state.redis, parent_chat_id):
        await asyncio.sleep(0.25)

    async with lock:
        if not owned and await has_active_tasks(source_request.app.state.redis, parent_chat_id):
            return

        user = await Users.get_user_by_id(user_id)
        if not user:
            return

        async with get_async_db() as db:
            stmt = select(Chat).where(Chat.id == parent_chat_id, Chat.user_id == user_id)
            if db.bind.dialect.name == 'postgresql':
                stmt = stmt.with_for_update()
            result = await db.execute(stmt)
            chat = result.scalar_one_or_none()
            if not chat:
                return

            history = copy.deepcopy((chat.chat or {}).get('history') or {})
            messages = history.get('messages') or {}
            # Mologate: messages other agents left while this chat was busy (utils/agent_messages.py).
            mail = (chat.meta or {}).get('agent_mail') or []
            if mail:
                chat.meta = {key: value for key, value in (chat.meta or {}).items() if key != 'agent_mail'}
                anchor_id = _latest_assistant_id(messages, history.get('currentId'))
                for entry in mail:
                    message_id = str(uuid4())
                    messages[message_id] = {
                        'id': message_id,
                        'parentId': anchor_id,
                        'childrenIds': [],
                        'role': 'user',
                        'content': entry.get('content') or '',
                        'meta': {
                            'internal': True,
                            'type': 'agent_message',
                            'from': entry.get('from'),
                            'from_name': entry.get('from_name'),
                            'from_chat_id': entry.get('from_chat_id'),
                        },
                        'timestamp': entry.get('timestamp') or int(time.time()),
                    }
            pending = [
                message
                for message in messages.values()
                for meta in [message.get('meta') or {}]
                if message.get('role') == 'user'
                and not message.get('childrenIds')
                and (
                    _is_pending_result(meta)
                    or (meta.get('internal') is True and meta.get('type') == 'timer')
                    or _is_pending_agent_message(meta)
                )
            ]
            if not pending:
                if mail:
                    await db.commit()
                return None

            first = pending[0]
            first_meta = first.get('meta') or {}
            kind = (
                first_meta.get('type')
                if first_meta.get('internal') is True and first_meta.get('type') in ('timer', 'agent_message', 'task')
                else 'subagent'
            )
            parent_id = first.get('parentId')
            if kind == 'timer' and first_meta.get('timer_id'):
                timer = await Chats.get_chat_by_id(first_meta['timer_id'])
                run = {**run, **(((timer.meta or {}).get('run') if timer else None) or {})}
            model_id = first.get('model') or run['model_id']
            if kind == 'timer':
                batch = [first]
            elif kind == 'agent_message':
                batch = [
                    message
                    for message in pending
                    if message.get('parentId') == parent_id and _is_pending_agent_message(message.get('meta') or {})
                ]
            else:
                batch = [
                    message
                    for message in pending
                    for meta in [message.get('meta') or {}]
                    if message.get('parentId') == parent_id
                    and (message.get('model') or model_id) == model_id
                    and meta.get('type') == kind
                    and _is_pending_result(meta)
                ]
            combined_content = '\n\n'.join(message.get('content', '') for message in batch if message.get('content'))
            if kind == 'agent_message':
                senders = []
                for message in batch:
                    sender = (message.get('meta') or {}).get('from')
                    if sender and sender not in senders:
                        senders.append(sender)
                combined_meta = {
                    'internal': True,
                    'type': 'agent_message',
                    'from': senders[0] if len(senders) == 1 else None,
                    'from_name': (first.get('meta') or {}).get('from_name') if len(senders) == 1 else None,
                    'from_chat_id': (first.get('meta') or {}).get('from_chat_id') if len(senders) == 1 else None,
                    'senders': senders,
                }
            elif kind == 'timer':
                timer_ids = [
                    message['meta']['timer_id'] for message in batch if (message.get('meta') or {}).get('timer_id')
                ]
                combined_meta = {'internal': True, 'type': 'timer'}
                if len(timer_ids) == 1:
                    combined_meta['timer_id'] = timer_ids[0]
                elif timer_ids:
                    combined_meta['timer_ids'] = timer_ids
            elif kind == 'task':
                # Mologate: a monitor's (or another task's) result; several that came together share a turn.
                if len(batch) == 1:
                    combined_meta = {key: value for key, value in first_meta.items() if key != 'delivery'}
                else:
                    combined_meta = {
                        'internal': True,
                        'type': 'task',
                        'task_ids': [(m.get('meta') or {}).get('task_id') for m in batch],
                        'handles': [(m.get('meta') or {}).get('handle') for m in batch],
                    }
            else:
                delegation_ids = [
                    message['meta']['delegation_id']
                    for message in batch
                    if (message.get('meta') or {}).get('delegation_id')
                ]
                subagent_chat_ids = [
                    message['meta']['subagent_chat_id']
                    for message in batch
                    if (message.get('meta') or {}).get('subagent_chat_id')
                ]
                combined_meta = {'internal': True, 'type': 'subagent'}
                if len(delegation_ids) == 1:
                    combined_meta['delegation_id'] = delegation_ids[0]
                elif delegation_ids:
                    combined_meta['delegation_ids'] = delegation_ids
                if len(subagent_chat_ids) == 1:
                    combined_meta['subagent_chat_id'] = subagent_chat_ids[0]
                elif subagent_chat_ids:
                    combined_meta['subagent_chat_ids'] = subagent_chat_ids

            # Results that came while a turn was running are marked pending (post_task_result).
            waiting = (
                kind in ('subagent', 'task') and first_meta.get('delivery' if kind == 'task' else 'status') == 'pending'
            )
            reuse_message = len(batch) == 1 and not waiting and kind != 'agent_message'
            user_message_id = first['id'] if reuse_message else str(uuid4())
            removed_ids = set()
            if not reuse_message:
                removed_ids = {message['id'] for message in batch}
                for message_id in removed_ids:
                    messages.pop(message_id, None)
                if parent_id and parent_id in messages:
                    messages[parent_id]['childrenIds'] = [
                        child_id
                        for child_id in messages[parent_id].get('childrenIds', [])
                        if child_id not in removed_ids
                    ]
                if waiting:
                    # Mologate: they hang under the answer before that turn; continue after the turn
                    # instead of branching beside it, which would hide its answer.
                    parent_id = _latest_assistant_id(messages, history.get('currentId')) or parent_id

            assistant_message_id = str(uuid4())
            message_list = get_message_list(messages, parent_id)
            system_prompt = run.get('system_prompt')
            user_message = {
                'id': user_message_id,
                'parentId': parent_id,
                'childrenIds': [assistant_message_id],
                'role': 'user',
                'content': combined_content,
                'model': model_id,
                'meta': combined_meta,
                'timestamp': int(time.time()),
            }
            assistant_message = {
                'id': assistant_message_id,
                'parentId': user_message_id,
                'childrenIds': [],
                'role': 'assistant',
                'content': '',
                'done': False,
                'model': model_id,
                'timestamp': int(time.time()),
            }

            if parent_id and parent_id in messages:
                parent_children = [
                    child_id for child_id in messages[parent_id].get('childrenIds', []) if child_id != user_message_id
                ]
                parent_children.append(user_message_id)
                messages[parent_id]['childrenIds'] = parent_children
            messages[user_message_id] = {**messages.get(user_message_id, {}), **user_message}
            messages[assistant_message_id] = assistant_message
            history['messages'] = messages
            history['currentId'] = assistant_message_id
            chat.chat = {**(chat.chat or {}), 'history': history}
            # The chat page prefers this column over history.currentId; a stale one hides the new turn.
            chat.current_message_id = assistant_message_id
            chat.updated_at = int(time.time())
            await db.commit()

        if removed_ids:
            await ChatMessages.delete_message_ids_by_chat_id(parent_chat_id, removed_ids)
        await ChatMessages.upsert_message(user_message_id, parent_chat_id, user_id, user_message)
        await ChatMessages.upsert_message(assistant_message_id, parent_chat_id, user_id, assistant_message)

        from open_webui.socket.main import sio

        await sio.emit(
            'events',
            {
                'chat_id': parent_chat_id,
                'message_id': assistant_message_id,
                'data': {'type': 'chat:reload'},
            },
            room=f'user:{user.id}',
        )

        form_data = {
            'model': model_id,
            'messages': [
                *([{'role': 'system', 'content': system_prompt}] if system_prompt else []),
                *message_list,
                {'role': 'user', 'content': combined_content},
            ],
            'stream': True,
            'chat_id': parent_chat_id,
            'id': assistant_message_id,
            'parent_id': parent_id,
            'user_message': user_message,
            'session_id': run.get('session_id') or f'{kind}-result:{parent_chat_id}',
            'background_tasks': {},
            'tool_ids': run.get('tool_ids') or [],
            'skill_ids': run.get('skill_ids') or [],
            'filter_ids': run.get('filter_ids') or [],
            'features': run.get('features') or {},
            'files': run.get('files') or [],
            'variables': run.get('variables') or {},
        }
        if run.get('terminal_id'):
            form_data['terminal_id'] = run['terminal_id']

        request = _build_request(source_request, user.id, internal=internal)
        if max_iterations:
            request.state.max_tool_call_iterations = max_iterations
        if stream_id:
            request.state.response_stream_task_id = stream_id
        if not internal:
            # A regular turn registers its task before the handler returns; holding the lock until then
            # keeps a second caller from starting another turn in the same chat meanwhile.
            await source_request.app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
            return assistant_message_id

    # An internal turn runs to its end inside the handler, so it runs outside the lock.
    await source_request.app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
    return assistant_message_id


async def post_task_result(
    app,
    *,
    parent_chat_id: str,
    user_id: str,
    content: str,
    meta: dict,
    run: dict,
    parent_message_id: str | None = None,
    model_id: str | None = None,
) -> str | None:
    """Mologate: bring a task's result back to the main chat as an internal user message under its
    latest finished answer, and start a turn for it. A busy chat keeps it waiting instead; the
    running turn starts one when it ends. ``meta`` is the message's: ``{'type': 'subagent', …}`` for
    a sub-agent, ``{'type': 'task', 'task_type', 'task_id', 'handle', 'status'}`` for other tasks.
    Returns the message id, or None when the main chat is gone."""
    redis = app.state.redis
    message_id = str(uuid4())
    message = {
        'id': message_id,
        'parentId': None,
        'childrenIds': [],
        'role': 'user',
        'content': content,
        'model': model_id or run.get('model_id'),
        'meta': {'internal': True, **meta},
        'timestamp': int(time.time()),
    }
    # A task result's ``status`` is the task's own, so its waiting mark goes under ``delivery``.
    waiting_key = 'delivery' if meta.get('type') == 'task' else 'status'

    lock = _parent_locks.setdefault(parent_chat_id, asyncio.Lock())
    async with lock:
        async with get_async_db() as db:
            stmt = select(Chat).where(Chat.id == parent_chat_id, Chat.user_id == user_id)
            if db.bind.dialect.name == 'postgresql':
                stmt = stmt.with_for_update()
            parent = (await db.execute(stmt)).scalar_one_or_none()
            if not parent:
                return None

            updated_chat = copy.deepcopy(parent.chat or {})
            updated_history = updated_chat.setdefault('history', {})
            updated_messages = updated_history.setdefault('messages', {})
            done_assistants = [
                message
                for message in updated_messages.values()
                if message.get('role') == 'assistant' and message.get('done') is not False
            ]
            result_parent_id = (
                max(done_assistants, key=lambda message: message.get('timestamp', 0)).get('id')
                if done_assistants
                else parent_message_id
            )
            message['parentId'] = result_parent_id
            if await has_active_tasks(redis, parent_chat_id):
                message['meta'][waiting_key] = 'pending'
            updated_messages[message_id] = message
            if result_parent_id and result_parent_id in updated_messages:
                children = updated_messages[result_parent_id].setdefault('childrenIds', [])
                if message_id not in children:
                    children.append(message_id)
            updated_history['messages'] = updated_messages
            parent.chat = {**(parent.chat or {}), **updated_chat, 'history': updated_history}
            parent.updated_at = int(time.time())
            await db.commit()

        await ChatMessages.upsert_message(
            message_id=message_id,
            chat_id=parent_chat_id,
            user_id=user_id,
            data=message,
        )

    if message['meta'].get(waiting_key) == 'pending':
        from open_webui.socket.main import sio

        await sio.emit(
            'events',
            {
                'chat_id': parent_chat_id,
                'message_id': message_id,
                'data': {'type': 'chat:reload'},
            },
            room=f'user:{user_id}',
        )
    if not await has_active_tasks(redis, parent_chat_id):
        await process_pending_internal_messages(_app_request(app), parent_chat_id, user_id, run)
    return message_id


async def delegate(
    task: str,
    context: str,
    background: bool,
    *,
    file_ids: list[str] | None = None,
    profile: str | None = None,
    request: Request,
    user_data: dict,
    metadata: dict,
    parent_chat_id: str,
    parent_message_id: str | None,
) -> str:
    global _foreground_semaphore

    task = task.strip()
    if not task:
        return 'Error: task must not be empty.'
    if not parent_chat_id or not user_data.get('id'):
        return 'Error: chat and user context are required.'

    config = await Config.get_many(
        'subagents.background_enabled',
        'subagents.max_concurrent',
        'subagents.max_async',
        'subagents.max_iterations',
        'subagents.max_output',
        'subagents.system_prompt',
        'subagents.profiles',
    )
    max_concurrent = int(config.get('subagents.max_concurrent') or 20)
    max_async = int(config.get('subagents.max_async') or 20)
    max_iterations = int(config.get('subagents.max_iterations') or 30)
    max_output = int(config.get('subagents.max_output') or 30_000)
    if max_concurrent != -1:
        max_concurrent = max(1, max_concurrent)
    if max_async != -1:
        max_async = max(1, max_async)

    if background and not config.get('subagents.background_enabled'):
        return 'Error: background sub-agents are disabled in settings.'

    features = copy.deepcopy(metadata.get('features') or {})
    if (
        background
        and features.get('code_interpreter')
        and await Config.get('code_interpreter.engine', 'pyodide') != 'jupyter'
    ):
        features.pop('code_interpreter')
    run = {
        'model_id': metadata.get('model_id') or (metadata.get('model') or {}).get('id'),
        'session_id': metadata.get('session_id'),
        'tool_ids': copy.deepcopy(metadata.get('tool_ids') or []),
        'skill_ids': copy.deepcopy(metadata.get('skill_ids') or []),
        'system_prompt': metadata.get('system_prompt'),
        'tool_servers': [] if background else copy.deepcopy(metadata.get('tool_servers') or []),
        'filter_ids': copy.deepcopy(metadata.get('filter_ids') or []),
        'terminal_id': metadata.get('terminal_id'),
        'features': features,
        'files': copy.deepcopy(metadata.get('files') or []),
        'variables': copy.deepcopy(metadata.get('variables') or {}),
        'direct': bool(metadata.get('direct')),
    }
    if not run.get('model_id'):
        return 'Error: model context is required.'
    if run.get('direct'):
        return 'Error: sub-agents are unavailable for direct connections.'
    if file_ids:
        requested_file_ids = {str(file_id) for file_id in file_ids if file_id}
        run['files'] = [
            copy.deepcopy(file)
            for file in metadata.get('files') or []
            if str(file.get('id') or '') in requested_file_ids
            or str(file.get('url') or '') in requested_file_ids
            or (isinstance(file.get('file'), dict) and str(file.get('file', {}).get('id') or '') in requested_file_ids)
        ]
        found_file_ids = {
            str(value)
            for file in run['files']
            for value in (
                file.get('id'),
                file.get('url'),
                file.get('file', {}).get('id') if isinstance(file.get('file'), dict) else None,
            )
            if value
        }
        missing_file_ids = sorted(requested_file_ids - found_file_ids)
        if missing_file_ids:
            return f'Error: file_ids not attached or unavailable: {", ".join(missing_file_ids)}'
    else:
        run['files'] = []

    # Mologate: the profile's role instructions, and its model when it names one that's available.
    agent_profile = resolve_profile(profile, config.get('subagents.profiles'))
    if not agent_profile:
        names = ', '.join(p['name'] for p in get_profiles(config.get('subagents.profiles')))
        return f'Error: unknown profile "{profile}". Available profiles: {names}.'
    if agent_profile.get('model'):
        profile_model_id = find_model_id(agent_profile['model'], request.app.state.MODELS)
        if profile_model_id:
            run['model_id'] = profile_model_id

    delegation_id = agent_tasks.new_handle(agent_tasks.HANDLE_PREFIXES['subagent'])
    subagent_system_prompt = '\n\n'.join(
        part
        for part in (
            run.get('system_prompt') or '',
            str(config.get('subagents.system_prompt') or '').strip() or DEFAULT_SUBAGENT_SYSTEM_PROMPT,
            f'## Your role: {agent_profile["name"]}\n{agent_profile["prompt"]}',
            AGENT_MESSAGING_PROMPT.format(agent_id=delegation_id),
        )
        if part
    )
    user = UserModel(**user_data)

    # Mologate: the task limits (utils/task_limits.py). A chat runs a few sub-agents at once and a member
    # a few in the background; each run has a time limit, and a long foreground one moves to the background.
    limits = await task_limits.get_all()
    per_chat = limits['subagents_per_chat']
    per_user = limits['background_tasks_per_user']
    run_seconds = limits['background_run_seconds']
    auto_background = limits['auto_background_seconds'] if config.get('subagents.background_enabled') else 0

    # What this sub-agent holds, given back once when its run ends or it fails to start.
    slots = {'chat': False, 'background': False, 'semaphore': None, 'released': False}

    async def release_slots():
        async with _background_lock:
            if slots['released']:
                return
            slots['released'] = True
            if slots['chat']:
                _discard(_chat_running, parent_chat_id, delegation_id)
            if slots['background']:
                _background_active.discard(delegation_id)
                _discard(_user_background, user.id, delegation_id)
            if slots['semaphore']:
                slots['semaphore'].release()

    if background:
        async with _background_lock:
            running = _count(_chat_running, parent_chat_id)
            if running >= per_chat:
                return (
                    f'Error: {running} sub-agents are already running in this chat, the most it runs at once. '
                    'Wait for one to finish (task_status shows them), or run this one in the foreground.'
                )
            refusal = _background_refusal(user.id, max_async=max_async, per_user=per_user)
            if refusal:
                return refusal
            _chat_running.setdefault(parent_chat_id, set()).add(delegation_id)
            _background_active.add(delegation_id)
            _user_background.setdefault(user.id, set()).add(delegation_id)
            slots.update(chat=True, background=True)
    else:
        # A foreground call over the chat's limit waits for a slot.
        while True:
            async with _background_lock:
                if _count(_chat_running, parent_chat_id) < per_chat:
                    _chat_running.setdefault(parent_chat_id, set()).add(delegation_id)
                    slots['chat'] = True
                    break
            await asyncio.sleep(0.5)
        if max_concurrent != -1:
            if _foreground_semaphore is None:
                _foreground_semaphore = asyncio.Semaphore(max_concurrent)
            try:
                await _foreground_semaphore.acquire()
            except asyncio.CancelledError:
                await release_slots()
                raise
            slots['semaphore'] = _foreground_semaphore

    mode = 'background' if background else 'foreground'
    try:
        chat_id = str(uuid4())
        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())
        prompt = f'{task}\n\n## Context\n{context}' if context else task
        prompt_files = copy.deepcopy(run.get('files') or [])
        user_message = {
            'id': user_message_id,
            'parentId': None,
            'childrenIds': [assistant_message_id],
            'role': 'user',
            'content': prompt,
            'timestamp': int(time.time()),
            'models': [run['model_id']],
            **({'files': prompt_files} if prompt_files else {}),
        }
        chat = await Chats.insert_new_chat(
            chat_id,
            user.id,
            ChatForm(
                chat={
                    'id': chat_id,
                    'title': f'Sub-agent: {task[:60]}',
                    'models': [run['model_id']],
                    'history': {
                        'currentId': assistant_message_id,
                        'messages': {
                            user_message_id: user_message,
                            assistant_message_id: {
                                'id': assistant_message_id,
                                'parentId': user_message_id,
                                'childrenIds': [],
                                'role': 'assistant',
                                'content': '',
                                'done': False,
                                'model': run['model_id'],
                                'timestamp': int(time.time()),
                            },
                        },
                    },
                    'messages': [
                        {
                            'role': 'user',
                            'content': prompt,
                            **({'files': prompt_files} if prompt_files else {}),
                        }
                    ],
                    'files': prompt_files,
                }
            ),
            internal_meta={
                'internal': True,
                'type': 'subagent',
                'parent_chat_id': parent_chat_id,
                'parent_message_id': parent_message_id,
                'delegation_id': delegation_id,
                'mode': mode,
                # Mologate: shown when the conversation is opened, and used to wake the sub-agent
                # when the main agent messages it after it finished (utils/agent_messages.py).
                'profile': agent_profile['name'],
                'task': task[:2000],
                'model_id': run['model_id'],
                'run': {**run, 'system_prompt': subagent_system_prompt, 'session_id': f'subagent:{chat_id}'},
                'parent_run': run,
                'wakes': 0,
                # Mologate: what the Tasks panel shows (utils/agent_tasks.py).
                'task_id': delegation_id,
                'title': task[: agent_tasks.TITLE_CHARS],
                'status': 'running',
                'started_at': int(time.time()),
            },
        )
        if not chat:
            raise RuntimeError('Failed to create sub-agent chat')
    except Exception as exc:
        await release_slots()
        prefix = 'background ' if background else ''
        return f'Error: failed to create {prefix}sub-agent: {exc}'

    async def emit(status: str):
        await agent_tasks.emit_task_changed(
            user.id, parent_chat_id, chat_id, task_type='subagent', status=status, handle=delegation_id
        )

    async def finish(status: str, error: str | None = None, summary: str = ''):
        """Record how the run ended, unless it was stopped from the Tasks panel or with cancel_task."""

        def change(meta: dict):
            if meta.get('status') in agent_tasks.FINISHED:
                return False
            meta.update(
                status=status,
                finished_at=int(time.time()),
                error=error,
                result_preview=agent_tasks.preview(summary),
            )
            return True

        try:
            if await agent_tasks.update_task_meta(chat_id, change):
                await emit(status)
        except Exception:
            log.exception('Could not record how sub-agent %s ended', delegation_id)

    await emit('running')
    # Mologate: the run's live output is kept under its task's id, so the Tasks panel and the
    # sub-agent's conversation can show it while it runs (tasks.get_response_streams_by_chat_id).
    stream_id = str(uuid4())
    run_limit_error = f'Stopped at the {agent_tasks.describe_seconds(run_seconds)} run-time limit'

    async def run_reserved() -> dict:
        try:
            child_request = _build_request(request, user.id, internal=True)
            child_request.state.max_tool_call_iterations = max_iterations
            child_request.state.response_stream_task_id = stream_id
            form_data = {
                'model': run['model_id'],
                'messages': [
                    {'role': 'system', 'content': subagent_system_prompt},
                    {'role': 'user', 'content': prompt},
                ],
                'stream': True,
                'chat_id': chat_id,
                'id': assistant_message_id,
                'parent_id': None,
                'user_message': user_message,
                'session_id': run.get('session_id') or f'subagent:{chat_id}',
                'background_tasks': {},
                'tool_ids': run.get('tool_ids') or [],
                'skill_ids': run.get('skill_ids') or [],
                'filter_ids': run.get('filter_ids') or [],
                'features': run.get('features') or {},
                'files': run.get('files') or [],
                'variables': run.get('variables') or {},
            }
            if run.get('terminal_id'):
                form_data['terminal_id'] = run['terminal_id']
            if run.get('tool_servers'):
                form_data['tool_servers'] = run['tool_servers']
            try:
                await asyncio.wait_for(
                    request.app.state.CHAT_COMPLETION_HANDLER(child_request, form_data, user=user),
                    timeout=run_seconds,
                )
            except TimeoutError:
                # Mologate: the run-time limit; what it wrote so far stays as partial output.
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat_id,
                    assistant_message_id,
                    {'done': True, 'error': {'content': run_limit_error}},
                )
            message = await Chats.get_message_by_id_and_message_id(chat_id, assistant_message_id)
            if not message:
                error = 'Sub-agent chat or completion message no longer exists.'
                await finish('failed', error)
                return {'status': 'error', 'summary': '', 'error': error}

            summary = message.get('content') or ''
            if isinstance(summary, list):
                summary = ''.join(
                    str(item.get('text', ''))
                    for item in summary
                    if isinstance(item, dict) and item.get('type') == 'text'
                )
            if not summary:
                summary = ''.join(
                    str(part.get('text', ''))
                    for item in message.get('output') or []
                    if item.get('type') == 'message'
                    for part in item.get('content') or []
                    if part.get('type') == 'output_text'
                )
            if len(summary) > max_output:
                summary = f'{summary[:max_output]}\n\n[output truncated]'
            error = agent_tasks.error_text(message.get('error'))
            # Mologate: messages that reached it after its last step get a turn of their own.
            from open_webui.utils.agent_messages import schedule_leftover_messages

            schedule_leftover_messages(request, user, chat_id)
            await finish('failed' if error else 'completed', error, summary)
            return {
                'status': 'error' if error else 'completed',
                'summary': summary or ('Sub-agent produced no output.' if not error else ''),
                'error': error,
            }
        except asyncio.CancelledError:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id,
                assistant_message_id,
                {'done': True, 'error': {'content': 'Sub-agent cancelled.'}},
            )
            await finish('cancelled')
            raise
        except Exception as exc:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id,
                assistant_message_id,
                {'done': True, 'error': {'content': str(exc)}},
            )
            await finish('failed', str(exc))
            raise
        finally:
            await release_slots()

    def result_text(result: dict, duration: float) -> str:
        lines = [
            f'[ASYNC SUBAGENT COMPLETE - {delegation_id}]',
            f'Task: {shorten(task, 600)}',
            f'Agent: {delegation_id} ({agent_profile["name"]}) · chat {chat_id}',
            f'Status: {result.get("status", "completed")}   Duration: {duration:.1f}s',
            'Use the result if relevant; no need to repeat this notification to the user.',
            '--- RESULT ---',
        ]
        if result.get('status') == 'completed':
            lines.append(result.get('summary') or 'Subagent completed without a final summary.')
        elif result.get('status') == 'interrupted':
            lines.append('The subagent was interrupted before completing.')
            if result.get('summary'):
                lines.extend(['Partial output:', result['summary']])
        else:
            detail = f' {result.get("error")}' if result.get('error') else ''
            lines.append(f'The subagent did not complete successfully.{detail}')
            if result.get('summary'):
                lines.extend(['Partial output:', result['summary']])
        return shorten('\n'.join(lines), 5000)

    async def deliver_when_done(child_task: asyncio.Task, started_at: float) -> None:
        """Post a background sub-agent's result to the main chat once its run ends."""
        await asyncio.wait({child_task})
        if child_task.cancelled():
            meta = await agent_tasks.get_task_meta(chat_id)
            if meta is None or meta.get('cancelled_by'):
                # Mologate: stopped from the Tasks panel or with cancel_task, so there's nothing to report.
                return
            result = {'status': 'interrupted', 'summary': '', 'error': 'cancelled'}
        elif child_task.exception() is not None:
            result = {'status': 'error', 'summary': '', 'error': str(child_task.exception())}
        else:
            result = child_task.result()
        try:
            await post_task_result(
                request.app,
                parent_chat_id=parent_chat_id,
                user_id=user.id,
                content=result_text(result, time.time() - started_at),
                meta={'type': 'subagent', 'delegation_id': delegation_id, 'subagent_chat_id': chat_id},
                run=run,
                parent_message_id=parent_message_id,
                model_id=run['model_id'],
            )
        except Exception:
            log.exception('Could not post the result of sub-agent %s', delegation_id)

    def deliver_in_background(child_task: asyncio.Task, started_at: float) -> None:
        delivery = asyncio.create_task(deliver_when_done(child_task, started_at))
        _deliveries.add(delivery)
        delivery.add_done_callback(_deliveries.discard)

    started_at = time.time()
    try:
        _, child_task = await create_task(
            request.app.state.redis,
            run_reserved(),
            id=chat_id,
            task_id=stream_id,
        )
    except Exception as exc:
        await release_slots()
        await finish('failed', str(exc))
        return f'Error: {exc}'

    if background:
        deliver_in_background(child_task, started_at)
        return JSONCodec.dumps(
            {
                'status': 'dispatched',
                'task_id': delegation_id,
                'delegation_id': delegation_id,
                'agent_id': delegation_id,
                'profile': agent_profile['name'],
                'subagent_chat_id': chat_id,
                'mode': 'background',
                'task': task,
                'note': f'Message it with send_message(to="{delegation_id}").',
            },
            ensure_ascii=False,
        )

    async def move_to_background() -> str | None:
        """Mologate: hand a long foreground sub-agent to the background, when the member has room."""
        async with _background_lock:
            if slots['released'] or child_task.done():
                return None
            if _background_refusal(user.id, max_async=max_async, per_user=per_user):
                return None
            _background_active.add(delegation_id)
            _user_background.setdefault(user.id, set()).add(delegation_id)
            slots['background'] = True
            # It counts as background work now, so it no longer holds a foreground slot.
            if slots['semaphore']:
                slots['semaphore'].release()
                slots['semaphore'] = None
        deliver_in_background(child_task, started_at)
        moved_at = int(time.time())
        try:
            await agent_tasks.update_task_meta(
                chat_id, lambda meta: meta.update(mode='background', moved_to_background_at=moved_at)
            )
        except Exception:
            log.exception('Could not mark sub-agent %s as moved to the background', delegation_id)
        await emit('running')
        elapsed = int(time.time() - started_at)
        return JSONCodec.dumps(
            {
                'status': 'moved_to_background',
                'task_id': delegation_id,
                'agent_id': delegation_id,
                'profile': agent_profile['name'],
                'subagent_chat_id': chat_id,
                'mode': 'background',
                'elapsed_seconds': elapsed,
                'task': task,
                'note': (
                    f'Still working after {agent_tasks.describe_seconds(elapsed)}, so it moved to the background. '
                    'Its result comes back to this chat as a message when it finishes. task_status shows '
                    'progress, send_message reaches it, cancel_task stops it.'
                ),
            },
            ensure_ascii=False,
        )

    # Mologate: wait for a while, then move to the background; without room there, keep waiting.
    timeout = auto_background or None
    while True:
        try:
            done, _ = await asyncio.wait({child_task}, timeout=timeout)
            moved = None if done else await move_to_background()
        except asyncio.CancelledError:
            # Stopping the reply stops its foreground sub-agents; one already moved keeps going.
            if not slots['background']:
                child_task.cancel()
            raise
        if done:
            break
        if moved:
            return moved
        timeout = 30

    if child_task.cancelled():
        cancelled_by = ((await agent_tasks.get_task_meta(chat_id)) or {}).get('cancelled_by')
        if cancelled_by == 'user':
            return 'Error: the user stopped this sub-agent.'
        if cancelled_by == 'agent':
            return 'Error: this sub-agent was stopped with cancel_task.'
        return 'Error: sub-agent was cancelled.'
    if child_task.exception() is not None:
        return f'Error: {child_task.exception()}'

    result = child_task.result()
    if result.get('status') != 'completed':
        return f'Error: {result.get("error") or "sub-agent failed."}'
    # Mologate: the agent's id lets the main agent message it later; the chat id lets the UI link to it.
    return (
        f'[Sub-agent {delegation_id} ({agent_profile["name"]}) finished · chat {chat_id}]\n\n'
        f'{result.get("summary") or "Sub-agent produced no output."}'
    )
