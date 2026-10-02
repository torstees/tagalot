"""Merging items (#120): what moves, conflicts, undo, and staying merged through scans."""

import pytest
from sqlalchemy import Connection, select

from tagalot.core.detail import PATH_MARK, load_detail
from tagalot.core.fields import edit_extra, edit_field
from tagalot.core.ingest import IngestSession, merged_into
from tagalot.core.merge import MergeError, merge_items, plan_merge, restore_merge
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityMerge,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
)
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree, add_tag_path, tag_entities
from tests.core.test_ingest_context import Album, Artist, Env, Label, env

__all__ = ["env"]  # fixtures

DIR, COVER, BACK, SCAN = range(4)  # env.resources: Album, cover.jpg, back.jpg, scan1.jpg


def _setup(env: Env) -> dict[str, int]:
    """Two albums of one record: the copy has a tag, a label, an artist, a scan, a cover
    of its own, and values set by hand; both are in the same folder."""
    res = [r.id for r in env.resources]
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        kept = ctx.upsert(Album, "a1", title="Blue", year=1971)
        copy = ctx.upsert(Album, "a2", title="Blue (copy)", year=1972)
        artist = ctx.upsert(Artist, "joni", title="Joni Mitchell")
        label = ctx.upsert(Label, "reprise", title="Reprise")
        ctx.link(kept, res[DIR], "folder")
        ctx.link(kept, res[BACK], "cover")
        ctx.link(copy, res[DIR], "folder")
        ctx.link(copy, res[COVER], "cover")
        ctx.link(copy, res[SCAN], "scan")
        ctx.contain(artist, copy)
        ctx.relate("released_by", copy, label)
        ctx.flush()
        tag_entities(conn, [copy.id], [add_tag_path(conn, ["Folk"])])
        edit_field(conn, env.schema, kept.id, "year", 2000)
        edit_field(conn, env.schema, copy.id, "year", 1999)
        edit_extra(conn, env.schema, copy.id, "note", "the good pressing", new=True)
    return {"kept": kept.id, "copy": copy.id, "artist": artist.id, "label": label.id}


def _links(conn: Connection, entity_id: int) -> set[tuple[int, str, bool]]:
    rows = conn.execute(
        select(EntityResource.resource_id, EntityResource.role, EntityResource.by_user).where(
            EntityResource.entity_id == entity_id
        )
    )
    return {tuple(r) for r in rows}  # type: ignore[misc]


def test_the_plan_says_what_moves_and_what_conflicts(env: Env) -> None:
    ids = _setup(env)
    with env.engine.connect() as conn:
        plan = plan_merge(conn, env.schema, ids["kept"], [ids["copy"]])
    assert plan.keep == (ids["kept"], "Blue")
    assert plan.others == ((ids["copy"], "Blue (copy)"),)
    assert (plan.tags, plan.files, plan.containers, plan.contents, plan.related) == (1, 1, 1, 0, 1)
    # The folder is linked to both already; the kept album has a cover of its own.
    assert plan.unplaced == (f"R {PATH_MARK} Album/cover.jpg",)
    [year] = plan.conflicts
    assert (year.key, year.label, year.default) == ("field:year", "Year", ids["kept"])
    assert [(c.entity_id, c.text) for c in year.choices] == [
        (ids["kept"], "2000"),
        (ids["copy"], "1999"),
    ]


