"""Search semantics that use containment (DESIGN.md §8): inheritance, within, aggregate_up,
show_contained, and their combinations, over a small music library that is a DAG."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, insert

from tagalot.core import closure
from tagalot.core.db import create_keep_engine
from tagalot.core.fts import sync_entities
from tagalot.core.models import Base, Entity, EntityTag, Tag
from tagalot.core.search import child_hits, count_matches, run_search
from tagalot.core.search_spec import SearchSpec, TextFilter
from tagalot.core.tags import TagTree

ARTIST, ALBUM, SONG = "music.artist", "music.album", "music.song"
GENRE, ROCK, JAZZ, MOOD, CALM, XMAS, FAV = range(1, 8)
TAGS = [
    (GENRE, None, "Genre"),
    (ROCK, GENRE, "Rock"),
    (JAZZ, GENRE, "Jazz"),
    (MOOD, None, "Mood"),
    (CALM, MOOD, "Calm"),
    (XMAS, None, "Christmas"),
    (FAV, None, "Favorite"),
]
# id: (type, title, tags)
ENTITIES = {
    1: (ARTIST, "The Beatles", [ROCK]),
    10: (ALBUM, "Abbey Road", []),
    100: (SONG, "Come Together", []),
    101: (SONG, "Something", [FAV]),
    11: (ALBUM, "A Beatles Christmas", [XMAS]),
    110: (SONG, "Christmas Time", []),
    111: (SONG, "Snow Song", [FAV]),
    2: (ARTIST, "Miles Davis", [JAZZ]),
    20: (ALBUM, "Kind of Blue", []),
    200: (SONG, "So What", []),
    201: (SONG, "Blue in Green", [CALM]),
    30: (ALBUM, "Greatest Hits", []),  # a compilation: 100 and 200 are also on it
    300: (SONG, "Loose Single", [ROCK]),
}
EDGES = [
    (1, 10), (10, 100), (10, 101),
    (1, 11), (11, 110), (11, 111),
    (2, 20), (20, 200), (20, 201),
    (30, 100), (30, 200),
]  # fmt: skip
SONGS = {i for i, (t, _, _) in ENTITIES.items() if t == SONG}


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path_factory.mktemp("keep") / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(Tag), [{"id": i, "parent_id": p, "name": n} for i, p, n in TAGS])
        conn.execute(
            insert(Entity),
            [{"id": i, "type": t, "title": ti} for i, (t, ti, _) in ENTITIES.items()],
        )
        conn.execute(
            insert(EntityTag),
            [
                {"entity_id": i, "tag_id": tag}
                for i, (_, _, tags) in ENTITIES.items()
                for tag in tags
            ],
        )
        closure.add_entities(conn, ENTITIES)
        closure.apply(conn, added=EDGES)
        sync_entities(conn, ENTITIES)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def tree(engine: Engine) -> TagTree:
    with engine.connect() as conn:
        return TagTree.load(conn)


CASES = {
    # Inheritance: tags on ancestors count; also for exclusion (§8).
    "direct tag only": (SearchSpec(include=(ROCK,)), {1, 300}),
    "direct, songs only": (SearchSpec(types=(SONG,), include=(ROCK,)), {300}),
    "inherited from the artist": (
        SearchSpec(types=(SONG,), include=(ROCK,), inherit_tags=True),
        {100, 101, 110, 111, 300},
    ),
    "inherited parent tag": (
        SearchSpec(types=(SONG,), include=(GENRE,), inherit_tags=True),
        SONGS,
    ),
    "exclusion is inherited too": (
        SearchSpec(types=(SONG,), include=(ROCK,), exclude=(XMAS,), inherit_tags=True),
        {100, 101, 300},
    ),
    "exclusion without inheritance": (SearchSpec(types=(SONG,), exclude=(XMAS,)), SONGS),
    "inheritance includes own tags": (
        SearchSpec(types=(SONG,), include=(FAV,), inherit_tags=True),
        {101, 111},
    ),
    "inherited AND own": (
        SearchSpec(include=(JAZZ, CALM), inherit_tags=True),
        {201},
    ),
    "inherited through both DAG paths": (
        SearchSpec(types=(SONG,), include=(JAZZ,), inherit_tags=True),
        {200, 201},
    ),
    # Within: descendants only, never the entity itself.
    "within an artist": (SearchSpec(within=1), {10, 11, 100, 101, 110, 111}),
    "within a compilation": (SearchSpec(within=30), {100, 200}),
    "within, with a tag": (SearchSpec(within=10, include=(FAV,)), {101}),
    "within, inherited exclusion": (
        SearchSpec(within=1, exclude=(XMAS,), inherit_tags=True),
        {10, 100, 101},
    ),
    "within a song": (SearchSpec(within=100), set()),
    # aggregate_up: a container matches if a descendant does.
    "albums, no aggregation": (SearchSpec(types=(ALBUM,), include=(FAV,)), set()),
    "albums via their songs": (
        SearchSpec(types=(ALBUM,), include=(FAV,), aggregate_up=True),
        {10, 11},
    ),
    "artists via songs two levels down": (
        SearchSpec(types=(ARTIST,), include=(FAV,), aggregate_up=True),
        {1},
    ),
    "an excluded container stays excluded": (
        SearchSpec(types=(ALBUM,), include=(FAV,), exclude=(XMAS,), aggregate_up=True),
        {10},
    ),
    "aggregated text": (
        SearchSpec(types=(ALBUM,), text="what", aggregate_up=True),
        {20, 30},
    ),
    "aggregated with inheritance": (
        SearchSpec(types=(ALBUM,), include=(CALM,), inherit_tags=True, aggregate_up=True),
        {20},
    ),
    "own match still counts": (
        SearchSpec(types=(ALBUM,), text="blue", aggregate_up=True),
        {20},
    ),
    "field filters apply to the container": (
        SearchSpec(
            types=(ALBUM,),
            include=(FAV,),
            fields=(TextFilter("title", "abbey"),),
            aggregate_up=True,
        ),
        {10},
    ),
    "aggregation needs a positive filter": (
        SearchSpec(types=(ALBUM,), aggregate_up=True),
        {10, 11, 20, 30},
    ),
    # show_contained: matches plus their descendants, whatever the types.
    "an album and its songs": (
        SearchSpec(types=(ALBUM,), include=(XMAS,), show_contained=True),
        {11, 110, 111},
    ),
    "an artist and everything under it": (
        SearchSpec(types=(ARTIST,), include=(JAZZ,), show_contained=True),
        {2, 20, 200, 201},
    ),
    "contained items still pass exclusion": (
        SearchSpec(types=(ALBUM,), text="abbey", exclude=(FAV,), show_contained=True),
        {10, 100},
    ),
    "contained with inherited exclusion": (
        SearchSpec(
            types=(ARTIST,),
            include=(ROCK,),
            exclude=(XMAS,),
            inherit_tags=True,
            show_contained=True,
        ),
        {1, 10, 100, 101},
    ),
    "contained within": (
        SearchSpec(within=1, types=(ALBUM,), include=(XMAS,), show_contained=True),
        {11, 110, 111},
    ),
    "overlapping containers list each item once": (
        SearchSpec(types=(ALBUM,), include=(ROCK,), inherit_tags=True, show_contained=True),
        {10, 11, 100, 101, 110, 111},
    ),
    "a song reached through two albums": (
        SearchSpec(types=(ALBUM,), text="greatest", show_contained=True),
        {30, 100, 200},
    ),
    "aggregate and show contained together": (
        SearchSpec(types=(ALBUM,), include=(CALM,), aggregate_up=True, show_contained=True),
        {20, 200, 201},
    ),
    # nest (the tree's top level): matches held by another match are listed under it.
    "nested: albums and loose songs": (
        SearchSpec(types=(ALBUM, SONG), nest=True),
        {10, 11, 20, 30, 300},
    ),
    "nested: everything under the artists": (SearchSpec(nest=True), {1, 2, 30, 300}),
    "nested: songs whose album doesn't match stay": (
        SearchSpec(types=(ALBUM, SONG), include=(FAV,), nest=True),
        {101, 111},
    ),
    "nested: held by any matching container, at any depth": (
        SearchSpec(types=(ARTIST, SONG), include=(ROCK,), inherit_tags=True, nest=True),
        {1, 300},  # the Beatles' songs are under it, through their albums
    ),
    "nest is ignored with show contained": (
        SearchSpec(types=(ALBUM,), include=(XMAS,), show_contained=True, nest=True),
        {11, 110, 111},
    ),
}


@pytest.mark.parametrize(("spec", "expected"), CASES.values(), ids=CASES.keys())
def test_containment_semantics(
    engine: Engine, tree: TagTree, spec: SearchSpec, expected: set[int]
) -> None:
    with engine.connect() as conn:
        hits = run_search(conn, spec, tree, limit=None)
        count = count_matches(conn, spec, tree)
    ids = [h.id for h in hits]
    assert set(ids) == expected
    assert len(ids) == len(set(ids)), "no entity is listed twice"
    assert count == len(ids)


# --- the tree layout's children (#91) ---

CHILD_CASES = {
    "an artist's albums, by title": (1, SearchSpec(), ["A Beatles Christmas", "Abbey Road"]),
    "an album's songs": (10, SearchSpec(), ["Come Together", "Something"]),
    "a compilation shares songs": (30, SearchSpec(), ["Come Together", "So What"]),
    "includes and text don't filter children": (
        10,
        SearchSpec(include=(JAZZ,), text="zzz"),
        ["Come Together", "Something"],
    ),
    "excluded children are left out": (1, SearchSpec(exclude=(XMAS,)), ["Abbey Road"]),
    "exclusion inherited from the album": (
        11,
        SearchSpec(exclude=(XMAS,), inherit_tags=True),
        [],
    ),
    "without inheritance, only the song's own tags": (
        11,
        SearchSpec(exclude=(FAV,)),
        ["Christmas Time"],
    ),
    "a song holds nothing": (100, SearchSpec(), []),
}


@pytest.mark.parametrize("case", CHILD_CASES, ids=list(CHILD_CASES))
def test_child_hits(engine: Engine, tree: TagTree, case: str) -> None:
    parent, spec, expected = CHILD_CASES[case]
    with engine.connect() as conn:
        assert [h.title for h in child_hits(conn, spec, tree, parent)] == expected
