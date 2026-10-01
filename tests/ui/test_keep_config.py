"""The Keep configuration window: roots (#109)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from sqlalchemy import func, select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity, Resource, ResourceStatus
from tagalot.core.root_admin import RemovalCounts
from tagalot.core.session import KeepSession
from tagalot.core.settings import load_settings
from tagalot.ui.keep_config import DELETE, KEEP, KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tests.core.media_files import write_mp3
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


@pytest.fixture
def config(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> Iterator[KeepConfigWindow]:
    dialog = window.configure_keep()
    dialog.settings_path = tmp_path / "settings.toml"
    dialog.ask_scan = lambda root: True  # type: ignore[method-assign]
    qtbot.waitUntil(lambda: "files and folders" in dialog.status.text(), timeout=5000)
    yield dialog
    qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)


def _items(session: KeepSession) -> int:
    with session.reader.connect() as conn:
        return int(conn.scalar(select(func.count()).select_from(Entity)) or 0)


def _statuses(session: KeepSession) -> set[ResourceStatus]:
    with session.reader.connect() as conn:
        return set(conn.scalars(select(Resource.status)))


def test_the_window_lists_roots_with_their_status(
    window: MainWindow, config: KeepConfigWindow
) -> None:
    assert config.roots.count() == 1
    assert config.roots.item(0).text() == "Music files  \u2014  online"
    assert config.name.text() == "Music files"
    assert "Online \u00b7 24 files and folders \u00b7 last scanned" in config.status.text()
    assert config.watch_button.text() == "Stop watching"
    assert window.configure_keep() is config  # one per main window


def test_renaming_and_excludes_are_saved_at_once(
    qtbot: QtBot, window: MainWindow, session: KeepSession, config: KeepConfigWindow
) -> None:
    config.name.setText("My music")
    with qtbot.waitSignal(config.changed, timeout=5000):
        config.name.editingFinished.emit()
    assert load_keep_config(session.keep.toml_path).roots[0].name == "My music"
    qtbot.waitUntil(lambda: config.roots.item(0).text().startswith("My music"), timeout=5000)

    config.exclude.setPlainText("**/Untagged/**\n\n")
    with qtbot.waitSignal(config.changed, timeout=5000) as blocker:
        QApplication.sendEvent(config.exclude, QFocusEvent(QEvent.Type.FocusOut))
    assert "Scan it to update its files" in blocker.args[0]
    assert load_keep_config(session.keep.toml_path).roots[0].exclude == ["**/Untagged/**"]


def test_a_bad_folder_is_explained_and_not_saved(
    session: KeepSession, config: KeepConfigWindow
) -> None:
    before = session.keep.toml_path.read_text(encoding="utf-8")
    config.path.setText(str(session.keep.dir / "inside"))
    config.path.editingFinished.emit()
    assert "keep folder" in config.error.text()
    assert config.path.text() == session.keep.config.roots[0].path  # the saved value again
    assert session.keep.toml_path.read_text(encoding="utf-8") == before


def test_this_computers_folder_is_saved_for_the_user(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow, tmp_path: Path
) -> None:
    config.local.setText("Z:/Music")
    with qtbot.waitSignal(config.changed, timeout=5000):
        config.local.editingFinished.emit()
    assert session.root_path("music") == "Z:/Music"
    saved = load_settings(tmp_path / "settings.toml")
    assert saved.root_overrides == {session.keep.config.id: {"music": "Z:/Music"}}
    with qtbot.waitSignal(config.changed, timeout=5000):
        config._set_local("")
    assert session.root_path("music") == session.keep.config.roots[0].path


def test_stop_watching_and_watch_again(
    qtbot: QtBot, window: MainWindow, session: KeepSession, config: KeepConfigWindow
) -> None:
    items = _items(session)
    with qtbot.waitSignal(config.changed, timeout=5000):
        config.watch_button.click()
    assert _statuses(session) == {ResourceStatus.OFFLINE}
    assert _items(session) == items
    qtbot.waitUntil(lambda: config.roots.item(0).text().endswith("not watched"), timeout=5000)
    assert config.watch_button.text() == "Watch again"
    assert not config.scan_button.isEnabled()

    with qtbot.waitSignal(window.scans.finished, timeout=10_000):  # it offers a scan: yes
        config.watch_button.click()
    assert _statuses(session) == {ResourceStatus.OK}
    qtbot.waitUntil(lambda: config.roots.item(0).text().endswith("online"), timeout=5000)


def test_remove_keeping_the_items_stops_watching(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow
) -> None:
    asked: list[RemovalCounts] = []

    def ask(root: object, counts: RemovalCounts) -> str:
        asked.append(counts)
        return KEEP

    config.ask_remove = ask  # type: ignore[method-assign]
    with qtbot.waitSignal(config.changed, timeout=5000):
        config.remove_button.click()
    assert asked == [RemovalCounts(deleted=24, kept=0)]
    assert session.keep.config.roots[0].watched is False
    assert _items(session) == 24


def test_remove_deleting_the_items(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow
) -> None:
    config.ask_remove = lambda root, counts: DELETE  # type: ignore[method-assign]
    confirmed: list[int] = []
    config.confirm_delete = lambda root, counts: not confirmed.append(counts.deleted)  # type: ignore[method-assign, func-returns-value]
    with qtbot.waitSignal(config.changed, timeout=5000) as blocker:
        config.remove_button.click()
    assert confirmed == [24]
    assert blocker.args == ["Removed Music files and 24 items from the keep."]
    assert session.keep.config.roots == []
    assert _items(session) == 0
    qtbot.waitUntil(lambda: config.empty.isVisible(), timeout=5000)


def test_cancelling_the_delete_changes_nothing(
    qtbot: QtBot, session: KeepSession, config: KeepConfigWindow
) -> None:
    config.ask_remove = lambda root, counts: DELETE  # type: ignore[method-assign]
    config.confirm_delete = lambda root, counts: False  # type: ignore[method-assign]
    config.remove_button.click()
    qtbot.wait(300)
    assert len(session.keep.config.roots) == 1
    assert _items(session) == 24


def test_adding_a_folder_watches_and_scans_it(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    config: KeepConfigWindow,
    tmp_path: Path,
) -> None:
    more = tmp_path / "More music"
    write_mp3(more / "Nina Simone" / "Live" / "01 Feeling Good.mp3", tags={"title": "Feeling Good"})
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        config.add_folder(str(more))
    roots = load_keep_config(session.keep.toml_path).roots
    assert [(r.id, r.name) for r in roots] == [("music", "Music files"), ("more-music", str(more))]
    assert config.current_root() is not None
    assert config.current_root().id == "more-music"  # type: ignore[union-attr]
    with session.reader.connect() as conn:
        assert "Feeling Good" in set(conn.scalars(select(Entity.title)))

    config.add_folder(str(more))  # again
    assert "already watches" in config.error.text()
