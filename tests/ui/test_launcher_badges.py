"""The Open Keep dialog shows each keep's icon and a hint of its colors (#559, DESIGN.md
§12 *Icons*, *Colors*): its own icon, else its theme's, else a generic one; on a square of
its colors when they aren't the system's."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QIcon
from pytestqt.qtbot import QtBot

from tagalot.core.color_schemes import save_user_scheme
from tagalot.core.keep import ThemeRef, create_keep, save_keep_config
from tagalot.core.keep_settings import with_colors
from tagalot.core.settings import Settings
from tagalot.themes.loader import ThemeCatalog, load_themes
from tagalot.ui.icons import icon, lucide
from tagalot.ui.launcher import (
    BADGE,
    DARK,
    GENERIC_KEEP,
    LauncherDialog,
    describe_recent,
    keep_badge,
)

pytestmark = pytest.mark.gui

OWN = {
    "background": "#f3ece0",
    "text": "#2b2722",
    "panels": "#fffaf2",
    "accent": "#8a5a2b",
    "dimmed": "#857a6c",
}
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 3h18"/></svg>'


@pytest.fixture
def catalog(tmp_path: Path) -> ThemeCatalog:
    return load_themes(user_dir=tmp_path / "no-user-themes")


def _keep(folder: Path, name: str, theme: str, **changes: object) -> Path:
    keep = create_keep(folder / f"{name}.keep", name, ThemeRef(theme, 1))
    config = keep.config
    if "colors" in changes:
        config = with_colors(config, str(changes.pop("colors")), OWN)
    save_keep_config(replace(config, **changes), keep.toml_path)  # type: ignore[arg-type]
    return keep.dir


def _launcher(qtbot: QtBot, keeps: list[Path], catalog: ThemeCatalog | None) -> LauncherDialog:
    dialog = LauncherDialog(Settings(recent_keeps=keeps), catalog=catalog)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.recent.count() == len(keeps), timeout=5000)
    return dialog


def _colors(badge: QIcon) -> dict[str, QColor]:
    """Colors inside the badge, near its left edge, and on its bottom edge."""
    image = badge.pixmap(BADGE, BADGE).toImage()
    side = image.width()
    return {
        "inside": image.pixelColor(round(side * 0.12), round(side * 0.5)),
        "stripe": image.pixelColor(side // 2, side - 2),
    }


def test_a_keep_following_the_system_shows_its_plain_icon(tmp_path: Path) -> None:
    config = create_keep(tmp_path / "A.keep", "A", ThemeRef("music", 1)).config
    assert config.colors == "system"
    assert keep_badge("headphones", "system").cacheKey() == icon("headphones").cacheKey()


def test_a_keep_with_a_color_scheme() -> None:
    seen = _colors(keep_badge("library", "custom", OWN))
    assert seen["inside"].name() == OWN["background"]
    assert seen["stripe"].name() == OWN["accent"]


def test_a_keep_always_dark() -> None:
    seen = _colors(keep_badge("clapperboard", "dark"))
    assert seen["inside"].name() == DARK["background"]
    assert seen["stripe"].name() == DARK["background"]  # no accent stripe


def test_a_keeps_own_icon_is_found_in_the_worker(tmp_path: Path) -> None:
    named = _keep(tmp_path, "Named", "music", icon="radio")
    pictured = _keep(tmp_path, "Pictured", "music", icon="icons/vinyl.svg")
    (pictured / "icons").mkdir()
    (pictured / "icons" / "vinyl.svg").write_text(SVG, encoding="utf-8")
    gone = _keep(tmp_path, "Gone", "music", icon="icons/gone.svg")
    plain = _keep(tmp_path, "Plain", "music")
    found = {k.path.name: k.icon for k in describe_recent([named, pictured, gone, plain])}
    assert found == {
        "Named.keep": "radio",
        "Pictured.keep": str(pictured / "icons" / "vinyl.svg"),
        "Gone.keep": None,  # the theme's then
        "Plain.keep": None,
    }


def test_the_launchers_icons(qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog) -> None:
    keeps = [
        _keep(tmp_path, "Music", "music"),
        _keep(tmp_path, "Radio", "music", icon="radio"),
        _keep(tmp_path, "Own", "books", colors="custom"),
        tmp_path / "Missing.keep",
        _keep(tmp_path, "Nord", "books", colors="nord"),
        _keep(tmp_path, "Mine", "books", colors="user:mine"),
    ]
    save_user_scheme("Mine", {**OWN, "background": "#102030"})
    dialog = _launcher(qtbot, keeps, catalog)
    icons = [dialog.recent.item(row).icon() for row in range(dialog.recent.count())]
    assert icons[0].cacheKey() == lucide("headphones").cacheKey()  # the theme's
    assert icons[1].cacheKey() == lucide("radio").cacheKey()  # its own
    assert _colors(icons[2])["inside"].name() == OWN["background"]  # its colors
    assert icons[3].cacheKey() == lucide(GENERIC_KEEP).cacheKey()  # can't be read
    assert _colors(icons[4])["inside"].name() == "#2e3440"  # a built-in scheme
    assert _colors(icons[5])["inside"].name() == "#102030"  # the user's own


def test_themes_loaded_later_give_their_icons(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = _launcher(qtbot, [_keep(tmp_path, "Music", "music")], catalog=None)
    qtbot.waitUntil(
        lambda: dialog.recent.item(0).icon().cacheKey() == lucide("headphones").cacheKey(),
        timeout=10000,
    )
    assert "Music · " in dialog.recent.item(0).text()  # the theme's name, not its id
