"""Links in a note to other items: wiki links and links to note files (#671)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tagalot.core.db import open_keep_database
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import Entity, EntityNote, Resource, ResourceKind, Root
from tagalot.core.note_items import (
    LinkedItem,
    find_note_file_items,
    find_wiki_items,
    item_url,
    link_wiki_items,
    url_key,
    wiki_key,
    wiki_names,
)
from tagalot.core.note_markdown import renderable

NOTES = {
    # entity id: (title, type, note file or None)
    1: ("Alice Smith", "generic.person", "Notes/People/Alice.md"),
    2: ("Alice", "generic.folder", None),
    3: ("Bob", "generic.person", "Notes/People/bob.markdown"),
    4: ("Bob", "generic.folder", "Home/Bob/Bob.md"),
    5: ("Émile", "generic.person", None),
    6: ("Carol", "generic.person", "Notes/Carol_1.md"),
    7: ("Malice", "generic.person", "Notes/Malice.md"),
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    keep = create_keep(tmp_path / "k", "K", ThemeRef(id="generic", version=1))
    _, engine = open_keep_database(keep)
    with Session(engine) as session, session.begin():
        session.add(Root(id="r", name="Files"))
        session.flush()
        for entity_id, (title, type_, relpath) in NOTES.items():
            session.add(Entity(id=entity_id, type=type_, title=title))
            if relpath is not None:
                session.add(
                    Resource(id=entity_id, root_id="r", relpath=relpath, kind=ResourceKind.FILE)
                )
        session.flush()
        for entity_id, (_, _, relpath) in NOTES.items():
            if relpath is not None:
                session.add(EntityNote(entity_id=entity_id, resource_id=entity_id, body=""))
    yield engine
    engine.dispose()


def test_wiki_names_are_the_targets_of_links_not_embeds() -> None:
    body = "See [[Alice]], [[alice.md|her]], [[People/Bob#Early|Bob]] and ![[cover.png]]."
    assert wiki_names(body) == ["Alice", "People/Bob"]
    assert wiki_key(" People\\Alice.MD ") == "people/alice"


@pytest.mark.parametrize(
    ("name", "ids"),
    [
        ("Alice", [1]),  # its note file's name wins over the folder titled Alice
        ("alice.md", [1]),
        ("People/Alice", [1]),  # with a folder part, the end of the note's path
        ("Other/Alice", []),
        ("Bob", [3, 4]),  # two note files called Bob: both, to choose from
        ("Home/Bob/Bob", [4]),
        ("Home/Bob", []),  # names a Home/Bob.md
        ("Alice Smith", [1]),  # no note file called that: the items titled it
        ("émile", [5]),  # case ignored, past ASCII too
        ("Carol_1", [6]),  # LIKE's wildcards are taken as themselves
        ("Carol%1", []),
        ("lice", []),  # a note's whole name, not its end
        ("Nobody", []),
    ],
)
def test_a_wiki_link_names_items(engine: Engine, name: str, ids: list[int]) -> None:
    with engine.connect() as conn:
        found = find_wiki_items(conn, [name])
    assert [item.id for item in found.get(wiki_key(name), [])] == ids


def test_linked_wiki_links_become_links_and_the_rest_stay_text() -> None:
    body = "Met [[Alice|her]] and [[Nobody]] in [[People/Bob]]."
    linked = link_wiki_items(body, {wiki_key("Alice"), wiki_key("People/Bob")})
    alice, bob = item_url("Alice"), item_url("People/Bob")
    assert linked == f"Met [her]({alice}) and [[Nobody]] in [People/Bob]({bob})."
    assert renderable(linked).endswith("and Nobody in [People/Bob](tagalot-item:people%2Fbob).")
    assert url_key(item_url("People/Bob")) == "people/bob"
    assert url_key("https://example.com") is None


def test_links_to_note_files_name_their_items(engine: Engine, tmp_path: Path) -> None:
    root = tmp_path / "root"
    folder = root / "Notes" / "People"
    folder.mkdir(parents=True)
    body = (
        "[Bob](bob.markdown), [Carol](../Carol_1.md#top), [also Carol](../carol_1.md), "
        "[a list](list.txt), [missing](Nobody.md), [web](https://example.com/Alice.md)"
    )
    with engine.connect() as conn:
        found = find_note_file_items(conn, body, str(folder), [], {"r": str(root)})
    assert found == {
        str(folder / "bob.markdown"): [LinkedItem(3, "Bob", "generic.person")],
        str(root / "Notes" / "Carol_1.md"): [LinkedItem(6, "Carol", "generic.person")],
        str(root / "Notes" / "carol_1.md"): [LinkedItem(6, "Carol", "generic.person")],
    }


def test_a_link_outside_the_watched_folders_names_nothing(engine: Engine, tmp_path: Path) -> None:
    with engine.connect() as conn:
        found = find_note_file_items(conn, "[Bob](bob.markdown)", str(tmp_path), [], {"r": None})
    assert found == {}
