"""Asking once per keep whether to look details up online (DESIGN.md §9 *Online details*)."""

from PySide6.QtWidgets import QMessageBox, QWidget

from tagalot.core.online import consent_text
from tagalot.themes.api import OnlineSource

ALLOW, NEVER, REFUSE = "allow", "never", "refuse"


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


def ask_new_online_services(parent: QWidget | None, sources: list[OnlineSource]) -> str | None:
    """Ask whether a keep that allows lookups may also use services added since (#483),
    naming each and what it is sent: ``"allow"``, ``"refuse"`` (don't use them; the others
    go on), or ``None`` for **Not now** (asked again when the keep is opened again)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    title = "Use another online service?" if len(sources) == 1 else "Use more online services?"
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(
        "This keep looks details up online, and Tagalot can now also use "
        f"{_names(sources)}. {consent_text(sources)}\n\n"
        "Nothing is sent to it until you allow it. You can change this later in Keep "
        "configuration (Keep tab)."
    )
    allow = box.addButton("Allow", QMessageBox.ButtonRole.AcceptRole)
    later = box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
    refuse = box.addButton("Don't use it", QMessageBox.ButtonRole.DestructiveRole)
    box.setDefaultButton(later)
    box.setEscapeButton(later)
    box.exec()
    clicked = box.clickedButton()
    if clicked is allow:
        return ALLOW
    if clicked is refuse:
        return REFUSE
    return None


def _names(sources: list[OnlineSource]) -> str:
    names = [s.name for s in sources]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
