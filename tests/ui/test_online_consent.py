"""Asking once per keep whether to look details up online, and the Keep configuration
toggle (#339)."""

from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.keep import ThemeRef, create_keep, load_keep_config
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.api import OnlineSource
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tagalot.ui.online import ALLOW, NEVER, ask_online_consent
from tagalot.ui.workers import ScanController
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

SOURCES = [
    OnlineSource("Crossref", "api.crossref.org", "DOIs"),
    OnlineSource("arXiv", "export.arxiv.org", "arXiv IDs", interval=3),
]


@pytest.mark.parametrize(
    ("button", "answer"),
    [("Allow for this keep", ALLOW), ("Not now", None), ("Never", NEVER)],
)
def test_the_question_names_each_service(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, button: str, answer: str | None
) -> None:
    shown: list[str] = []

    def answer_it(box: QMessageBox) -> int:
        shown.append(box.informativeText())
        assert [b.text() for b in box.buttons()] == ["Allow for this keep", "Not now", "Never"]
        assert box.defaultButton().text() == "Not now"
        next(b for b in box.buttons() if b.text() == button).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer_it)
    parent = QWidget()
    qtbot.addWidget(parent)
    assert ask_online_consent(parent, SOURCES) == answer
    assert shown[0].startswith(
        "Tagalot would send DOIs to Crossref and arXiv IDs to arXiv. Nothing else about "
        "your files leaves this computer."
    )


def test_keep_configuration_turns_lookups_on_and_off(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    dialog = window.configure_keep()
    assert dialog.online.isHidden()  # the music theme looks nothing up
    assert dialog.contents.isHidden()  # nor has it documents
    assert not dialog.online_lookups.isChecked()  # not asked yet
    dialog.online_lookups.setChecked(True)
    qtbot.waitUntil(lambda: session.keep.config.online_lookups == "allow", timeout=5000)
    dialog.online_lookups.setChecked(False)
    qtbot.waitUntil(lambda: session.keep.config.online_lookups == "never", timeout=5000)
    assert load_keep_config(session.keep.toml_path).online_lookups == "never"
    qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)


TITLES_THEME = """
from tagalot.themes.api import Entity, OnlineSource, Theme, field


class Film(Entity):
    film_id: str | None = field("Film ID")


class Films(Theme):
    id, name, version, api_version = "films_find_by", "Films", 1, 23
    entities = [Film]
    online_sources = [
        OnlineSource(
            "Films DB",
            "api.films.test",
            "film IDs",
            token_env="TAGALOT_TEST_FILMS_TOKEN",
            titles=True,
        ),
        OnlineSource("Film Posters", "posters.films.test", "film IDs"),
    ]
"""


def test_keep_configuration_chooses_what_identifies_an_item(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TAGALOT_TEST_FILMS_TOKEN", "token")
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "films_find_by.py").write_text(TITLES_THEME, encoding="utf-8")
    keep = create_keep(tmp_path / "k", "K", ThemeRef("films_find_by", 1))
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as session:
        window = MainWindow(session, scans=ScanController(QThreadPool()))
        qtbot.addWidget(window)
        dialog = window.configure_keep()
        assert not dialog.find_by_row.isHidden()  # a service finds items by title
        assert dialog.tokens.isHidden()  # its token is set
        assert [dialog.find_by.itemText(i) for i in range(dialog.find_by.count())] == [
            "IDs only",
            "IDs, or a title with its year",
            "IDs, or any title",
        ]
        assert dialog.find_by.currentData() == "ids"
        assert dialog.online_sends.text().startswith(
            "Tagalot would send film IDs to Films DB and film IDs to Film Posters."
        )
        dialog.find_by.setCurrentIndex(1)
        qtbot.waitUntil(lambda: session.keep.config.online_find_by == "title-year", timeout=5000)
        assert dialog.online_sends.text().startswith(
            "Tagalot would send film IDs, or titles with their years, to Films DB and film IDs "
            "to Film Posters."
        )
        assert load_keep_config(session.keep.toml_path).online_find_by == "title-year"
        qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)
        dialog.close()

        monkeypatch.delenv("TAGALOT_TEST_FILMS_TOKEN")
        dialog = KeepConfigWindow(session)  # as when Tagalot starts again
        qtbot.addWidget(dialog)
        assert dialog.find_by.currentData() == "title-year"
        assert not dialog.tokens.isHidden()
        assert dialog.tokens.text() == (
            "Films DB needs a token: set TAGALOT_TEST_FILMS_TOKEN to yours under "
            "Environment, below. Until then it isn't used."
        )
        assert dialog.online_sends.text().startswith("Tagalot would send film IDs to Film Posters.")
        # #544: set it for the keep, in the user's settings
        rows = [dialog.env_list.item(i).text() for i in range(dialog.env_list.count())]
        assert rows == ["TAGALOT_TEST_FILMS_TOKEN: not set (Films DB needs it)"]
        assert not dialog.env_remove.isEnabled()
        dialog.env_list.setCurrentRow(0)
        dialog.ask_env_value = lambda name: "s3cret"  # type: ignore[method-assign]
        dialog.env_set.click()
        assert session.environment() == {"TAGALOT_TEST_FILMS_TOKEN": "s3cret"}
        assert dialog.env_list.item(0).text() == "TAGALOT_TEST_FILMS_TOKEN = " + "•" * 8
        dialog.env_show.setChecked(True)
        assert dialog.env_list.item(0).text() == "TAGALOT_TEST_FILMS_TOKEN = s3cret"
        assert dialog.tokens.isHidden()
        assert dialog.online_sends.text().startswith(
            "Tagalot would send film IDs, or titles with their years, to Films DB"
        )
        qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)
        assert "s3cret" not in session.keep.toml_path.read_text(encoding="utf-8")
        dialog.ask_env_name = lambda: "OTHER_KEY"  # type: ignore[method-assign]
        dialog.ask_env_value = lambda name: "x"  # type: ignore[method-assign]
        dialog.env_add.click()
        assert session.environment() == {"TAGALOT_TEST_FILMS_TOKEN": "s3cret", "OTHER_KEY": "x"}
        dialog.env_list.setCurrentRow(0)  # OTHER_KEY, by name
        dialog.env_remove.click()
        assert session.environment() == {"TAGALOT_TEST_FILMS_TOKEN": "s3cret"}
        qtbot.waitUntil(lambda: dialog.busy == 0, timeout=5000)
        dialog.close()
        window.close()
