"""Tests for the keep launcher and the new-keep dialog."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox
from pytestqt.qtbot import QtBot

from tagalot.core.keep import DEFAULT_EXCLUDES, folder_name, load_keep_config, root_id_for
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, load_settings
from tagalot.themes.loader import ThemeCatalog, load_themes
from tagalot.ui import app as app_module
from tagalot.ui.launcher import (
    LauncherDialog,
    NewKeepDialog,
    describe_recent,
)

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


def _demo_keep(folder: Path) -> Path:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep_dir: Path = module.make_demo(folder)
    return keep_dir


@pytest.fixture
def catalog(tmp_path: Path) -> ThemeCatalog:
    return load_themes(user_dir=tmp_path / "no-user-themes")


@pytest.fixture
def opened() -> Iterator[list[KeepSession]]:
    sessions: list[KeepSession] = []
    yield sessions
    for session in sessions:
        session.close()


def _launcher(
    qtbot: QtBot, settings: Settings, tmp_path: Path, catalog: ThemeCatalog
) -> LauncherDialog:
    dialog = LauncherDialog(settings, settings_path=tmp_path / "settings.toml", catalog=catalog)
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def test_recent_keeps_are_listed(qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog) -> None:
    demo = _demo_keep(tmp_path / "scratch")
    settings = Settings(recent_keeps=[demo, tmp_path / "Gone.keep"])
    dialog = _launcher(qtbot, settings, tmp_path, catalog)
    qtbot.waitUntil(lambda: dialog.recent.count() == 2, timeout=5000)
    first, second = dialog.recent.item(0).text(), dialog.recent.item(1).text()
    assert first.startswith("Demo")
    assert "Files" in first  # the theme's name, not just its id
    assert "not found" in second
    assert dialog.open_button.isEnabled()


def test_describe_recent(tmp_path: Path) -> None:
    demo = _demo_keep(tmp_path / "scratch")
    good, missing = describe_recent([demo, tmp_path / "missing"])
    assert good.config is not None
    assert good.config.name == "Demo"
    assert missing.config is None
    assert missing.problem is not None
    assert "cannot read" in missing.problem


def test_opening_a_recent_keep(
    qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog, opened: list[KeepSession]
) -> None:
    demo = _demo_keep(tmp_path / "scratch")
    dialog = _launcher(qtbot, Settings(recent_keeps=[demo]), tmp_path, catalog)
    qtbot.waitUntil(lambda: dialog.recent.count() == 1, timeout=5000)
    dialog.opened.connect(opened.append)
    with qtbot.waitSignal(dialog.opened, timeout=10_000):
        dialog.open_button.click()
        assert not dialog.open_button.isEnabled()  # busy while opening
        assert dialog.status.text().startswith("Opening")
    assert opened[0].keep.config.name == "Demo"
    assert not dialog.isVisible()


def test_a_failed_open_leaves_the_launcher_usable(
    qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    dialog = _launcher(qtbot, Settings(recent_keeps=[tmp_path / "Gone.keep"]), tmp_path, catalog)
    qtbot.waitUntil(lambda: dialog.recent.count() == 1, timeout=5000)
    dialog.open_button.click()
    qtbot.waitUntil(lambda: bool(warnings), timeout=10_000)
    assert "does not exist" in warnings[0]
    qtbot.waitUntil(dialog.open_button.isEnabled, timeout=5000)
    assert dialog.isVisible()


def test_remove_from_recent(qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog) -> None:
    gone = tmp_path / "Gone.keep"
    settings = Settings(recent_keeps=[gone])
    dialog = _launcher(qtbot, settings, tmp_path, catalog)
    qtbot.waitUntil(lambda: dialog.recent.count() == 1, timeout=5000)
    dialog.remove_recent(gone)
    assert dialog.recent.count() == 0
    qtbot.waitUntil(lambda: (tmp_path / "settings.toml").exists(), timeout=5000)
    assert load_settings(tmp_path / "settings.toml").recent_keeps == []


def test_theme_problems_are_shown(qtbot: QtBot, tmp_path: Path) -> None:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    dialog = _launcher(qtbot, Settings(), tmp_path, load_themes(user_dir=themes))
    assert dialog.problems.isVisible()
    assert "1 theme file has problems" in dialog.problems.text()


def test_empty_recent_list(qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog) -> None:
    dialog = _launcher(qtbot, Settings(), tmp_path, catalog)
    qtbot.waitUntil(lambda: "No recent keeps" in dialog.status.text(), timeout=5000)
    assert not dialog.open_button.isEnabled()
    assert dialog.new_button.isEnabled()


# --- new keep ---


def _fill(dialog: NewKeepDialog, name: str, location: Path | str, root: Path | str) -> None:
    dialog.name_edit.setText(name)
    dialog.location_edit.setText(str(location))
    dialog.root_edit.setText(str(root))


@pytest.mark.parametrize(
    ("name", "location", "root", "problem"),
    [
        ("", "D:/Keeps", "D:/Music", "Give the keep a name."),
        ("A/B", "D:/Keeps", "D:/Music", "can't contain"),
        ("Music", "", "D:/Music", "Choose where to put the keep."),
        ("Music", "D:/Keeps", "", "Choose a folder for the keep to watch."),
    ],
)
def test_new_keep_validation(
    qtbot: QtBot, catalog: ThemeCatalog, name: str, location: str, root: str, problem: str
) -> None:
    dialog = NewKeepDialog(catalog)
    qtbot.addWidget(dialog)
    _fill(dialog, name, location, root)
    assert problem in (dialog.problem() or "")
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_new_keep_form_values(qtbot: QtBot, catalog: ThemeCatalog) -> None:
    dialog = NewKeepDialog(catalog)
    qtbot.addWidget(dialog)
    _fill(dialog, "  My   Music ", "D:\\Keeps", "\\\\nas\\music")
    assert dialog.problem() is None
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert dialog.name() == "My Music"
    assert dialog.keep_dir() == Path("D:\\Keeps") / "My Music.keep"
    assert str(dialog.root()) == "\\\\nas\\music"  # a UNC root survives the form
    assert dialog.theme().id == "generic"
    assert "My Music.keep" in dialog.preview.text()


def test_creating_a_keep_opens_it(
    qtbot: QtBot,
    tmp_path: Path,
    catalog: ThemeCatalog,
    opened: list[KeepSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = tmp_path / "My Photos"
    files.mkdir()

    def fill_and_accept(self: NewKeepDialog) -> QDialog.DialogCode:
        _fill(self, "Photos", tmp_path, files)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(NewKeepDialog, "exec", fill_and_accept)
    dialog = _launcher(qtbot, Settings(), tmp_path, catalog)
    dialog.opened.connect(opened.append)
    with qtbot.waitSignal(dialog.opened, timeout=10_000):
        dialog.new_button.click()
    config = load_keep_config(tmp_path / "Photos.keep" / "keep.toml")
    assert (config.name, config.theme.id) == ("Photos", "generic")
    assert [(r.id, r.name, r.path) for r in config.roots] == [
        ("my-photos", "My Photos", str(files))
    ]
    assert config.roots[0].exclude == list(DEFAULT_EXCLUDES)  # written to keep.toml
    assert opened[0].keep.config.id == config.id
    assert load_settings(tmp_path / "settings.toml").recent_keeps == [tmp_path / "Photos.keep"]


def test_a_document_theme_asks_how_to_search_inside_documents(
    qtbot: QtBot, catalog: ThemeCatalog
) -> None:
    dialog = NewKeepDialog(catalog)
    qtbot.addWidget(dialog)
    assert dialog.theme().id == "generic"
    assert not dialog._form.isRowVisible(dialog.contents_combo)  # plain files: no documents
    assert dialog.contents_index() is None
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findText("Research"))
    assert dialog._form.isRowVisible(dialog.contents_combo)
    assert dialog.contents_combo.currentText() == "Words"  # the default
    assert dialog.contents_index() == "words"
    dialog.contents_combo.setCurrentIndex(dialog.contents_combo.findText("Substrings"))
    assert dialog.contents_index() == "substrings"
    dialog.contents_combo.setCurrentIndex(dialog.contents_combo.findText("Off"))
    assert dialog.contents_index() is None
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findText("Books"))
    assert dialog._form.isRowVisible(dialog.contents_combo)


def test_a_new_research_keep_searches_inside_documents(
    qtbot: QtBot,
    tmp_path: Path,
    catalog: ThemeCatalog,
    opened: list[KeepSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = tmp_path / "Papers"
    files.mkdir()

    def fill_and_accept(self: NewKeepDialog) -> QDialog.DialogCode:
        _fill(self, "Papers", tmp_path, files)
        self.theme_combo.setCurrentIndex(self.theme_combo.findText("Research"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(NewKeepDialog, "exec", fill_and_accept)
    dialog = _launcher(qtbot, Settings(), tmp_path, catalog)
    dialog.opened.connect(opened.append)
    with qtbot.waitSignal(dialog.opened, timeout=10_000):
        dialog.new_button.click()
    config = load_keep_config(tmp_path / "Papers.keep" / "keep.toml")
    assert (config.theme.id, config.contents_index) == ("research", "words")


@pytest.mark.parametrize(
    ("folder", "name", "root_id"),
    [
        ("D:/My Photos", "My Photos", "my-photos"),
        ("D:\\Music\\", "Music", "music"),  # trailing separator
        (r"\\nas\music", "music", "music"),  # a UNC share root: Path(...).name is ""
        ("/Volumes/Música 2024!", "Música 2024!", "m-sica-2024"),
        ("___", "___", "root"),
    ],
)
def test_root_names_and_ids(folder: str, name: str, root_id: str) -> None:
    assert folder_name(folder) == name
    assert root_id_for(folder) == root_id


# --- the app ---


def test_the_app_shows_the_launcher_and_then_a_keep_window(
    qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog
) -> None:
    demo = _demo_keep(tmp_path / "scratch")
    tagalot = app_module.TagalotApp(Settings(recent_keeps=[demo]), tmp_path / "settings.toml")
    launcher = tagalot.show_launcher()
    qtbot.addWidget(launcher)
    assert launcher.isVisible()
    assert tagalot.show_launcher() is launcher  # reused while open
    qtbot.waitUntil(
        lambda: launcher.recent.count() == 1 and launcher.catalog is not None, timeout=10_000
    )
    with qtbot.waitSignal(launcher.opened, timeout=10_000):
        launcher.open_button.click()
    [window] = tagalot.windows
    qtbot.addWidget(window)
    assert window.windowTitle() == "Demo — Tagalot"
    assert window.open_other_action.text() == "Open another keep…"
    tagalot.close_all()
