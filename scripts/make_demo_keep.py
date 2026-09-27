"""Create a demo keep in ``scratch/`` for trying Tagalot by hand.

Usage (from the repository folder):

    uv run python scripts/make_demo_keep.py            # create it if it doesn't exist
    uv run python scripts/make_demo_keep.py --reset    # delete and recreate it
    uv run tagalot scratch/Demo.keep

``scratch/`` is gitignored. It holds ``Demo.keep`` (the keep) and ``demo-files/`` (the folder
it watches): a few real images, documents, nested folders, and names with accents and spaces,
plus operating-system leftovers (``.DS_Store``, ``._sunset.jpg``, ``Thumbs.db``) that the
default exclude patterns skip.

The keep is scanned and given a small tag tree (with an alias), applied to most files, so
tag filters can be tried before the tagging panel exists. Scanning again finds nothing new.
"""

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import insert, select

from tagalot.core.keep import DEFAULT_EXCLUDES, RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityTag
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

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
JUNK = {
    # Operating-system leftovers that the default excludes skip.
    ".DS_Store": b"Bud1\x00\x00\x00\x01",
    "Photos/Beach/._sunset.jpg": b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X",
    "Photos/Iceland/Thumbs.db": b"\xd0\xcf\x11\xe0",
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
        _tag(session)
    return keep_dir


def _tag(session: KeepSession) -> None:
    """Create :data:`TAGS` and apply them (the app can't apply tags until the tagging panel)."""
    with session.reader.connect() as conn:
        ids = {title: i for i, title in conn.execute(select(Entity.id, Entity.title))}
    tag_ids: dict[tuple[str, ...], int] = {}
    rows = []
    for path, titles in TAGS.items():
        tag_ids[path] = session.tags.add(tag_ids.get(path[:-1]), path[-1])
        rows.extend({"entity_id": ids[t], "tag_id": tag_ids[path]} for t in titles)
    for path, aliases in ALIASES.items():
        for alias in aliases:
            session.tags.add_alias(tag_ids[path], alias)
    session.writer.run(lambda conn: conn.execute(insert(EntityTag), rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="delete and recreate the demo keep")
    args = parser.parse_args(argv)
    try:
        keep_dir = make_demo(reset=args.reset)
    except (FileExistsError, DemoInUseError) as e:
        print(e)
        return 1
    print(f"Created {keep_dir.relative_to(REPO)} watching scratch/demo-files (scanned and tagged).")
    print("Open it with:  uv run tagalot scratch/Demo.keep")
    return 0


if __name__ == "__main__":
    sys.exit(main())
