"""Create a demo keep in ``scratch/`` for trying Tagalot by hand.

Usage (from the repository folder):

    uv run python scripts/make_demo_keep.py                   # create it if it doesn't exist
    uv run python scripts/make_demo_keep.py --reset           # delete and recreate it
    uv run tagalot scratch/Demo.keep
    uv run python scripts/make_demo_keep.py --reset --media   # the media keep instead
    uv run python scripts/make_demo_keep.py --reset --assets  # the 2D assets keep instead
    uv run python scripts/make_demo_keep.py --reset --music   # the music keep instead
    uv run python scripts/make_demo_keep.py --reset --books   # the books keep instead
    uv run python scripts/make_demo_keep.py --reset --research  # the research keep instead

``scratch/`` is gitignored. It holds ``Demo.keep`` (the keep) and ``demo-files/`` (the folder
it watches): a few real images, documents, nested folders, and names with accents and spaces,
plus operating-system leftovers (``.DS_Store``, ``._sunset.jpg``, ``Thumbs.db``) that the
default exclude patterns skip.

The keep is scanned and given a small tag tree (with an alias), applied to most files, so
tag filters can be tried before the tagging panel exists. Scanning again finds nothing new.

``--media`` creates ``scratch/Media.keep`` instead (the demo keep is left alone, so it can
stay open): artists, albums, and songs with a few tags,
for trying views with several entity types (Search all's sections) before the music theme
exists. Its small theme, ``tagalot_demo_media.py``, is installed in the user themes folder;
delete that file when you're done with the media demo.

``--assets`` creates ``scratch/Assets.keep`` watching ``scratch/asset-files`` with the built-in
assets2d theme: two artists' images (one with a ``folder.jpg``), a font, a zip and a 7z
archive of images, and a picture directly in the root (no artist), with a few tags.

``--music`` creates ``scratch/Music.keep`` watching ``scratch/music-files`` with the built-in
music theme: short silent MP3s with tags. There is an album with a cover and a song in two
versions (MP3 and FLAC; the FLAC holds no audio), an album with embedded cover art, a
two-disc album, a compilation, an untagged folder, and a song directly in the root.

``--books`` creates ``scratch/Books.keep`` watching ``scratch/book-files`` with the built-in
books theme: small EPUBs, PDFs, Markdown files, and comic archives in folders named for where
they came from (Kobo, Humble Bundle, Comixology, Royal Road, Loose). One book is in two stores
(one work, two files), two series are in reading order, a comic series is in a universe, a
PDF has a cover drawn from its first page, a Markdown story has front matter, some files have
no metadata (named from their file names or headings), and one book is a near-duplicate of
another. Genres come from the files' subjects (file keywords); "Humor" and "Epic Fantasy"
match no tag.

``--research`` creates ``scratch/Research.keep`` watching ``scratch/research-files`` with the
built-in research theme: small PDFs with a text layer holding DOIs and arXiv IDs. One paper
is two downloads (one paper, two versions), a preprint and its published version are one
paper, one PDF's title is an editor's leftover ("Microsoft Word - …", ignored), and one has
no metadata (a sidecar ``.bib`` beside it names it). A Zotero-style library export gives the
papers their venues, matched by arXiv ID and DOI; one of its entries has no PDF.
"""

import argparse
import base64
import inspect
import io
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import py7zr
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.builtin_themes.books import BooksTheme
from tagalot.builtin_themes.movies import MoviesTheme
from tagalot.builtin_themes.research import ResearchTheme
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import DEFAULT_EXCLUDES, RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityTag
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.loader import default_user_themes_dir, load_themes

REPO = Path(__file__).resolve().parent.parent
SCRATCH = REPO / "scratch"

IMAGES = {
    "Photos/Iceland/glacier.jpg": (70, 130, 180),
    "Photos/Iceland/geyser.jpg": (200, 200, 210),
    "Photos/Iceland 2024/northern lights.png": (40, 160, 90),
    "Photos/Beach/sunset.jpg": (240, 120, 40),
    "Photos/Beach/Family at the beach.jpg": (230, 200, 120),
}
TEXT = {
    "Documents/notes.txt": "Shopping list: bread, coffee, film for the camera.\n",
    "Documents/Café receipts/März.txt": "Latte 3.40\nCroissant 2.10\n",
    "Documents/tax 2025.pdf": "%PDF-1.4\n% A placeholder, not a real PDF.\n%%EOF\n",
    "readme.md": "# Demo files\nSample files for trying Tagalot.\n",
}
TAGS: dict[tuple[str, ...], list[str]] = {
    # tag path: titles of the files it is applied to (parents come before children)
    ("Places",): [],
    ("Places", "Iceland"): ["glacier.jpg", "geyser.jpg", "northern lights.png"],
    ("Places", "Beach"): ["sunset.jpg", "Family at the beach.jpg"],
    ("People",): [],
    ("People", "Family"): ["Family at the beach.jpg"],
    ("Topics",): [],
    ("Topics", "Money"): ["tax 2025.pdf", "März.txt"],
    ("Topics", "Sky"): ["sunset.jpg", "northern lights.png"],
    ("Favorites",): ["glacier.jpg", "Family at the beach.jpg"],
}
ALIASES = {("Topics", "Money"): ["finance"], ("Places", "Iceland"): ["Ísland"]}
DESCRIPTIONS = {
    ("Places", "Iceland"): "Summer 2019 road trip around the Ring Road",
    ("Topics", "Money"): "Receipts, taxes, and bills",
    ("Topics", "Sky"): "Sunsets, auroras, and clouds",
    ("Favorites",): "The ones worth printing",
}
JUNK = {
    # Operating-system leftovers that the default excludes skip.
    ".DS_Store": b"Bud1\x00\x00\x00\x01",
    "Photos/Beach/._sunset.jpg": b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X",
    "Photos/Iceland/Thumbs.db": b"\xd0\xcf\x11\xe0",
}


