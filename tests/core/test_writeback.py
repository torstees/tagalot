"""Writing metadata back into Markdown files (#299): editing the front-matter block only,
which tags and fields are written, and planning and writing files in a writable folder."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import TagNode, TagTree
from tagalot.core.writeback import (
    WriteBackError,
    edit_front_matter,
    find_front_matter,
    plan_write_back,
    tag_changes,
    write_files,
)

BODY = "# Chapter One\n\nIt was a dark night.  \n\n---\n\nThe end.\n"


# --- the block ---


def test_find_front_matter() -> None:
    assert find_front_matter("---\ntitle: A\n---\nbody").style == "yaml"
    found = find_front_matter("﻿+++\ntitle = 'A'\n+++\n")
    assert (found.style, found.start) == ("toml", 5)
    assert find_front_matter("# Heading\n---\n").style is None
    assert find_front_matter("---\n# no closing marker\n").style is None


def test_yaml_keeps_comments_quotes_order_and_the_body() -> None:
    text = (
        "---\n"
        "title: 'The Long Way'   # the working title\n"
        "author: Becky Chambers\n"
        "tags: [space, found-family]\n"
        "# private notes below\n"
        "rating: 5\n"
        "---\n" + BODY
    )
    new = edit_front_matter(text, {"tags": ["space", "Genre/Science Fiction"], "year": 2014})
    assert new == (
        "---\n"
        "title: 'The Long Way'   # the working title\n"
        "author: Becky Chambers\n"
        "tags: [space, Genre/Science Fiction]\n"
        "# private notes below\n"
        "rating: 5\n"
        "year: 2014\n"
        "---\n" + BODY
    )


def test_yaml_lists_keep_their_style_and_keys_their_case() -> None:
    text = "---\nTitle: A\nTags:\n  - one\n  - two\n---\nbody\n"
    new = edit_front_matter(text, {"tags": ["one", "three"], "title": "B"})
    assert new == "---\nTitle: B\nTags:\n  - one\n  - three\n---\nbody\n"


def test_yaml_removes_keys_and_keeps_crlf() -> None:
    text = "---\r\ntitle: A\r\nurl: https://x.example\r\n---\r\nbody\r\n"
    assert edit_front_matter(text, {"url": None}) == "---\r\ntitle: A\r\n---\r\nbody\r\n"


def test_a_file_without_front_matter_gains_a_block() -> None:
    assert edit_front_matter(BODY, {"tags": ["a"]}) == "---\ntags:\n- a\n---\n" + BODY
    assert edit_front_matter(BODY, {"url": None}) == BODY  # nothing to write
    assert edit_front_matter("---\n---\nbody", {"title": "A"}) == "---\ntitle: A\n---\nbody"


def test_toml_replaces_only_the_keys_written() -> None:
    text = (
        "+++\n"
        'title = "Gideon"  # draft\n'
        "tags = [\n"
        '  "necromancy",\n'
        '  "space",\n'
        "]\n"
        "# keep this comment\n"
        "year = 2019\n"
        "\n"
        "[extra]\n"
        'tags = ["not these"]\n'
        "+++\n" + BODY
    )
    new = edit_front_matter(text, {"tags": ["necromancy"], "year": None, "series": "Locked Tomb"})
    assert new == (
        "+++\n"
        'title = "Gideon"  # draft\n'
        'tags = ["necromancy"]\n'
        "# keep this comment\n"
        'series = "Locked Tomb"\n'
        "\n"
        "[extra]\n"
        'tags = ["not these"]\n'
        "+++\n" + BODY
    )


def test_a_block_that_doesnt_parse_is_refused() -> None:
    with pytest.raises(WriteBackError, match="doesn't parse"):
        edit_front_matter("---\ntitle: [unclosed\n---\n", {"title": "A"})
    with pytest.raises(WriteBackError, match="keys and values"):
        edit_front_matter("---\n- a list\n---\n", {"title": "A"})


# --- tags ---

TREE = TagTree(
    [
        TagNode(1, None, "Genre", None, 0),
        TagNode(2, 1, "Fantasy", None, 0),
        TagNode(3, 1, "Science Fiction", None, 1),
        TagNode(4, None, "Read", None, 1),
    ],
    {3: ["Sci-Fi"]},
)


def test_tags_keep_the_files_words_and_add_paths() -> None:
    current = {"tags": ["fantasy", "Sci-Fi", "to-read"]}
    # The item has Fantasy and Sci-Fi from the file, and Read by hand.
    assert tag_changes(current, {2, 3, 4}, set(), TREE) == {
        "tags": ["fantasy", "Sci-Fi", "to-read", "Read"]
    }
    # The user took Fantasy off: its word goes; unmatched words stay.
    assert tag_changes(current, {3}, {2}, TREE) == {"tags": ["Sci-Fi", "to-read"]}
    assert tag_changes(current, {2, 3}, set(), TREE) == {}  # nothing changes


def test_tags_go_under_the_key_the_file_uses() -> None:
    assert tag_changes({}, {2}, set(), TREE) == {"tags": ["Genre/Fantasy"]}
    assert tag_changes({"Keywords": "fantasy"}, {2, 4}, set(), TREE) == {
        "Keywords": ["fantasy", "Read"]
    }
    both = {"tags": ["Read"], "keywords": ["fantasy"]}
    assert tag_changes(both, {4}, {2}, TREE) == {"keywords": []}


# --- planning and writing ---


@pytest.fixture
def keep(tmp_path: Path) -> Iterator[tuple[KeepSession, Path]]:
    files = tmp_path / "files"
    files.mkdir()
    (files / "story.md").write_text(
        "---\ntitle: The Story\nauthor: Ann Writer\ntags: [Fantasy]\n---\n" + BODY,
        encoding="utf-8",
    )
    (files / "epub-only.epub").write_bytes(b"not a real epub")
    root = RootConfig("r", "Files", str(files), writable=True)
    keep_dir = tmp_path / "Books.keep"
    create_keep(keep_dir, "Books", ThemeRef("books", 1), [root])
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        tags = session.tags
        genre = tags.add(None, "Genre")
        tags.add(genre, "Fantasy")
        read = tags.add(None, "Read")
        story = _id(session, "The Story")
        tags.apply([story], [read])
        tags.edit_field(story, "year", 2020)
        yield session, files


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _plan(session: KeepSession, ids: list[int], writable: tuple[str, ...] = ("r",)):  # type: ignore[no-untyped-def]
    with session.reader.connect() as conn:
        return plan_write_back(
            conn,
            session.schema,
            session.tag_cache.get(),
            ids,
            {r.id: session.root_path(r.id) for r in session.keep.config.roots},
            writable,
        )


def test_plan_and_write(keep: tuple[KeepSession, Path]) -> None:
    session, files = keep
    story = files / "story.md"
    plan = _plan(session, [_id(session, "The Story"), _id(session, "epub-only")])
    assert plan.skipped == [("epub-only", "it has no Markdown file")]
    [write] = plan.files
    assert write.changes
    assert write.before == "title: The Story\nauthor: Ann Writer\ntags: [Fantasy]\n"
    assert write.after == (
        "title: The Story\nauthor: Ann Writer\ntags: [Fantasy, Read]\nyear: 2020\n"
    )
    assert story.read_text(encoding="utf-8").startswith("---\ntitle: The Story\n")  # untouched

    [result] = write_files(plan.files, session.keep.dir, {"r"})
    assert result.error is None
    assert result.backup is not None
    assert result.backup.read_text(encoding="utf-8").endswith(BODY)
    assert "tags: [Fantasy]" in result.backup.read_text(encoding="utf-8")  # as it was
    assert result.backup.is_relative_to(session.keep.dir / "backups")
    written = story.read_text(encoding="utf-8")
    assert written == "---\n" + write.after + "---\n" + BODY
    assert not [p for p in files.iterdir() if p.name.startswith(".")]  # no temp file left


def test_a_file_that_changed_is_refused(keep: tuple[KeepSession, Path]) -> None:
    session, files = keep
    story = files / "story.md"
    stat = story.stat()
    os.utime(story, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    [write] = _plan(session, [_id(session, "The Story")]).files
    assert write.problem == "it changed since Tagalot last read it: scan it, then try again"
    assert write_files([write], session.keep.dir, {"r"}) == []


def test_a_change_after_the_preview_is_refused(keep: tuple[KeepSession, Path]) -> None:
    session, files = keep
    [write] = _plan(session, [_id(session, "The Story")]).files
    story = files / "story.md"
    story.write_text("changed by someone else", encoding="utf-8")
    [result] = write_files([write], session.keep.dir, {"r"})
    assert result.error == "it changed after the preview; nothing was written"
    assert story.read_text(encoding="utf-8") == "changed by someone else"
    assert not (session.keep.dir / "backups").exists()


def test_only_writable_folders_are_written(keep: tuple[KeepSession, Path]) -> None:
    session, files = keep
    before = (files / "story.md").read_bytes()
    [write] = _plan(session, [_id(session, "The Story")], writable=()).files
    assert write.problem == "its folder doesn't allow writing (Keep configuration → Folders)"
    [planned] = _plan(session, [_id(session, "The Story")]).files
    [result] = write_files([planned], session.keep.dir, set())  # no longer writable
    assert result.error == "its folder no longer allows writing"
    assert (files / "story.md").read_bytes() == before


def test_nothing_to_change_writes_nothing(keep: tuple[KeepSession, Path]) -> None:
    session, _ = keep
    [write] = _plan(session, [_id(session, "The Story")]).files
    write_files([write], session.keep.dir, {"r"})
    session.scan_all()  # Tagalot reads the file it wrote
    [again] = _plan(session, [_id(session, "The Story")]).files
    assert not again.changes
    assert again.problem is None
    assert again.before == again.after
