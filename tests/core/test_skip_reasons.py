"""Why files are skipped, for Triage (#527): each file checked against the rules in force
now, with counts of files and of items hidden (#526)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert

from tagalot.core.archive_members import set_archive_mode, set_member_choices
from tagalot.core.db import create_keep_engine
from tagalot.core.keep import RootConfig
from tagalot.core.models import (
    ArchiveMode,
    Base,
    Entity,
    EntityResource,
    Resource,
    ResourceKind,
    Root,
)
from tagalot.core.skip_reasons import (
    ARCHIVE_OFF,
    ARCHIVE_SKIPPED,
    EXTENSION,
    FOLDERS,
    MEMBER_HIDDEN,
    NEXT_SCAN,
    NOT_READ_INSIDE,
    NOTES,
    PATTERN,
    SkippedReport,
    ThemeScope,
    skip_reasons,
)

PHOTOS = RootConfig("p", "Photos", "/p", exclude=["Drafts/**", "**/*.tmp", "Unused/**"])
FONTS = RootConfig("f", "Fonts", "/f", exclude=["Old/**"], archives=["Read/**"])
THEME = ThemeScope(frozenset({".jpg", ".png", ".md", ".ttf"}), dirs=False)

# (root, relpath, kind, skipped, its archive's relpath)
FILES = [
    ("p", "kept.jpg", "file", False, None),
    ("p", "Drafts", "dir", True, None),  # x/** takes the folder too
    ("p", "Drafts/a.jpg", "file", True, None),
    ("p", "Drafts/b.tmp", "file", True, None),  # under two patterns
    ("p", "c.bmp", "file", True, None),  # the theme doesn't read .bmp
    ("p", "Album", "dir", True, None),  # nor record folders
    ("p", "Notes/todo.txt", "file", True, None),  # the notes folder takes no .txt
    ("p", "Back.jpg", "file", True, None),  # no rule leaves it out any more
    ("f", "Off/off.zip", "file", False, None),  # turned off
    ("f", "Off/off.zip/A.ttf", "file", True, "Off/off.zip"),
    ("f", "Read/chosen.zip", "file", False, None),  # member by member
    ("f", "Read/chosen.zip/B.ttf", "file", True, "Read/chosen.zip"),
    ("f", "Read/chosen.zip/C.ttf", "file", False, "Read/chosen.zip"),
    ("f", "Other/plain.zip", "file", False, None),  # under no archives pattern
    ("f", "Other/plain.zip/D.ttf", "file", True, "Other/plain.zip"),
    ("f", "Old/old.zip", "file", True, None),  # skipped by a pattern
    ("f", "Old/old.zip/E.ttf", "file", True, "Old/old.zip"),
]
ITEMS = {  # title: its files
    "Kept": ["kept.jpg"],
    "A": ["Drafts/a.jpg"],
    "Mixed": ["Drafts/b.tmp", "kept.jpg"],  # a file still read: not hidden
    "Bitmap": ["c.bmp"],
    "Font A": ["Off/off.zip/A.ttf"],
    "Font B": ["Read/chosen.zip/B.ttf"],
}


def _fill(conn: Connection) -> None:
    conn.execute(insert(Root), [{"id": "p", "name": "Photos"}, {"id": "f", "name": "Fonts"}])
    ids: dict[str, int] = {}
    for root, relpath, kind, skipped, archive in FILES:
        ids[relpath] = conn.execute(
            insert(Resource)
            .values(
                root_id=root,
                relpath=relpath,
                kind=ResourceKind(kind),
                ext=Path(relpath).suffix if kind == "file" else "",
                skipped=skipped,
                parent_resource_id=ids[archive] if archive else None,
            )
            .returning(Resource.id)
        ).scalar_one()
    for title, files in ITEMS.items():
        eid = conn.execute(
            insert(Entity).values(type="t", title=title).returning(Entity.id)
        ).scalar_one()
        conn.execute(
            insert(EntityResource),
            [{"entity_id": eid, "resource_id": ids[f], "role": "file"} for f in files],
        )
    set_archive_mode(conn, ids["Off/off.zip"], ArchiveMode.NONE)
    set_member_choices(conn, ids["Read/chosen.zip"], {"B.ttf": False, "C.ttf": True})


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _fill(conn)
    yield engine
    engine.dispose()


def _report(engine: Engine, roots: list[RootConfig] | None = None) -> SkippedReport:
    with engine.connect() as conn:
        return skip_reasons(conn, roots or [PHOTOS, FONTS], THEME, ("p", "Notes"))


def test_each_rule_with_its_files_and_hidden_items(engine: Engine) -> None:
    report = _report(engine)
    found = [
        (r.kind, r.root_name, r.value, sorted(f.relpath for f in r.files), r.hidden)
        for r in report.rules
    ]
    assert found == [
        (PATTERN, "Fonts", "Old/**", ["Old/old.zip"], 0),
        (PATTERN, "Photos", "**/*.tmp", ["Drafts/b.tmp"], 0),
        (PATTERN, "Photos", "Drafts/**", ["Drafts", "Drafts/a.jpg", "Drafts/b.tmp"], 1),
        (NOTES, "Photos", "", ["Notes/todo.txt"], 0),
        (FOLDERS, "", "", ["Album"], 0),
        (EXTENSION, "", ".bmp", ["c.bmp"], 1),
        (ARCHIVE_SKIPPED, "Fonts", "", ["Old/old.zip/E.ttf"], 0),
        (ARCHIVE_OFF, "Fonts", "", ["Off/off.zip/A.ttf"], 1),
        (MEMBER_HIDDEN, "Fonts", "", ["Read/chosen.zip/B.ttf"], 1),
        (NOT_READ_INSIDE, "Fonts", "", ["Other/plain.zip/D.ttf"], 0),
        (NEXT_SCAN, "Photos", "", ["Back.jpg"], 0),
    ]
    assert all(r.count == len(r.files) for r in report.rules)
    assert report.total == 12  # b.tmp, under two patterns, once


def test_rules_say_where_they_live(engine: Engine) -> None:
    rules = {(r.kind, r.value): r for r in _report(engine).rules}
    pattern = rules[(PATTERN, "Drafts/**")]
    assert pattern.title == "Skip pattern “Drafts/**”"
    assert pattern.where.startswith("In the Skip box of Photos (Keep configuration")
    assert rules[(EXTENSION, ".bmp")].title == "The theme doesn't read .bmp"


def test_a_removed_pattern_reads_them_again_at_the_next_scan(engine: Engine) -> None:
    photos = RootConfig("p", "Photos", "/p", exclude=["**/*.tmp"])
    rules = {(r.kind, r.root_name): r for r in _report(engine, [photos, FONTS]).rules}
    assert sorted(f.relpath for f in rules[(NEXT_SCAN, "Photos")].files) == [
        "Back.jpg",
        "Drafts/a.jpg",
    ]
    assert rules[(FOLDERS, "")].count == 2  # Drafts is a folder the theme doesn't record


def test_nothing_skipped(tmp_path: Path) -> None:
    engine = create_keep_engine(tmp_path / "empty.db")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        assert skip_reasons(conn, [PHOTOS], THEME) == SkippedReport([], 0)
    engine.dispose()
