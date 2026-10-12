"""Painting title cards (#415)."""

import importlib.util
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.thumbnails.title_card import Card, title_card
from tagalot.ui.thumbnails import ThumbnailLoader
from tagalot.ui.title_card import paint_title_card

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


def rgb(image: QImage, x: int, y: int) -> tuple[int, int, int]:
    color = QColor(image.pixel(x, y))
    return color.red(), color.green(), color.blue()


def colors(image: QImage) -> set[tuple[int, int, int]]:
    return {
        rgb(image, x, y) for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)
    }


@pytest.mark.parametrize(
    ("card", "size", "expected"),
    [
        (title_card("Dune", "Frank Herbert"), 256, (171, 256)),
        (title_card("Neil Gaiman", aspect=1), 128, (128, 128)),
        (title_card("Wide", aspect=2), 100, (100, 50)),
    ],
)
def test_a_card_is_the_thumbnails_size(
    qapp: object, card: Card, size: int, expected: tuple[int, int]
) -> None:
    image = paint_title_card(card, size)
    assert (image.width(), image.height()) == expected


def test_a_card_is_its_color_with_its_text_on_it(qapp: object) -> None:
    card = title_card("The Wandering Inn", "pirateaba")
    image = paint_title_card(card, 256)
    assert rgb(image, 2, 2) == card.background
    assert rgb(image, image.width() // 2, 4) == card.background
    seen = colors(image)
    assert any(sum(c) < sum(card.background) - 200 for c in seen)  # dark text


@pytest.mark.parametrize(
    "title",
    [
        "A Very Long Title That Goes On: Being the Complete Chronicles of Something Else",
        "Supercalifragilisticexpialidocious" * 4,
        "三体",
        "🐉 Dragons 🐉",
    ],
)
def test_any_title_paints(qapp: object, title: str) -> None:
    image = paint_title_card(title_card(title, "Someone, Another Person, A Third"), 96)
    assert not image.isNull()
    assert len(colors(image)) > 2


def test_an_author_without_a_picture_shows_a_title_card(qtbot: QtBot, tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        with session.reader.connect() as conn:
            gaiman = conn.scalar(select(Entity.id).where(Entity.title == "Neil Gaiman"))
        assert gaiman is not None
        loader = ThumbnailLoader(session.thumbnails)
        loader.request(gaiman)
        qtbot.waitUntil(lambda: loader.get(gaiman) is not None, timeout=10_000)
        loaded = loader.get(gaiman)
        assert loaded is not None
        assert loaded.image is not None
        assert loaded.icon is None
        assert loaded.image.width() == loaded.image.height() == session.thumbnail_max
        card = title_card("Neil Gaiman", aspect=1)
        assert rgb(loaded.image, 2, 2) == card.background
        loader.wait()
