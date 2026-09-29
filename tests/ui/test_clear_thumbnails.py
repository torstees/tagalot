"""Keep → Clear thumbnail cache (#78)."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.thumbnails.cache import CacheStats
from tagalot.ui.main_window import MainWindow, format_bytes
from tests.ui.test_grid import _close, _grid_page, _row, _window, keep_dir, session

pytestmark = pytest.mark.gui

__all__ = ["keep_dir", "session"]  # fixtures


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1000, "1,000 bytes"),
        (1024, "1.0 KB"),
        (980 * 1024, "980 KB"),
        (int(12.4 * 1024 * 1024), "12.4 MB"),
        (3 * 1024**4, "3,072 GB"),
    ],
)
def test_format_bytes(size: int, text: str) -> None:
    assert format_bytes(size) == text


def _load_thumbnails(qtbot: QtBot, window: MainWindow) -> int:
    page = _grid_page(qtbot, window)
    photo = page.model.hit(_row(page, "glacier.jpg"))
    assert photo is not None
    page.grid.grab()
    qtbot.waitUntil(lambda: window.thumbnails.get(photo.id) is not None, timeout=10_000)
    qtbot.waitUntil(lambda: window.thumbnails.idle, timeout=10_000)  # every card on screen
    page.list_button.click()  # the grid would refill the cache as soon as it's cleared
    return photo.id


def test_clearing_asks_then_empties_the_cache(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, session)
    photo = _load_thumbnails(qtbot, window)
    stats = session.thumbnails.cache.stats()
    assert stats.count >= 1
    asked: list[CacheStats] = []

    def confirm(stats: CacheStats) -> bool:
        asked.append(stats)
        return True

    monkeypatch.setattr(window, "confirm_clear_thumbnails", confirm)
    window.clear_thumbnails_action.trigger()
    qtbot.waitUntil(lambda: window.statusBar().currentMessage().startswith("Cleared"))
    assert asked == [stats]
    assert window.statusBar().currentMessage() == f"Cleared {stats.count:,} thumbnails."
    assert session.thumbnails.cache.stats().count == 0
    assert window.clear_thumbnails_action.isEnabled()
    assert window.thumbnails.get(photo) is None  # made again when shown
    _grid_page(qtbot, window)
    qtbot.waitUntil(lambda: window.thumbnails.get(photo) is not None, timeout=10_000)
    _close(window)


def test_cancel_keeps_the_cache(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, session)
    _load_thumbnails(qtbot, window)
    before = session.thumbnails.cache.stats()
    monkeypatch.setattr(window, "confirm_clear_thumbnails", lambda stats: False)
    window.clear_thumbnails_action.trigger()
    qtbot.waitUntil(window.clear_thumbnails_action.isEnabled)
    assert session.thumbnails.cache.stats() == before
    _close(window)


def test_an_empty_cache_says_so(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, session)  # in the list: no thumbnails are made
    session.thumbnails.cache.clear()
    monkeypatch.setattr(window, "confirm_clear_thumbnails", lambda stats: pytest.fail("asked"))
    window.clear_thumbnails_action.trigger()
    qtbot.waitUntil(
        lambda: window.statusBar().currentMessage() == "The thumbnail cache is already empty."
    )
    _close(window)
