"""Mologate: what a response changed, from the source that knows (docs/container-nodes.md).

Every finished response is saved with one change record, `changes` on the message, whatever
produced it:

    {
        'source': 'session' | 'git',
        'files': [{'path', 'status': 'added' | 'modified' | 'deleted', 'added', 'removed',
                   'diff'?: unified diff, 'diff_truncated'?: bool}],
        'total', 'added', 'removed', 'truncated',
        'workspace'?: {'base', 'branch', 'files', 'total', 'added', 'removed'},
    }

`files` is what this response changed. `workspace`, which only a repository source fills in, is
everything not yet committed against the linked branch's base: what a commit or pull request
would hold. The chat shows each response's files under it, and the bar above the message box, with
`workspace`, only in a chat linked to a repository.

Sources:

- 'session': the chat container's files, recorded when the response starts and compared when it
  ends (`containers.start_change_tracking` / `collect_changes`). Every chat with a container.
- 'git' (not built yet): a GitHub repository linked when the chat is created and cloned into its
  container (ROADMAP.md, GitHub). The link lives on the chat as `meta.repository` (see
  `repository()`); a source registered under 'git' takes over those chats and fills in `workspace`
  from `git diff` against the base.
"""

import asyncio
import hashlib
import logging
import time
import re
from typing import Awaitable, Callable

from open_webui.models.chats import Chats
from open_webui.utils import containers

log = logging.getLogger(__name__)

# A source turns a finished response's metadata (and its chat's repository link, when it has one)
# into a change record, or None when nothing changed.
Collect = Callable[[dict, dict | None], Awaitable[dict | None]]

REPO_NAME = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
REF = re.compile(r'^[A-Za-z0-9._/-]{1,200}$')


async def _session(metadata: dict, repository: dict | None) -> dict | None:
    return await containers.collect_changes(metadata)


SOURCES: dict[str, Collect] = {'session': _session}


def register(name: str, collect: Collect) -> None:
    """Add a source, such as 'git' once chats can link a repository."""
    SOURCES[name] = collect


def repository(meta: dict | None) -> dict | None:
    """A chat's linked repository, `meta.repository`, when it is a valid one:
    {'provider': 'github', 'name': 'owner/repo', 'branch': ..., 'base': ..., 'path': ...}."""
    link = (meta or {}).get('repository')
    if not isinstance(link, dict) or link.get('provider') != 'github':
        return None
    if not REPO_NAME.match(str(link.get('name') or '')):
        return None
    if any(link.get(key) is not None and not REF.match(str(link[key])) for key in ('branch', 'base')):
        return None
    return link


async def collect(metadata: dict) -> dict | None:
    """The change record for a finished response, from the chat's repository when one is linked and
    a 'git' source exists, else from the container session."""
    link = None
    if 'git' in SOURCES and metadata.get('chat_id'):
        chat = await Chats.get_chat_by_id(metadata['chat_id'])
        link = repository(chat.meta if chat else None)
    name = 'git' if link else 'session'
    try:
        record = await SOURCES[name](metadata, link)
    except Exception as e:
        log.warning('Could not collect the changes from %s: %s', name, e)
        return None
    return {**record, 'source': name} if record else None


async def publish_live_changes(metadata: dict, event_emitter, *, force: bool = False) -> None:
    """Refresh the response's verified file changes after tools, without scanning on every token.

    This is best effort: a broken/offline node must never turn a successful tool into an error.
    Repeated records are deduplicated and a reverted workspace clears the previous live record.
    """
    if not event_emitter or not metadata.get('container_snapshot'):
        return
    now = time.monotonic()
    if not force and now - metadata.get('_changes_checked_at', 0) < 2:
        return
    metadata['_changes_checked_at'] = now
    try:
        record = await asyncio.wait_for(collect(metadata), timeout=5)
        # None also means tracking failed; do not clear verified changes on an unknown result.
        if record is None:
            return
        from open_webui.utils.json_codec import JSONCodec

        signature = hashlib.sha256(JSONCodec.dumps(record, sort_keys=True).encode()).hexdigest()
        saved = not (metadata.get('chat_id') and metadata.get('message_id'))
        if not saved and signature != metadata.get('_changes_saved_signature'):
            result = await Chats.upsert_message_to_chat_by_id_and_message_id(
                metadata['chat_id'], metadata['message_id'], {'changes': record}, touch=False
            )
            if result is not None:
                metadata['_changes_saved_signature'] = signature
        if signature != metadata.get('_changes_signature'):
            await event_emitter({'type': 'chat:message:changes', 'data': {'changes': record}})
            metadata['_changes_signature'] = signature

    except Exception as error:
        log.info('Could not publish live file changes: %s', error)
