"""Files inside archives wherever a file shows (#524): file rows, Open, Duplicates,
thumbnails, and Write to file…."""

import io
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from sqlalchemy import delete, select, update

from tagalot.builtin_themes.generic import GenericTheme
from tagalot.core.archive_members import _safe_parts
from tagalot.core.dedupe import Verifier, exact_groups
from tagalot.core.detail import FileRow, resource_file, role_files
from tagalot.core.gallery import extract_member
from tagalot.core.handlers import choose_file, file_to_open, files_to_open
from tagalot.core.keep import RootConfig
from tagalot.core.member_paths import PATH_MARK
from tagalot.core.models import EntityResource, Resource
from tagalot.core.roots import local_path
from tagalot.core.skip_reasons import ThemeScope, skip_reasons
from tagalot.core.tags import TagTree
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.core.triage import unlinked_files
from tagalot.core.writeback import INSIDE_ARCHIVE, plan_write_back
from tests.core.test_archive_members import PACK, Env, _zip, env

__all__ = ["env"]  # the fixture

REGULAR = f"{PACK}/fonts/Regular.ttf"
SHOWN = f"{PACK} {PATH_MARK} fonts/Regular.ttf"


def _root_path(env: Env) -> Callable[[str], str | None]:
    return lambda root_id: str(env.files) if root_id == "r" else None


def test_a_members_file_row_names_its_archive(env: Env) -> None:
    env.archives = ["**"]
    env.scan()
    regular = env.entity_of(REGULAR)
    with env.reader.connect() as conn:
        [row] = role_files(conn, regular, None, _root_path(env))
        [listed] = files_to_open(conn, env.schema, regular, _root_path(env))
        alone = resource_file(conn, env.resource(REGULAR), _root_path(env))
    assert row.archive == PACK
    assert row.member == "fonts/Regular.ttf"
    assert row.shown == SHOWN
    assert row.path == local_path(str(env.files), PACK)  # the archive's
    assert listed == row
    assert alone == FileRow(**{**row.__dict__, "role": None, "by_user": False, "root_id": "r"})

    # Opened from a copy: the archive's path and the member's inside it.
    file = choose_file([row])
    assert (file.path, file.member) == (row.path, "fonts/Regular.ttf")
    assert (file.ext, file.name) == (".ttf", "Regular.ttf")

    # A file of its own has none of that.
    with env.reader.connect() as conn:
        [archive] = file_to_open(conn, env.resource(PACK), _root_path(env))
    assert (archive.archive, archive.member, archive.shown) == ("", "", PACK)
    assert choose_file([archive]).member == ""


def test_opening_copies_the_member_out(env: Env, tmp_path: Path) -> None:
    archive = str(env.files / PACK)
    before = Path(archive).read_bytes()
    copy = extract_member(archive, "fonts/Regular.ttf", tmp_path / "copies")
    assert copy.read_bytes() == b"regular font"
    assert copy.is_relative_to(tmp_path / "copies")
    # Recorded names have no leading "/" or empty parts; the archive's may.
    bold = extract_member(archive, "/fonts//Bold.ttf", tmp_path / "b")
    assert bold.read_bytes() == b"bold font"
    # ...and the copy stays in its folder, whatever the name (a leading "/" or "..").
    assert bold == tmp_path / "b" / "fonts" / "Bold.ttf"
    assert Path(archive).read_bytes() == before  # only read


def test_triage_rows_name_the_archive(env: Env) -> None:
    env.archives = ["**"]
    env.scan()
    member = env.resource(REGULAR)
    env.run(  # no item uses it now
        lambda conn: conn.execute(
            delete(EntityResource).where(EntityResource.resource_id == member)
        )
    )
    with env.reader.connect() as conn:
        rows = {f.relpath: f for f in unlinked_files(conn)}
    assert rows[REGULAR].archive == PACK
    assert rows[REGULAR].shown == SHOWN

    env.archives = []  # the archive no longer read: its members are skipped
    env.scan()
    with env.reader.connect() as conn:
        report = skip_reasons(conn, [RootConfig("r", "Files", str(env.files))], ThemeScope(None))
    files = {f.relpath: f for rule in report.rules for f in rule.files}
    assert files[REGULAR].archive == PACK
    assert files[REGULAR].shown == SHOWN


