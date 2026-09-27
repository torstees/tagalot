"""A realistic 50k-entity keep for search benchmarks (DESIGN.md §8 performance target).

500 artists x 10 albums x 9 songs = 50,500 entities, a 3-level tree of 300 tags applied at
every level, closure rows, and search-index rows. A few albums are compilations, so the
containment graph is a DAG.
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, insert

from tagalot.core import closure
from tagalot.core.fts import rebuild_search_index
from tagalot.core.models import Entity, EntityTag, Tag

ARTISTS, ALBUMS_PER_ARTIST, SONGS_PER_ALBUM = 500, 10, 9
WORDS = [
    "love",
    "night",
    "blue",
    "road",
    "river",
    "fire",
    "gold",
    "rain",
    "dream",
    "heart",
    "city",
    "light",
    "shadow",
    "summer",
    "winter",
    "storm",
    "ocean",
    "silver",
    "echo",
    "wild",
    "stone",
    "glass",
    "paper",
    "ghost",
    "velvet",
    "thunder",
    "garden",
]


@dataclass(frozen=True)
class BenchKeep:
    artists: list[int]
    albums: list[int]
    songs: list[int]
    top_tags: list[int]
    """Top-level tags (each with a subtree of 9 children and 81 grandchildren... roughly)."""
    leaf_tags: list[int]


def build(conn: Connection, seed: int = 7) -> BenchKeep:
    rng = random.Random(seed)
    # Tags: 3 top-level x 10 x 9 = 300 tags plus the 3 roots' children.
    tags, top, leaves = [], [], []
    next_id = 1
    for t in range(3):
        top_id = next_id
        next_id += 1
        tags.append({"id": top_id, "parent_id": None, "name": f"Top {t}"})
        top.append(top_id)
        for m in range(10):
            mid_id = next_id
            next_id += 1
            tags.append({"id": mid_id, "parent_id": top_id, "name": f"Mid {t}.{m}"})
            for leaf in range(9):
                tags.append({"id": next_id, "parent_id": mid_id, "name": f"Leaf {t}.{m}.{leaf}"})
                leaves.append(next_id)
                next_id += 1
    conn.execute(insert(Tag), tags)

    start = datetime(2020, 1, 1, tzinfo=UTC)
    entities, edges, entity_tags = [], [], []
    artists, albums, songs = [], [], []
    eid = 0

    def title() -> str:
        return " ".join(rng.choice(WORDS).capitalize() for _ in range(rng.randint(1, 4)))

    def add(kind: str, n_tags: int) -> int:
        nonlocal eid
        eid += 1
        when = start + timedelta(minutes=eid)
        entities.append(
            {"id": eid, "type": kind, "title": title(), "created_at": when, "updated_at": when}
        )
        for tag in rng.sample(leaves, n_tags):
            entity_tags.append({"entity_id": eid, "tag_id": tag})
        return eid

    for _ in range(ARTISTS):
        artist = add("music.artist", 1)
        artists.append(artist)
        for _ in range(ALBUMS_PER_ARTIST):
            album = add("music.album", rng.randint(0, 1))
            albums.append(album)
            edges.append((artist, album))
            for _ in range(SONGS_PER_ALBUM):
                song = add("music.song", rng.randint(0, 2))
                songs.append(song)
                edges.append((album, song))
    # Compilations: some songs also appear on a second album.
    for song in rng.sample(songs, 2_000):
        edges.append((rng.choice(albums), song))

    for batch in range(0, len(entities), 5_000):
        conn.execute(insert(Entity), entities[batch : batch + 5_000])
    for batch in range(0, len(entity_tags), 5_000):
        conn.execute(insert(EntityTag), entity_tags[batch : batch + 5_000])
    closure.add_entities(conn, range(1, eid + 1))  # ids are 1..eid
    closure.apply(conn, added=edges)
    rebuild_search_index(conn)
    return BenchKeep(artists, albums, songs, top, leaves)
