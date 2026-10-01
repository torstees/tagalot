"""Files a scan now leaves out are skipped, not missing (#152)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from tagalot.builtin_themes.music import MusicTheme
from tagalot.core.keep import RootConfig
from tagalot.core.models import Resource, ResourceKind, ResourceStatus
from tagalot.core.root_admin import root_statuses
from tagalot.core.scanjob import scan_root
from tagalot.core.scanner import KnownResource, WalkEntry, diff_root, walk_scope
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTreeCache
from tagalot.core.triage import MISSING, unlinked_files
from tests.themes.test_music import T0, Env, env, library

__all__ = ["env", "library"]  # fixtures

FILE, DIR = ResourceKind.FILE, ResourceKind.DIR


@pytest.mark.parametrize(
    ("relpath", "kind", "ext", "listed"),
    [
        ("a/b.mp3", FILE, ".mp3", True),
        ("a/b.MP3", FILE, ".MP3", True),  # extensions ignore case
        ("a/b.txt", FILE, ".txt", False),  # not an extension it takes
        ("cache/b.mp3", FILE, ".mp3", False),  # under an excluded folder
        ("x/cache/deep/b.mp3", FILE, ".mp3", False),
        ("cached/b.mp3", FILE, ".mp3", True),
        ("a/skip.mp3", FILE, ".mp3", False),  # excluded itself
        ("a", DIR, "", True),
        ("cache", DIR, "", False),
    ],
)
def test_walk_scope(relpath: str, kind: ResourceKind, ext: str, listed: bool) -> None:
    in_scope = walk_scope(["**/cache", "**/skip.mp3"], {".mp3"}, dirs=True)
    assert in_scope(relpath, kind, ext) is listed


def test_folders_follow_the_dirs_rule() -> None:
    assert not walk_scope(dirs=False)("a", DIR, "")
    assert walk_scope(dirs=lambda relpath: relpath.count("/") == 0)("a", DIR, "")
    assert not walk_scope(dirs=lambda relpath: relpath.count("/") == 0)("a/b", DIR, "")


def _known(
    rid: int, ext: str = ".mp3", status: ResourceStatus = ResourceStatus.OK, skipped: bool = False
) -> KnownResource:
    return KnownResource(rid, FILE, 10, 1, status, ext, skipped)


def test_the_diff_skips_what_the_walk_leaves_out() -> None:
    known = {
        "keep.mp3": _known(1),
        "gone.mp3": _known(2),
        "notes.txt": _known(3, ".txt"),
        "old.txt": _known(4, ".txt", ResourceStatus.MISSING),  # marked missing by an old scan
        "already.txt": _known(5, ".txt", skipped=True),
        "back.mp3": _known(6, skipped=True),
    }
    entries = [
        WalkEntry("keep.mp3", FILE, ".mp3", 10, 1),
        WalkEntry("back.mp3", FILE, ".mp3", 10, 1),
    ]
    diff = diff_root(known, entries, in_scope=walk_scope(extensions={".mp3"}))
    assert diff.missing == [2]  # really gone
    assert sorted(diff.skipped) == [3, 4]  # left out now; 5 is already flagged
    assert diff.restored == [6]  # in scope again: no longer skipped
    assert diff.unchanged == [1]
    assert diff_root(known, entries).missing == [2, 3, 5]  # without a scope, as before


def _scan(env: Env, exclude: list[str], hours: int) -> None:
    root = RootConfig("r", "Music", str(env.files), exclude=exclude)
    scan_root(
        env.writer, env.reader, root, root.path, when=T0 + timedelta(hours=hours),
        theme=MusicTheme, schema=env.schema,
    )  # fmt: skip


def _resources(env: Env) -> dict[str, tuple[ResourceStatus, bool]]:
    with env.reader.connect() as conn:
        rows = conn.execute(select(Resource.relpath, Resource.status, Resource.skipped))
        return {relpath: (status, skipped) for relpath, status, skipped in rows}


def test_excluding_a_folder_skips_its_files_and_unexcluding_restores_them(env: Env) -> None:
    env.scan()
    _scan(env, ["**/Untagged/**"], 1)
    resources = _resources(env)
    assert resources["Untagged/03 - Lonely Road.mp3"] == (ResourceStatus.OK, True)
    assert resources["Untagged"] == (ResourceStatus.OK, True)  # the folder too
    assert resources["Jazz Hits/01.mp3"] == (ResourceStatus.OK, False)
    tree = TagTreeCache(env.reader).get()
    with env.reader.connect() as conn:
        missing = run_search(conn, SearchSpec(triage=MISSING), tree, limit=None)
        assert missing == []  # skipped isn't missing
        assert root_statuses(conn)["r"].skipped == 3  # the folder and its two files
        assert [f.relpath for f in unlinked_files(conn)] == ["Miles Davis/Kind of Blue/cover.jpg"]

    _scan(env, [], 2)
    resources = _resources(env)
    assert resources["Untagged/03 - Lonely Road.mp3"] == (ResourceStatus.OK, False)
    assert not any(skipped for _, skipped in resources.values())


def test_files_wrongly_marked_missing_become_skipped(env: Env) -> None:
    """Before #152 a scan marked excluded files missing; the next scan sets that right."""
    env.scan()
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource)
            .where(Resource.relpath.startswith("Untagged"))
            .values(status=ResourceStatus.MISSING)
        )
    )
    tree = TagTreeCache(env.reader).get()
    with env.reader.connect() as conn:
        listed = run_search(conn, SearchSpec(triage=MISSING), tree, limit=None)
    assert sorted(h.title for h in listed) == ["Lonely Road", "Untagged", "broken"]  # as left
    _scan(env, ["**/Untagged/**"], 1)
    assert _resources(env)["Untagged/03 - Lonely Road.mp3"] == (ResourceStatus.MISSING, True)
    with env.reader.connect() as conn:
        assert run_search(conn, SearchSpec(triage=MISSING), tree, limit=None) == []
