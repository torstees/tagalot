"""Link to item…: choose the item (and role) to link files to by hand (#243).

A search box lists matching items (title and type) among the types with a role that takes
every one of the files; choosing one offers those roles. The window does the linking
(``KeepSession.link_files``), as one undo step.
"""

import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.links import accepting_roles
from tagalot.core.search import SearchHit, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.themes.api import Kind, entity_label
from tagalot.ui.workers import run_in_pool

MAX_RESULTS = 50
_HIT = Qt.ItemDataRole.UserRole


class LinkDialog(QDialog):
    """Pick an item and one of its roles; :meth:`chosen` gives (entity id, role)."""

    def __init__(
        self,
        session: KeepSession,
        what: str,
        kinds: list[Kind | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.kinds = kinds
        self.setWindowTitle("Link to item")
        self.resize(460, 420)
        schema = session.schema
        self.types = {
            t.type_id: t.entity
            for t in schema.entities.values()
            if accepting_roles(t.entity, kinds)
        }
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.look_up)
        self._generation = 0
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find an item by name")
        self.search.textChanged.connect(lambda _: self._timer.start())
        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.currentItemChanged.connect(lambda *_: self._show_roles())
        self.results.itemDoubleClicked.connect(lambda _: self._accept_if_ready())
        self.role = QComboBox()
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.link_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.link_button.setText("Link")
        self.link_button.setEnabled(False)
        self.buttons.accepted.connect(self._accept_if_ready)
        self.buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("Role:", self.role)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Link <b>{what}</b> to:"))
        layout.addWidget(self.search)
        layout.addWidget(self.results, 1)
        layout.addLayout(form)
        layout.addWidget(self.note)
        layout.addWidget(self.buttons)
        if not self.types:
            self.note.setText("No kind of item in this keep takes that kind of file.")
            self.search.setEnabled(False)
        else:
            self.note.setText(
                "Your link stays as you made it: scans won't change the item from this file."
            )
            self.look_up()

    def look_up(self) -> None:
        """List the items matching the search box, in a worker."""
        if not self.types:
            return
        self._generation += 1
        generation, session = self._generation, self.session
        spec = SearchSpec(types=tuple(self.types), text=self.search.text().strip() or None)

        def job() -> list[SearchHit]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return run_search(conn, spec, tree, limit=MAX_RESULTS)

        def done(hits: list[SearchHit]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(hits)

        run_in_pool(job, on_done=done)

    def _show(self, hits: list[SearchHit]) -> None:
        self.results.clear()
        for hit in hits:
            item = QListWidgetItem(f"{hit.title}  —  {entity_label(self.types[hit.type])}")
            item.setData(_HIT, hit)
            self.results.addItem(item)
        if hits:
            self.results.setCurrentRow(0)
        self._show_roles()

    def _show_roles(self) -> None:
        self.role.clear()
        hit = self.current_hit()
        if hit is not None:
            for declared in accepting_roles(self.types[hit.type], self.kinds):
                self.role.addItem(declared.name, declared.name)
        self.link_button.setEnabled(hit is not None and self.role.count() > 0)

    def current_hit(self) -> SearchHit | None:
        if self.results.currentRow() < 0:
            return None
        hit = self.results.currentItem().data(_HIT)
        return hit if isinstance(hit, SearchHit) else None

    def _accept_if_ready(self) -> None:
        if self.link_button.isEnabled():
            self.accept()

    def chosen(self) -> tuple[int, str] | None:
        hit = self.current_hit()
        role = self.role.currentData()
        return (hit.id, role) if hit is not None and isinstance(role, str) else None