def test_merging_moves_everything_and_undo_puts_it_back(env: Env) -> None:
    ids = _setup(env)
    kept, copy = ids["kept"], ids["copy"]
    res = [r.id for r in env.resources]
    with env.engine.begin() as conn:
        change = merge_items(conn, env.schema, kept, [copy], {"field:year": copy})
    assert change.label == "Merge 'Blue (copy)' into 'Blue'"
    table = env.schema.entities[Album].table
    with env.engine.connect() as conn:
        assert conn.scalar(select(Entity.id).where(Entity.id == copy)) is None
        assert conn.scalar(select(table.c.year).where(table.c.id == kept)) == 1999  # chosen
        assert conn.scalar(select(Entity.extra).where(Entity.id == kept)) == {
            "note": "the good pressing"
        }
        assert set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == kept)))
        assert _links(conn, kept) == {
            (res[DIR], "folder", False),
            (res[BACK], "cover", False),  # the kept cover stays; the copy's is left over
            (res[SCAN], "scan", True),  # moved: the user's now
        }
        assert conn.execute(
            select(EntityContains.parent_id).where(EntityContains.child_id == kept)
        ).scalars().all() == [ids["artist"]]
        rel = env.schema.relationships["released_by"].table
        assert conn.execute(select(rel.c.a_id, rel.c.b_id)).all() == [(kept, ids["label"])]
        assert conn.execute(
            select(
                EntityMerge.merged_id, EntityMerge.type, EntityMerge.ingest_key, EntityMerge.into_id
            )
        ).all() == [(copy, "music.album", "a2", kept)]
        assert merged_into(conn, [copy, kept]) == {copy: kept}

    with env.engine.begin() as conn:
        restore_merge(conn, env.schema, change, forward=False)
    with env.engine.connect() as conn:
        assert conn.scalar(select(Entity.title).where(Entity.id == copy)) == "Blue (copy)"
        assert conn.scalar(select(table.c.year).where(table.c.id == kept)) == 2000
        assert conn.scalar(select(table.c.year).where(table.c.id == copy)) == 1999
        assert _links(conn, kept) == {(res[DIR], "folder", False), (res[BACK], "cover", False)}
        assert (res[SCAN], "scan", False) in _links(conn, copy)
        assert conn.scalar(select(EntityMerge.merged_id)) is None
        assert not set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == kept)))

    with env.engine.begin() as conn:
        restore_merge(conn, env.schema, change, forward=True)
    with env.engine.connect() as conn:
        assert conn.scalar(select(Entity.id).where(Entity.id == copy)) is None
        assert merged_into(conn, [copy]) == {copy: kept}


def test_the_kept_items_own_values_win_by_default(env: Env) -> None:
    ids = _setup(env)
    with env.engine.begin() as conn:
        merge_items(conn, env.schema, ids["kept"], [ids["copy"]])
        table = env.schema.entities[Album].table
        assert conn.scalar(select(table.c.year).where(table.c.id == ids["kept"])) == 2000
        source = conn.scalar(
            select(FieldProvenance.source).where(
                FieldProvenance.entity_id == ids["kept"], FieldProvenance.field == "year"
            )
        )
        assert source is FieldSource.USER


def test_a_value_set_only_on_the_other_item_comes_along(env: Env) -> None:
    ids = _setup(env)
    with env.engine.begin() as conn:
        edit_field(conn, env.schema, ids["copy"], "title", "Blue (remaster)")
        plan = plan_merge(conn, env.schema, ids["kept"], [ids["copy"]])
        assert [c.key for c in plan.conflicts] == ["field:year"]  # the title isn't one
        merge_items(conn, env.schema, ids["kept"], [ids["copy"]])
        assert conn.scalar(select(Entity.title).where(Entity.id == ids["kept"])) == (
            "Blue (remaster)"
        )


def test_a_merged_item_isnt_made_again(env: Env) -> None:
    ids = _setup(env)
    with env.engine.begin() as conn:
        merge_items(conn, env.schema, ids["kept"], [ids["copy"]])
    scan = env.resources[SCAN]
    with env.engine.begin() as conn:
        ctx = IngestSession(conn, env.schema)
        ref = ctx.upsert(Album, "a2", title="Blue (copy)", year=1)  # its file read again
        assert ref.id == ids["copy"]
        ctx.link(ref, scan, "scan")
        ctx.unlink(ref, env.resources[DIR], "folder")
        artist = ctx.upsert(Artist, "joni")
        ctx.contain(artist, ref)
        ctx.uncontain(artist, ref)
        label = ctx.upsert(Label, "reprise")
        ctx.relate("released_by", ref, label)
        ctx.update(ref, year=5)
        ctx.delete(ref)
        assert ctx.linked(ref) == []
        assert ctx.contents(ref) == []
        assert ctx.get(ref).title == "Blue"  # reads see the item it's part of
        ctx.flush()
        assert conn.scalar(select(Entity.id).where(Entity.id == ids["copy"])) is None
        assert conn.scalar(select(Entity.id).where(Entity.ingest_key == "a2")) is None
        table = env.schema.entities[Album].table
        assert conn.scalar(select(table.c.year).where(table.c.id == ids["kept"])) == 2000
        # The kept album still has what the merge gave it.
        assert (scan.id, "scan", True) in _links(conn, ids["kept"])
        assert conn.execute(
            select(EntityContains.parent_id).where(EntityContains.child_id == ids["kept"])
        ).scalars().all() == [ids["artist"]]


