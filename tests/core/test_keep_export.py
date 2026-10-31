"""The keep export (#612): everything done by hand, named so a fresh scan can find it again,
on the research demo with one of each kind of hand-made record."""

import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image
from sqlalchemy import Connection, insert, select, update

from tagalot.__main__ import main
from tagalot.builtin_themes.research import Author, Paper, ResearchTheme, Venue
from tagalot.core.db import open_keep_database
from tagalot.core.exports import ExportError, export_keep_file
from tagalot.core.fields import edit_extra, edit_field, edit_note
from tagalot.core.keep import open_keep
from tagalot.core.keep_export import FORMAT, keep_data
from tagalot.core.models import (
    ArchiveRead,
    DedupeDismissal,
    Entity,
    EntityField,
    EntityMerge,
    EntityMergeResource,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    FileTagRemoval,
    ItemPicture,
    KeywordIgnored,
    LinkedFolder,
    LinkedPlace,
    NoteKeyRemoval,
    PictureIgnore,
    Resource,
    SavedSearch,
    TriageDismissal,
    UserContains,
    UserOrder,
    UserRelation,
)
from tagalot.core.settings import Settings
from tagalot.core.tag_pictures import prepare_picture, set_tag_picture
from tagalot.core.tags import TagTree, add_tag
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def research(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "make_demo_keep.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep: Path = module.make_research_demo(tmp_path / "scratch")
    return keep


def _ids(conn: Connection, schema: ThemeSchema, entity: type) -> list[int]:
    type_id = schema.entities[entity].type_id
    return list(conn.scalars(select(Entity.id).where(Entity.type == type_id).order_by(Entity.id)))


def _file(conn: Connection, entity_id: int) -> int:
    found = conn.scalar(
        select(EntityResource.resource_id).where(EntityResource.entity_id == entity_id)
    )
    assert found is not None
    return int(found)


def _seed(keep_dir: Path) -> dict[str, Any]:
    """One of each kind of hand-made record; what the test expects to find, by name."""
    schema = build_theme_schema(ResearchTheme)
    _, engine = open_keep_database(open_keep(keep_dir))
    try:
        with engine.begin() as conn:
            papers, authors = _ids(conn, schema, Paper), _ids(conn, schema, Author)
            project = _ids(conn, schema, Venue)[0]  # a container of papers
            first, second, third = papers[:3]
            tag = add_tag(conn, None, "Must read")
            child = add_tag(conn, tag, "This week")
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8), (10, 200, 10)).save(buffer, "PNG")
            set_tag_picture(conn, tag, prepare_picture(buffer.getvalue()))
            conn.execute(insert(EntityTag).values(entity_id=first, tag_id=child, by_file=False))
            conn.execute(insert(EntityTag).values(entity_id=second, tag_id=tag, by_file=True))
            edit_field(conn, schema, first, "title", "A Better Title")
            edit_field(conn, schema, first, "year", 1999)
            edit_extra(conn, schema, first, "Shelf", "B4", new=True)
            conn.execute(insert(EntityField).values(entity_id=first, field="score", integer=4))
            edit_note(conn, schema, second, "Read *twice*.\nThen again.")
            picture = _file(conn, third)
            conn.execute(
                insert(ItemPicture).values(entity_id=first, source="user", resource_id=picture)
            )
            conn.execute(
                insert(EntityResource).values(
                    entity_id=first, resource_id=picture, role="extra", by_user=True
                )
            )
            conn.execute(insert(UserContains).values(parent_id=project, child_id=first, added=True))
            conn.execute(
                insert(UserContains).values(parent_id=project, child_id=third, added=False)
            )
            conn.execute(
                insert(UserRelation).values(name="authors", a_id=authors[0], b_id=first, added=True)
            )
            conn.execute(insert(UserOrder).values(name="authors", b_id=first))
            conn.execute(insert(FileTagRemoval).values(entity_id=second, tag_id=tag))
            conn.execute(insert(NoteKeyRemoval).values(entity_id=second, key="status"))
            # From the item's note, not by hand: left out.
            conn.execute(insert(EntityField).values(entity_id=second, field="score", integer=2))
            conn.execute(update(Entity).where(Entity.id == second).values(extra={"Mood": "calm"}))
            conn.execute(
                insert(FieldProvenance).values(
                    [
                        {"entity_id": second, "field": "own:score", "source": FieldSource.NOTE},
                        {"entity_id": second, "field": "extra:Mood", "source": FieldSource.NOTE},
                    ]
                )
            )
            merged = 900_000
            conn.execute(
                insert(EntityMerge).values(
                    merged_id=merged,
                    type=schema.entities[Paper].type_id,
                    ingest_key="old:key",
                    into_id=third,
                )
            )
            conn.execute(
                insert(EntityMergeResource).values(
                    merged_id=merged, resource_id=_file(conn, second), role="paper"
                )
            )
            files = list(conn.scalars(select(Resource.id).order_by(Resource.id)))
            conn.execute(
                insert(LinkedPlace).values(resource_id=files[-1], first_id=files[-2], how="user")
            )
            root = conn.scalar(select(Resource.root_id))
            conn.execute(
                insert(LinkedFolder).values(
                    root_id=root, relpath="Copies", first_root_id=root, first_relpath="Papers"
                )
            )
            conn.execute(
                insert(SavedSearch).values(
                    name="To read",
                    definition={
                        "version": 1,
                        "base": {"include": [tag], "exclude": [], "within": project},
                        "filters": {},
                    },
                )
            )
            conn.execute(insert(KeywordIgnored).values(match_key="misc"))
            conn.execute(insert(ArchiveRead).values(resource_id=files[0], mode="none"))
            conn.execute(
                insert(TriageDismissal).values(list="unlinked", target_id=files[1], marker="x")
            )
            conn.execute(
                insert(TriageDismissal).values(list="untagged", target_id=third, marker="x")
            )
            conn.execute(insert(DedupeDismissal).values(kind="exact", key="ab12", marker="1,2"))
            conn.execute(
                insert(DedupeDismissal).values(kind="similar", key=f"{first}:{second}", marker="")
            )
            conn.execute(insert(PictureIgnore).values(scope="value", value="cover.png"))
            relpath = {rid: rp for rid, rp in conn.execute(select(Resource.id, Resource.relpath))}
            titles = dict(conn.execute(select(Entity.id, Entity.title)).all())
            authorship = schema.relationships["authors"].table
            author_ids: list[int] = list(
                conn.scalars(
                    select(authorship.c.a_id)
                    .where(authorship.c.b_id == first)
                    .order_by(authorship.c.position, authorship.c.a_id)
                )
            )
            in_order = [titles[a] for a in author_ids]
        return {
            "first": "A Better Title",
            "second": titles[second],
            "third": titles[third],
            "author": titles[authors[0]],
            "authors in order": in_order,
            "project": titles[project],
            "picture": relpath[picture],
            "merged file": relpath[_file_of(engine, second)],
            "place": relpath[files[-1]],
            "same as": relpath[files[-2]],
            "root": root,
        }
    finally:
        engine.dispose()


