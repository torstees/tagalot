"""Problems in Markdown files' front matter (#438): keys given twice (or in different case,
which Tagalot reads as one key), and front matter that doesn't parse; found in scans,
kept for Triage → Markdown, and replaced when the file is read again."""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from tagalot.core.db import open_keep_database
from tagalot.core.keep import KEEP_FORMAT_VERSION, open_keep, save_keep_config
from tagalot.core.markdown_check import markdown_problems
from tagalot.core.models import Resource, ResourceKind, Root, SchemaVersion
from tagalot.core.writeback import WriteBackError, edit_front_matter
from tagalot.themes.readers import front_matter_problems
from tests.core.book_files import write_markdown
from tests.core.test_schema_version import _keep
from tests.themes.test_books import T0, Env, env

__all__ = ["env"]  # fixtures

CASES = {
    "a key twice": (
        "---\nuniverse: Zhor\ntitle: X\nuniverse: ''\n---\n",
        ["`universe` is given twice (lines 2 and 4); the last, '', is used."],
    ),
    "three times, the last a list": (
        "---\na: 1\na: 2\na:\n  - x\n---\n",
        ["`a` is given 3 times (lines 2, 3, and 4); the last is used."],
    ),
    "in different case": (
        "---\nTitle: a\ntitle: b\n---\n",
        ["`Title` and `title` are one key to Tagalot (lines 2 and 3); the last, 'b', is used."],
    ),
    "inside a key's value": (
        "---\ncredits:\n  writer: A\n  writer: B\n---\n",
        ["`writer` is given twice (lines 3 and 4); the last, 'B', is used."],
    ),
    "yaml that doesn't parse": (
        "---\ntitle: ok\nx: [1\n---\n",
        [
            "The YAML front matter doesn't parse: expected ',' or ']', but got '<stream end>' "
            "(line 3)"
        ],
    ),
    "toml given twice doesn't parse": (
        "+++\na = 1\na = 2\n+++\n",
        ["The TOML front matter doesn't parse: Cannot overwrite a value (at end of document)"],
    ),
    "toml in different case": (
        '+++\nTitle = "a"\ntitle = "b"\n+++\n',
        ["`Title` and `title` are one key to Tagalot; the last is used."],
    ),
    "fine": ("---\ntitle: X\ntags: [a, b]\n---\nBody\n", []),
    "no front matter": ("# Just a heading\n", []),
    "a thematic break, not front matter": ("---\nno closing line\n", []),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_front_matter_problems(case: tuple[str, list[str]]) -> None:
    text, problems = case
    assert front_matter_problems(text) == problems


def _listed(env: Env) -> dict[str, tuple[str, ...]]:
    with env.reader.connect() as conn:
        return {p.relpath: p.problems for p in markdown_problems(conn)}


def test_a_scan_finds_them_and_a_fixed_file_clears_them(env: Env) -> None:
    write_markdown(
        env.files / "Stories/batuk.md",
        "---\ntitle: Batuk\nuniverse: Zhor\nseries: ''\nuniverse: ''\n---\n",
    )
    write_markdown(env.files / "Stories/broken.md", "---\ntitle: [unclosed\n---\n")
    report = env.scan()
    twice = "`universe` is given twice (lines 3 and 5); the last, '', is used."
    assert [w.message for w in report.ingest_warnings if w.relpath == "Stories/batuk.md"] == [twice]
    # Front matter that doesn't parse is the book's own warning, not said twice.
    assert [w.relpath for w in report.ingest_warnings].count("Stories/broken.md") == 1
    listed = _listed(env)
    assert listed["Stories/batuk.md"] == (twice,)
    assert listed["Stories/broken.md"][0].startswith("The YAML front matter doesn't parse")

    write_markdown(env.files / "Stories/batuk.md", "---\ntitle: Batuk\nuniverse: Zhor\n---\n")
    os.utime(env.files / "Stories/batuk.md", ns=(1, 1_900_000_000_000_000_000))
    env.scan(T0 + timedelta(minutes=1))
    assert set(_listed(env)) == {"Stories/broken.md"}  # read again: nothing wrong now
    os.remove(env.files / "Stories/broken.md")
    env.scan(T0 + timedelta(minutes=2))
    assert _listed(env) == {}  # a missing file isn't listed


def test_upgrading_a_keep_reads_its_markdown_files_again(tmp_path: Path) -> None:
    keep, engine = open_keep_database(_keep(tmp_path))
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r", name="R"))
        for relpath, ext in (("a.md", ".md"), ("b.markdown", ".markdown"), ("c.png", ".png")):
            conn.execute(
                insert(Resource).values(
                    root_id="r", relpath=relpath, kind=ResourceKind.FILE, ext=ext, ingested_at=T0
                )
            )
        conn.exec_driver_sql("DROP TABLE markdown_problem")
        conn.exec_driver_sql("DROP TABLE item_picture")  # format 23
        conn.exec_driver_sql("ALTER TABLE entity_note DROP COLUMN picture")
        conn.execute(update(SchemaVersion).values(version=21))
    engine.dispose()
    keep.config.format_version = 21
    save_keep_config(keep.config, keep.toml_path)
    migrated, engine = open_keep_database(open_keep(keep.dir), allow_migration=True)
    try:
        assert migrated.config.format_version == KEEP_FORMAT_VERSION
        with engine.connect() as conn:
            rows = conn.execute(select(Resource.relpath, Resource.ingested_at)).all()
    finally:
        engine.dispose()
    assert {rel: when is None for rel, when in rows} == {
        "a.md": True,
        "b.markdown": True,
        "c.png": False,
    }


def test_write_to_file_says_which_key_is_given_twice() -> None:
    with pytest.raises(WriteBackError) as info:
        edit_front_matter("---\nuniverse: Zhor\nuniverse: ''\n---\n", {"title": "A"})
    assert str(info.value) == (
        "its front matter gives a key twice: `universe` is given twice (lines 2 and 3); "
        "the last, '', is used. Remove one first"
    )
