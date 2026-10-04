"""Files Tagalot ships with: its icon (#269).

``tagalot.svg`` is the source; ``scripts/make_icons.py`` renders it to ``tagalot-<size>.png``
here (the window and taskbar icon) and to the .ico, .icns, and .png in ``packaging/``.
"""

from importlib import resources
from importlib.resources.abc import Traversable

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
"""The sizes the icon is drawn at, each its own PNG."""


def icon_files() -> dict[int, Traversable]:
    """The icon's PNG for each size in :data:`ICON_SIZES`."""
    folder = resources.files(__name__)
    return {size: folder / f"tagalot-{size}.png" for size in ICON_SIZES}
