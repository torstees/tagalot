"""Bundle a subset of Lucide's icons (https://lucide.dev, ISC license) under
``src/tagalot/resources/icons/`` (DESIGN.md §3): the icons Tagalot's own screens and
built-in themes use, and a choice for themes' and users' own types.

Run it after changing ``VERSION`` or ``NAMES``: ``uv run python scripts/fetch_lucide.py``.
It downloads the ``lucide-static`` package from npm, writes each icon on one line and
their keywords in ``tags.json``, and copies Lucide's license beside them.
"""

import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.58.0"
URL = f"https://registry.npmjs.org/lucide-static/-/lucide-static-{VERSION}.tgz"
TARGET = Path(__file__).resolve().parent.parent / "src" / "tagalot" / "resources" / "icons"

GROUPS = {
    "Tagalot's own screens": """
        layout-dashboard search bookmark inbox copy tags tag hash key-round folder
        folder-open file file-text arrow-left arrow-right refresh-cw settings library-big
    """,
    "media and documents": """
        film clapperboard tv video popcorn ticket drama camera aperture image images music
        music-2 disc disc-3 mic mic-vocal headphones radio guitar piano drum list-music
        list-video book book-open library notebook notebook-pen scroll scroll-text newspaper
        sticky-note quote languages file-archive file-image file-audio file-video file-code
        archive package box boxes type case-sensitive layers shapes palette brush paintbrush
        pen pen-tool pencil feather
    """,
    "research and work": """
        graduation-cap flask-conical microscope atom brain lightbulb code terminal bug
        database server hard-drive cpu monitor laptop smartphone briefcase calculator
        chart-column chart-pie receipt wallet banknote mail message-square link paperclip
        calendar clock hourglass history
    """,
    "people and things": """
        user users user-round contact baby smile heart thumbs-up star sparkles award trophy
        medal crown gem flame flag bell compass anchor map map-pin globe earth house
        building building-2 castle landmark church store shopping-bag shopping-cart gift
        cake coffee utensils chef-hat wine beer cooking-pot apple pizza shirt glasses watch
        gamepad-2 joystick puzzle dice-5 sword swords shield skull ghost wand-sparkles
        rocket ship car plane train-front bike dumbbell cat dog bird fish rabbit paw-print
        leaf sprout tree-pine flower mountain sun moon cloud wrench hammer ruler scissors
        eye
    """,
}
"""The icons bundled, by what they're for."""

NAMES = sorted({name for names in GROUPS.values() for name in names.split()})


def main() -> int:
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    missing = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        TARGET.mkdir(parents=True, exist_ok=True)
        for old in TARGET.glob("*.svg"):
            old.unlink()
        for name in NAMES:
            try:
                member = archive.extractfile(f"package/icons/{name}.svg")
            except KeyError:
                missing.append(name)
                continue
            assert member is not None
            (TARGET / f"{name}.svg").write_text(
                _one_line(member.read().decode()), encoding="utf-8", newline="\n"
            )
        license = archive.extractfile("package/LICENSE")
        assert license is not None
        (TARGET / "LICENSE").write_bytes(license.read().replace(b"\r\n", b"\n"))
        # Each icon's keywords, for searching the icon picker ("movie" finds film).
        tags_file = archive.extractfile("package/tags.json")
        assert tags_file is not None
        tags = json.load(tags_file)
        kept = {name: tags.get(name, []) for name in NAMES}
        (TARGET / "tags.json").write_text(
            json.dumps(kept, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
    if missing:
        print(f"Not in Lucide {VERSION}: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"{len(NAMES)} icons from Lucide {VERSION} in {TARGET}")
    return 0


def _one_line(svg: str) -> str:
    """The icon without its comment and class, on one line."""
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.DOTALL)
    svg = re.sub(r'\s*class="[^"]*"', "", svg)
    svg = re.sub(r"\s+", " ", svg).replace("> <", "><").replace(" />", "/>")
    return svg.replace(" >", ">").strip() + "\n"


if __name__ == "__main__":
    sys.exit(main())
