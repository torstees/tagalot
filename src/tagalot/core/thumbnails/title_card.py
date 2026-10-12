"""Title cards: what an item without a picture shows (DESIGN.md §10 "Title cards", #415).

A card shows the item's title, wrapped and as large as fits, and an optional line under it
(a book's author), on a soft color picked from the title, so items without a picture still
look different from each other. The core decides what a card says and its colors; the UI
paints it (with the system's fonts, which cover every script).
"""

import colorsys
import hashlib
from dataclasses import dataclass

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Card:
    """What to paint for an item without a picture."""

    title: str
    subtitle: str
    """The line under the title (``""`` for none)."""
    aspect: float
    """Width for each unit of height."""
    background: Color
    ink: Color
    """The text's color: a dark shade of the background's."""


def title_card(title: str, subtitle: str = "", aspect: float = 2 / 3) -> Card:
    """The card of an item titled ``title``; its colors come from the title, so the same
    title always gets the same card."""
    background, ink = card_colors(title)
    return Card(title.strip(), subtitle.strip(), min(max(aspect, 0.25), 4.0), background, ink)


def card_colors(title: str) -> tuple[Color, Color]:
    """A soft color whose hue comes from ``title``, and a dark shade of it for text."""
    digest = hashlib.sha256(title.strip().casefold().encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65536
    return _rgb(colorsys.hls_to_rgb(hue, 0.84, 0.42)), _rgb(colorsys.hls_to_rgb(hue, 0.2, 0.45))


def _rgb(color: tuple[float, float, float]) -> Color:
    r, g, b = (round(c * 255) for c in color)
    return r, g, b
