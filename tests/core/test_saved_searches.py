"""Saved searches (#127): the definition, and saving, replacing, renaming, deleting, undo."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from tagalot.core.db import open_keep_database
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import SavedSearch
from tagalot.core.saved_searches import (
    SavedDefinition,
    SavedSearchError,
    delete_search,
    find_by_name,
    list_saved,
    rename_search,
    restore_saved,
    save_search,
)
from tagalot.core.search_spec import RangeFilter, SearchSpec, SortKey

DEFINITION = SavedDefinition(
    base=SearchSpec(types=("movies.movie",), inherit_tags=True, sort=(SortKey("year", True),)),
    filters=SearchSpec(
        types=("movies.movie",),
        include=(3,),
        exclude=(4,),
        text="heat",
        within=9,
        fields=(RangeFilter("year", 1990, 1999),),
        show_contained=True,
    ),
    within_title="Michael Mann Crime",
    within_type="movies.collection",
    layout="grid",
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    _, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("generic", 1)))
    return engine


def test_a_definition_round_trips() -> None:
    assert SavedDefinition.from_json(DEFINITION.to_json()) == DEFINITION
    # A bare search (an older or hand-made row) opens with no chips.
    bare = SavedDefinition.from_json(SearchSpec(text="x").to_json())
    assert bare == SavedDefinition(base=SearchSpec(text="x"))
    newer = {**DEFINITION.to_json(), "version": 99}
    with pytest.raises(SavedSearchError, match="newer version"):
        SavedDefinition.from_json(newer)
    with pytest.raises(SavedSearchError):
        SavedDefinition.from_json("nope")
    odd = {**DEFINITION.to_json(), "layout": "carousel"}
    assert SavedDefinition.from_json(odd).layout == "list"


def test_saving_renaming_deleting_and_undo(engine: Engine) -> None:
    with engine.begin() as conn:
        first = save_search(conn, "  90s   films ", DEFINITION)
        assert first.label == "Save the search '90s films'"
        assert first.saved_id is not None
        second = save_search(conn, "Alpha", SavedDefinition())
        assert [n for _, n, _ in list_saved(conn)] == ["90s films", "Alpha"]
        assert find_by_name(conn, "90S FILMS") == first.saved_id
        with pytest.raises(SavedSearchError, match="already a saved search named 'alpha'"):
            save_search(conn, "alpha", DEFINITION)
        with pytest.raises(SavedSearchError, match="needs a name"):
            save_search(conn, "   ", DEFINITION)

        update = save_search(conn, "Alpha", DEFINITION, replace=second.saved_id)
        assert update.label == "Update the saved search 'Alpha'"
        assert update.saved_id == second.saved_id
        stored = conn.scalar(
            select(SavedSearch.definition).where(SavedSearch.id == second.saved_id)
        )
        assert SavedDefinition.from_json(stored) == DEFINITION

        renamed = rename_search(conn, second.saved_id, "Beta")  # type: ignore[arg-type]
        assert renamed.label == "Rename the saved search 'Alpha' to 'Beta'"
        with pytest.raises(SavedSearchError, match="already"):
            rename_search(conn, second.saved_id, "90s Films")  # type: ignore[arg-type]

        deleted = delete_search(conn, first.saved_id)
        assert [n for _, n, _ in list_saved(conn)] == ["Beta"]
        restore_saved(conn, deleted, forward=False)  # undo: back, with its id
        assert find_by_name(conn, "90s films") == first.saved_id
        restore_saved(conn, deleted, forward=True)
        assert find_by_name(conn, "90s films") is None

        restore_saved(conn, renamed, forward=False)
        restore_saved(conn, update, forward=False)
        assert [n for _, n, _ in list_saved(conn)] == ["Alpha"]
        stored = conn.scalar(
            select(SavedSearch.definition).where(SavedSearch.id == second.saved_id)
        )
        assert SavedDefinition.from_json(stored) == SavedDefinition()
        restore_saved(conn, second, forward=False)
        assert list_saved(conn) == []
        with pytest.raises(SavedSearchError, match="no longer exists"):
            delete_search(conn, 999)
