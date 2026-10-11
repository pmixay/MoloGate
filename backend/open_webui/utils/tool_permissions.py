"""Permission prompts: tool calls are approved automatically, and only critical or very dangerous
ones stop to ask the user.

Before a batch of tool calls runs, a reviewer model reads it with low sensitivity and flags only
actions that could do severe, hard-to-reverse harm outside the chat's sandbox. Flagged calls pause
the turn with an Allow / Always allow in this chat / Deny prompt; everything else runs at once.
Choosing "Always allow in this chat" stores ``tool_approval_always_allow`` in the chat's params,
and from then on nothing in that chat is reviewed or prompted.

The reviewer is ``codex-auto-approve``, falling back to GLM-5.3 Flash; admins can change both
(``chat.tool_permissions.model`` / ``.fallback_model``). Names are matched loosely, so
"GLM-5.3 Flash" finds ``z-ai/glm-5.3-flash``. When neither model is available or both fail, a
short list of rules still catches the most destructive commands; anything else is allowed, since
auto-approve is the default.

Read-only built-in tools are never reviewed. Chats nobody is watching (automations, channels,
internal sub-agent runs) are not reviewed either: there is no one to ask.
"""

import asyncio
import logging
import re
from typing import Any

from fastapi.responses import JSONResponse
from open_webui.models.config import Config
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.model_ids import find_model_id

log = logging.getLogger(__name__)

ALWAYS_ALLOW_PARAM = 'tool_approval_always_allow'
REVIEW_PARAM = 'tool_review'

DEFAULT_REVIEW_MODEL = 'codex-auto-approve'
DEFAULT_FALLBACK_MODEL = 'glm-5.3-flash'

REVIEW_TIMEOUT_SECONDS = 30
MAX_ARGUMENTS_CHARS = 4000
MAX_INTENT_CHARS = 2000

# Built-in tools that only read, search or produce harmless output.
READ_ONLY_TOOLS = {
    'get_current_timestamp',
    'calculate_timestamp',
    'search_web',
    'fetch_url',
    'generate_image',
    'edit_image',
    'list_memory_paths',
    'read_memory_path',
    'search_memories',
    'list_memories',
    'search_notes',
    'view_note',
    'search_chats',
    'view_chat',
    'search_channels',
    'search_channel_messages',
    'view_channel_message',
    'view_channel_thread',
    'list_knowledge_bases',
    'search_knowledge_bases',
    'search_knowledge_files',
    'list_chat_files',
    'grep_chat_files',
    'query_chat_files',
    'grep_knowledge_files',
    'view_file',
    'view_knowledge_file',
    'list_knowledge',
    'query_knowledge_files',
    'query_knowledge_bases',
    'view_skill',
    'list_automations',
    'search_calendar_events',
    'create_tasks',
    'update_task',
    'timer',
    'list_agents',
    'send_message',
}

# The safety net when no reviewer answers: only unmistakably destructive commands.
DANGEROUS_PATTERNS = [
    (
        re.compile(
            r'\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z-]+\s+)*(/|/\*|~/?|\$HOME/?)(\s|$|["\'])'
        ),
        'Deletes the whole filesystem or home directory.',
    ),
    (re.compile(r'--no-preserve-root'), 'Deletes the whole filesystem.'),
    (re.compile(r'\bmkfs(\.\w+)?\b|\bwipefs\b|\bdd\b[^\n]*\bof=/dev/'), 'Overwrites a disk.'),
    (re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'), 'Starts a fork bomb.'),
    (
        re.compile(r'\bgit\s+push\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b|\s\+\S)'),
        'Force-pushes, which can overwrite history on the remote.',
    ),
    (re.compile(r'\bgit\s+push\b[^\n]*(--delete\b|\s:\S)'), 'Deletes a branch or tag on the remote.'),
    (re.compile(r'\b(drop\s+(database|schema)|truncate\s+table)\b', re.IGNORECASE), 'Drops or empties a database.'),
    (
        re.compile(r'\bterraform\s+destroy\b|\bkubectl\s+delete\b|\bhelm\s+(uninstall|delete)\b'),
        'Tears down infrastructure.',
    ),
]

REVIEW_PROMPT = """You review actions an AI agent in Mologate, a team's AI workspace, is about to take.
Each chat has its own sandbox container. Approve by default: sensitivity is LOW.

Flag an action ("ask") ONLY when it is critical or very dangerous: it could cause severe,
hard-to-reverse harm outside the chat's sandbox. For example:
- deleting or overwriting important data, databases, repositories or backups outside the sandbox;
- force-pushing, deleting remote branches or tags, rewriting shared history;
- destroying or reconfiguring production systems or cloud infrastructure;
- spending money or making purchases;
- sending emails or messages to people, or publishing content publicly;
- changing access permissions, accounts or credentials, or exposing secrets.

Everything else is allowed ("allow") without asking, including reading and searching, browsing the
web, running code and tests, installing packages, and creating, editing or deleting files inside
the sandbox. When in doubt, allow.

What the user asked for:
<request>
{intent}
</request>

Actions the agent is about to take:
{actions}

Reply with JSON only, no prose. Give a reason only for "ask": one short sentence for the user on
what could go wrong.
{{"actions": [{{"index": 1, "decision": "allow"}}, {{"index": 2, "decision": "ask", "reason": "..."}}]}}"""


def is_always_allowed(params: dict | None) -> bool:
    return bool((params or {}).get(ALWAYS_ALLOW_PARAM))


async def chat_always_allows(chat_id: str) -> bool:
    """The saved chat's choice, for when the request's params are older than it."""
    from open_webui.models.chats import Chats

    chat = await Chats.get_chat_by_id(chat_id)
    return bool(chat) and is_always_allowed((chat.chat or {}).get('params'))


def _tool_call_parts(tool_call: dict) -> tuple[str, str]:
    function = tool_call.get('function') or {}
    name = function.get('name') or tool_call.get('name') or ''
    arguments = function.get('arguments') if function else tool_call.get('arguments')
    if not isinstance(arguments, str):
        try:
            arguments = JSONCodec.dumps(arguments or {}, ensure_ascii=False)
        except Exception:
            arguments = str(arguments)
    return name, arguments


def flag_by_rules(name: str, arguments: str) -> str | None:
    """A reason when the call is unmistakably destructive, else None."""
    text = f'{name} {arguments}'.replace('\\n', '\n')
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def _response_text(response: Any) -> str:
    if isinstance(response, list) and len(response) == 1:
        response = response[0]
    if isinstance(response, JSONResponse):
        try:
            response = JSONCodec.loads(response.body.decode('utf-8', 'replace'))
        except Exception:
            return ''
    if not isinstance(response, dict):
        return ''

    choices = response.get('choices') or []
    if choices:
        message = choices[0].get('message') or {}
        return message.get('content') or ''

    parts = []
    for item in response.get('output') or []:
        for content in item.get('content') or []:
            if isinstance(content, dict):
                parts.append(content.get('text') or '')
    return '\n'.join(part for part in parts if part)


def parse_review(text: str, count: int) -> dict[int, str | None] | None:
    """{index: reason} for flagged actions (1-based), or None when the reply isn't usable."""
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        data = JSONCodec.loads(text[start : end + 1])
    except Exception:
        return None

    actions = data.get('actions') if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return None

    flagged = {}
    for action in actions:
        if not isinstance(action, dict):
            continue
        try:
            index = int(action.get('index'))
        except (TypeError, ValueError):
            continue
        if 1 <= index <= count and str(action.get('decision', '')).lower() == 'ask':
            reason = action.get('reason')
            flagged[index] = reason.strip()[:300] if isinstance(reason, str) and reason.strip() else None
    return flagged


async def _ask_reviewer(request, user, model_id: str, prompt: str) -> str:
    from open_webui.utils.chat import generate_chat_completion

    payload = {
        'model': model_id,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': 'tool_permission_review',
        },
    }
    # The reviewer is a system check, not a model the user picked, so model access rules don't apply.
    response = await asyncio.wait_for(
        generate_chat_completion(request, form_data=payload, user=user, bypass_filter=True),
        timeout=REVIEW_TIMEOUT_SECONDS,
    )
    return _response_text(response)