MEDIA_THEME_FILE = "tagalot_demo_media.py"
MEDIA_THEME = '''"""A tiny three-type theme for Tagalot's media demo (make_demo_keep.py --media).

Safe to delete; only scratch/Media.keep uses it.
"""

from tagalot.themes.api import Entity, Theme, action, field


class Artist(Entity):
    title_label = "Name"
    country: str | None = field("Country", card=True, search="choice")


class Album(Entity):
    year: int | None = field("Year", card=True, search="range")


class Song(Entity):
    length: int | None = field("Length (s)", card=True, search="range")


class DemoMedia(Theme):
    id, name, version = "demo_media", "Demo media", 1
    entities = [Artist, Album, Song]

    @action("Double the length", [Song])
    def double_length(self, songs, ctx):
        """Changes data, so Edit > Undo puts it back."""
        for song in songs:
            length = ctx.get(song).fields.get("length") or 0
            ctx.update(song, length=length * 2)
        ctx.message(f"Doubled the length of {len(songs)} songs.")

    @action("Save the titles", [Artist, Album, Song])
    def save_titles(self, items, ctx):
        """Writes a file of its own and opens it; changes no data."""
        path = ctx.temp_path("titles.txt")
        with open(path, "w", encoding="utf-8") as f:
            for item in items:
                print(ctx.get(item).title, file=f)
        ctx.open(path)
        ctx.message(f"Saved {len(items)} titles.")
'''
MEDIA: dict[str, list[tuple[str, dict[str, object]]]] = {
    "Artist": [
        ("Courtney Love", {"country": "US"}),
        ("Love", {"country": "US"}),
        ("Blur", {"country": "UK"}),
        ("The Beatles", {"country": "UK"}),
        ("Sade", {"country": "UK"}),
    ],
    "Album": [
        ("Love Deluxe", {"year": 1992}),
        ("Forever Changes", {"year": 1967}),
        ("Parklife", {"year": 1994}),
        ("Abbey Road", {"year": 1969}),
        ("Magical Mystery Tour", {"year": 1967}),
        ("Diamond Life", {"year": 1984}),
        ("Lovesexy", {"year": 1988}),
        ("Love Over Gold", {"year": 1982}),
        ("Love Is Here and Now You're Gone", {"year": 1967}),
        ("Lovers Rock", {"year": 2000}),
        ("Love Songs", {"year": 1977}),
    ],
    "Song": [
        ("All You Need Is Love", {"length": 228}),
        ("No Ordinary Love", {"length": 440}),
        ("Song 2", {"length": 122}),
        ("Come Together", {"length": 259}),
        ("Smooth Operator", {"length": 298}),
        ("Alone Again Or", {"length": 176}),
    ],
}
MEDIA_TAGS: dict[tuple[str, ...], list[str]] = {
    ("Genre",): [],
    ("Genre", "Rock"): ["Blur", "The Beatles", "Parklife", "Abbey Road", "Song 2", "Come Together"],
    ("Genre", "Soul"): ["Sade", "Love Deluxe", "Diamond Life", "No Ordinary Love"],
    ("Decade",): [],
    ("Decade", "1960s"): ["Abbey Road", "Magical Mystery Tour", "Forever Changes"],
    ("Decade", "1990s"): ["Love Deluxe", "Parklife"],
}


class DemoInUseError(Exception):
    """The demo keep or its files are open elsewhere (usually in Tagalot); nothing was deleted."""


def _remove(folders: list[Path]) -> None:
    """Delete ``folders`` completely, or not at all.

    Each is first renamed aside. Windows refuses to rename a folder while a file in it is open
    (Tagalot holding ``keep.db``), so an open keep stops the reset before anything is deleted,
    instead of leaving a half-deleted keep behind.
    """
    moved: list[tuple[Path, Path]] = []
    for folder in folders:
        if not folder.exists():
            continue
        aside = folder.with_name(folder.name + ".deleting")
        shutil.rmtree(aside, ignore_errors=True)  # left over from an interrupted reset
        try:
            folder.rename(aside)
        except OSError as e:
            for original, renamed in reversed(moved):
                renamed.rename(original)
            raise DemoInUseError(
                f"Can't reset: {folder} is in use ({e.strerror}). Close Tagalot (and any window "
                "showing that folder), then try again. Nothing was deleted."
            ) from e
        moved.append((folder, aside))
    for _, aside in moved:
        shutil.rmtree(aside)


