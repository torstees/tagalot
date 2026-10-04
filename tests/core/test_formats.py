"""Display formats: sizes and durations for numbers (#184), web addresses for text (#298)."""

from datetime import date

import pytest

from tagalot.core.formats import format_bytes, format_duration, format_value, is_web_address
from tagalot.themes.api import Entity, Theme, ThemeDeclarationError, field
from tagalot.themes.loader import validate_theme


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (1023, "1,023 bytes"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (980 * 1024, "980 KB"),
        (int(12.4 * 1024**2), "12.4 MB"),
        (5 * 1024**3, "5.0 GB"),
        (3 * 1024**4, "3.0 TB"),
        (2048 * 1024**4, "2,048 TB"),
    ],
)
def test_format_bytes(size: int, text: str) -> None:
    assert format_bytes(size) == text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0:00"),
        (5, "0:05"),
        (205, "3:25"),
        (204.6, "3:25"),  # rounded
        (3723, "1:02:03"),
        (36000, "10:00:00"),
        (-65, "-1:05"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


@pytest.mark.parametrize(
    ("value", "display", "text"),
    [
        (2048, "bytes", "2.0 KB"),
        (205.0, "duration", "3:25"),
        (2048, None, None),  # no format: shown as usual
        (None, "bytes", None),
        (True, "bytes", None),  # not a number
        ("big", "bytes", None),
        (date(2026, 1, 1), "duration", None),
    ],
)
def test_format_value(value: object, display: str | None, text: str | None) -> None:
    assert format_value(value, display) == text


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ThemeDeclarationError, match="display='stars' is not one of"):
        field("Rating", display="stars")


def test_a_format_needs_a_number_field() -> None:
    class Track(Entity):
        length: int | None = field("Length", display="duration")
        size: float | None = field("Size", display="bytes")
        note: str | None = field("Note", display="bytes")

    class Tracks(Theme):
        id, name = "tracks", "Tracks"
        entities = [Track]

    assert validate_theme(Tracks) == [
        "Track.note: display='bytes' needs a number field (int or float)"
    ]


def test_url_needs_a_text_field() -> None:
    class Page(Entity):
        link: str | None = field("Link", display="url")
        count: int | None = field("Count", display="url")

    class Pages(Theme):
        id, name = "pages", "Pages"
        entities = [Page]

    assert validate_theme(Pages) == ["Page.count: display='url' needs a text field (str)"]


@pytest.mark.parametrize(
    ("value", "web"),
    [
        ("https://example.com/a?b=1", True),
        ("HTTP://EXAMPLE.COM", True),
        ("  https://example.com  ", True),
        ("ftp://example.com", False),
        ("file:///C:/x.epub", False),
        ("javascript:alert(1)", False),
        ("https://", False),
        ("https://exa mple.com", False),
        (None, False),
        (12, False),
    ],
)
def test_is_web_address(value: object, web: bool) -> None:
    assert is_web_address(value) is web
    assert format_value(value, "url") is None  # read as the text itself
