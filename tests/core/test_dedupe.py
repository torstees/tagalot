"""Exact duplicates by fingerprint, and checking them by full hash (#117)."""

import shutil
from pathlib import Path

from sqlalchemy import update

from tagalot.core.dedupe import DuplicateGroup, Verifier, exact_groups
from tagalot.core.models import Resource, ResourceStatus
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures

SO_WHAT = "Miles Davis/Kind of Blue/01 So What.mp3"
HEAD_TAIL = 64 * 1024


def _collision(path: Path, middle: bytes) -> None:
    """A 300 KiB file whose first and last 64 KiB are fixed: only ``middle`` differs, so
    two of them share a fingerprint without being the same."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = middle * ((300 * 1024 - 2 * HEAD_TAIL) // len(middle))
    path.write_bytes(b"H" * HEAD_TAIL + body + b"T" * HEAD_TAIL)


def _groups(env: Env) -> list[DuplicateGroup]:
    with env.reader.connect() as conn:
        return exact_groups(conn, lambda _: str(env.files))


def _setup(env: Env) -> None:
    (env.files / "Copies").mkdir()
    shutil.copy(env.files / SO_WHAT, env.files / "Copies" / "So What (copy).mp3")
    _collision(env.files / "Pictures" / "one.jpg", b"a")
    _collision(env.files / "Pictures" / "two.jpg", b"b")
    _collision(env.files / "Pictures" / "three.jpg", b"a")


def test_files_with_the_same_fingerprint_are_grouped(env: Env) -> None:
    _setup(env)
    env.scan()
    groups = _groups(env)
    assert [sorted(f.relpath for f in g.files) for g in groups] == [
        ["Pictures/one.jpg", "Pictures/three.jpg", "Pictures/two.jpg"],  # most wasted first
        ["Copies/So What (copy).mp3", SO_WHAT],
    ]
    pictures, songs = groups
    assert pictures.wasted == 2 * 300 * 1024
    copy = next(f for f in songs.files if f.relpath == SO_WHAT)
    assert [title for _, title in copy.items] == ["So What"]  # the item using it
    assert copy.path == str(env.files / SO_WHAT)


def test_checking_splits_files_that_only_share_a_fingerprint(env: Env) -> None:
    _setup(env)
    env.scan()
    pictures, songs = _groups(env)
    verifier = Verifier()
    result = verifier.verify_group(pictures)
    ids = {f.relpath.rpartition("/")[2]: f.resource_id for f in pictures.files}
    assert result.identical == ((ids["one.jpg"], ids["three.jpg"]),)
    assert result.different == (ids["two.jpg"],)
    assert not result.confirmed
    assert verifier.verify_group(songs).confirmed


def test_missing_and_offline_files(env: Env) -> None:
    _setup(env)
    env.scan()

    def set_status(relpath: str, status: ResourceStatus) -> None:
        env.writer.run(
            lambda conn: conn.execute(
                update(Resource).where(Resource.relpath == relpath).values(status=status)
            )
        )

    set_status("Pictures/two.jpg", ResourceStatus.MISSING)
    set_status("Pictures/three.jpg", ResourceStatus.OFFLINE)
    pictures = next(g for g in _groups(env) if g.files[0].relpath.startswith("Pictures"))
    assert sorted(f.relpath for f in pictures.files) == ["Pictures/one.jpg", "Pictures/three.jpg"]
    result = Verifier().verify_group(pictures)
    assert list(result.unread.values()) == ["offline"]
    assert result.identical == ()  # only one file could be read


def test_a_file_changed_after_a_check_is_read_again(env: Env) -> None:
    """Check, change a copy without scanning, check again: it must differ now (the
    session's hash for its old contents can't be reused)."""
    _setup(env)
    env.scan()
    _, songs = _groups(env)
    verifier = Verifier()
    assert verifier.verify_group(songs).confirmed
    with (env.files / "Copies" / "So What (copy).mp3").open("ab") as f:
        f.write(b"x")  # what the PR's PowerShell step does
    result = verifier.verify_group(songs)  # the group as the last scan saw it
    assert not result.confirmed
    assert len(result.different) == 2
    copy = next(f for f in songs.files if f.relpath.startswith("Copies/"))
    assert result.changed == (copy.resource_id,)  # the page suggests a scan