def make_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/demo-files`` and ``scratch/Demo.keep``; returns the keep folder."""
    files, keep_dir = scratch / "demo-files", scratch / "Demo.keep"
    if reset:
        _remove([keep_dir, files])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")

    for relpath, color in IMAGES.items():
        path = files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (320, 240), color)
        ImageDraw.Draw(image).text((12, 12), path.stem, fill=(255, 255, 255))
        image.save(path)
    for relpath, content in TEXT.items():
        path = files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for relpath, data in JUNK.items():
        (files / relpath).write_bytes(data)

    create_keep(
        keep_dir,
        "Demo",
        ThemeRef("generic", 1),
        [RootConfig("demo", "Demo files", str(files), list(DEFAULT_EXCLUDES))],
    )
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        _tag(session, TAGS, ALIASES, DESCRIPTIONS)
    return keep_dir


def make_media_demo(
    scratch: Path = SCRATCH, *, reset: bool = False, themes_dir: Path | None = None
) -> Path:
    """Create ``scratch/Media.keep`` (artists, albums, songs, tags) and install its theme in
    ``themes_dir`` (default: the user themes folder); returns the keep folder."""
    keep_dir = scratch / "Media.keep"
    if reset:
        _remove([keep_dir])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")
    themes_dir = themes_dir or default_user_themes_dir()
    themes_dir.mkdir(parents=True, exist_ok=True)
    (themes_dir / MEDIA_THEME_FILE).write_text(MEDIA_THEME, encoding="utf-8")

    create_keep(keep_dir, "Media", ThemeRef("demo_media", 1))
    catalog = load_themes(user_dir=themes_dir)
    with KeepSession.open(keep_dir, Settings(), catalog=catalog) as session:
        classes = {e.__name__: e for e in session.theme.entities}

        def fill(conn: Connection) -> None:
            ctx = IngestSession(conn, session.schema)
            for type_name, items in MEDIA.items():
                for title, values in items:
                    ctx.upsert(classes[type_name], title, title=title, **values)
            ctx.flush()

        session.writer.run(fill)
        _tag(session, MEDIA_TAGS, {}, {})
    return keep_dir


ASSET_IMAGES = {
    # relative path: (size, background, accent)
    "Aurora Studio/folder.jpg": ((256, 256), (40, 30, 90), (250, 200, 80)),
    "Aurora Studio/Backgrounds/dusk sky.png": ((1920, 1080), (60, 40, 120), (250, 140, 60)),
    "Aurora Studio/Backgrounds/forest.png": ((1600, 900), (20, 70, 40), (120, 200, 90)),
    "Aurora Studio/Icons/gem.png": ((64, 64), (0, 0, 0, 0), (80, 200, 230)),
    "Aurora Studio/Icons/heart.png": ((64, 64), (0, 0, 0, 0), (230, 60, 90)),
    "Kenji Sato/sketch 01.jpg": ((800, 1200), (235, 230, 220), (60, 60, 60)),
    "Kenji Sato/sketch 02.jpg": ((1200, 800), (235, 230, 220), (120, 90, 60)),
    "Kenji Sato/tileset.png": ((512, 512), (30, 30, 30), (200, 160, 60)),
    "mood board.png": ((1000, 600), (200, 210, 220), (90, 110, 160)),
}
ASSET_TAGS = {
    ("Use", "Background"): ["dusk sky.png", "forest.png"],
    ("Use", "Icon"): ["gem.png", "heart.png"],
    ("Style", "Pixel art"): ["tileset.png", "gem.png", "heart.png"],
    ("Style", "Sketch"): ["sketch 01.jpg", "sketch 02.jpg"],
    ("Favorites",): ["forest.png", "Aileron-Regular.ttf"],
}


def _asset_image(
    size: tuple[int, int], background: tuple[int, ...], accent: tuple[int, ...]
) -> Image.Image:
    """A simple picture: a background with a circle and a stripe, so thumbnails differ."""
    image = Image.new("RGBA" if len(background) == 4 else "RGB", size, background)
    draw = ImageDraw.Draw(image)
    w, h = size
    r = min(w, h) // 3
    draw.ellipse((w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r), fill=accent)
    draw.rectangle((0, h * 3 // 4, w, h * 3 // 4 + max(2, h // 20)), fill=accent)
    return image


def _png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    _asset_image(size, color, (255, 255, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def _demo_font() -> bytes:
    """Aileron Regular (SIL Open Font License), the font Pillow embeds as its default."""
    found = re.search(r'b"""(.*?)"""', inspect.getsource(ImageFont.load_default), re.DOTALL)
    assert found is not None
    return base64.b64decode(found.group(1))


def make_assets_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/asset-files`` and ``scratch/Assets.keep`` (the assets2d theme),
    scanned and tagged; returns the keep folder."""
    files, keep_dir = scratch / "asset-files", scratch / "Assets.keep"
    if reset:
        _remove([keep_dir, files])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")
    for relpath, (size, background, accent) in ASSET_IMAGES.items():
        path = files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        _asset_image(size, background, accent).save(path)
    fonts = files / "Aurora Studio" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "Aileron-Regular.ttf").write_bytes(_demo_font())
    with zipfile.ZipFile(files / "Aurora Studio" / "UI pack.zip", "w") as zf:
        zf.writestr("UI pack/cover.png", _png((320, 200), (200, 80, 60)))
        for i in range(1, 4):
            zf.writestr(f"UI pack/button {i}.png", _png((96, 32), (60, 120, 200)))
        zf.writestr("UI pack/license.txt", "Demo assets for Tagalot.")
    with py7zr.SevenZipFile(files / "Kenji Sato" / "sketchbook.7z", "w") as sz:
        for i in (2, 10, 1):  # natural order picks page 1
            sz.writestr(_png((300, 400), (240 - i * 10, 230, 210)), f"page {i}.png")

    create_keep(
        keep_dir,
        "Assets",
        ThemeRef("assets2d", 1),
        [RootConfig("assets", "Asset files", str(files), list(DEFAULT_EXCLUDES))],
    )
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        _tag(session, ASSET_TAGS, {}, {})
    return keep_dir


MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
"""One silent MPEG-1 Layer III frame: 128 kbit/s, 44.1 kHz, about 26 ms."""
KIND_OF_BLUE = {"artist": "Miles Davis", "album": "Kind of Blue", "date": "1959", "genre": "Jazz"}
PASTEL = {"artist": "Nina Simone", "album": "Pastel Blues", "date": "1965", "genre": "Blues"}
LIVE = {"artist": "Queen", "albumartist": "Queen", "album": "Live Box", "genre": "Rock"}
HITS = {"album": "Jazz Hits", "genre": "Jazz"}
SONGS: dict[str, tuple[dict[str, str], int]] = {
    # relative path: (tags, seconds)
    "Miles Davis/Kind of Blue/01 So What.mp3": ({**KIND_OF_BLUE, "title": "So What"}, 9),
    "Miles Davis/Kind of Blue/02 Freddie Freeloader.mp3": (
        {**KIND_OF_BLUE, "title": "Freddie Freeloader"},
        8,
    ),
    "Miles Davis/Kind of Blue/03 Blue in Green.mp3": (
        {**KIND_OF_BLUE, "title": "Blue in Green"},
        5,
    ),
    "Nina Simone/Pastel Blues/01 Be My Husband.mp3": ({**PASTEL, "title": "Be My Husband"}, 4),
    "Nina Simone/Pastel Blues/02 Trouble in Mind.mp3": (
        {**PASTEL, "title": "Trouble in Mind"},
        5,
    ),
    "Queen/Live Box/CD1/01 Intro.mp3": ({**LIVE, "title": "Intro"}, 2),
    "Queen/Live Box/CD1/02 Tie Your Mother Down.mp3": (
        {**LIVE, "title": "Tie Your Mother Down"},
        4,
    ),
    "Queen/Live Box/CD2/01 Intro.mp3": ({**LIVE, "title": "Intro"}, 3),
    "Jazz Hits/01 Take Five.mp3": ({**HITS, "title": "Take Five", "artist": "Dave Brubeck"}, 5),
    "Jazz Hits/02 Blue.mp3": ({**HITS, "title": "Blue", "artist": "Joni Mitchell"}, 3),
    "Untagged/03 - Lonely Road.mp3": ({}, 3),
    "Demo Tape.mp3": ({"title": "Demo Tape", "artist": "Garage Band"}, 2),
}
MUSIC_TAGS = {
    ("Mood", "Calm"): ["Blue in Green", "Blue", "Trouble in Mind"],
    ("Mood", "Loud"): ["Tie Your Mother Down"],
    ("Favorites",): ["So What", "Pastel Blues"],
}


