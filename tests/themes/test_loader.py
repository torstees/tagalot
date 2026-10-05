"""Tests for theme discovery, import, and validation (DESIGN.md §9 "Discovery")."""

import textwrap
from pathlib import Path

import pytest

from tagalot.themes.api import Entity, SortBy, Theme, contains, field
from tagalot.themes.loader import (
    ThemeCatalog,
    default_user_themes_dir,
    load_themes,
    validate_theme,
)
from tests.themes.test_api import MoviesTheme

GOOD = """
from datetime import date
from tagalot.themes.api import Entity, SearchView, Theme, field, role

class Track(Entity):
    year: int | None = field("Year", search="range")
    roles = [role("audio", kinds={{"audio"}}, primary=True)]

class {cls}(Theme):
    id, name, version = "{theme_id}", "{name}", 1
    extensions = {{".flac", ".mp3"}}
    entities = [Track]
    views = [SearchView("Tracks", [Track])]
"""


def _write(folder: Path, name: str, source: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _good(theme_id: str, cls: str = "T", name: str = "Good") -> str:
    return GOOD.format(theme_id=theme_id, cls=cls, name=name)


def _load(folder: Path, *extra: Path) -> ThemeCatalog:
    return load_themes(extra_dirs=extra, user_dir=folder, include_builtin=False)


def _messages(catalog: ThemeCatalog, path: Path) -> list[str]:
    return [p.message for p in catalog.problems_for(str(path))]


def test_loads_a_theme_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "tunes.py", _good("tunes"))
    catalog = _load(tmp_path)
    assert catalog.problems == []
    loaded = catalog.get("tunes")
    assert loaded is not None
    assert loaded.theme.name == "Good"
    assert (loaded.source, loaded.builtin) == (str(path), False)


def test_loads_a_package_theme_with_its_own_modules(tmp_path: Path) -> None:
    _write(tmp_path, "pkg_theme/helpers.py", "LABEL = 'From a helper'\n")
    _write(
        tmp_path,
        "pkg_theme/__init__.py",
        _good("pkgtheme")
        .replace('"Good"', "LABEL")
        .replace(
            "from datetime import date", "from datetime import date\nfrom .helpers import LABEL"
        ),
    )
    catalog = _load(tmp_path)
    assert catalog.problems == []
    loaded = catalog.get("pkgtheme")
    assert loaded is not None
    assert loaded.theme.name == "From a helper"


def test_extra_folders_are_searched_after_the_user_folder(tmp_path: Path) -> None:
    user, extra = tmp_path / "user", tmp_path / "extra"
    _write(user, "a.py", _good("aaa"))
    _write(extra, "b.py", _good("bbb"))
    assert set(_load(user, extra).themes) == {"aaa", "bbb"}


def test_ignored_and_missing_paths(tmp_path: Path) -> None:
    _write(tmp_path, "_private.py", "raise RuntimeError('never imported')")
    _write(tmp_path, ".hidden.py", "raise RuntimeError('never imported')")
    _write(tmp_path, "notes.txt", "not a theme")
    _write(tmp_path, "folder_without_init/theme.py", "raise RuntimeError('never imported')")
    catalog = _load(tmp_path, tmp_path / "does-not-exist")
    assert (catalog.themes, catalog.problems) == ({}, [])


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def broken(:\n", "SyntaxError"),
        ("import tagalot_nonexistent_module\n", "ModuleNotFoundError"),
        ("x = 1\nraise RuntimeError('boom at import')\n", "RuntimeError: boom at import"),
        ("import sys\nsys.exit(3)\n", "SystemExit"),
        ("x = 1\n", "defines no Theme subclass"),
    ],
)
def test_broken_files_are_reported_and_others_still_load(
    tmp_path: Path, source: str, message: str
) -> None:
    bad = _write(tmp_path, "bad.py", source)
    _write(tmp_path, "good.py", _good("good"))
    catalog = _load(tmp_path)  # never raises
    assert "good" in catalog.themes
    [problem] = _messages(catalog, bad)
    assert message in problem


