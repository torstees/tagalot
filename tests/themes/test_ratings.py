"""Ratings (#419): a field out of 5 or 10 shown as five stars in half-star steps, filled
from the files that carry one (EPUB, ComicInfo, front matter, audio tags), and the
user's to change."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from tagalot.builtin_themes.books import Book, BooksTheme, Comic
from tagalot.builtin_themes.movies import MoviesTheme
from tagalot.builtin_themes.music import Album, MusicTheme, Song, read_tags, song_rating
from tagalot.builtin_themes.research import Paper, ResearchTheme
from tagalot.core.fields import edit_field
from tagalot.core.formats import (
    format_rating_number,
    format_stars,
    format_value,
    half_stars,
    rating_of,
    rating_scale,
)
from tagalot.core.models import Entity, Resource
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import Theme, ThemeDeclarationError, entity_fields, field, read_rating
from tagalot.themes.loader import validate_theme
from tagalot.themes.readers import read_comic_info, read_epub, read_front_matter
from tests.core.book_files import comic_info, write_cbz, write_epub, write_markdown
from tests.core.media_files import write_flac, write_mp3
from tests.themes.test_books import T0, Env, _record, env

__all__ = ["env"]  # fixtures


@pytest.mark.parametrize(
    ("value", "scale", "stars"),
    [
        (3.5, 5, "★★★½☆"),
        (5, 5, "★★★★★"),
        (0, 5, "☆☆☆☆☆"),
        (7, 10, "★★★½☆"),  # out of 10: a point is a half star
        (7.3, 10, "★★★½☆"),  # a scraper's score: to the nearest half star
        (8.8, 10, "★★★★½"),
        (12, 10, "★★★★★"),  # never more than five
    ],
)
def test_stars(value: float, scale: int, stars: str) -> None:
    assert format_stars(value, scale) == stars
    assert format_value(value, f"stars/{scale}") == stars


def test_half_stars_and_back() -> None:
    assert half_stars(3.5, 5) == 7
    assert half_stars(7, 10) == 7
    assert rating_of(7, 5) == 3.5
    assert rating_of(7, 10) == 7.0
    assert rating_scale("stars/10") == 10
    assert rating_scale("duration") is None
    assert rating_scale(None) is None
    assert format_value(None, "stars/5") is None
    assert format_rating_number(3.5, 5) == "3.5 out of 5"
    assert format_rating_number(4.0, 5) == "4 out of 5"


@pytest.mark.parametrize(
    ("value", "out_of", "rating"),
    [
        (4, 5, 4.0),
        ("4.5", 5, 4.5),
        ("4.3", 5, 4.5),  # to the nearest half
        ("8/10", 5, 4.0),
        ("8.0", 10, 4.0),  # Calibre's, out of 10
        ("★★★½", 5, 3.5),
        ("***", 5, 3.0),
        (0, 5, None),  # unrated
        ("", 5, None),
        ("great", 5, None),
        (7, 5, None),  # more than it can be
        (True, 5, None),
        (None, 5, None),
    ],
)
def test_read_rating(value: Any, out_of: float, rating: float | None) -> None:
    assert read_rating(value, out_of=out_of) == rating


# --- declaring a rating ---


def test_a_rating_field_is_declared_with_its_scale() -> None:
    class Thing(ThemeEntity):
        rating: float | None = field("Rating", search="range", rating=5)

    (info,) = entity_fields(Thing)
    assert (info.spec.rating, info.spec.display) == (5, "stars/5")
    with pytest.raises(ThemeDeclarationError, match="must be 5 or 10"):
        field("Rating", rating=4)
    with pytest.raises(ThemeDeclarationError, match="takes no display"):
        field("Rating", rating=5, display="duration")


def test_the_loader_checks_ratings() -> None:
    class Thing(ThemeEntity):
        score: int | None = field("Score", rating=10)

    class Things(Theme):
        id, name, api_version = "things", "Things", 15
        entities = [Thing]

    assert validate_theme(Things) == [
        "Thing.score: a rating must be a float field",
        "Thing.score: a rating needs api_version = 16 (or later)",
    ]


@pytest.mark.parametrize("theme", [BooksTheme, MusicTheme, MoviesTheme, ResearchTheme])
def test_the_built_in_themes_rate(theme: type[Theme]) -> None:
    assert validate_theme(theme) == []
    scales = {
        e.__name__: f.spec.rating
        for e in theme.entities
        for f in entity_fields(e)
        if f.name == "rating"
    }
    expected = {
        BooksTheme: {"Author": 5, "Book": 5, "Comic": 5, "Series": 5, "Universe": 5},
        MusicTheme: {"Album": 5, "Artist": 5, "Song": 5},
        MoviesTheme: {"Movie": 10},
        ResearchTheme: {"Paper": 5},
    }[theme]
    assert scales == expected
    for entity in theme.entities:  # in lists and on cards, at first (#419 review)
        ratings = [f for f in entity_fields(entity) if f.name == "rating"]
        assert all(f.spec.card for f in ratings), entity.__name__
        assert ("rating" in entity.card_lines) == bool(ratings), entity.__name__
    assert Paper in ResearchTheme.entities
    assert Album in MusicTheme.entities


# --- from the files ---


def test_readers_give_ratings(tmp_path: Path) -> None:
    epub = write_epub(
        tmp_path / "a.epub", title="A", meta='<meta name="calibre:rating" content="9.0"/>'
    )
    assert read_epub(str(epub))["rating"] == 4.5  # Calibre's 9 out of 10
    assert read_epub(str(write_epub(tmp_path / "b.epub", title="B")))["rating"] is None
    comic = write_cbz(tmp_path / "c.cbz", comic_info(Series="Saga", CommunityRating="3.5"))
    info = read_comic_info(str(comic))
    assert info is not None
    assert info["rating"] == 3.5
    note = write_markdown(tmp_path / "d.md", "---\ntitle: D\nRating: 4\n---\n")
    front = read_front_matter(str(note))
    assert front["rating"] == 4.0
    assert "Rating" not in front["other"]  # a field, not an Extra one


def test_books_and_comics_take_their_files_ratings(env: Env) -> None:
    write_epub(
        env.files / "Kobo/Mort.epub",
        title="Mort",
        meta='<meta name="calibre:rating" content="8"/>',
    )
    write_cbz(
        env.files / "Comics/Saga 001.cbz",
        comic_info(Series="Saga", Number="1", CommunityRating="4.5"),
    )
    write_markdown(env.files / "Royal Road/story.md", "---\ntitle: Story\nrating: 3.5\n---\n")
    env.scan()
    assert env.fields(Book, "Mort")["rating"] == 4.0
    assert env.fields(Comic, "Saga #1")["rating"] == 4.5
    assert env.fields(Book, "Story")["rating"] == 3.5
    assert env.fields(Book, "Equal Rites")["rating"] is None


def test_the_users_rating_wins(env: Env) -> None:
    write_markdown(env.files / "Royal Road/story.md", "---\ntitle: Story\nrating: 3.5\n---\n")
    env.scan()
    with env.reader.connect() as conn:
        story = conn.scalar(select(Entity.id).where(Entity.title == "Story"))
    assert story is not None
    env.writer.run(lambda conn: edit_field(conn, env.schema, story, "rating", 2.0))
    write_markdown(env.files / "Royal Road/story.md", "---\ntitle: Story\nrating: 5\n---\n")
    env.writer.run(lambda conn: conn.execute(update(Resource).values(ingested_at=None)))
    env.scan(T0 + timedelta(minutes=1))
    assert env.fields(Book, "Story")["rating"] == 2.0  # never replaced by the file's


def test_write_to_file_writes_an_edited_rating() -> None:
    item = _record("Story", rating=4.0)
    assert BooksTheme().front_matter(Book, item, frozenset({"rating"}), {}) == {"rating": 4}
    half = _record("Story", rating=3.5)
    assert BooksTheme().front_matter(Book, half, frozenset({"rating"}), {}) == {"rating": 3.5}


@pytest.mark.parametrize(
    ("tags", "rating"),
    [
        ({"popm": "255"}, 5.0),
        ({"popm": "196"}, 4.0),  # as Windows Media Player writes four stars
        ({"popm": "128"}, 3.0),
        ({"popm": "64"}, 2.0),
        ({"popm": "1"}, 1.0),
        ({"rating": "4"}, 4.0),  # out of 5
        ({"rating": "90"}, 4.5),  # out of 100
        ({"rating": "4/5"}, 4.0),
        ({"fmps": "0.7"}, 3.5),  # 0 to 1
        ({}, None),
    ],
)
def test_song_ratings(tags: dict[str, str], rating: float | None) -> None:
    assert song_rating(**tags) == rating


def test_audio_files_ratings(tmp_path: Path) -> None:
    mp3 = tmp_path / "a.mp3"
    write_mp3(mp3, tags={"title": "A"}, popm=196)
    assert read_tags(str(mp3))["rating"] == 4.0
    flac = tmp_path / "b.flac"
    write_flac(flac, tags={"title": "B", "RATING": "60"})
    assert read_tags(str(flac))["rating"] == 3.0
    write_flac(tmp_path / "c.flac", tags={"title": "C", "FMPS_RATING": "1.0"})
    assert read_tags(str(tmp_path / "c.flac"))["rating"] == 5.0
    write_mp3(tmp_path / "d.mp3", tags={"title": "D"})
    assert read_tags(str(tmp_path / "d.mp3"))["rating"] is None
    assert Song in MusicTheme.entities


def test_a_rating_cells_tooltip_gives_the_number() -> None:
    from typing import cast

    from tagalot.ui.models.results import ResultColumn, Row, cell_text, cell_tooltip

    column = ResultColumn("rating", "Rating", numeric=True, display="stars/5")
    row = cast(Row, (None, {"rating": 3.5}))
    assert cell_text(column, row, {}) == "★★★½☆"
    assert cell_tooltip(column, row, {}) == "★★★½☆  3.5 out of 5"
    movie = ResultColumn("rating", "Rating", numeric=True, display="stars/10")
    assert cell_tooltip(movie, cast(Row, (None, {"rating": 8.8})), {}) == "★★★★½  8.8 out of 10"
    assert cell_tooltip(column, cast(Row, (None, {"rating": None})), {}) == ""
