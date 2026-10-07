"""Files Tagalot ships with: its icon and artwork (#269, #327).

The SVGs are the sources: ``tagalot-shield-hash.svg`` is the app icon, which
``scripts/make_icons.py`` renders to ``tagalot-<size>.png`` here (the window and taskbar
icon) and to the .ico, .icns, and .png in ``packaging/``. The others are larger pictures:
the tower (the About dialog, installer, and README), the knight with his visor up (the
mascot: splash screen, empty pages, docs), and the flag (docs); the shield with a tag and
the knight with his visor down are spares.
"""

from importlib import resources
from importlib.resources.abc import Traversable

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
"""The sizes the icon is drawn at, each its own PNG."""


def icon_files() -> dict[int, Traversable]:
    """The icon's PNG for each size in :data:`ICON_SIZES`."""
    folder = resources.files(__name__)
    return {size: folder / f"tagalot-{size}.png" for size in ICON_SIZES}


TOWER_SIZE = 256
"""The tower picture's size (the About dialog shows it at half, sharp on high-DPI screens)."""


def tower_file() -> Traversable:
    """The tower, rendered from ``tagalot-tower.svg`` (the About dialog, #359)."""
    return resources.files(__name__) / "tagalot-tower.png"