def test_the_line_of_an_import_error_is_reported(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.py", "a = 1\nb = 2\nc = 1 / 0\n")
    [problem] = _messages(_load(tmp_path), bad)
    assert "line 3" in problem
    assert "ZeroDivisionError" in problem


def test_two_themes_in_one_module(tmp_path: Path) -> None:
    source = _good("one") + "\nclass Second(T):\n    id = 'two'\n"
    path = _write(tmp_path, "two.py", source)
    catalog = _load(tmp_path)
    assert catalog.themes == {}
    assert "defines 2 Theme subclasses (Second, T)" in _messages(catalog, path)[0]


def test_imported_theme_classes_dont_count(tmp_path: Path) -> None:
    # A module that imports another theme class still defines exactly one of its own.
    source = _good("mine") + "\nfrom tests.themes.test_api import MoviesTheme  # noqa\n"
    _write(tmp_path, "mine.py", source)
    assert set(_load(tmp_path).themes) == {"mine"}


def test_duplicate_ids_first_one_wins(tmp_path: Path) -> None:
    first = _write(tmp_path, "a_first.py", _good("same", name="First"))
    second = _write(tmp_path, "b_second.py", _good("same", name="Second"))
    catalog = _load(tmp_path)
    assert catalog.themes["same"].theme.name == "First"
    assert _messages(catalog, second) == [f"theme id 'same' is already provided by {first}"]


def test_reloading_a_folder_picks_up_edits(tmp_path: Path) -> None:
    path = _write(tmp_path, "edit.py", _good("edit", name="Before"))
    assert _load(tmp_path).themes["edit"].theme.name == "Before"
    _write(tmp_path, "edit.py", _good("edit", name="After"))
    assert _load(tmp_path).themes["edit"].theme.name == "After"
    assert path.exists()


BAD_DECLARATIONS = """
from datetime import date
from tagalot.themes.api import (
    DetailView, Entity, RoleImage, SearchView, Section, SortBy, Theme, action, contains, field,
    related, role
)

class Song(Entity):
    when: date = field("When")
    roles = [
        role("audio", kinds={"audio"}, primary=True),
        role("video", kinds={"video"}, primary=True),
        role("art", kinds={"image"}),
        role("art", kinds={"image"}),
    ]

class Album(Entity):
    pass

class Stranger(Entity):
    pass

class Bad(Theme):
    id, name, version = "bad", "", 0
    api_version = 99
    extensions = {".FLAC", "mp3"}
    entities = [Song, Album]
    containment = [contains(Album, Stranger)]
    relationships = [related("features", Song, Album)]
    views = [
        SearchView("Strangers", [Stranger]),
        SearchView("Songs", [Song], default_sort=[SortBy("tempo")]),
        DetailView(Song, [Section.role("cover"), Section.gallery("art"), Section.related("nope")]),
        DetailView(Stranger, [Section.fields()]),
    ]

    @action("Play", applies_to=["lyrics", Stranger])
    def play(self, entities, ctx):
        pass

    def thumbnail_chain(self, entity_type):
        if entity_type is Album:
            raise LookupError("no chain")
        return [RoleImage("poster"), "cover.jpg"]
"""


def test_declaration_problems_are_all_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.py", BAD_DECLARATIONS)
    catalog = _load(tmp_path)
    assert catalog.themes == {}
    problems = "\n".join(_messages(catalog, path))
    for expected in [
        "the theme needs a name",
        "version must be a positive integer, not 0",
        "needs theme API version 99; this Tagalot provides 4",
        "extension '.FLAC' must be lowercase",
        "extension 'mp3' must be lowercase and start with '.'",
        "Song: role art is declared twice",
        "Song: only one role can be primary (audio, video)",
        "containment uses Stranger, which the theme doesn't declare",
        "search view 'Strangers' uses Stranger",
        "search view 'Songs' sorts by unknown 'tempo'",
        "Song detail view: no role named 'cover'",
        "Song detail view: gallery 'art' needs a role with many=True",
        "Song detail view: no relationship named 'nope'",
        "detail view for Stranger, which isn't declared",
        "action 'play' applies to unknown role 'lyrics'",
        "action 'play' applies to undeclared Stranger",
        "Song thumbnails: no role named 'poster'",
        "Song thumbnails: 'cover.jpg' isn't a ThumbnailProvider",
        "Album thumbnails: thumbnail_chain failed: no chain",
        "Song.when: date and time fields must allow None",  # from the trial table build
    ]:
        assert expected in problems


def test_a_theme_without_entities() -> None:
    class Empty(Theme):
        id, name = "empty", "Empty"

    assert validate_theme(Empty) == ["the theme declares no entities"]


def test_the_design_example_is_valid() -> None:
    assert validate_theme(MoviesTheme) == []


def test_builtin_placeholders_are_skipped_quietly() -> None:
    catalog = load_themes(user_dir=Path("does-not-exist"))
    assert catalog.problems == []  # empty built-in modules are placeholders, not errors


def test_default_user_folder() -> None:
    path = default_user_themes_dir()
    assert (path.name, path.parent.name) == ("themes", "tagalot")


def test_a_same_size_edit_is_not_hidden_by_cached_bytecode(tmp_path: Path) -> None:
    # Python checks .pyc files by mtime (to the second) and size; "1" -> "2" keeps the size.
    path = _write(tmp_path, "quick.py", _good("quick"))
    assert _load(tmp_path).themes["quick"].theme.version == 1
    path.write_text(
        path.read_text(encoding="utf-8").replace('"Good", 1', '"Good", 2'), encoding="utf-8"
    )
    assert _load(tmp_path).themes["quick"].theme.version == 2
    assert not (tmp_path / "__pycache__").exists()  # user themes never leave .pyc files


def test_card_lines_and_thumbnail_sizes_are_checked() -> None:
    class Song(Entity):
        length: int | None = field("Length")
        card_lines = ("length", "tempo")

    class Songs(Theme):
        id, name = "songs", "Songs"
        entities = [Song]
        thumbnail_max = 8

    assert validate_theme(Songs) == [
        "Song: card line 'tempo' isn't one of its fields",
        "thumbnail_max must be a whole number from 16 to 2048, not 8",
    ]

    class TooBig(Theme):
        id, name = "big", "Big"
        entities = [Song]
        thumbnail_max, thumbnail_default = 512, 1024

    assert "thumbnail_default must be a whole number from 16 to thumbnail_max (512), not 1024" in (
        validate_theme(TooBig)
    )


def test_contents_sort_names_fields_of_what_the_type_contains() -> None:
    class Track(Entity):
        number: int | None = field("Number")

    class Record(Entity):
        year: int | None = field("Year")
        contents_sort = (SortBy("number"), SortBy("title"), SortBy("year"))

    class Records(Theme):
        id, name = "records", "Records"
        entities = [Record, Track]
        containment = [contains(Record, Track)]

    # A record's own year isn't something its tracks have.
    assert validate_theme(Records) == [
        "Record: contents_sort uses 'year', which nothing it contains has"
    ]


def test_write_back_names_declared_types_and_needs_api_3() -> None:
    class Note(Entity):
        pass

    class Other(Entity):
        pass

    class Notes(Theme):
        id, name, api_version = "notes", "Notes", 2
        entities = [Note]
        write_back = [Note, Other]

    assert validate_theme(Notes) == [
        f"write_back names {Other!r}, which isn't a declared entity type",
        "a theme with write_back must set api_version = 3 (or later)",
    ]
