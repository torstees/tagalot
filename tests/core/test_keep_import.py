"""Import keep… (#613): a keep export loaded into a fresh copy of the keep gives it back
everything done by hand; one undo step; a preview that changes nothing; conflicts."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, select

from tagalot.builtin_themes.research import Author, Paper, Venue
from tagalot.core.containers import contain_by_hand, uncontain_by_hand
from tagalot.core.entity_state import restore_states, snapshot_entities
from tagalot.core.exports import export_keep_file, read_keep
from tagalot.core.fields import edit_extra, edit_field, edit_note, edit_own_field
from tagalot.core.keep import open_keep, save_keep_config
from tagalot.core.keep_export import keep_data
from tagalot.core.keep_import import KeepImportError, read_export
from tagalot.core.keywords import set_ignored
from tagalot.core.links import link_files
from tagalot.core.merge import merge_items
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityNote,
    EntityResource,
    EntityTag,
    Resource,
)
from tagalot.core.own_fields import OwnField
from tagalot.core.relations import add_related, move_related
from tagalot.core.saved_searches import SavedDefinition, save_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import TagTree, add_tag, tag_entities
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import dismiss

ROOT = Path(__file__).resolve().parents[2]


def _demo(scratch: Path) -> Path:
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "make_demo_keep.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep: Path = module.make_research_demo(scratch)
    return keep


SCORE = OwnField("research.paper", "score", "Score", "integer")


@pytest.fixture
def keeps(tmp_path: Path) -> tuple[Path, Path]:
    """Two copies of the research demo, with a field of the keep's own: one to work in by
    hand, one fresh."""
    found = _demo(tmp_path / "a"), _demo(tmp_path / "b")
    for keep_dir in found:
        keep = open_keep(keep_dir)
        keep.config.fields = {SCORE.type_id: {SCORE.name: SCORE}}
        save_keep_config(keep.config, keep.toml_path)
    return found


def _ids(conn: Connection, schema: ThemeSchema, entity: type) -> list[int]:
    type_id = schema.entities[entity].type_id
    return list(conn.scalars(select(Entity.id).where(Entity.type == type_id).order_by(Entity.id)))


def _without_notes(conn: Connection, ids: list[int]) -> list[int]:
    noted = set(conn.scalars(select(EntityNote.entity_id)))
    return [i for i in ids if i not in noted]


def _work_by_hand(keep_dir: Path) -> None:
    """One of each kind of thing done by hand, through the keep's own commands."""
    with KeepSession.open(keep_dir, Settings()) as session:
        schema = session.schema

        def work(conn: Connection) -> None:
            papers = _without_notes(conn, _ids(conn, schema, Paper))
            authors = _ids(conn, schema, Author)
            first, second, third = papers[:3]
            tag = add_tag(conn, None, "Must read")
            child = add_tag(conn, tag, "This week")
            tag_entities(conn, [first, second], [child])
            edit_field(conn, schema, first, "title", "A Better Title")
            edit_field(conn, schema, first, "year", 1999)
            edit_field(conn, schema, second, "read_status", "Reading")
            edit_extra(conn, schema, first, "Shelf", "B4", new=True)
            edit_own_field(conn, schema, SCORE, first, 4)
            edit_note(conn, schema, second, "Read *twice*.\nThen again.")
            picture = conn.scalars(
                select(EntityResource.resource_id).where(EntityResource.entity_id == third)
            ).first()
            assert picture is not None
            link_files(conn, schema, first, [picture], "supplement")
            venue = _ids(conn, schema, Venue)[0]
            inside = set(
                conn.scalars(
                    select(EntityContains.child_id).where(EntityContains.parent_id == venue)
                )
            )
            outside = next(p for p in papers if p not in inside)
            contain_by_hand(conn, schema, venue, [outside])
            if inside:
                uncontain_by_hand(conn, schema, venue, [min(inside)])
            authorship = schema.relationships["authors"].table
            theirs: list[int] = list(
                conn.scalars(select(authorship.c.a_id).where(authorship.c.b_id == first))
            )
            other = next(a for a in authors if a not in theirs)
            add_related(conn, schema, "authors", other, first)
            move_related(conn, schema, "authors", first, [other], -1)
            merge_items(conn, schema, third, [papers[3]])
            save_search(
                conn,
                "To read",
                SavedDefinition(base=SearchSpec(include=(tag,), within=venue)),
            )
            set_ignored(conn, ["misc"], True)
            dismiss(conn, "untagged", [third])

        session.writer.run(work)


def _export(keep_dir: Path) -> dict[str, Any]:
    opened = read_keep(keep_dir, Settings())
    try:
        with opened.reader.connect() as conn:
            data = keep_data(conn, opened.schema, TagTree.load(conn), opened.keep.config)
    finally:
        opened.reader.dispose()
    del data["exported"]
    return data


def _import(keep_dir: Path, file: Path, **options: Any) -> Any:
    with KeepSession.open(keep_dir, Settings()) as session:
        data = read_export(file.read_text(encoding="utf-8"), "research")
        return session.tags.import_keep(data, file.name, base=file.parent, **options)


@pytest.fixture
def exported(keeps: tuple[Path, Path], tmp_path: Path) -> Path:
    worked, _ = keeps
    _work_by_hand(worked)
    target = tmp_path / "out" / "Research keep export.yaml"
    export_keep_file(worked, target, Settings())
    return target


