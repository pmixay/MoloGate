import re


def strip_provider_model_prefix(model_id: str, prefix_id: str | None) -> str:
    if prefix_id and model_id.startswith(f'{prefix_id}.'):
        return model_id[len(f'{prefix_id}.') :]
    return model_id


def normalize_model_name(value: str) -> str:
    return re.sub(r'[\s_]+', '-', (value or '').strip().lower())


def find_model_id(name: str, models) -> str | None:
    """A model id for ``name``: exact, else by id ignoring case, spaces and provider prefix, else by name.

    "GLM-5.3 Flash" finds ``z-ai/glm-5.3-flash``.
    """
    if not name:
        return None
    if models.get(name):
        return name

    wanted = normalize_model_name(name)
    for model_id, model in models.items():
        normalized = normalize_model_name(model_id)
        if (
            normalized == wanted
            or normalized.rsplit('/', 1)[-1] == wanted
            or normalize_model_name(str(model.get('name') or '')) == wanted
        ):
            return model_id
    return None


_DATE_PATTERN = re.compile(r'(?<!\d)(20\d{2})-?(\d{2})-?(\d{2})(?!\d)')
_VERSION_PATTERN = re.compile(r'\d+(?:\.\d+)*')
_PLAIN_TOKENS = {'gpt', 'chatgpt', 'latest'}


def _family_rank(text: str, family: str) -> tuple | None:
    """How recent a model id or name is within ``family``, or None when it isn't in the family.

    Ranks by version, then fewer qualifiers (``gpt-luna-6`` over ``gpt-luna-6-mini``), then an
    undated alias over a dated snapshot, then the newest date.
    """
    base = normalize_model_name(text).rsplit('/', 1)[-1]
    if not re.search(rf'(^|[^a-z]){re.escape(family)}([^a-z]|$)', base):
        return None

    date_match = _DATE_PATTERN.search(base)
    date = int(''.join(date_match.groups())) if date_match else 0
    if date_match:
        base = base[: date_match.start()] + base[date_match.end() :]

    version_match = _VERSION_PATTERN.search(base)
    version = tuple(int(part) for part in version_match.group(0).split('.')) if version_match else ()
    qualifiers = [
        token
        for token in re.split(r'[-:]+', base)
        if token and token != family and token not in _PLAIN_TOKENS and not _VERSION_PATTERN.fullmatch(token)
    ]
    return (version, -len(qualifiers), date == 0, date)


def find_latest_model_in_family(family: str, models) -> str | None:
    """The newest model whose id (or name) belongs to ``family``, e.g. ``luna`` finds ``gpt-luna-6``
    over ``gpt-luna-5.5``. Picks up a new version as soon as it appears in the model list."""
    family = normalize_model_name(family)
    if not family:
        return None

    best_id, best_rank = None, None
    for model_id, model in models.items():
        if model.get('owned_by') == 'arena':
            continue
        rank = _family_rank(model_id, family) or _family_rank(str(model.get('name') or ''), family)
        if rank is not None and (best_rank is None or (rank, -len(model_id)) > (best_rank, -len(best_id))):
            best_id, best_rank = model_id, rank
    return best_id
