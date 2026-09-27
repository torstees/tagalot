"""Create a demo keep in ``scratch/`` for trying Tagalot by hand.

Usage (from the repository folder):

    uv run python scripts/make_demo_keep.py            # create it if it doesn't exist
    uv run python scripts/make_demo_keep.py --reset    # delete and recreate it
    uv run tagalot scratch/Demo.keep

``scratch/`` is gitignored. It holds ``Demo.keep`` (the keep) and ``demo-files/`` (the folder
it watches): a few real images, documents, nested folders, and names with accents and spaces.
"""

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from tagalot.core.keep import RootConfig, ThemeRef, create_keep

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


def make_demo(scratch: Path = SCRATCH, *, reset: bool = False) -> Path:
    """Create ``scratch/demo-files`` and ``scratch/Demo.keep``; returns the keep folder."""
    files, keep_dir = scratch / "demo-files", scratch / "Demo.keep"
    if reset:
        for path in (keep_dir, files):
            shutil.rmtree(path, ignore_errors=True)
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

    create_keep(
        keep_dir, "Demo", ThemeRef("generic", 1), [RootConfig("demo", "Demo files", str(files))]
    )
    return keep_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="delete and recreate the demo keep")
    args = parser.parse_args(argv)
    try:
        keep_dir = make_demo(reset=args.reset)
    except FileExistsError as e:
        print(e)
        return 1
    print(f"Created {keep_dir.relative_to(REPO)} watching scratch/demo-files.")
    print("Open it with:  uv run tagalot scratch/Demo.keep")
    return 0


if __name__ == "__main__":
    sys.exit(main())
