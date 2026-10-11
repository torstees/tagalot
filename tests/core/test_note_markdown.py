"""Showing a note's body (#380, DESIGN.md §9 *Notes*): Obsidian's embeds and links as plain
Markdown, its pictures read from the note's folder, its links resolved from there."""

import ntpath
import posixpath
from pathlib import Path

import pytest

from tagalot.core.note_markdown import (
    MAX_PICTURES,
    picture_refs,
    read_pictures,
    renderable,
    resolve_link,
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("![[sky.png]]", "![sky.png](<sky.png>)"),
        ("![[Art/sky at dusk.jpg|300]]", "![Art/sky at dusk.jpg](<Art/sky at dusk.jpg>)"),
        ("![[Other note]]", "Other note"),  # an embedded note: its name
        ("See [[Keanu Reeves]].", "See Keanu Reeves."),
        ("See [[Keanu Reeves|Keanu]].", "See Keanu."),
        ("See [[Keanu Reeves#Films]].", "See Keanu Reeves."),
        (
            "A [link](https://example.com) and ![p](a.png)",
            "A [link](https://example.com) and ![p](a.png)",
        ),
    ],
)
def test_renderable(body: str, expected: str) -> None:
    assert renderable(body) == expected


def test_picture_refs() -> None:
    body = renderable(
        '![a](sky.png) ![b](<my pic.jpg> "title") <img src="x/y.gif" width=3> '
        "![[z.webp]] ![again](sky.png) ![web](https://example.com/w.png)"
    )
    assert picture_refs(body) == [
        "sky.png",
        "my pic.jpg",
        "z.webp",
        "https://example.com/w.png",
        "x/y.gif",
    ]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("sky.png", r"\\nas\art\Aurora\sky.png"),
        ("Previews/dusk%20one.png", r"\\nas\art\Aurora\Previews\dusk one.png"),
        ("../Other/b.md#top", r"\\nas\art\Other\b.md"),
        ("https://example.com/a.png", None),
        ("mailto:me@example.com", None),
        ("#heading", None),
        ("/etc/passwd", None),
        (r"C:\Windows\x.png", None),
        ("", None),
    ],
)
def test_resolving_links_on_windows(ref: str, expected: str | None) -> None:
    assert resolve_link(r"\\nas\art\Aurora", ref, ntpath) == expected


def test_resolving_links_on_posix() -> None:
    assert resolve_link("/mnt/art/Aurora", "Previews/a.png", posixpath) == (
        "/mnt/art/Aurora/Previews/a.png"
    )


def test_reading_pictures(tmp_path: Path) -> None:
    (tmp_path / "Previews").mkdir()
    (tmp_path / "Previews" / "dusk one.png").write_bytes(b"one")
    (tmp_path / "sky.jpg").write_bytes(b"two")
    (tmp_path / "notes.txt").write_bytes(b"not a picture")
    body = renderable(
        "![a](Previews/dusk%20one.png) ![[sky.jpg]] ![gone](gone.png) ![t](notes.txt) "
        "![web](https://example.com/w.png)"
    )
    assert read_pictures(body, str(tmp_path)) == {
        "Previews/dusk%20one.png": b"one",
        "sky.jpg": b"two",
    }


def test_reading_at_most_some_pictures(tmp_path: Path) -> None:
    for n in range(MAX_PICTURES + 3):
        (tmp_path / f"{n}.png").write_bytes(b"p")
    body = " ".join(f"![{n}]({n}.png)" for n in range(MAX_PICTURES + 3))
    assert len(read_pictures(body, str(tmp_path))) == MAX_PICTURES
