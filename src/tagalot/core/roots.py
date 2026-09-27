"""Root reachability and the ``root`` table (DESIGN.md §6 step 1).

Checking a root touches only the file system and may run in any worker. The functions that
take a :class:`~sqlalchemy.Connection` write to the database and are called by the DB writer.
Nothing here ever deletes a resource or entity (AGENTS.md rule 7).
"""

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType

from sqlalchemy import Connection, insert, select, update

from tagalot.core.keep import RootConfig
from tagalot.core.models import Resource, ResourceStatus, Root

logger = logging.getLogger(__name__)

REACHABILITY_TIMEOUT_S = 10.0
"""An unreachable network share can block for a minute; give up well before that."""


@dataclass(frozen=True)
class RootCheck:
    """The result of checking whether a root can be scanned."""

    online: bool
    error: str | None = None


def check_root(
    path: str,
    *,
    timeout: float = REACHABILITY_TIMEOUT_S,
    probe: Callable[[str], None] | None = None,
) -> RootCheck:
    """Check that ``path`` is a folder that can be listed, within ``timeout`` seconds.

    ``path`` is this machine's path for the root (after per-user overrides). ``probe`` raises
    ``OSError`` if the folder can't be listed; tests replace it. A probe still running at the
    timeout is abandoned on a daemon thread, since blocked network I/O can't be cancelled.
    """
    probe = probe or _list_one_entry
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            probe(path)
            outcome.append(None)
        except BaseException as e:
            outcome.append(e)

    thread = threading.Thread(target=run, name=f"check-root {path}", daemon=True)
    thread.start()
    thread.join(timeout)
    if not outcome:
        return RootCheck(online=False, error=f"No response from {path} after {timeout:g} s")
    error = outcome[0]
    if error is None:
        return RootCheck(online=True)
    if isinstance(error, FileNotFoundError):
        return RootCheck(online=False, error=f"{path} was not found")
    if isinstance(error, NotADirectoryError):
        return RootCheck(online=False, error=f"{path} is not a folder")
    if isinstance(error, PermissionError):
        return RootCheck(online=False, error=f"Permission denied reading {path}")
    if isinstance(error, OSError):
        return RootCheck(online=False, error=f"Cannot read {path}: {error.strerror or error}")
    raise error


def _list_one_entry(path: str) -> None:
    if not os.path.isdir(path):
        if os.path.exists(path):
            raise NotADirectoryError(path)
        raise FileNotFoundError(path)
    with os.scandir(path) as entries:
        next(entries, None)


def local_path(root_path: str, relpath: str, pathmod: ModuleType = os.path) -> str:
    r"""Join this machine's root path and a stored POSIX relative path into a local path.

    This is the boundary conversion of AGENTS.md rule 6: ``\\nas\music`` + ``A/b.flac``
    gives ``\\nas\music\A\b.flac`` on Windows. ``pathmod`` is for tests (``ntpath``,
    ``posixpath``).
    """
    return str(pathmod.join(root_path, *relpath.split("/")))


def sync_roots(conn: Connection, roots: list[RootConfig]) -> None:
    """Make the ``root`` table mirror ``keep.toml``: add new roots and update names.

    Rows for roots no longer in ``keep.toml`` are left alone; removing a root and its
    resources is an explicit operation.
    """
    existing = {i: n for i, n in conn.execute(select(Root.id, Root.name))}
    for root in roots:
        if root.id not in existing:
            conn.execute(insert(Root).values(id=root.id, name=root.name, online=False))
        elif existing[root.id] != root.name:
            conn.execute(update(Root).where(Root.id == root.id).values(name=root.name))


def record_root_check(conn: Connection, root_id: str, check: RootCheck) -> int:
    """Store a check result. Offline roots mark their ``ok`` resources ``offline``.

    Resources already ``missing`` stay missing, and nothing is deleted. When a root comes
    back online, resource statuses are left for the scan to correct. Returns the number of
    resources marked offline.
    """
    conn.execute(
        update(Root).where(Root.id == root_id).values(online=check.online, last_error=check.error)
    )
    if check.online:
        return 0
    result = conn.execute(
        update(Resource)
        .where(Resource.root_id == root_id, Resource.status == ResourceStatus.OK)
        .values(status=ResourceStatus.OFFLINE)
    )
    logger.warning(
        "Root %s is offline (%s); marked %d resources offline",
        root_id,
        check.error,
        result.rowcount,
    )
    return result.rowcount
