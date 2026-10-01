"""The Keep configuration window's Thumbnails and Keep tabs, and per-folder options (#109)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel, QMenu, QSpinBox
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.thumbnails.cache import CacheStats
from tagalot.ui.keep_config import THUMBNAILS_TAB, KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tests.ui.test_contents_search import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures: the assets demo (its theme has artist_level)


@pytest.fixture
def config(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> Iterator[KeepConfigWindow]:
    dialog = window.configure_keep()
    dialog.settings_path = tmp_path / "settings.toml"
    dialog.ask_scan = lambda root: True  # type: ignore[method-assign]
    qtbot.waitUntil(lambda: dialog.cache_stats.text() != "", timeout=5000)
    yield dialog
    qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)


def test_the_largest_thumbnail_size(
    qtbot: QtBot, window: MainWindow, session: KeepSession, config: KeepConfigWindow
) -> None:
    assert config.theme_size.isChecked()
    assert config.theme_size.text() == "The theme's size (1024 px)"
    assert not config.max_size.isEnabled()
    with qtbot.waitSignal(config.thumbnail_max_changed, timeout=5000):
        config.theme_size.setChecked(False)  # keeps 1024 for now, as the keep's own
    config.max_size.setValue(512)
    with qtbot.waitSignal(config.thumbnail_max_changed, timeout=5000):
        config.max_size.editingFinished.emit()
    assert load_keep_config(session.keep.toml_path).thumbnail_max == 512
    assert session.thumbnail_max == 512
    assert session.thumbnails.size == 512
    labels = [a.text() for a in window.size_menu.actions() if a.text()]
    assert "Largest (512 px)" in labels
    assert window.thumbnail_size <= 512

    with qtbot.waitSignal(config.thumbnail_max_changed, timeout=5000):
        config.theme_size.setChecked(True)
    assert load_keep_config(session.keep.toml_path).thumbnail_max is None
    assert session.thumbnail_max == 1024


def test_clearing_thumbnails_from_the_window(
    qtbot: QtBot, window: MainWindow, config: KeepConfigWindow
) -> None:
    asked: list[CacheStats] = []
    window.confirm_clear_thumbnails = lambda stats: bool(asked.append(stats))  # type: ignore[method-assign, func-returns-value]
    keep_menu = window.menuBar().actions()[0].menu()
    assert isinstance(keep_menu, QMenu)
    labels = [a.text() for a in keep_menu.actions()]
    assert "Clear thumbnail cache…" not in labels  # moved to this window
    assert "Configure keep…" in labels
    config.clear_thumbnails_requested.emit()
    qtbot.waitUntil(
        lambda: bool(asked) or "already empty" in window.statusBar().currentMessage(),
        timeout=5000,
    )


def test_renaming_the_keep(qtbot: QtBot, session: KeepSession, config: KeepConfigWindow) -> None:
    assert config.keep_name.text() == "Assets"
    config.keep_name.setText("Art")
    with qtbot.waitSignal(config.changed, timeout=5000):
        config.keep_name.editingFinished.emit()
    assert load_keep_config(session.keep.toml_path).name == "Art"
    assert config.windowTitle() == "Configure Art"


def test_the_keeps_theme_options(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow
) -> None:
    level = config.keep_options.findChild(QSpinBox, "option_artist_level")
    assert isinstance(level, QSpinBox)
    assert level.value() == 1  # the default
    level.setValue(2)
    with qtbot.waitSignal(config.changed, timeout=5000) as blocker:
        level.editingFinished.emit()
    assert "next scan" in blocker.args[0]
    assert load_keep_config(session.keep.toml_path).theme_options == {"artist_level": 2}

    qtbot.waitUntil(
        lambda: config.keep_options.findChild(QSpinBox, "option_artist_level") is not level,
        timeout=5000,
    )  # shown again from the saved file
    reset = next(
        b
        for b in config.keep_options.findChildren(type(config.add_button))
        if b.text() == "Default"
    )
    assert reset.isEnabled()
    with qtbot.waitSignal(config.changed, timeout=5000):
        reset.click()
    assert load_keep_config(session.keep.toml_path).theme_options == {}


def test_a_folders_own_theme_options(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow
) -> None:
    assert config.root_options.isVisible()
    use_own = config.root_options.findChild(QCheckBox)
    assert isinstance(use_own, QCheckBox)
    assert use_own.text() == "Artist folder level"
    assert not use_own.isChecked()
    with qtbot.waitSignal(config.changed, timeout=5000):
        use_own.setChecked(True)  # starts from the keep's value
    assert load_keep_config(session.keep.toml_path).roots[0].options == {"artist_level": 1}

    qtbot.waitUntil(
        lambda: (
            (box := config.root_options.findChild(QSpinBox, "option_artist_level")) is not None
            and box.isEnabled()
        ),
        timeout=5000,
    )
    box = config.root_options.findChild(QSpinBox, "option_artist_level")
    assert isinstance(box, QSpinBox)
    box.setValue(3)
    with qtbot.waitSignal(config.changed, timeout=5000):
        box.editingFinished.emit()
    assert load_keep_config(session.keep.toml_path).roots[0].options == {"artist_level": 3}

    use_own = config.root_options.findChild(QCheckBox)
    assert isinstance(use_own, QCheckBox)
    with qtbot.waitSignal(config.changed, timeout=5000):
        use_own.setChecked(False)
    assert load_keep_config(session.keep.toml_path).roots[0].options == {}


def test_theme_info(config: KeepConfigWindow) -> None:
    tab = config.tabs.widget(2)
    assert tab is not None
    texts = [label.text() for label in tab.findChildren(QLabel)]
    assert any("2D assets" in t and "built in" in t for t in texts)
    assert "Artists, Images, Fonts, Archives" in texts


def test_the_stored_count_follows_new_thumbnails(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow
) -> None:
    """Thumbnails stored while browsing show up without reopening anything."""
    config.tabs.setCurrentIndex(THUMBNAILS_TAB)
    assert config._stats_timer.isActive()  # counting while the tab is in view
    before = session.thumbnails.cache.stats().count
    for entity_id in _entity_ids(session)[:3]:
        session.thumbnails.resolve(entity_id)  # what showing a grid does
    after = session.thumbnails.cache.stats().count
    assert after > before
    config._stats_timer.timeout.emit()
    qtbot.waitUntil(
        lambda: config.cache_stats.text().startswith(f"{after:,} thumbnails"), timeout=5000
    )
    config.tabs.setCurrentIndex(0)
    assert not config._stats_timer.isActive()  # not while another tab is shown


def _entity_ids(session: KeepSession) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).order_by(Entity.id)))


def test_f5_makes_thumbnails_in_the_background(
    qtbot: QtBot, window: MainWindow, session: KeepSession, config: KeepConfigWindow
) -> None:
    assert config.after_scan.isChecked()
    before = session.thumbnails.cache.stats().count
    with qtbot.waitSignal(window._queue_done, timeout=30_000) as blocker:
        window.scan_now()
    assert blocker.args[0].done == len(_entity_ids(session))  # nothing was shown yet
    assert session.thumbnails.cache.stats().count > before
    assert not window.thumbnail_status.isVisible()  # gone when done

    with qtbot.waitSignal(config.changed, timeout=5000):
        config.after_scan.setChecked(False)
    assert load_keep_config(session.keep.toml_path).thumbnails_after_scan is False
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    qtbot.wait(200)
    assert not session.thumbnail_queue.running