def _track_numbers(relpath: str, tags: dict[str, str]) -> dict[str, str]:
    """Tags with the track (and disc) numbers from a file name like ``CD2/01 Intro.mp3``."""
    folder, _, name = relpath.rpartition("/")
    if not tags or not name[0].isdigit():
        return tags
    numbered = dict(tags, tracknumber=str(int(name.split()[0])))
    if folder.endswith(("CD1", "CD2")):
        numbered["discnumber"] = folder[-1]
    return numbered


def _mp3(path: Path, tags: dict[str, str], seconds: int, cover: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAME * (seconds * 38))
    if cover:
        art = ID3()
        art.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=cover))
        art.save(path)
    if tags:
        easy = EasyID3(path) if cover else EasyID3()
        for name, value in tags.items():
            easy[name] = value
        easy.save(path)


def _flac(path: Path, tags: dict[str, str], seconds: int) -> None:
    """A FLAC header claiming ``seconds`` of audio (it holds none), with ``tags``."""
    stream = (44100 << 44) | (1 << 41) | (15 << 36) | (seconds * 44100)
    info = bytes([16, 0, 16, 0]) + bytes(6) + stream.to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    audio = FLAC(path)
    for name, value in tags.items():
        audio[name] = value
    audio.save()


def _cover(color: tuple[int, int, int], text: str) -> bytes:
    image = _asset_image((300, 300), color, (240, 230, 200))
    ImageDraw.Draw(image).text((16, 16), text, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


def make_music_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/music-files`` and ``scratch/Music.keep`` (the music theme), scanned
    and tagged; returns the keep folder."""
    files, keep_dir = scratch / "music-files", scratch / "Music.keep"
    if reset:
        _remove([keep_dir, files])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")
    pastel = _cover((120, 40, 60), "Pastel Blues")
    for relpath, (tags, seconds) in SONGS.items():
        cover = pastel if relpath.startswith("Nina Simone/") else None
        _mp3(files / relpath, _track_numbers(relpath, tags), seconds, cover)
    so_what = "Miles Davis/Kind of Blue/01 So What.mp3"
    _flac(
        files / so_what.replace(".mp3", ".flac"),
        _track_numbers(so_what, SONGS[so_what][0]),
        545,
    )
    (files / "Miles Davis/Kind of Blue/cover.jpg").write_bytes(
        _cover((30, 60, 120), "Kind of Blue")
    )

    create_keep(
        keep_dir,
        "Music",
        ThemeRef("music", 1),
        [RootConfig("music", "Music files", str(files), list(DEFAULT_EXCLUDES))],
    )
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        _tag(session, MUSIC_TAGS, {}, {})
    return keep_dir


MOVIE_FILES = [
    "Inception (2010)/Inception.mkv",
    "Inception (2010)/Inception (2010)-trailer.mkv",
    "Heat (1995).mkv",
    "Heat (1995) - 720p.mkv",
    "Alien (1979).avi",
    "The.Matrix.1999.1080p/The.Matrix.1999.1080p.BluRay.mkv",
    "The Lord of the Rings/The Fellowship of the Ring (2001)/The Fellowship of the Ring.mkv",
    "The Lord of the Rings/The Two Towers (2002)/The Two Towers.mkv",
    "The Lord of the Rings/The Return of the King (2003)/The Return of the King - part1.mkv",
    "The Lord of the Rings/The Return of the King (2003)/The Return of the King - part2.mkv",
    "The Lord of the Rings/Extras/Making of.mkv",
    "Home Movies/Beach day.mp4",
    "Home Movies/Birthday.mp4",
]
"""Movie files for the movies demo: stand-ins, not real video. Those in
:data:`MOVIE_VIDEOS` are a video's headers (MediaInfo reads them); the rest a few bytes."""

MOVIE_VIDEOS = {
    "Inception (2010)/Inception.mkv": ((1920, 800), 8880, ["eng", "fre"]),
    "Heat (1995).mkv": ((1920, 1040), 10200, ["eng"]),
    "Heat (1995) - 720p.mkv": ((1280, 692), 10200, ["eng"]),
    "The.Matrix.1999.1080p/The.Matrix.1999.1080p.BluRay.mkv": ((3840, 1600), 8160, ["eng"]),
    "The Lord of the Rings/The Fellowship of the Ring (2001)/The Fellowship of the Ring.mkv": (
        (1920, 800),
        10680,
        ["eng", "ger"],
    ),
    "The Lord of the Rings/The Two Towers (2002)/The Two Towers.mkv": (
        (1280, 536),
        10740,
        ["eng"],
    ),
}
"""(width, height), seconds, and audio languages of the demo's readable videos."""

MOVIE_PICTURES = {
    "Inception (2010)/poster.jpg": ((40, 60, 90), "Inception"),
    "Inception (2010)/screenshots/still 1.jpg": ((90, 80, 60), "Inception: still 1"),
    "Inception (2010)/screenshots/still 2.jpg": ((60, 90, 80), "Inception: still 2"),
    "Heat (1995)-poster.jpg": ((150, 60, 30), "Heat"),
    "Heat (1995)-fanart.jpg": ((30, 40, 70), "Heat: fanart"),
    "The Lord of the Rings/folder.jpg": ((120, 100, 40), "The Lord of the Rings"),
    "The Lord of the Rings/The Fellowship of the Ring (2001)/poster.jpg": (
        (70, 90, 40),
        "The Fellowship",
    ),
    "The Lord of the Rings/The Two Towers (2002)/poster.jpg": ((60, 60, 60), "Two Towers"),
}
"""Posters and screenshots for the movies demo (The Matrix, Alien, and the rest have none)."""

MOVIE_NFOS = {
    "Inception (2010)/movie.nfo": """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>Inception</title>
  <year>2010</year>
  <plot>A thief who steals secrets through dreams is offered a way home.</plot>
  <genre>Science Fiction</genre>
  <genre>Thriller</genre>
  <director>Christopher Nolan</director>
  <runtime>148</runtime>
  <rating>8.8</rating>
  <actor><name>Leonardo DiCaprio</name><role>Cobb</role><order>0</order></actor>
  <actor><name>Elliot Page</name><role>Ariadne</role><order>1</order></actor>
  <actor><name>Tom Hardy</name><role>Eames</role><order>2</order></actor>
</movie>
""",
    "Heat (1995).nfo": """<movie>
  <title>Heat</title>
  <genre>Crime</genre>
  <director>Michael Mann</director>
  <runtime>170</runtime>
  <set><name>Michael Mann Crime</name></set>
  <actor><name>Al Pacino</name></actor>
  <actor><name>Robert De Niro</name></actor>
</movie>
""",
    "Alien (1979).nfo": """<movie>
  <title>Alien</title>
  <genre>Science Fiction</genre>
  <genre>Horror</genre>
  <director>Ridley Scott</director>
  <runtime>117</runtime>
  <set><name>Alien Collection</name></set>
  <actor><name>Sigourney Weaver</name></actor>
</movie>
""",
}
"""Kodi .nfo files for the movies demo: details, a cast, and sets."""

MOVIE_TAGS = {
    ("Genre",): [],
    ("Genre", "Sci-Fi"): ["Inception", "The Matrix", "Alien"],
    ("Genre", "Crime"): ["Heat"],
    ("Genre", "Fantasy"): ["The Lord of the Rings"],
    ("Watched",): ["Heat", "Alien"],
}


def _mkv(size: tuple[int, int], seconds: float, languages: list[str]) -> bytes:
    """A Matroska file's headers with no frames: its length, a VP9 video track of ``size``,
    and an Opus audio track per language (as ``tests/core/media_files.write_mkv``)."""

    def element(eid: int, data: bytes) -> bytes:
        ident = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
        return ident + b"\x01" + len(data).to_bytes(7, "big") + data

    def uint(eid: int, value: int) -> bytes:
        return element(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))

    def text(eid: int, value: str) -> bytes:
        return element(eid, value.encode())

    header = element(
        0x1A45DFA3,
        uint(0x4286, 1)
        + uint(0x42F7, 1)
        + uint(0x42F2, 4)
        + uint(0x42F3, 8)
        + text(0x4282, "matroska")
        + uint(0x4287, 4)
        + uint(0x4285, 2),
    )
    info = element(
        0x1549A966,
        uint(0x2AD7B1, 1_000_000) + element(0x4489, struct.pack(">d", seconds * 1000)),
    )
    tracks = element(
        0xAE,
        uint(0xD7, 1)
        + uint(0x73C5, 1)
        + uint(0x83, 1)
        + text(0x86, "V_VP9")
        + element(0xE0, uint(0xB0, size[0]) + uint(0xBA, size[1])),
    )
    for n, language in enumerate(languages, start=2):
        tracks += element(
            0xAE,
            uint(0xD7, n)
            + uint(0x73C5, n)
            + uint(0x83, 2)
            + text(0x86, "A_OPUS")
            + text(0x22B59C, language)
            + element(0xE1, element(0xB5, struct.pack(">d", 48000.0)) + uint(0x9F, 2)),
        )
    return header + element(0x18538067, info + element(0x1654AE6B, tracks))