async def get_reviewer_model_ids(models) -> list[str]:
    config = await Config.get_many('chat.tool_permissions.model', 'chat.tool_permissions.fallback_model')
    names = [
        config.get('chat.tool_permissions.model') or DEFAULT_REVIEW_MODEL,
        config.get('chat.tool_permissions.fallback_model') or DEFAULT_FALLBACK_MODEL,
    ]
    model_ids = []
    for name in names:
        model_id = find_model_id(name, models)
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    return model_ids


async def review_tool_calls(request, user, tool_calls: list[dict], intent: str = '') -> dict[str, dict]:
    """Review a batch of tool calls. Returns {call_id: {'reason': str, 'source': str}} for the
    calls the user must confirm; an empty dict means everything runs."""
    reviewable = []
    for tool_call in tool_calls:
        name, arguments = _tool_call_parts(tool_call)
        if name == 'ask_user' or name in READ_ONLY_TOOLS:
            continue
        reviewable.append((tool_call.get('id') or tool_call.get('call_id') or '', name, arguments))
    if not reviewable:
        return {}

    models = request.app.state.MODELS
    actions = '\n'.join(
        f'{index}. tool `{name}` with arguments: {arguments[:MAX_ARGUMENTS_CHARS]}'
        for index, (_, name, arguments) in enumerate(reviewable, start=1)
    )
    prompt = REVIEW_PROMPT.format(intent=(intent or '(not available)')[:MAX_INTENT_CHARS], actions=actions)

    for model_id in await get_reviewer_model_ids(models):
        try:
            flagged = parse_review(await _ask_reviewer(request, user, model_id, prompt), len(reviewable))
        except Exception as e:
            log.warning('Permission review with %s failed: %s', model_id, e)
            continue
        if flagged is None:
            log.warning('Permission review with %s returned an unusable reply', model_id)
            continue

        return {
            reviewable[index - 1][0]: {
                'reason': reason
                or flag_by_rules(reviewable[index - 1][1], reviewable[index - 1][2])
                or 'This action could be hard to undo.',
                'source': 'reviewer',
                'model': model_id,
            }
            for index, reason in flagged.items()
        }

    # No reviewer answered: fall back to the rules alone.
    result = {}
    for call_id, name, arguments in reviewable:
        if reason := flag_by_rules(name, arguments):
            result[call_id] = {'reason': reason, 'source': 'rules'}
    return result


def mark_reviewed_tool_calls(output: list[dict], tool_calls: list[dict], flagged: dict[str, dict]) -> None:
    """Record the review on the batch's function_call items: flagged ones carry the reason shown
    in the prompt, the rest are approved and run once the flagged ones are answered."""
    batch_ids = {tool_call.get('id') or tool_call.get('call_id') for tool_call in tool_calls}
    for item in output:
        call_id = item.get('call_id') or item.get('id')
        if item.get('type') != 'function_call' or call_id not in batch_ids or item.get('name') == 'ask_user':
            continue
        if call_id in flagged:
            item['permission'] = flagged[call_id]
            item.pop('approved', None)
        else:
            item['approved'] = True
