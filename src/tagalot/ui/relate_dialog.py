"""Add to cast…: choose an item to relate to this one by hand, or name a new one (#260).

A search box lists the other side's items as you type (an actor for a movie's cast, a movie
for an actor's filmography), leaving out those already related; "New actor: <name>" makes
one from what was typed. The window does the relating (``KeepSession.add_related``), as one
undo step.
"""

import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.search import SearchHit, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.themes.api import entity_label
from tagalot.ui.workers import run_in_pool

MAX_RESULTS = 50
_HIT = Qt.ItemDataRole.UserRole
_NEW = Qt.ItemDataRole.UserRole + 1


class RelateDialog(QDialog):
    """Pick (or name) an item to relate; :meth:`chosen` gives (item id, None) or
    (None, new item's name)."""

    def __init__(
        self,
        session: KeepSession,
        title: str,
        other_type: str,
        section: str,
        already: set[int],
        parent: QWidget | None = None,
        *,
        prompt: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.other_type = other_type
        self.already = already
        entity = session.schema.by_type_id(other_type).entity
        self.noun = entity_label(entity).lower()
        self.setWindowTitle(f"Add to {section.lower()}")
        self.resize(420, 400)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.look_up)
        self._generation = 0

        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Find a {self.noun} by name, or type a new one")
        self.search.textChanged.connect(lambda _: self._timer.start())
        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.currentItemChanged.connect(lambda *_: self._enable())
        self.results.itemDoubleClicked.connect(lambda _: self._accept_if_ready())
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.add_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.add_button.setText("Add")
        self.add_button.setEnabled(False)
        self.buttons.accepted.connect(self._accept_if_ready)
        self.buttons.rejected.connect(self.reject)
        note = QLabel("What you add stays: scans reading this item's files won't remove it.")
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(prompt or f"Add to <b>{title}</b>'s {section.lower()}:"))
        layout.addWidget(self.search)
        layout.addWidget(self.results, 1)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self.look_up()

    def look_up(self) -> None:
        """List the items matching the search box, in a worker."""
        self._generation += 1
        generation, session = self._generation, self.session
        text = self.search.text().strip()
        spec = SearchSpec(types=(self.other_type,), text=text or None)

        def job() -> list[SearchHit]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return run_search(conn, spec, tree, limit=MAX_RESULTS)

        def done(hits: list[SearchHit]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(text, hits)

        run_in_pool(job, on_done=done)

    def _show(self, text: str, hits: list[SearchHit]) -> None:
        self.results.clear()
        name = " ".join(text.split())
        if name and not any(h.title.casefold() == name.casefold() for h in hits):
            item = QListWidgetItem(f"New {self.noun}: {name}")
            item.setData(_NEW, name)
            self.results.addItem(item)
        for hit in hits:
            item = QListWidgetItem(hit.title)
            item.setData(_HIT, hit.id)
            if hit.id in self.already:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip("Already here")
            self.results.addItem(item)
        for n in range(self.results.count()):  # the first that can be added
            if self.results.item(n).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.results.setCurrentRow(n)
                break
        self._enable()

    def chosen(self) -> tuple[int | None, str | None]:
        if self.results.currentRow() < 0:
            return None, None
        item = self.results.currentItem()
        new = item.data(_NEW)
        if isinstance(new, str):
            return None, new
        hit = item.data(_HIT)
        return (hit, None) if isinstance(hit, int) else (None, None)

    def _enable(self) -> None:
        item = self.results.currentItem()
        self.add_button.setEnabled(
            item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
        )

    def _accept_if_ready(self) -> None:
        if self.add_button.isEnabled():
            self.accept()
