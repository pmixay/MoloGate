"""When an idle container is paused, stopped or deleted. Pure functions, so they're easy to test."""

from mologate_node.config import Settings

ACTIVE_STATES = {'running', 'paused', 'restarting'}
STOPPED_STATES = {'created', 'exited', 'dead'}


def lifecycle_action(
    state: str,
    idle: float,
    inflight: int,
    spare: bool,
    age: float,
    settings: Settings,
) -> str | None:
    """'pause', 'stop', 'delete' or None for one container.

    A running container is frozen after ``idle_pause`` seconds without a request (it keeps its
    memory and resumes in milliseconds), stopped after ``idle_stop`` (frees its memory, resumes in
    about a second) and deleted with its files after ``delete_after`` (7 days by default). A spare,
    started ahead of a new chat, is deleted when nobody claims it within ``spare_ttl``.
    """
    if spare:
        return 'delete' if age >= settings.spare_ttl else None
    if inflight > 0:
        return None
    if state in STOPPED_STATES or state == 'missing':
        return 'delete' if idle >= settings.delete_after else None
    if state == 'running':
        if (
            settings.idle_pause > 0
            and idle >= settings.idle_pause
            and (settings.idle_stop <= 0 or idle < settings.idle_stop)
        ):
            return 'pause'
        if settings.idle_stop > 0 and idle >= settings.idle_stop:
            return 'stop'
    if state == 'paused' and settings.idle_stop > 0 and idle >= settings.idle_stop:
        return 'stop'
    return None


def eviction_order(candidates: list[tuple[str, str, float, int]], min_idle: float = 30) -> list[str]:
    """Contexts that may be stopped to make room, least recently used first.

    ``candidates`` holds (context, state, idle seconds, in-flight requests). Paused containers go
    before running ones; anything with a request in flight or used in the last ``min_idle`` seconds
    is never evicted.
    """
    eligible = [c for c in candidates if c[1] in ACTIVE_STATES and c[3] == 0 and c[2] >= min_idle]
    eligible.sort(key=lambda c: (c[1] != 'paused', -c[2]))
    return [c[0] for c in eligible]
