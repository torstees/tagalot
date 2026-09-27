"""Tests for the search query builder: types, tag groups, exclusion, text, fields, sorting."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityTag, Tag, entity_fts
from tagalot.core.search import SearchError, count_matches, run_search
from tagalot.core.search_spec import (
    ChoiceFilter,
    RangeFilter,
    SearchSpec,
    SortKey,
    TextFilter,
    TextMatch,
)
from tagalot.core.tags import TagTree

GENRE, ROCK, PUNK, CLASSIC, JAZZ, BEBOP, MOOD, CALM, UPBEAT, XMAS = range(1, 11)
TAGS = [
    (GENRE, None, "Genre"),
    (ROCK, GENRE, "Rock"),
    (PUNK, ROCK, "Punk"),
    (CLASSIC, ROCK, "Classic Rock"),
    (JAZZ, GENRE, "Jazz"),
    (BEBOP, JAZZ, "Bebop"),
    (MOOD, None, "Mood"),
    (CALM, MOOD, "Calm"),
    (UPBEAT, MOOD, "Upbeat"),
    (XMAS, None, "Christmas"),
]
SONG, ALBUM = "music.song", "music.album"
# id, type, title, fts body, tags, created (day of month)
ENTITIES = [
    (1, SONG, "Abbey Road", "The Beatles 1969", [CLASSIC], 1),
    (2, SONG, "Anarchy in the U.K.", "Sex Pistols", [PUNK, UPBEAT], 2),
    (3, SONG, "Blitzkrieg Bop", "Ramones", [PUNK], 3),
    (4, SONG, "So What", "Miles Davis", [BEBOP, CALM], 4),
    (5, ALBUM, "Kind of Blue", "Miles Davis 1959", [JAZZ, CALM], 5),
    (6, SONG, "Halo", "Beyoncé", [], 6),
    (7, SONG, "Jingle Bell Rock", "Bobby Helms", [CLASSIC, XMAS], 7),
    (8, ALBUM, "a_b 100% Hits", 'Say "AND" * OR', [], 8),
    (9, SONG, "abbey road (demo)", "", [UPBEAT], 9),
]


def _created(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(Tag), [{"id": i, "parent_id": p, "name": n} for i, p, n in TAGS])
        conn.execute(
            insert(Entity),
            [
                {
                    "id": i,
                    "type": t,
                    "title": ti,
                    "created_at": _created(d),
                    "updated_at": _created(d),
                }
                for i, t, ti, _, _, d in ENTITIES
            ],
        )
        conn.execute(
            insert(EntityTag),
            [{"entity_id": i, "tag_id": tag} for i, *_, tags, _ in ENTITIES for tag in tags],
        )
        conn.execute(
            insert(entity_fts),
            [{"rowid": i, "title": ti, "body": b} for i, _, ti, b, _, _ in ENTITIES],
        )
    yield engine
    engine.dispose()


@pytest.fixture
def tree(engine: Engine) -> TagTree:
    with engine.connect() as conn:
        return TagTree.load(conn)


def _ids(
    engine: Engine, tree: TagTree, spec: SearchSpec, offset: int = 0, limit: int | None = None
) -> list[int]:
    with engine.connect() as conn:
        return [hit.id for hit in run_search(conn, spec, tree, offset=offset, limit=limit)]


BY_TITLE = [8, 1, 9, 2, 3, 6, 7, 5, 4]  # case-insensitive title order


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param(SearchSpec(), BY_TITLE, id="everything, by title"),
        pytest.param(SearchSpec(types=(ALBUM,)), [8, 5], id="types"),
        pytest.param(SearchSpec(types=(ALBUM, SONG)), BY_TITLE, id="several types"),
        # Include: a tag matches itself and every descendant (§7, §8).
        pytest.param(SearchSpec(include=(PUNK,)), [2, 3], id="leaf tag"),
        pytest.param(SearchSpec(include=(ROCK,)), [1, 2, 3, 7], id="parent tag matches subtree"),
        pytest.param(SearchSpec(include=(GENRE,)), [1, 2, 3, 7, 5, 4], id="grandparent"),
        pytest.param(SearchSpec(include=(ROCK, UPBEAT)), [2], id="groups are ANDed"),
        pytest.param(SearchSpec(include=(JAZZ, CALM)), [5, 4], id="AND across trees"),
        pytest.param(SearchSpec(include=(GENRE, MOOD)), [2, 5, 4], id="AND of two subtrees"),
        # Exclude: any tag in any excluded subtree removes the item.
        pytest.param(SearchSpec(exclude=(XMAS,)), [8, 1, 9, 2, 3, 6, 5, 4], id="exclude"),
        pytest.param(SearchSpec(exclude=(GENRE,)), [8, 9, 6], id="exclude a subtree"),
        pytest.param(SearchSpec(exclude=(PUNK, JAZZ)), [8, 1, 9, 6, 7], id="exclude merged set"),
        pytest.param(SearchSpec(include=(ROCK,), exclude=(XMAS,)), [1, 2, 3], id="but not"),
        pytest.param(SearchSpec(include=(ROCK,), exclude=(ROCK,)), [], id="include and exclude"),
        # Tags missing from the tree (e.g. deleted after a search was saved).
        pytest.param(SearchSpec(include=(999,)), [], id="missing include matches nothing"),
        pytest.param(SearchSpec(exclude=(999,)), BY_TITLE, id="missing exclude excludes nothing"),
        pytest.param(SearchSpec(types=(SONG,), include=(CALM,)), [4], id="types and tags"),
    ],
)
def test_tags_and_types(
    engine: Engine, tree: TagTree, spec: SearchSpec, expected: list[int]
) -> None:
    assert _ids(engine, tree, spec) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("road", [1, 9]),  # substring, any case
        ("BEY", [1, 9, 6]),  # "Abbey", "Beyoncé"
        ("beyonce", [6]),  # diacritics ignored (trigram remove_diacritics)
        ("miles", [5, 4]),  # the body
        ("miles 1959", [5]),  # terms are ANDed, across title and body
        ("abbey demo", [9]),
        ("2031", []),
        ("ab", [1, 9]),  # 2 characters: LIKE fallback ("a_b" is not "ab")
        ("da", [5, 4]),  # 2 characters, found in the body ("Miles Davis")
        ("ab road", [1, 9]),  # a short term and a trigram term together
        ('"AND"', [8]),  # quotes and FTS keywords are just text
        ("AND", [8]),  # an FTS keyword, searched as plain text
        ("*", [8]),  # a lone * is a literal character
        ("100%", [8]),  # % is literal, not a wildcard
        ("a_", [8]),  # _ is literal
        ("   ", BY_TITLE),  # blank means no text filter
    ],
)
def test_text(engine: Engine, tree: TagTree, text: str, expected: list[int]) -> None:
    assert _ids(engine, tree, SearchSpec(text=text)) == expected


@pytest.mark.parametrize(
    ("f", "expected"),
    [
        (TextFilter("title", "ROAD"), [1, 9]),
        (TextFilter("title", "abbey", TextMatch.STARTS_WITH), [1, 9]),
        (TextFilter("title", "road", TextMatch.STARTS_WITH), []),  # not at the start
        (TextFilter("title", "a_", TextMatch.STARTS_WITH), [8]),  # _ literal
        (TextFilter("title", "a%", TextMatch.STARTS_WITH), []),  # % literal
        (TextFilter("title", "100%"), [8]),
        (RangeFilter("created_at", _created(3), _created(5)), [3, 5, 4]),
        (RangeFilter("created_at", None, _created(2)), [1, 2]),
        (RangeFilter("created_at", _created(8), None), [8, 9]),
        (RangeFilter("created_at"), BY_TITLE),
        (ChoiceFilter("title", ("Halo", "So What")), [6, 4]),
    ],
)
def test_field_filters(engine: Engine, tree: TagTree, f: object, expected: list[int]) -> None:
    assert _ids(engine, tree, SearchSpec(fields=(f,))) == expected  # type: ignore[arg-type]


def test_unknown_field(engine: Engine, tree: TagTree) -> None:
    with pytest.raises(SearchError, match="Unknown field 'year'"):
        _ids(engine, tree, SearchSpec(fields=(TextFilter("year", "19"),)))
    with pytest.raises(SearchError, match="Unknown field 'rating'"):
        _ids(engine, tree, SearchSpec(sort=(SortKey("rating"),)))


def test_sorting(engine: Engine, tree: TagTree) -> None:
    newest = SearchSpec(sort=(SortKey("created_at", descending=True),))
    assert _ids(engine, tree, newest) == [9, 8, 7, 6, 5, 4, 3, 2, 1]
    by_type_then_title = SearchSpec(sort=(SortKey("title", descending=True),))
    assert _ids(engine, tree, by_type_then_title) == list(reversed(BY_TITLE))


def test_ties_break_by_id_so_paging_is_stable(engine: Engine, tree: TagTree) -> None:
    same_title = SearchSpec(sort=())  # only the id tiebreak remains
    assert _ids(engine, tree, same_title) == list(range(1, 10))


def test_paging_and_count(engine: Engine, tree: TagTree) -> None:
    spec = SearchSpec()
    pages = [_ids(engine, tree, spec, offset=o, limit=4) for o in (0, 4, 8)]
    assert pages == [BY_TITLE[:4], BY_TITLE[4:8], BY_TITLE[8:]]
    with engine.connect() as conn:
        assert count_matches(conn, spec, tree) == 9
        assert count_matches(conn, SearchSpec(include=(ROCK,), exclude=(XMAS,)), tree) == 3
        assert count_matches(conn, SearchSpec(text="miles"), tree) == 2


def test_hits_carry_type_and_title(engine: Engine, tree: TagTree) -> None:
    with engine.connect() as conn:
        [hit] = run_search(conn, SearchSpec(include=(BEBOP,)), tree)
    assert (hit.id, hit.type, hit.title) == (4, SONG, "So What")


@pytest.mark.parametrize(
    "spec",
    [
        SearchSpec(inherit_tags=True),
        SearchSpec(show_contained=True),
        SearchSpec(aggregate_up=True),
        SearchSpec(within=1),
    ],
)
def test_containment_options_are_not_silently_ignored(
    engine: Engine, tree: TagTree, spec: SearchSpec
) -> None:
    with pytest.raises(NotImplementedError, match="Not supported yet"):
        _ids(engine, tree, spec)