def _file_of(engine: Any, entity_id: int) -> int:
    with engine.connect() as conn:
        return _file(conn, entity_id)


def _export(keep_dir: Path) -> dict[str, Any]:
    from tagalot.core.exports import read_keep

    opened = read_keep(keep_dir, Settings())
    try:
        with opened.reader.connect() as conn:
            return keep_data(conn, opened.schema, TagTree.load(conn), opened.keep.config)
    finally:
        opened.reader.dispose()


def _item(data: dict[str, Any], title: str) -> dict[str, Any]:
    return next(i for i in data["items"] if i["title"] == title)


def test_everything_done_by_hand(research: Path) -> None:
    seen = _seed(research)
    data = _export(research)
    assert data["tagalot"] == "keep export"
    assert data["format"] == FORMAT
    assert data["theme"] == "research"
    assert data["tags"]["Must read"]["tags"] == {"This week": None}

    first = _item(data, seen["first"])
    assert first["type"] == "Paper"
    assert "Must read > This week" in first["tags"]
    assert first["fields"] == {"title": "A Better Title", "year": 1999}
    assert first["extra fields"] == {"Shelf": "B4"}
    assert first["own fields"] == {"score": 4}
    assert first["picture"] == seen["picture"]
    assert first["files linked"] == [{"role": "extra", "file": seen["picture"]}]
    assert [r["title"] for r in first["put in"]] == [seen["project"]]
    assert [r["title"] for r in first["ordered"]["authors"]] == seen["authors in order"]

    second = _item(data, seen["second"])
    assert second["note"] == "Read *twice*.\nThen again."
    assert "Must read" not in second.get("tags", [])  # that one came from a file's keyword
    assert second["file tags removed"] == ["Must read"]
    assert second["note fields removed"] == ["status"]
    assert "own fields" not in second  # from its note
    assert "extra fields" not in second
    third = _item(data, seen["third"])
    assert [r["title"] for r in third["taken out of"]] == [seen["project"]]
    author = _item(data, seen["author"])
    assert [r["title"] for r in author["related"]["authors"]] == [seen["first"]]

    assert data["merged"] == [
        {
            "item": {"type": "Paper", "key": "old:key", "files": [seen["merged file"]]},
            "into": {
                k: v
                for k, v in _item(data, seen["third"]).items()
                if k in ("type", "title", "key", "files")
            },
        }
    ]
    assert data["places"][0] == {
        "file": {"folder": seen["root"], "path": seen["place"]},
        "same as": {"folder": seen["root"], "path": seen["same as"]},
    }
    assert data["places"][1]["folder"] == {"folder": seen["root"], "path": "Copies"}
    search = data["saved searches"][0]
    assert search["name"] == "To read"
    assert search["search"]["base"]["include"] == ["Must read"]  # a path, not an id
    assert search["search"]["base"]["within"]["title"] == seen["project"]
    assert data["keywords"] == {"ignored": ["misc"]}
    assert data["archives"][0]["read"] == "none"
    dismissed = data["dismissed"]
    assert len(dismissed["unlinked files"]) == 1
    assert [r["title"] for r in dismissed["untagged items"]] == [seen["third"]]
    assert dismissed["identical files"] == ["ab12"]
    assert {r["title"] for r in dismissed["similar items"][0]} == {seen["first"], seen["second"]}
    assert dismissed["picture problems"] == [{"value": "cover.png"}]