BOOK_EPUBS: dict[str, dict[str, Any]] = {
    "Kobo/The Colour of Magic.epub": {
        "title": "The Colour of Magic",
        "creators": [("Terry Pratchett", "aut")],
        "series": ("Discworld", 1),
        "subjects": ["Fantasy", "Humor"],
        "cover": ((40, 90, 150), "The Colour of Magic"),
    },
    "Humble Bundle/The Colour of Magic.epub": {  # the same book from another store
        "title": "The Colour of Magic",
        "creators": [("Terry Pratchett", "aut"), ("Josh Kirby", "ill")],
        "series": ("Discworld", 1),
        "subjects": ["Fantasy"],
    },
    "Kobo/Equal Rites.epub": {
        "title": "Equal Rites",
        "creators": [("Terry Pratchett", "aut")],
        "series": ("Discworld", 3),
        "subjects": ["Fantasy", "Humor"],
        "cover": ((120, 50, 110), "Equal Rites"),
    },
    "Kobo/The Final Empire.epub": {
        "title": "The Final Empire",
        "creators": [("Brandon Sanderson", "aut")],
        "epub3_series": ("Mistborn", 1),
        "sets": ["The Mistborn Trilogy"],
        "subjects": ["Fantasy", "Epic Fantasy"],
        "cover": ((150, 60, 30), "The Final Empire"),
    },
    "Humble Bundle/The Well of Ascension.epub": {
        "title": "The Well of Ascension",
        "creators": [("Brandon Sanderson", "aut")],
        "epub3_series": ("Mistborn", 2),
        "sets": ["The Mistborn Trilogy"],
        "subjects": ["Fantasy", "Epic Fantasy"],
        "cover": ((170, 120, 30), "The Well of Ascension"),
    },
    # Files without metadata: named from their file names.
    "Loose/Discworld 02 - The Light Fantastic.epub": {},
    "Loose/Ursula K. Le Guin - A Wizard of Earthsea (1968).epub": {},
    "Loose/Terry Pratchett - The Color of Magic.epub": {},  # a near-duplicate
}
"""EPUBs for the books demo, by path: their metadata (a cover as color and text)."""

BOOK_COMICS: dict[str, dict[str, str] | None] = {
    "Comixology/Saga 001.cbz": {
        "Series": "Saga",
        "Number": "1",
        "Writer": "Brian K. Vaughan",
        "Penciller": "Fiona Staples",
        "Genre": "Science Fiction, Space Opera",
        "Publisher": "Image",
        "Year": "2012",
    },
    "Comixology/Saga 002.cbz": {
        "Series": "Saga",
        "Number": "2",
        "Writer": "Brian K. Vaughan",
        "Penciller": "Fiona Staples",
        "Genre": "Science Fiction",
        "Publisher": "Image",
        "Year": "2012",
    },
    "Loose/Ms Marvel 001.cbz": {
        "Series": "Ms. Marvel",
        "Number": "1",
        "Title": "No Normal",
        "Writer": "G. Willow Wilson",
        "Penciller": "Adrian Alphona",
        "Genre": "Superhero",
        "SeriesGroup": "Marvel",
        "Publisher": "Marvel",
        "Year": "2014",
        "Web": "https://comics.example.com/ms-marvel-1",
    },
    "Loose/Sandman #001 (1989).cbz": None,  # no ComicInfo.xml: named from its file name
}
"""Comic archives for the books demo, by path: their ComicInfo.xml fields."""

