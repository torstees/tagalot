"""Export tags… and Import tags… on the Tag manager (#395), with which items have each
tag (#396)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity, EntityTag
from tagalot.core.search_fields import type_labels
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tag_yaml import ImportPlan
from tagalot.core.tags import PATH_SEPARATOR
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_import import TagImportDialog, items_text
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    qtbot.waitUntil(lambda: _page(window).loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> TagManagerPage:
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    return page


def _paths(session: KeepSession) -> set[str]:
    tree = session.tag_cache.get()
    return {PATH_SEPARATOR.join(tree.path(t)) for t in tree}


def _wait_status(qtbot: QtBot, window: MainWindow, prefix: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage().startswith(prefix), (
            window.statusBar().currentMessage()
        )

    qtbot.waitUntil(check, timeout=5000)


def test_export_saves_the_tree(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out" / "tags.yaml"
    target.parent.mkdir()
    asked: list[str] = []

    def choose(name: str) -> str:
        asked.append(name)
        return str(target)

    monkeypatch.setattr(window, "choose_export_path", choose)
    _page(window).export_tags_action.trigger()  # Export tags… → Tags only…
    qtbot.waitUntil(target.exists, timeout=5000)
    assert asked == ["Demo tags.yaml"]
    text = target.read_text(encoding="utf-8")
    assert "    Iceland:\n      description: Summer 2019 road trip" in text
    assert "aliases: [Ísland]" in text
    assert "\nFavorites:\n" in text


def test_import_previews_then_imports_as_one_step(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "more tags.yaml"
    source.write_text(
        "Places:\n  tags:\n    Norway: [Oslo]\n    ísland:\n      color: '#2e86de'\n"
        "People:\n  color: nope\n",
        encoding="utf-8",
    )
    before = _paths(session)
    seen: list[tuple[str, str, bool]] = []

    def review(dialog: TagImportDialog) -> bool:
        seen.append((dialog.summary.text(), dialog.report.toPlainText(), dialog.delete_unused()))
        dialog.delete_box.setChecked(True)
        seen.append((dialog.summary.text(), dialog.report.toPlainText(), dialog.delete_unused()))
        dialog.delete_box.setChecked(False)
        return dialog.import_button.isEnabled()

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(window, "review_tag_import", review)
    _page(window).import_button.click()
    _wait_status(qtbot, window, "Imported tags: 2 new, 1 updated.")

    (summary, report, deleting), (_, with_delete, _) = seen
    assert summary == "2 new tags, 1 updated, 1 problem."
    assert not deleting
    assert "Line 7: 'nope' isn't a color" in report
    assert f"Places{PATH_SEPARATOR}Norway{PATH_SEPARATOR}Oslo" in report
    assert f"Places{PATH_SEPARATOR}Iceland: color" in report  # matched by its alias
    assert "Kept, since items use them (" in with_delete
    assert "Kept, since items use them" not in report

    assert _paths(session) == before | {
        f"Places{PATH_SEPARATOR}Norway",
        f"Places{PATH_SEPARATOR}Norway{PATH_SEPARATOR}Oslo",
    }
    assert window.tag_actions.undo_label == "Import tags from 'more tags.yaml'"
    window.tag_actions.undo()
    qtbot.waitUntil(lambda: _paths(session) == before, timeout=5000)


def test_a_file_that_isnt_yaml_cant_be_imported(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "broken.yaml"
    source.write_text("Places: [Iceland\n", encoding="utf-8")
    seen: list[TagImportDialog] = []

    def review(dialog: TagImportDialog) -> bool:
        seen.append(dialog)
        return False

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(window, "review_tag_import", review)
    _page(window).import_button.click()
    qtbot.waitUntil(lambda: bool(seen), timeout=5000)
    [dialog] = seen
    assert not dialog.import_button.isEnabled()
    assert not dialog.delete_box.isEnabled()
    assert dialog.report.toPlainText().startswith("Problems, left out (1)\n  Line 2: ")


def test_a_file_that_isnt_text_is_reported(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "picture.yaml"
    source.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x80\x81")
    warnings: list[str] = []

    def warn(parent: QWidget, title: str, text: str) -> QMessageBox.StandardButton:
        warnings.append(text)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(QMessageBox, "warning", warn)
    _page(window).import_button.click()
    qtbot.waitUntil(lambda: bool(warnings), timeout=5000)
    assert warnings == ["Couldn't read picture.yaml: it isn't a text file."]


def _tagged(session: KeepSession) -> set[tuple[int, int]]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(EntityTag.entity_id, EntityTag.tag_id).where(EntityTag.by_file.is_(False))
        )
        return {(e, t) for e, t in rows}


def test_tags_and_their_items_go_out_and_come_back(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export with which items have each tag, take the tags off, import them back (#396)."""
    target = tmp_path / "out" / "tags and items.yaml"
    target.parent.mkdir()
    asked: list[str] = []

    def choose(name: str) -> str:
        asked.append(name)
        return str(target)

    monkeypatch.setattr(window, "choose_export_path", choose)
    _page(window).export_items_action.trigger()  # Export tags… → Tags and the items…
    qtbot.waitUntil(target.exists, timeout=5000)
    assert asked == ["Demo tags and items.yaml"]
    text = target.read_text(encoding="utf-8")
    assert "\n---\nitems:\n- type: " in text
    tagged = _tagged(session)
    assert tagged

    ids = sorted({e for e, _ in tagged})
    session.tags.remove(ids, {t for _, t in tagged})  # every tag of the user's off
    assert not _tagged(session)

    seen: list[TagImportDialog] = []

    def review(dialog: TagImportDialog) -> bool:
        seen.append(dialog)
        return dialog.import_button.isEnabled()

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(target))
    monkeypatch.setattr(window, "review_tag_import", review)
    _page(window).import_button.click()
    qtbot.waitUntil(lambda: _tagged(session) == tagged, timeout=5000)
    [dialog] = seen
    assert not dialog.match_box.isHidden()
    items = len(ids)
    assert dialog.summary.text().endswith(f"{items:,} items found: {len(tagged):,} tags to add.")
    dialog.match_box.setChecked(True)
    assert dialog.summary.text().endswith(", 0 tags to take off.")
    _wait_status(qtbot, window, "Imported tags: nothing to change; tagged")


