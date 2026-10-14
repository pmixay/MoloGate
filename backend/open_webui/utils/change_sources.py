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

import logging
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
