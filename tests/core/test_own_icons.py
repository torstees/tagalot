"""Your own icons for types, saved searches, and the keep (#557, DESIGN.md §12 *Icons*):
stored in keep.toml's [appearance] and saved searches' definitions; pictures copied into the
keep folder."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.movies import Movie, MoviesTheme
from tagalot.core.db import open_keep_database
from tagalot.core.icons import (
    IconFileError,
    copy_icon_picture,
    keep_icon,
    matching_icons,
    type_icon,
)
from tagalot.core.keep import (
    Keep,
    KeepConfigError,
    KeepError,
    ThemeRef,
    create_keep,
    dump_keep_config,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.keep_settings import with_keep_icon, with_type_icon
from tagalot.core.models import SavedSearch
from tagalot.core.saved_searches import (
    SavedDefinition,
    list_saved,
    restore_saved,
    save_search,
    set_search_icon,
)

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 3h18"/></svg>'


@pytest.fixture
def keep(tmp_path: Path) -> Keep:
    return create_keep(tmp_path / "Movies.keep", "Movies", ThemeRef("movies", 1))


def _with(keep: Keep, **changes: Any) -> Keep:
    return replace(keep, config=replace(keep.config, **changes))


def test_appearance_round_trip(keep: Keep) -> None:
    assert "[appearance" not in dump_keep_config(keep.config)  # absent unless chosen
    config = replace(keep.config, icon="popcorn", type_icons={"movies.movie": "icons/reel.svg"})
    save_keep_config(config, keep.toml_path)
    text = keep.toml_path.read_text(encoding="utf-8")
    assert "[appearance]\nicon = 'popcorn'" in text
    assert "[appearance.icons]\n'movies.movie' = 'icons/reel.svg'" in text
    loaded = load_keep_config(keep.toml_path)
    assert (loaded.icon, loaded.type_icons) == ("popcorn", {"movies.movie": "icons/reel.svg"})


def test_a_types_icons_without_the_keeps(keep: Keep) -> None:
    config = replace(keep.config, type_icons={"movies.movie": "popcorn"})
    save_keep_config(config, keep.toml_path)
    assert "[appearance]\n" not in keep.toml_path.read_text(encoding="utf-8")
    assert load_keep_config(keep.toml_path).type_icons == {"movies.movie": "popcorn"}


@pytest.mark.parametrize(
    "table", ["[appearance]\nicon = 5", "[appearance]\nicons = 'film'", "[appearance.icons]\na = 1"]
)
def test_bad_appearance(keep: Keep, table: str) -> None:
    text = keep.toml_path.read_text(encoding="utf-8") + "\n" + table + "\n"
    keep.toml_path.write_text(text, encoding="utf-8")
    with pytest.raises(KeepConfigError):
        load_keep_config(keep.toml_path)


def test_choosing_icons(keep: Keep) -> None:
    config = with_type_icon(keep.config, "movies.movie", "popcorn")
    config = with_type_icon(config, "movies.person", "icons/me.png")
    assert config.type_icons == {"movies.movie": "popcorn", "movies.person": "icons/me.png"}
    assert with_type_icon(config, "movies.movie", None).type_icons == {
        "movies.person": "icons/me.png"
    }
    assert with_keep_icon(config, "ticket").icon == "ticket"
    assert with_keep_icon(with_keep_icon(config, "ticket"), None).icon is None
    for wrong in ("flim", "../elsewhere.svg", "icons/notes.txt", "C:/pictures/me.png"):
        with pytest.raises(KeepError):
            with_type_icon(config, "movies.movie", wrong)


def test_the_keeps_choice_wins_over_the_themes(keep: Keep) -> None:
    assert type_icon(MoviesTheme, Movie, keep) == "film"
    assert keep_icon(MoviesTheme, keep) == "clapperboard"
    chosen = _with(keep, icon="ticket", type_icons={"movies.movie": "popcorn"})
    assert type_icon(MoviesTheme, Movie, chosen) == "popcorn"
    assert keep_icon(MoviesTheme, chosen) == "ticket"
    # A picture in the keep folder; one that's gone shows the theme's again.
    stored = copy_icon_picture(keep.dir, _picture(keep.dir.parent / "reel.svg"))
    pictured = _with(keep, type_icons={"movies.movie": stored})
    assert type_icon(MoviesTheme, Movie, pictured) == str(keep.dir / "icons" / "reel.svg")
    (keep.dir / "icons" / "reel.svg").unlink()
    assert type_icon(MoviesTheme, Movie, pictured) == "film"


def _picture(path: Path, data: bytes = SVG) -> Path:
    path.write_bytes(data)
    return path


def test_a_picture_is_copied_into_the_keep(keep: Keep, tmp_path: Path) -> None:
    source = _picture(tmp_path / "reel.svg")
    assert copy_icon_picture(keep.dir, source) == "icons/reel.svg"
    assert (keep.dir / "icons" / "reel.svg").read_bytes() == SVG
    assert copy_icon_picture(keep.dir, source) == "icons/reel.svg"  # the same: once
    (tmp_path / "other").mkdir()
    other = _picture(tmp_path / "other" / "reel.svg", SVG.replace(b"18", b"9"))
    assert copy_icon_picture(keep.dir, other) == "icons/reel 2.svg"  # same name, not same
    assert source.read_bytes() == SVG  # the original is never changed


def test_pictures_that_cant_be_icons(keep: Keep, tmp_path: Path) -> None:
    with pytest.raises(IconFileError, match="SVG or PNG"):
        copy_icon_picture(keep.dir, _picture(tmp_path / "notes.txt"))
    with pytest.raises(IconFileError, match="too big"):
        copy_icon_picture(keep.dir, _picture(tmp_path / "huge.png", b"x" * (1024 * 1024 + 1)))
    with pytest.raises(IconFileError, match="Couldn't read"):
        copy_icon_picture(keep.dir, tmp_path / "gone.svg")
    assert not (keep.dir / "icons").exists() or not any((keep.dir / "icons").iterdir())


def test_searching_icons_by_keyword() -> None:
    assert {"film", "clapperboard"} <= set(matching_icons("movie"))
    assert "user" in matching_icons("person")
    assert matching_icons("disc 3") == ["disc-3"]  # every word
    assert matching_icons("no such thing at all") == []
    assert len(matching_icons("")) > 150


def test_a_saved_searchs_icon(keep: Keep) -> None:
    _, engine = open_keep_database(keep)
    try:
        with engine.begin() as conn:
            saved_id = save_search(conn, "Favourites", SavedDefinition()).saved_id
            assert saved_id is not None
            change = set_search_icon(conn, saved_id, "star")
            assert change.label == "Change the icon of the saved search 'Favourites'"
            assert list_saved(conn)[0][2]["icon"] == "star"
            assert SavedDefinition.from_json(list_saved(conn)[0][2]).icon == "star"
            # Updating the search from its page keeps its icon.
            save_search(conn, "Favourites", SavedDefinition(layout="grid"), replace=saved_id)
            assert list_saved(conn)[0][2]["icon"] == "star"
            restore_saved(conn, change, forward=False)  # undo
            stored = conn.scalar(select(SavedSearch.definition))
            assert stored is not None
            assert "icon" not in stored
            set_search_icon(conn, saved_id, "star")
            set_search_icon(conn, saved_id, "")  # the standard one again
            assert "icon" not in list_saved(conn)[0][2]
    finally:
        engine.dispose()
    assert "icon" not in SavedDefinition().to_json()  # none unless chosen