def test_a_keep_with_only_tagging(research: Path) -> None:
    data = _export(research)  # the demo as made: tagged, nothing else done by hand
    assert list(data) == [
        "tagalot",
        "format",
        "keep",
        "theme",
        "exported",
        "folders",
        "tags",
        "items",
    ]
    assert {k for i in data["items"] for k in i} <= {"type", "title", "key", "files", "tags"}


def test_the_file_and_its_pictures(research: Path, tmp_path: Path) -> None:
    _seed(research)
    target = tmp_path / "out" / "Research export.yaml"
    done = export_keep_file(research, target, Settings())
    assert done.pictures == 1
    assert done.items >= 4
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Tagalot keep export")
    assert "picture: Research export pictures/Must read.jpg" in text
    assert (tmp_path / "out" / "Research export pictures" / "Must read.jpg").is_file()
    parsed = yaml.safe_load(text)
    assert parsed["items"] == _export(research)["items"]
    files = Path(open_keep(research).config.roots[0].path)
    with pytest.raises(ExportError, match="--allow-watched"):
        export_keep_file(research, files / "export.yaml", Settings())


def test_the_command_line(
    research: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(research)
    monkeypatch.setattr("tagalot.backup_tool.load_settings", Settings)  # not the real one
    target = tmp_path / "export.yaml"
    monkeypatch.setattr(
        sys, "argv", ["tagalot", "--export-keep", str(research), "--to", str(target)]
    )
    assert main() == 0
    assert capsys.readouterr().out.startswith("ok  exported 6 tags and 4 items to ")
    assert target.is_file()
    monkeypatch.setattr(sys, "argv", ["tagalot", "--export-keep", str(research)])
    assert main() == 2
    assert capsys.readouterr().out.startswith("error: --export-keep needs --to FILE")


def test_merged_twice_names_where_it_ended(research: Path) -> None:
    _seed(research)
    schema = build_theme_schema(ResearchTheme)
    _, engine = open_keep_database(open_keep(research))
    try:
        with engine.begin() as conn:
            papers = _ids(conn, schema, Paper)
            type_id = schema.entities[Paper].type_id
            # 900001 was merged into 900002, itself merged later into the last paper.
            conn.execute(
                insert(EntityMerge).values(
                    merged_id=900_001, type=type_id, ingest_key="a", into_id=900_002
                )
            )
            conn.execute(
                insert(EntityMerge).values(
                    merged_id=900_002, type=type_id, ingest_key="b", into_id=papers[-1]
                )
            )
            last = conn.scalar(select(Entity.title).where(Entity.id == papers[-1]))
            conn.execute(update(Entity).where(Entity.id == papers[-1]).values(title=last))
    finally:
        engine.dispose()
    merged = {m["item"]["key"]: m["into"]["title"] for m in _export(research)["merged"]}
    assert merged["a"] == last
    assert merged["b"] == last
