"""Links outside the keep's folders (#603), on the music demo: the first scan that finds
some asks once, an answer of Include reads them at once, and Keep configuration changes
the answer."""

import shutil
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Resource, ResourceStatus
from tagalot.core.session import KeepSession
from tagalot.ui.keep_config import OUTSIDE_LINKS
from tagalot.ui.main_window import MainWindow
from tests.core.test_shortcuts import make_lnk
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

LINK = "Jazz Hits/Elsewhere.mp3.lnk"


def _link_outside(session: KeepSession, tmp_path: Path) -> None:
    root = Path(session.root_path("music"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(root / "Jazz Hits" / "02 Blue.mp3", elsewhere / "Elsewhere.mp3")
    (root / LINK).write_bytes(make_lnk(local=str(elsewhere / "Elsewhere.mp3")))


def _status(session: KeepSession) -> ResourceStatus | None:
    with session.reader.connect() as conn:
        return conn.scalar(select(Resource.status).where(Resource.relpath == LINK))


def test_the_first_scan_asks_and_include_reads_them(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _link_outside(session, tmp_path)
    asked: list[list[str]] = []

    def answer(paths: list[str]) -> bool:
        asked.append(paths)
        return True

    monkeypatch.setattr(window, "confirm_outside_links", answer)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    assert asked == [[LINK]]
    # Included: saved, and read by a scan of its own.
    qtbot.waitUntil(lambda: _status(session) is ResourceStatus.OK, timeout=10_000)
    assert load_keep_config(session.keep.toml_path).outside_links is True
    qtbot.waitUntil(lambda: window.scan_action.isEnabled(), timeout=10_000)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    assert len(asked) == 1  # asked once


def test_leave_out_is_kept_and_keep_configuration_changes_it(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _link_outside(session, tmp_path)
    monkeypatch.setattr(window, "confirm_outside_links", lambda paths: False)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    qtbot.waitUntil(lambda: session.keep.config.outside_links is False, timeout=5000)
    assert _status(session) is None  # left out

    dialog = window.configure_keep()
    qtbot.waitUntil(lambda: dialog.outside_links.currentData() is False, timeout=5000)
    include = next(i for i, (_, v) in enumerate(OUTSIDE_LINKS) if v is True)
    dialog.outside_links.setCurrentIndex(include)
    with qtbot.waitSignal(dialog.changed, timeout=5000) as saved:
        dialog.outside_links.activated.emit(include)
    assert "next scan" in saved.args[0]
    assert load_keep_config(session.keep.toml_path).outside_links is True
    qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)
