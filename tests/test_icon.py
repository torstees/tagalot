"""Tagalot's icon (#269): the rendered files exist at their sizes, the window uses them, and
``--check`` notices if a build left them out."""

import configparser
from pathlib import Path

import pytest
from PIL import Image
from pytestqt.qtbot import QtBot

from tagalot import selfcheck
from tagalot.resources import ICON_SIZES, icon_files

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


@pytest.mark.parametrize("size", ICON_SIZES)
def test_each_size_is_drawn_at_that_size(size: int) -> None:
    with Image.open(str(icon_files()[size])) as image:
        assert image.size == (size, size)
        assert image.mode == "RGBA"
        assert image.getchannel("A").getpixel((0, size - 1)) == 0  # see-through corner


def test_the_packaging_icons() -> None:
    with Image.open(PACKAGING / "tagalot.ico") as ico:
        assert {(s, s) for s in ICON_SIZES} <= set(ico.info["sizes"])
    with Image.open(PACKAGING / "tagalot.icns") as icns:
        assert icns.size == (1024, 1024)
    with Image.open(PACKAGING / "tagalot.png") as png:
        assert png.size == (256, 256)


def test_the_linux_desktop_entry() -> None:
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str  # type: ignore[assignment,method-assign]
    entry.read(PACKAGING / "tagalot.desktop", encoding="utf-8")
    desktop = entry["Desktop Entry"]
    assert desktop["Type"] == "Application"
    assert desktop["Name"] == "Tagalot"
    # The AppImage's icon is tagalot.png, and ui.app names this desktop file "tagalot".
    assert desktop["Icon"] == "tagalot"
    assert desktop["Exec"].split()[0] == "tagalot"


def test_check_reports_a_missing_icon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import tagalot.resources

    monkeypatch.setattr(
        tagalot.resources, "icon_files", lambda: {s: tmp_path / f"{s}.png" for s in ICON_SIZES}
    )
    lines: list[str] = []
    assert selfcheck.run_check(lines.append) == 1
    assert any(line.startswith("Icon") and "MISSING" in line and "0 of 7" in line for line in lines)


@pytest.mark.gui
def test_the_window_icon_has_every_size(qtbot: QtBot) -> None:
    from tagalot.ui.app import app_icon

    sizes = {s.width() for s in app_icon().availableSizes()}
    assert sizes == set(ICON_SIZES)


def test_the_docs_pictures() -> None:
    """The docs site's favicon and pictures (#327), rendered from the resources' SVGs."""
    images = ROOT / "docs" / "images"
    with Image.open(images / "favicon.ico") as ico:
        assert {(16, 16), (32, 32), (48, 48)} <= set(ico.info["sizes"])
    for name, source in (
        ("logo", "tagalot-tower.svg"),
        ("knight", "tagalot-knight-visor-up.svg"),
        ("flag", "tagalot-flag.svg"),
    ):
        with Image.open(images / f"{name}.png") as png:
            assert png.size == (256, 256)
        resources = ROOT / "src" / "tagalot" / "resources"
        assert (images / f"{name}.svg").read_bytes() == (resources / source).read_bytes()