def test_matching_the_file_takes_other_tags_off(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tagged = sorted(_tagged(session))
    entity_id, tag_id = tagged[0]
    with session.reader.connect() as conn:
        title, type_id = conn.execute(
            select(Entity.title, Entity.type).where(Entity.id == entity_id)
        ).one()
    label = type_labels(session.schema)[type_id]
    source = tmp_path / "one.yaml"
    source.write_text(
        f"---\nitems:\n- type: {label}\n  title: '{title}'\n  tags: []\n", encoding="utf-8"
    )

    def review(dialog: TagImportDialog) -> bool:
        dialog.match_box.setChecked(True)
        return dialog.import_button.isEnabled()

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(window, "review_tag_import", review)
    _page(window).import_button.click()
    qtbot.waitUntil(lambda: not any(e == entity_id for e, _ in _tagged(session)), timeout=5000)
    assert (entity_id, tag_id) not in _tagged(session)


def test_the_preview_says_why_there_is_nothing_to_import() -> None:
    """Items found that already have their tags, and one not found: nothing to do, and the
    preview says so (found in review)."""
    plan = ImportPlan(items=12, not_found=("Book “Made Up”",))
    assert items_text(plan, False) == (
        "12 items found, 1 not found: 0 tags to add. The items found already have the file's tags."
    )
    others = ImportPlan(items=12, tags_removed=2)
    assert items_text(others, False).endswith(
        "(and 2 others of yours, which ticking the last box takes off)."
    )
    assert items_text(others, True) == "12 items found: 0 tags to add, 2 tags to take off."
