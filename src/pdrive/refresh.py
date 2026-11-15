"""pdrive.refresh – background folder refresher selection logic (stdlib only)."""

from __future__ import annotations

from typing import Optional


def _parent(path: str) -> Optional[str]:
    """Return the parent remote path, or None for the root '/'."""
    if path == "/":
        return None
    idx = path.rfind("/")
    if idx == 0:
        return "/"
    return path[:idx]


def _first_component(path: str) -> Optional[str]:
    """Return the first path component after the leading '/', or None for '/'."""
    if path == "/":
        return None
    parts = path.split("/")
    # parts[0] == "" because path starts with "/"
    if len(parts) > 1 and parts[1]:
        return parts[1]
    return None


def closeness(folder: str, used: str) -> float:
    """Score how *close* a cached folder is to a recently-used path.

    Both arguments are normalised absolute remote paths ("/", "/Movies", …)
    with no trailing slash except the root.
    """
    if folder == used:
        return 1.0

    parent_used = _parent(used)
    parent_folder = _parent(folder)

    # 0.7 – direct parent or direct child
    if parent_used == folder:
        return 0.7
    if parent_folder == used:
        return 0.7

    # 0.5 – siblings (same parent, different names, neither is "/")
    if (
        parent_folder is not None
        and parent_folder == parent_used
        and folder != "/"
        and used != "/"
    ):
        return 0.5

    # 0.3 – any other ancestor or descendant
    # folder is an ancestor of used (but not the direct parent, handled above)
    if (folder == "/" and used != "/") or used.startswith(folder + "/"):
        return 0.3
    # folder is a descendant of used (but not the direct child, handled above)
    if (used == "/" and folder != "/") or folder.startswith(used + "/"):
        return 0.3

    # 0.1 – share the same first path component
    fc_f = _first_component(folder)
    fc_u = _first_component(used)
    if fc_f is not None and fc_u is not None and fc_f == fc_u:
        return 0.1

    # 0.0 – unrelated
    return 0.0


def pick_refresh(
    folders: list[tuple[str, Optional[str], float]],
    activity: list[tuple[str, float]],
    now: float,
    min_age: float = 300.0,
    half_life: float = 600.0,
) -> Optional[str]:
    """Choose which cached folder to re-list this tick.

    Parameters
    ----------
    folders : list of (folder_uid, path | None, fetched_at)
    activity : list of (path, used_at) – recent user actions
    now : current epoch timestamp
    min_age : folders fetched less than this many seconds ago are skipped
    half_life : exponential decay half-life for user-activity recency

    Returns the uid of the highest-priority qualifying folder, or None.
    """
    if not activity:
        return None

    best_uid: Optional[str] = None
    best_priority: float = 0.0
    best_age: float = 0.0

    for uid, path, fetched_at in folders:
        if path is None:
            continue

        age = now - fetched_at
        if age < min_age:
            continue

        relevance: float = 0.0
        for used_path, used_at in activity:
            decay = 0.5 ** ((now - used_at) / half_life)
            rel = closeness(path, used_path) * decay
            if rel > relevance:
                relevance = rel

        priority = relevance * age

        if priority > best_priority:
            best_priority = priority
            best_uid = uid
            best_age = age
        elif priority == best_priority and priority > 0 and age > best_age:
            best_uid = uid
            best_age = age

    return best_uid if best_priority > 0 else None
