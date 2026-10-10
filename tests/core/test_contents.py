"""Documents' text in ``fulltext.db`` (#348): which files are read, in the background, only
when new or changed, never while contents search is Off; unreadable files reported and not
retried; a deleted file's text gone."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, select, update

from tagalot.core.contents import (
    READERS_VERSION,
    ContentsResult,
    contents_file,
    contents_meta,
    contents_page,
    readers_version,
)
from tagalot.core.keep import KeepConfigError, RootConfig, ThemeRef, create_keep, load_keep_config
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.core.book_files import paged_pdf

THEME = """
from tagalot.themes.api import Entity, ResourceInfo, Theme, role


class Doc(Entity):
    roles = [role("file", kinds={"any"}, primary=True)]
    full_text = True


class Note(Entity):
    roles = [role("file", kinds={"any"}, primary=True)]


class Docs(Theme):
    id, name, version, api_version = "docs", "Docs", 1, 6
    extensions = {".pdf", ".txt", ".rot", ".note"}
    entities = [Doc, Note]

    def ingest(self, batch, ctx):
        for resource in batch:
            kind = Note if resource.ext == ".note" else Doc
            item = ctx.upsert(kind, resource.relpath, title=resource.relpath)
            ctx.link(item, resource, "file")

    def document_text(self, resource: ResourceInfo):
        if resource.ext != ".rot":
            return None
        with open(resource.path, encoding="utf-8") as f:
            return [f.read()[::-1]]  # the theme's own format: text written backwards
"""


@pytest.fixture
def files(tmp_path: Path) -> Path:
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "paper.pdf").write_bytes(paged_pdf([["Attention is all"], ["you need"]]))
    (folder / "notes.txt").write_text("plain words", encoding="utf-8")
    (folder / "secret.rot").write_text("sdrawkcab", encoding="utf-8")
    (folder / "broken.pdf").write_bytes(b"not a pdf")
    (folder / "skip.note").write_text("not a document", encoding="utf-8")
    return folder


@pytest.fixture
def session(tmp_path: Path, files: Path) -> Iterator[KeepSession]:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        opened.scan_all()
        yield opened


def _read(session: KeepSession) -> tuple[int, ContentsResult | None]:
    finished = threading.Event()
    results: list[ContentsResult] = []

    def done(result: ContentsResult) -> None:
        results.append(result)
        finished.set()

    count = session.queue_contents(done=done)
    if count:
        assert finished.wait(10)
    return count, results[0] if results else None


def _pages(session: KeepSession) -> dict[str, list[str]]:
    from tagalot.core.models import Resource

    with session.reader.connect() as conn:
        names = dict(conn.execute(select(Resource.id, Resource.relpath)).all())
    found: dict[str, list[str]] = {}
    with session.contents.reader.connect() as conn:
        rows = conn.execute(
            select(contents_page.c.resource_id, contents_page.c.text).order_by(
                contents_page.c.resource_id, contents_page.c.page
            )
        )
        for rid, text in rows:
            found.setdefault(names[rid], []).append(text)
    return found


def test_nothing_is_read_while_off(session: KeepSession) -> None:
    assert session.queue_contents() == 0
    assert not (session.keep.dir / "fulltext.db").exists()


def test_documents_are_read_as_pages(session: KeepSession) -> None:
    session.set_contents_index("words")
    count, result = _read(session)
    assert count == 4  # the .note isn't a document type's file
    assert result is not None
    assert (result.read, result.pages) == (3, 4)
    assert _pages(session) == {
        "paper.pdf": ["Attention is all", "you need"],
        "notes.txt": ["plain words"],
        "secret.rot": ["backwards"],  # the theme's document_text
    }
    [(root, relpath, message)] = result.failed
    assert (root, relpath) == ("f", "broken.pdf")
    assert message.startswith("ValueError: not a PDF")
    problems = [(p.kind, p.relpath) for p in session.problems.items()]
    assert ("contents", "broken.pdf") in problems
    assert (session.keep.dir / "fulltext.db").exists()


def test_only_new_and_changed_files_are_read_again(session: KeepSession, files: Path) -> None:
    session.set_contents_index("substrings")
    _read(session)
    assert _read(session)[0] == 0  # the broken file isn't retried either
    (files / "notes.txt").write_text("different words now", encoding="utf-8")
    session.scan_all()
    count, _ = _read(session)
    assert count == 1
    assert _pages(session)["notes.txt"] == ["different words now"]


def test_failed_files_are_tried_again_when_the_readers_improve(session: KeepSession) -> None:
    """A store last read by older readers (#375: before Kindle's HUFF/CDIC text) tries the
    files that failed once more, and only those; then they rest again."""
    session.set_contents_index("words")
    _read(session)
    with session.contents.reader.connect() as conn:
        assert readers_version(conn) == READERS_VERSION
    assert _read(session)[0] == 0

    def older(conn: Connection) -> None:
        conn.execute(
            update(contents_meta).where(contents_meta.c.key == "readers").values(value="1")
        )

    session.contents.writer.run(older)
    count, result = _read(session)
    assert count == 1  # broken.pdf, not the files that were read
    assert result is not None
    assert [f[1] for f in result.failed] == ["broken.pdf"]
    with session.contents.reader.connect() as conn:
        assert readers_version(conn) == READERS_VERSION
    assert _read(session)[0] == 0  # it failed again: not tried until the next improvement


def test_a_deleted_files_text_goes(session: KeepSession, files: Path) -> None:
    session.set_contents_index("words")
    _read(session)
    (files / "notes.txt").unlink()
    session.scan_all()  # missing: its text stays (it may come back)
    _read(session)
    assert "notes.txt" in _pages(session)
    session.delete_root("f")  # the folder and its items leave the keep: the text goes too
    assert _read(session)[0] == 0
    with session.contents.reader.connect() as conn:
        assert conn.scalar(select(contents_file.c.resource_id).limit(1)) is None
        assert conn.scalar(select(contents_page.c.resource_id).limit(1)) is None


def test_the_contents_index_is_saved_in_keep_toml(session: KeepSession) -> None:
    session.set_contents_index("substrings")
    path = session.keep.toml_path
    text = path.read_text(encoding="utf-8")
    assert "[contents]\nindex = 'substrings'\n" in text
    assert load_keep_config(path).contents_index == "substrings"
    session.set_contents_index(None)
    assert "[contents]" not in path.read_text(encoding="utf-8")
    path.write_text(text.replace("'substrings'", "'letters'"), encoding="utf-8")
    with pytest.raises(KeepConfigError, match='index must be "words" or "substrings"'):
        load_keep_config(path)


def test_full_text_needs_a_primary_role_and_api_version_6() -> None:
    from tagalot.themes.api import Entity, Theme, role
    from tagalot.themes.loader import validate_theme

    class Loose(Entity):
        roles = [role("file", kinds={"any"})]
        full_text = True

    class Old(Theme):
        id, name, version, api_version = "old", "Old", 1, 5
        entities = [Loose]

    problems = validate_theme(Old)
    assert "Loose has full_text = True but no primary role" in problems
    assert "a theme with full_text types must set api_version = 6 (or later)" in problems
