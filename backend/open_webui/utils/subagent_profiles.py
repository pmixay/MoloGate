"""Sub-agent profiles: preset roles the main agent picks when it delegates (``delegate_task``).

A profile adds role instructions on top of the sub-agent system prompt, and may run the sub-agent
on a different model. Mologate ships the built-in profiles below; admins add their own under
Settings → Sub-agents (``subagents.profiles``). Profiles shape behavior through instructions: a
sub-agent keeps the tools of the chat that started it.
"""

import re

DEFAULT_PROFILE = 'general-purpose'

BUILTIN_PROFILES = [
    {
        'name': 'general-purpose',
        'description': 'Any multi-step task: research, code changes, running commands. The default.',
        'prompt': (
            'You are a general-purpose sub-agent. Work the task through from start to finish using any tool '
            'you need, and check your own work before you report.'
        ),
    },
    {
        'name': 'explore',
        'description': 'Investigates and reports without changing anything: finds files, reads code and docs, '
        'searches the web.',
        'prompt': (
            'You are an exploration sub-agent. Find and read what the task needs, then report what you found, '
            'with exact file paths, names and sources. Do not create, edit or delete files, and do not run '
            'commands that change anything.'
        ),
    },
    {
        'name': 'plan',
        'description': 'Designs an implementation plan: steps, files to change, risks. Changes nothing.',
        'prompt': (
            'You are a planning sub-agent. Study what exists, then return a concrete, step-by-step plan: the '
            'files to change, the approach for each, the order, and the risks or open questions. Do not make '
            'the changes yourself.'
        ),
    },
    {
        'name': 'reviewer',
        'description': 'Reviews work or verifies a claim independently and reports problems found.',
        'prompt': (
            'You are a reviewing sub-agent. Check the work or claim you were given independently and skeptically: '
            'reproduce what you can, look for bugs, gaps and wrong assumptions, and report each problem with its '
            'evidence. Say plainly when you find nothing wrong. Do not fix things yourself unless asked.'
        ),
    },
]

BUILTIN_NAMES = {profile['name'] for profile in BUILTIN_PROFILES}

_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9-]{1,31}$')


def validate_custom_profiles(profiles) -> list[dict]:
    """Clean an admin's custom profiles; raises ValueError with a message for the admin."""
    if not isinstance(profiles, list):
        raise ValueError('Profiles must be a list.')

    cleaned = []
    seen = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError('Each profile must be an object.')
        name = str(profile.get('name') or '').strip().lower()
        if not _NAME_PATTERN.match(name):
            raise ValueError(
                f'"{name or "(empty)"}" is not a valid profile name: use 2-32 lowercase letters, digits or dashes, '
                'starting with a letter.'
            )
        if name in BUILTIN_NAMES:
            raise ValueError(f'"{name}" is a built-in profile.')
        if name in seen:
            raise ValueError(f'Profile "{name}" is listed twice.')
        seen.add(name)

        prompt = str(profile.get('prompt') or '').strip()
        if not prompt:
            raise ValueError(f'Profile "{name}" needs instructions.')
        cleaned.append(
            {
                'name': name,
                'description': str(profile.get('description') or '').strip()[:300],
                'prompt': prompt[:8000],
                'model': str(profile.get('model') or '').strip() or None,
            }
        )
    return cleaned


def get_profiles(custom_profiles) -> list[dict]:
    """Built-in profiles followed by the admin's; malformed custom entries are skipped."""
    profiles = [dict(profile, builtin=True) for profile in BUILTIN_PROFILES]
    for profile in custom_profiles if isinstance(custom_profiles, list) else []:
        try:
            profiles.extend(dict(cleaned, builtin=False) for cleaned in validate_custom_profiles([profile]))
        except ValueError:
            continue
    return profiles


def resolve_profile(name: str | None, custom_profiles) -> dict | None:
    wanted = (name or DEFAULT_PROFILE).strip().lower()
    return next((profile for profile in get_profiles(custom_profiles) if profile['name'] == wanted), None)
