"""Mologate: the chat's tasks, as the Tasks panel and the agent's task tools see them.

A task is work an agent started that runs apart from its reply: a sub-agent (foreground or in the
background), a monitor waiting for a condition, or a scheduled prompt. Each one is an internal
child chat of the main chat (``chat.meta.internal`` with ``parent_chat_id``), with ``meta.type``
``'subagent'``, ``'monitor'`` or ``'timer'``; this module normalizes them into one shape.

Not to be confused with ``open_webui/tasks.py`` (the running asyncio tasks) or the chat's
checklist (``create_tasks``/``update_task``).
"""

import logging
import uuid

log = logging.getLogger(__name__)

# The ``type`` a task has in the API, and the ``meta.type`` of its row.
TASK_TYPES = {'subagent': 'subagent', 'monitor': 'monitor', 'scheduled': 'timer'}
# Short handles the agent uses: deleg_… (sub-agents' delegation id), mon_…, sched_….
HANDLE_PREFIXES = {'subagent': 'deleg', 'monitor': 'mon', 'scheduled': 'sched'}

ACTIVE = {'running', 'scheduled'}
TERMINAL = {'completed', 'failed', 'cancelled'}
STATUSES = ACTIVE | TERMINAL


def new_handle(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:8]}'


async def emit_task_changed(
    user_id: str,
    chat_id: str,
    task_id: str,
    *,
    task_type: str,
    status: str,
    handle: str | None = None,
    notify: dict | None = None,
) -> None:
    """Tell the chat owner's open tabs a task changed; the Tasks panel refetches on it.

    ``notify`` ({title, body}) also shows a toast, e.g. when a schedule turns itself off.
    """
    from open_webui.socket.main import sio

    try:
        await sio.emit(
            'events',
            {
                'chat_id': chat_id,
                'message_id': None,
                'data': {
                    'type': 'chat:task',
                    'data': {
                        'task_id': task_id,
                        'handle': handle,
                        'task_type': task_type,
                        'status': status,
                        'notify': notify,
                    },
                },
            },
            room=f'user:{user_id}',
        )
    except Exception as e:
        log.debug(f'Could not send the task update for {task_id}: {e}')
