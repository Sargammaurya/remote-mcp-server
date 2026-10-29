"""
Per-user path confinement.

Every file a client names is resolved inside that user's own workspace,
<workspace_root>/users/<user_id>/. Symlinks are resolved before the
containment check, so a link pointing outside the user's root is refused
just like a `..` path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_USER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class PathError(ValueError):
    """A path would leave the caller's workspace, or is malformed."""


def user_root(workspace_root: Path, user_id: str) -> Path:
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise PathError(f"invalid user id {user_id!r}")
    return Path(workspace_root) / "users" / user_id


def resolve_user_path(workspace_root: Path, user_id: str, relative: str) -> Path:
    """Resolve `relative` inside the user's root; refuse anything that escapes it."""
    root = user_root(workspace_root, user_id)
    if not isinstance(relative, str) or not relative.strip():
        raise PathError("path is empty")
    if "\x00" in relative:
        raise PathError("path contains a NUL byte")
    if os.path.isabs(relative):
        raise PathError(f"absolute paths are not allowed: {relative!r}")

    real_root = Path(os.path.realpath(root))
    real_target = Path(os.path.realpath(root / relative))
    if real_target != real_root and real_root not in real_target.parents:
        raise PathError(f"path escapes the user workspace: {relative!r}")
    return real_target