def test_merges_follow_further_merges(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        a, b, c = (ctx.upsert(Album, k).id for k in ("a", "b", "c"))
        ctx.flush()
        merge_items(conn, env.schema, b, [a])
        merge_items(conn, env.schema, c, [b])
        assert merged_into(conn, [a, b, c]) == {a: c, b: c}


def test_what_cant_be_merged(env: Env) -> None:
    ids = _setup(env)
    with env.engine.connect() as conn:
        with pytest.raises(MergeError, match="same type"):
            plan_merge(conn, env.schema, ids["kept"], [ids["artist"]])
        with pytest.raises(MergeError, match="at least two"):
            plan_merge(conn, env.schema, ids["kept"], [ids["kept"]])
        with pytest.raises(MergeError, match="no longer exists"):
            plan_merge(conn, env.schema, ids["kept"], [999_999])


def test_records_point_at_a_living_item_and_undo_puts_them_back(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        a, b, c = (ctx.upsert(Album, k).id for k in ("a", "b", "c"))
        ctx.flush()
        merge_items(conn, env.schema, b, [a])
        second = merge_items(conn, env.schema, c, [b])

        def records() -> dict[int, int]:
            rows = conn.execute(select(EntityMerge.merged_id, EntityMerge.into_id))
            return {m: i for m, i in rows}

        assert records() == {a: c, b: c}
        restore_merge(conn, env.schema, second, forward=False)
        assert records() == {a: b}
        restore_merge(conn, env.schema, second, forward=True)
        assert records() == {a: c, b: c}


def test_pages_and_searches_follow_a_merge(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        old, new = ctx.upsert(Artist, "old", title="Old"), ctx.upsert(Artist, "new", title="New")
        album = ctx.upsert(Album, "x", title="X")
        ctx.contain(old, album)
        ctx.flush()
        merge_items(conn, env.schema, new.id, [old.id])
        detail = load_detail(conn, env.schema, old.id, lambda _: None)
        assert detail is not None
        assert (detail.id, detail.title, detail.merged_from) == (new.id, "New", old.id)
        assert load_detail(conn, env.schema, 999_999, lambda _: None) is None
        hits = run_search(conn, SearchSpec(within=old.id), TagTree.load(conn))
        assert [h.title for h in hits] == ["X"]  # a saved search within the old artist


def test_a_merged_items_files_still_find_it(env: Env) -> None:
    """Themes that find an item through its file (``entities_of``) get the merged item,
    and what they write to it is dropped: they don't make a new item for the file."""
    ids = _setup(env)
    cover = env.resources[COVER]  # the copy's cover: left linked to nothing
    with env.engine.begin() as conn:
        merge_items(conn, env.schema, ids["kept"], [ids["copy"]])
        ctx = IngestSession(conn, env.schema)
        [ref] = ctx.entities_of(cover, "cover")
        assert ref.id == ids["copy"]
        assert ctx.entities_of(cover, "scan") == []
        ctx.link(ref, cover, "cover")
        ctx.flush()
        rows = conn.execute(
            select(EntityResource.entity_id).where(EntityResource.resource_id == cover.id)
        ).all()
        assert rows == []  # still linked to nothing


def test_new_items_are_numbered_past_merged_ones(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        a, b = ctx.upsert(Album, "a").id, ctx.upsert(Album, "b").id
        ctx.flush()
        merge_items(conn, env.schema, a, [b])  # b had the highest id
        new = env.session(conn).upsert(Album, "c").id
        assert new > b
