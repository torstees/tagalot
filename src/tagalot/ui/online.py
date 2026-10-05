"""Asking once per keep whether to look details up online (DESIGN.md §9 *Online details*)."""

from PySide6.QtWidgets import QMessageBox, QWidget

from tagalot.core.online import consent_text
from tagalot.themes.api import OnlineSource

ALLOW, NEVER = "allow", "never"


def ask_online_consent(parent: QWidget | None, sources: list[OnlineSource]) -> str | None:
    """Ask whether this keep may look details up online, naming each service and what it
    is sent: ``"allow"``, ``"never"``, or ``None`` for **Not now** (nothing is sent, and the
    question comes back next time)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Look up details online?")
    box.setText("Look up details online?")
    box.setInformativeText(
        f"{consent_text(sources)}\n\nYou can change this later in Keep configuration (Keep tab)."
    )
    allow = box.addButton("Allow for this keep", QMessageBox.ButtonRole.AcceptRole)
    later = box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
    never = box.addButton("Never", QMessageBox.ButtonRole.DestructiveRole)
    box.setDefaultButton(later)
    box.setEscapeButton(later)
    box.exec()
    clicked = box.clickedButton()
    if clicked is allow:
        return ALLOW
    if clicked is never:
        return NEVER
    return None