BOOK_MARKDOWN = {
    "Royal Road/Mother of Learning 01.md": (
        "---\n"
        "title: Mother of Learning\n"
        "author: nobody103\n"
        "series: Mother of Learning\n"
        "number: 1\n"
        "universe: Eldemar\n"
        "tags: [Fantasy, time-loop]\n"
        "url: https://example.com/mother-of-learning\n"
        "date: 2011-10-21\n"
        "cover: images/mol-cover.jpg\n"
        "---\n"
        "# Arc 1: Good Morning Brother\n\nZorian's eyes abruptly shot open…\n"
    ),
    "Loose/A Short Story.md": "Written on a train.\n\n"
    "# The Lighthouse Keeper\n\nIt was a dark night.\n",
}
"""Markdown books for the books demo: one with YAML front matter, one titled by its heading."""

BOOK_TAGS = {
    ("Genre", "Fantasy"): [],  # given by the books' own subjects (file keywords)
    ("Genre", "Science Fiction"): [],
    ("Genre", "Humour"): ["Discworld"],  # on the series: its books count (inherit tags)
    ("Read",): ["The Colour of Magic", "Saga #1"],
}


def _epub(path: Path, details: dict[str, Any]) -> None:
    """A small EPUB with this metadata (as ``tests/core/book_files.write_epub``)."""
    lines = []
    if "title" in details:
        lines.append(f"<dc:title>{escape(details['title'])}</dc:title>")
    for name, code in details.get("creators", []):
        lines.append(f'<dc:creator opf:role="{code}">{escape(name)}</dc:creator>')
    lines += [f"<dc:subject>{escape(s)}</dc:subject>" for s in details.get("subjects", [])]
    if "series" in details:
        name, index = details["series"]
        lines.append(f'<meta name="calibre:series" content="{escape(name)}"/>')
        lines.append(f'<meta name="calibre:series_index" content="{index}"/>')
    if "epub3_series" in details:
        name, index = details["epub3_series"]
        lines.append(f'<meta property="belongs-to-collection" id="s">{escape(name)}</meta>')
        lines.append('<meta refines="#s" property="collection-type">series</meta>')
        lines.append(f'<meta refines="#s" property="group-position">{index}</meta>')
    for n, name in enumerate(details.get("sets", [])):
        lines.append(f'<meta property="belongs-to-collection" id="c{n}">{escape(name)}</meta>')
        lines.append(f'<meta refines="#c{n}" property="collection-type">set</meta>')
    items = '<item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>'
    if "cover" in details:
        items += (
            '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
        )
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">'
        f"{''.join(lines)}</metadata><manifest>{items}</manifest>"
        '<spine><itemref idref="text"/></spine></package>'
    )
    container = (
        '<?xml version="1.0"?><container version="1.0"'
        ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", container)
        book.writestr("OEBPS/content.opf", opf)
        book.writestr("OEBPS/text.xhtml", "<html><body><p>Once upon a time.</p></body></html>")
        if "cover" in details:
            color, text = details["cover"]
            book.writestr("OEBPS/cover.jpg", _cover(color, text))


def _cbz(path: Path, info: dict[str, str] | None) -> None:
    """A comic archive of two pages, with ``info`` as its ComicInfo.xml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as comic:
        comic.writestr("001.jpg", _cover((30, 30, 30), path.stem))
        comic.writestr("002.jpg", _cover((200, 200, 200), f"{path.stem}, page 2"))
        if info is not None:
            body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in info.items())
            comic.writestr("ComicInfo.xml", f'<?xml version="1.0"?><ComicInfo>{body}</ComicInfo>')


def _office_and_links(files: Path) -> None:
    """Office documents (Word with its thumbnail, OpenDocument, Pages, a legacy .doc) and
    link files (.url, .webloc, .desktop) for the books demo."""
    import plistlib

    core = (
        '<?xml version="1.0"?><cp:coreProperties'
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
        "<dc:title>The Hedge Knight</dc:title><dc:creator>George R. R. Martin</dc:creator>"
        "<cp:keywords>Fantasy</cp:keywords><dc:subject>A tale of the Seven Kingdoms</dc:subject>"
        "</cp:coreProperties>"
    )
    (files / "Loose").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(files / "Loose/hedge-knight.docx", "w") as doc:
        doc.writestr("[Content_Types].xml", "<Types/>")
        doc.writestr("word/document.xml", "<document/>")
        doc.writestr("docProps/core.xml", core)
        doc.writestr("docProps/thumbnail.jpeg", _cover((90, 70, 30), "The Hedge Knight"))
    meta = (
        '<?xml version="1.0"?><office:document-meta'
        ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
        "<dc:title>Notes on Dragons</dc:title>"
        "<meta:initial-creator>Samwell Tarly</meta:initial-creator>"
        "<meta:keyword>Essay</meta:keyword></office:meta></office:document-meta>"
    )
    with zipfile.ZipFile(files / "Loose/dragons.odt", "w") as doc:
        doc.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        doc.writestr("meta.xml", meta)
        doc.writestr("Thumbnails/thumbnail.png", _cover((40, 120, 40), "Notes on Dragons"))
    with zipfile.ZipFile(files / "Loose/My Novel Draft.pages", "w") as doc:
        doc.writestr("Index/Document.iwa", b"\x00")
        doc.writestr("preview.jpg", _cover((200, 200, 220), "My Novel Draft"))
    (files / "Loose/Old Draft.doc").write_bytes(b"\xd0\xcf\x11\xe0 not really Word")
    (files / "Web").mkdir(parents=True, exist_ok=True)
    (files / "Web/Worm.url").write_text(
        "[InternetShortcut]\r\nURL=https://parahumans.example.com/\r\n", encoding="utf-8"
    )
    (files / "Web/Pale.webloc").write_bytes(plistlib.dumps({"URL": "https://pale.example.com/"}))
    (files / "Web/guide.desktop").write_text(
        "[Desktop Entry]\nType=Link\nName=A Practical Guide to Evil\n"
        "URL=https://practicalguide.example.com/\n",
        encoding="utf-8",
    )


RESEARCH_PDFS: dict[str, tuple[list[str], dict[str, str]]] = {
    "Library/Vaswani et al. - 2017 - Attention Is All You Need.pdf": (
        ["Attention Is All You Need", "arXiv:1706.03762v5 [cs.CL] 6 Dec 2017"],
        {"Author": "Ashish Vaswani; Noam Shazeer; Niki Parmar; Jakob Uszkoreit"},
    ),
    "Downloads/1706.03762v7.pdf": (  # a second download: the same paper
        ["Attention Is All You Need", "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023"],
        {},
    ),
    "Library/He et al. - 2016 - Deep Residual Learning for Image Recognition.pdf": (
        ["Deep Residual Learning for Image Recognition", "doi:10.1109/CVPR.2016.90"],
        {
            "Title": "Deep Residual Learning for Image Recognition",
            "Author": "Kaiming He; Xiangyu Zhang; Shaoqing Ren; Jian Sun",
            "Keywords": "Vision; Deep learning",
        },
    ),
    "Preprints/2203.02155v1.pdf": (  # a preprint...
        ["Training language models to follow instructions", "arXiv:2203.02155v1 [cs.CL]"],
        {
            "Title": "Training language models to follow instructions with human feedback",
            "Author": "Long Ouyang; Jeff Wu; Xu Jiang",
            "Keywords": "Language models",
        },
    ),
    "Library/Ouyang et al. - 2022 - Training language models.pdf": (  # ...and its paper
        [
            "Training language models to follow instructions",
            "https://doi.org/10.5555/3600270.3602281",
            "arXiv:2203.02155",
        ],
        {"Title": "Microsoft Word - final_v3.docx"},  # junk: ignored
    ),
    "Notes/A Paper Without Metadata.pdf": (["Some notes, no identifiers."], {}),
}
"""PDFs for the research demo, with a text layer: (first page's lines, document info)."""

RESEARCH_BIBLIOGRAPHIES = {
    # A library export (as Zotero writes one), matched to the PDFs by arXiv ID and DOI.
    "Zotero/My Library.bib": """@inproceedings{vaswani_attention_2017,
	title = {Attention {Is} {All} {You} {Need}},
	booktitle = {Advances in {Neural} {Information} {Processing} {Systems}},
	volume = {30},
	author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and Uszkoreit, Jakob},
	year = {2017},
	eprint = {1706.03762},
	archivePrefix = {arXiv},
	keywords = {Transformers},
}

@inproceedings{he_deep_2016,
	title = {Deep {Residual} {Learning} for {Image} {Recognition}},
	booktitle = {{IEEE} {Conference} on {Computer} {Vision} and {Pattern} {Recognition}},
	pages = {770--778},
	doi = {10.1109/CVPR.2016.90},
	author = {He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian},
	year = {2016},
}

@inproceedings{ouyang_training_2022,
	title = {Training language models to follow instructions with human feedback},
	booktitle = {Advances in {Neural} {Information} {Processing} {Systems}},
	volume = {35},
	doi = {10.5555/3600270.3602281},
	author = {Ouyang, Long and Wu, Jeff and Jiang, Xu},
	year = {2022},
}

@article{not_downloaded_2019,
	title = {A Paper In the Library But Not Downloaded},
	doi = {10.1000/nowhere},
	year = {2019},
}
""",
    # A sidecar: named like the PDF beside it, it describes that paper.
    "Notes/A Paper Without Metadata.bib": """@article{doe_notes_2021,
  title = {Notes Toward a Theory of Everything},
  author = {Doe, Jane},
  journal = {Journal of Speculation},
  year = {2021},
}
""",
}
"""Bibliography files for the research demo: a Zotero-style export and a sidecar."""

RESEARCH_TAGS = {
    ("Topic", "Transformers"): ["Attention Is All You Need"],
    ("Topic", "Vision"): [],  # given by a file's keywords
    ("Read",): ["Deep Residual Learning for Image Recognition"],
}


def _text_pdf(path: Path, lines: list[str], info: dict[str, str]) -> None:
    """A one-page PDF with a text layer (as ``tests/core/book_files.text_pdf``)."""

    def literal(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"({escaped})"

    shown = " ".join(f"{literal(line)} Tj T*" for line in lines)
    stream = f"BT /F1 11 Tf 72 740 Td 14 TL {shown} ET".encode("latin-1")
    entries = " ".join(f"/{k} {literal(v)}" for k, v in info.items())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        f"<< {entries} >>".encode("latin-1"),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def make_research_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/research-files`` and ``scratch/Research.keep`` (the research theme),
    scanned and tagged; returns the keep folder."""
    files, keep_dir = scratch / "research-files", scratch / "Research.keep"
    if reset:
        _remove([keep_dir, files])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")
    for relpath, (lines, info) in RESEARCH_PDFS.items():
        _text_pdf(files / relpath, lines, info)
    for relpath, text in RESEARCH_BIBLIOGRAPHIES.items():
        (files / relpath).parent.mkdir(parents=True, exist_ok=True)
        (files / relpath).write_text(text, encoding="utf-8")
    root = RootConfig("papers", "Papers", str(files), list(DEFAULT_EXCLUDES))
    create_keep(keep_dir, "Research", ThemeRef("research", ResearchTheme.version), [root])
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        _tag(session, RESEARCH_TAGS, {}, {})
    return keep_dir


def make_books_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/book-files`` and ``scratch/Books.keep`` (the books theme), scanned
    and tagged; returns the keep folder. The first folder names where a file came from
    (``source_level = 1``)."""
    files, keep_dir = scratch / "book-files", scratch / "Books.keep"
    if reset:
        _remove([keep_dir, files])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")
    for relpath, details in BOOK_EPUBS.items():
        _epub(files / relpath, details)
    for relpath, info in BOOK_COMICS.items():
        _cbz(files / relpath, info)
    for relpath, text in BOOK_MARKDOWN.items():
        (files / relpath).parent.mkdir(parents=True, exist_ok=True)
        (files / relpath).write_text(text, encoding="utf-8")
    # The picture Mother of Learning's front matter names as its cover.
    (files / "Royal Road/images").mkdir(parents=True, exist_ok=True)
    (files / "Royal Road/images/mol-cover.jpg").write_bytes(
        _cover((20, 110, 110), "Mother of Learning")
    )
    # A PDF: its document info, and a first page that becomes its cover.
    cover = Image.open(io.BytesIO(_cover((60, 60, 60), "Good Omens")))
    cover.save(
        files / "Kobo/Good Omens.pdf",
        "PDF",
        title="Good Omens",
        author="Terry Pratchett & Neil Gaiman",
        subject="The world will end on a Saturday. Next Saturday, in fact.",
        keywords="Fantasy; Humor",
    )
    _office_and_links(files)
    root = RootConfig(
        "books", "Book files", str(files), list(DEFAULT_EXCLUDES), {"source_level": 1}
    )
    create_keep(keep_dir, "Books", ThemeRef("books", BooksTheme.version), [root])
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        _tag(session, BOOK_TAGS, {}, {})
    return keep_dir


def make_movies_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/movie-files`` and ``scratch/Movies.keep`` (the movies theme),
    scanned and tagged; returns the keep folder."""
    files, keep_dir = scratch / "movie-files", scratch / "Movies.keep"
    if reset:
        _remove([keep_dir, files])
    elif keep_dir.exists():
        raise FileExistsError(f"{keep_dir} already exists; use --reset to recreate it")
    for relpath in MOVIE_FILES:
        path = files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if relpath in MOVIE_VIDEOS:
            size, seconds, languages = MOVIE_VIDEOS[relpath]
            path.write_bytes(_mkv(size, seconds, languages))
        else:
            path.write_bytes(f"Not a real video: {relpath}\n".encode())
    for relpath, (color, text) in MOVIE_PICTURES.items():
        (files / relpath).parent.mkdir(parents=True, exist_ok=True)
        (files / relpath).write_bytes(_cover(color, text))
    for relpath, text in MOVIE_NFOS.items():
        (files / relpath).write_text(text, encoding="utf-8")
    photo = files / "Inception (2010)/.actors/Leonardo_DiCaprio.jpg"
    photo.parent.mkdir()
    photo.write_bytes(_cover((110, 90, 70), "Leonardo DiCaprio"))
    create_keep(
        keep_dir,
        "Movies",
        ThemeRef("movies", MoviesTheme.version),
        [RootConfig("movies", "Movie files", str(files), list(DEFAULT_EXCLUDES))],
    )
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        _tag(session, MOVIE_TAGS, {}, {})
    return keep_dir


def _tag(
    session: KeepSession,
    tags: dict[tuple[str, ...], list[str]],
    aliases: dict[tuple[str, ...], list[str]],
    descriptions: dict[tuple[str, ...], str],
) -> None:
    """Create ``tags`` and apply them by title (the app can't apply tags until the tagging
    panel), then add ``aliases`` and ``descriptions``."""
    with session.reader.connect() as conn:
        ids = {title: i for i, title in conn.execute(select(Entity.id, Entity.title))}
    tag_ids: dict[tuple[str, ...], int] = {}
    rows = []
    for path, titles in tags.items():
        parent = tag_ids.get(path[:-1])
        if parent is None and len(path) > 1:  # a parent not listed itself: make it first
            parent = tag_ids[path[:-1]] = session.tags.add_path(list(path[:-1]))
        tag_ids[path] = session.tags.add(parent, path[-1])
        rows.extend({"entity_id": ids[t], "tag_id": tag_ids[path]} for t in titles)
    for path, names in aliases.items():
        for alias in names:
            session.tags.add_alias(tag_ids[path], alias)
    for path, description in descriptions.items():
        session.tags.set_description(tag_ids[path], description)
    # The demo's own tagging: where a file's keyword already gave the tag (a song's genre,
    # #295), the row becomes the user's.
    tagged = sqlite_insert(EntityTag).on_conflict_do_update(
        index_elements=["entity_id", "tag_id"], set_={"by_file": False}
    )
    session.writer.run(lambda conn: conn.execute(tagged, rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="delete and recreate the keep")
    parser.add_argument(
        "--media",
        action="store_true",
        help="create scratch/Media.keep (artists, albums, songs) instead, and install its theme",
    )
    parser.add_argument(
        "--assets",
        action="store_true",
        help="create scratch/Assets.keep (the 2D assets theme) instead",
    )
    parser.add_argument(
        "--movies",
        action="store_true",
        help="create scratch/Movies.keep (the movies theme) instead",
    )
    parser.add_argument(
        "--research",
        action="store_true",
        help="create scratch/Research.keep (the research theme) instead",
    )
    parser.add_argument(
        "--books",
        action="store_true",
        help="create scratch/Books.keep (the books theme) instead",
    )
    parser.add_argument(
        "--music",
        action="store_true",
        help="create scratch/Music.keep (the music theme) instead",
    )
    args = parser.parse_args(argv)
    scratch = SCRATCH  # read here, so tests can point it elsewhere
    try:
        if args.research:
            keep_dir = make_research_demo(scratch, reset=args.reset)
        elif args.books:
            keep_dir = make_books_demo(scratch, reset=args.reset)
        elif args.movies:
            keep_dir = make_movies_demo(scratch, reset=args.reset)
        elif args.music:
            keep_dir = make_music_demo(scratch, reset=args.reset)
        elif args.assets:
            keep_dir = make_assets_demo(scratch, reset=args.reset)
        elif args.media:
            keep_dir = make_media_demo(scratch, reset=args.reset)
        else:
            keep_dir = make_demo(scratch, reset=args.reset)
    except (FileExistsError, DemoInUseError) as e:
        print(e)
        return 1
    if args.research:
        print(f"Created {keep_dir} watching research-files (scanned and tagged).")
        print("Open it with:  uv run tagalot scratch/Research.keep")
    elif args.books:
        print(f"Created {keep_dir} watching book-files (scanned and tagged).")
        print("Open it with:  uv run tagalot scratch/Books.keep")
    elif args.movies:
        print(f"Created {keep_dir} watching movie-files (scanned and tagged).")
        print("Open it with:  uv run tagalot scratch/Movies.keep")
    elif args.music:
        print(f"Created {keep_dir} watching music-files (scanned and tagged).")
        print("Open it with:  uv run tagalot scratch/Music.keep")
    elif args.assets:
        print(f"Created {keep_dir} watching asset-files (scanned and tagged).")
        print("Open it with:  uv run tagalot scratch/Assets.keep")
    elif args.media:
        theme = default_user_themes_dir() / MEDIA_THEME_FILE
        print(f"Created {keep_dir} with artists, albums, and songs.")
        print("Open it with:  uv run tagalot scratch/Media.keep")
        print(f"Its theme is {theme}; delete that file when you're done with the media demo.")
    else:
        print(f"Created {keep_dir} watching demo-files (scanned and tagged).")
        print("Open it with:  uv run tagalot scratch/Demo.keep")
    return 0


if __name__ == "__main__":
    sys.exit(main())