def test_duplicates_compare_members_by_fingerprint(env: Env) -> None:
    (env.files / "Fonts/Regular copy.ttf").write_bytes(b"regular font")
    env.archives = ["**"]
    env.scan()
    with env.reader.connect() as conn:
        groups = exact_groups(conn, _root_path(env))
    [group] = [g for g in groups if any(f.archive for f in g.files)]
    by_path = {f.relpath: f for f in group.files}
    assert set(by_path) == {"Fonts/Regular copy.ttf", REGULAR}
    member = by_path[REGULAR]
    assert member.archive == PACK
    assert member.path == local_path(str(env.files), PACK)
    assert member.shown == SHOWN
    assert by_path["Fonts/Regular copy.ttf"].archive == ""

    # Checking reads the member from its archive, and finds them the same.
    result = Verifier().verify_group(group)
    assert result.confirmed, result

    # A copy changed in the middle isn't the same.
    (env.files / "Fonts/Regular copy.ttf").write_bytes(b"regular fonx")
    result = Verifier().verify_group(group)
    assert not result.confirmed


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def test_a_members_thumbnail_is_drawn_from_its_archive(env: Env, tmp_path: Path) -> None:
    _zip(env.files / "Pics/pics.zip", {"art/red.png": _png()}, extra=False)
    env.archives = ["Pics/**"]
    env.scan()
    member = env.resource("Pics/pics.zip/art/red.png")
    resolver = ThumbnailResolver(
        env.reader,
        env.writer,
        GenericTheme,
        ThumbCache(tmp_path / "thumbs.db"),
        lambda root_id: str(env.files),
        64,
    )
    problems: list[tuple[str, str]] = []
    resolver.report = problems.extend
    picture = resolver.resource_thumbnail(member)
    assert picture is not None
    assert (picture.width, picture.height) == (40, 20)
    assert problems == []
    # The item made from it shows it too.
    result = resolver.resolve(env.entity_of("Pics/pics.zip/art/red.png"))
    assert result.thumbnail is not None
    assert result.resource_id == member

    # A member its archive no longer holds can't be drawn, and says where it was.
    _zip(env.files / "Pics/pics.zip", {"art/other.png": _png()}, extra=False)
    with env.reader.connect() as conn:
        size = conn.scalar(select(Resource.size).where(Resource.id == member)) or 0
    env.run(  # as if it changed: not from the cache
        lambda conn: conn.execute(
            update(Resource).where(Resource.id == member).values(size=size + 1)
        )
    )
    assert resolver.resource_thumbnail(member) is None
    assert problems
    assert problems[0][0].endswith(f"pics.zip {PATH_MARK} art/red.png")


def test_write_to_file_says_a_member_is_inside_an_archive(env: Env) -> None:
    env.archives = ["**"]
    env.scan()
    readme = env.entity_of(f"{PACK}/readme.md")
    with env.reader.connect() as conn:
        plan = plan_write_back(
            conn, env.schema, TagTree.load(conn), [readme], {"r": str(env.files)}, ["r"]
        )
    [write] = plan.files
    assert write.relpath == f"{PACK}/readme.md"
    assert write.problem == INSIDE_ARCHIVE
    assert not write.changes


def test_copies_never_leave_their_folder() -> None:
    """A member's name never makes its copy's path absolute or climb out of the folder,
    on any OS (found on Linux CI: a leading "/" kept by ``PurePosixPath.parts``)."""
    for name in ("/etc/passwd", "../../up.txt", "a/../../b.txt", r"c:\x\y.txt", "//a//b.txt"):
        parts = _safe_parts(name)
        assert parts
        assert all(p not in ("", ".", "..") and "/" not in p and "\\" not in p for p in parts)
        assert not Path(*parts).is_absolute()
