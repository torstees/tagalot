"""Theme dashboard cards: declared statistics, top values, and method cards (#114)."""

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.music import Album, MusicTheme, Song
from tagalot.core.dashboard import CardRow, ThemeCard, theme_cards
from tagalot.core.formats import format_duration
from tagalot.core.models import Entity as Entity_
from tagalot.core.search_spec import ChoiceFilter, RangeFilter
from tagalot.core.theme_schema import build_theme_schema
from tagalot.themes.api import (
    CardRows,
    DashboardContext,
    Entity,
    Theme,
    ThemeDeclarationError,
    dashboard_card,
    field,
    stat,
    top_values,
)
from tagalot.themes.loader import validate_theme
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures


class MusicWithCards(MusicTheme):
    """Music, plus method cards: one that works and one that fails."""

    @dashboard_card("Longest songs")
    def longest(self, ctx: DashboardContext) -> CardRows:
        songs = [ctx.get(s) for s in ctx.find(Song)]
        songs.sort(key=lambda r: -(r.fields.get("duration") or 0))
        total = ctx.stat(Song, "duration", "max")
        rows = [(r.title, f"{r.fields['duration']:.0f} s") for r in songs[:2]]
        return [*rows, ("Of", f"{ctx.count(Song):,} songs, longest {total:.0f} s")]

    @dashboard_card("Broken")
    def broken(self, ctx: DashboardContext) -> CardRows:
        raise RuntimeError("theme bug")


def _durations(env: Env) -> list[tuple[str, float]]:
    table = env.schema.entities[Song].table
    with env.reader.connect() as conn:
        rows = conn.execute(
            select(Entity_.title, table.c.duration)
            .join(table, table.c.id == Entity_.id)
            .where(table.c.duration.is_not(None))
        ).all()
    return [(title, duration) for title, duration in rows]  # two songs are "Intro"


def _cards(env: Env, theme: type[MusicTheme] = MusicTheme) -> dict[str, ThemeCard]:
    schema = build_theme_schema(theme)
    with env.reader.connect() as conn:
        return {c.title: c for c in theme_cards(conn, schema)}


def test_the_music_themes_cards(env: Env) -> None:
    env.scan()
    cards = _cards(env)
    assert list(cards) == ["Total running time", "Average song", "Top genres", "Top years"]
    durations = _durations(env)
    total = sum(d for _, d in durations)
    assert cards["Total running time"].rows == (CardRow(format_duration(total)),)
    assert cards["Top genres"].rows == (
        CardRow("Jazz", "1", ("music.album", ChoiceFilter("genre", ("Jazz",)))),
    )
    # A range field links to an exact range.
    assert cards["Top years"].rows == (
        CardRow("1959", "1", ("music.album", RangeFilter("year", 1959, 1959))),
    )


def test_method_cards_and_a_failing_one(env: Env) -> None:
    env.scan()
    cards = _cards(env, MusicWithCards)
    assert list(cards)[-2:] == ["Longest songs", "Broken"]  # after the declared ones
    longest = cards["Longest songs"]
    assert longest.error is None
    durations = _durations(env)
    title, seconds = max(durations, key=lambda d: d[1])
    assert longest.rows[0] == CardRow(title, f"{seconds:.0f} s")
    assert longest.rows[-1].label == "Of"
    assert cards["Broken"].error == "RuntimeError: theme bug"
    assert cards["Broken"].rows == ()


def test_declarations_are_checked() -> None:
    with pytest.raises(ThemeDeclarationError, match="how='median'"):
        stat("Median", Song, "duration", "median")
    with pytest.raises(ThemeDeclarationError, match="limit must be at least 1"):
        top_values("None", Album, "genre", limit=0)

    class Other(Entity):
        note: str | None = field("Note")

    class Bad(Theme):
        id, name = "bad", "Bad"
        entities = [Other]
        dashboard = [
            stat("Total notes", Other, "note", "sum"),  # text can't be summed
            stat("Notes", Other, "note", "count"),  # but can be counted
            top_values("Missing", Other, "nope"),
            stat("Elsewhere", Song, "duration"),
        ]

    assert validate_theme(Bad) == [
        "dashboard card 'Total notes': sum needs a number field (int or float)",
        "dashboard card 'Missing': Other has no field 'nope'",
        "dashboard card 'Elsewhere' uses Song, which the theme doesn't declare",
    ]
    assert validate_theme(MusicWithCards) == []