def test_a_fresh_keep_gets_everything_back(keeps: tuple[Path, Path], exported: Path) -> None:
    worked, fresh = keeps
    wanted = _export(worked)
    assert {"items", "merged", "saved searches", "keywords", "dismissed"} <= set(wanted)
    assert _export(fresh) != wanted

    plan = _import(fresh, exported)
    assert plan.changes
    assert not plan.not_found, plan.not_found
    assert not plan.conflicts
    assert not plan.problems, plan.problems
    assert _export(fresh) == wanted

    again = _import(fresh, exported)  # what is there already is left alone
    assert not again.changes, (again.changed, again.records)


def test_one_undo_step(keeps: tuple[Path, Path], exported: Path) -> None:
    worked, fresh = keeps
    before, wanted = _export(fresh), _export(worked)
    with KeepSession.open(fresh, Settings()) as session:
        data = read_export(exported.read_text(encoding="utf-8"), "research")
        session.tags.import_keep(data, exported.name, base=exported.parent)
        assert session.tags.undo_label == f"Import keep export {exported.name!r}"
        undone = session.tags.undo()
        assert undone == f"Import keep export {exported.name!r}"
        assert session.tags.redo_label == undone
        assert session.tags.undo_label != undone
    assert _export(fresh) == before
    with KeepSession.open(fresh, Settings()) as session:
        session.tags.import_keep(data, exported.name, base=exported.parent)
        session.tags.undo()
        session.tags.redo()
    assert _export(fresh) == wanted


def test_the_preview_changes_nothing(keeps: tuple[Path, Path], exported: Path) -> None:
    _, fresh = keeps
    before = _export(fresh)
    previewed = _import(fresh, exported, preview=True)
    assert _export(fresh) == before
    done = _import(fresh, exported)
    assert previewed == done


def test_values_set_by_hand_here_are_conflicts(keeps: tuple[Path, Path], exported: Path) -> None:
    worked, fresh = keeps
    wanted = _export(worked)
    paper = next(i for i in wanted["items"] if i["title"] == "A Better Title")
    with KeepSession.open(fresh, Settings()) as session:
        schema = session.schema

        def mine(conn: Connection) -> None:
            entity = conn.scalar(select(Entity.id).where(Entity.ingest_key == paper["key"]))
            assert entity is not None
            edit_field(conn, schema, entity, "year", 2001)

        session.writer.run(mine)
    kept = _import(fresh, exported)
    assert any("Year is '2001' here, '1999' in the file" in c for c in kept.conflicts)
    item = next(i for i in _export(fresh)["items"] if i.get("key") == paper["key"])
    assert item["fields"]["year"] == 2001
    replaced = _import(fresh, exported, replace=True)
    assert replaced.replace
    assert any("Year is '2001' here" in c for c in replaced.conflicts)
    item = next(i for i in _export(fresh)["items"] if i.get("key") == paper["key"])
    assert item["fields"]["year"] == 1999


def test_items_not_found_are_listed(keeps: tuple[Path, Path], tmp_path: Path) -> None:
    _, fresh = keeps
    file = tmp_path / "small.yaml"
    file.write_text(
        "tagalot: keep export\nformat: 1\nkeep: Research\ntheme: research\n"
        "items:\n- {type: Paper, title: Not Here At All, note: hello}\n",
        encoding="utf-8",
    )
    plan = _import(fresh, file)
    assert plan.not_found == ("Paper “Not Here At All”",)
    assert not plan.changes


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("tagalot: tags\n", "isn't a keep export"),
        ("tagalot: keep export\nformat: 99\ntheme: research\n", "newer version"),
        ("tagalot: keep export\nformat: 1\ntheme: movies\n", "'movies' theme"),
        ("{", "isn't readable YAML"),
    ],
)
def test_files_it_refuses(text: str, message: str) -> None:
    with pytest.raises(KeepImportError, match=message):
        read_export(text, "research")


def test_resource_paths_are_relative(keeps: tuple[Path, Path]) -> None:
    """The two demo copies name their files alike: what a fresh scan finds again."""
    worked, fresh = keeps
    paths = []
    for keep in (worked, fresh):
        opened = read_keep(keep, Settings())
        try:
            with opened.reader.connect() as conn:
                paths.append(sorted(conn.scalars(select(Resource.relpath))))
        finally:
            opened.reader.dispose()
    assert paths[0] == paths[1]


def test_putting_an_item_back_keeps_its_file_tags(keeps: tuple[Path, Path]) -> None:
    """Undoing a change to an item puts its tags back as they were: a tag from its files'
    keywords stays one (found while building #613)."""
    _, fresh = keeps
    with KeepSession.open(fresh, Settings()) as session:
        schema = session.schema

        def check(conn: Connection) -> None:
            entity = conn.scalar(select(EntityTag.entity_id).where(EntityTag.by_file.is_(True)))
            assert entity is not None
            rows = set(
                conn.execute(
                    select(EntityTag.tag_id, EntityTag.by_file).where(EntityTag.entity_id == entity)
                ).all()
            )
            before = snapshot_entities(conn, schema, [entity])
            edit_field(conn, schema, entity, "year", 1900)
            restore_states(conn, schema, before)
            after = set(
                conn.execute(
                    select(EntityTag.tag_id, EntityTag.by_file).where(EntityTag.entity_id == entity)
                ).all()
            )
            assert after == rows

        session.writer.run(check)
