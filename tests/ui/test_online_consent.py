"""Asking once per keep whether to look details up online, and the Keep configuration
toggle (#339)."""

import pytest
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.keep import load_keep_config
from tagalot.core.session import KeepSession
from tagalot.themes.api import OnlineSource
from tagalot.ui.main_window import MainWindow
from tagalot.ui.online import ALLOW, NEVER, ask_online_consent
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
