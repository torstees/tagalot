"""Tagging from the UI: apply, remove, undo, and redo, off the GUI thread (DESIGN.md §7, §12).

Tag operations wait for the DB writer, so they run in a worker. They run one at a time, in
the order the user asked for them, on a pool of one thread: undo history is a stack, and two
drops finishing out of order would record them out of order.
"""

import logging
from collections.abc import Callable, Iterable

import shiboken6
from PySide6.QtCore import QObject, QThreadPool, Signal

from tagalot.core.session import KeepSession
from tagalot.core.tags import TagError
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)


def items_text(count: int) -> str:
    return "1 item" if count == 1 else f"{count:,} items"


class TagActions(QObject):
    """Runs tagging for one keep window.

    Signals: :attr:`changed` after an operation that changed something, with a status-bar
    message (tag assignments or the tag tree may be different: refresh views);
    :attr:`message` for a status-bar message when nothing changed or something failed.
    """

    changed = Signal(str)
    message = Signal(str)

    def __init__(self, session: KeepSession, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)  # one at a time, in order
        self.busy = 0

    # --- operations ---

    def apply(self, entity_ids: Iterable[int], tag_ids: Iterable[int], names: str) -> None:
        """Tag entities; ``names`` is how the tags read in messages ("'Iceland'")."""
        entities, tags = list(entity_ids), list(tag_ids)
        if not entities or not tags:
            return
        self._run(
            lambda: self.session.tags.apply(entities, tags),
            # With one tag, n is how many items newly got it; with several, count the items.
            lambda n: (
                f"Tagged {items_text(n if len(tags) == 1 else len(entities))} with {names}."
                if n
                else f"Already tagged with {names}: nothing to change."
            ),
        )

    def remove(self, entity_ids: Iterable[int], tag_ids: Iterable[int], names: str) -> None:
        """Untag entities."""
        entities, tags = list(entity_ids), list(tag_ids)
        if not entities or not tags:
            return
        self._run(
            lambda: self.session.tags.remove(entities, tags),
            lambda n: (
                f"Removed {names} from {items_text(len(entities))}."
                if n
                else f"None of those items had {names}: nothing to change."
            ),
        )

    def create(self, names: list[str], entity_ids: Iterable[int], path: str) -> None:
        """Create the tag at ``names`` (and missing parents), then apply it to
        ``entity_ids`` if there are any; ``path`` is how it reads in messages."""
        entities = list(entity_ids)

        def work() -> int:
            tag_id = self.session.tags.add_path(names)
            if entities:
                self.session.tags.apply(entities, [tag_id])
            return tag_id

        self._run(
            work,
            lambda _: (
                f"Created tag {path} and tagged {items_text(len(entities))} with it."
                if entities
                else f"Created tag {path}."
            ),
        )

    def undo(self) -> None:
        self._run(self.session.tags.undo, lambda label: f"Undid: {label}." if label else "")

    def redo(self) -> None:
        self._run(self.session.tags.redo, lambda label: f"Redid: {label}." if label else "")

    @property
    def undo_label(self) -> str | None:
        return self.session.tags.undo_label

    @property
    def redo_label(self) -> str | None:
        return self.session.tags.redo_label

    # --- running ---

    def _run[T](self, work: Callable[[], T], describe: Callable[[T], str]) -> None:
        self.busy += 1

        def done(result: T) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            text = describe(result)
            if result:  # a count or a label: something changed
                self.changed.emit(text)
            elif text:
                self.message.emit(text)

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            if isinstance(error, TagError):
                self.message.emit(str(error))
            else:
                logger.error("Tagging failed", exc_info=error)
                self.message.emit(f"Tagging failed: {error}")

        run_in_pool(work, on_done=done, on_error=failed, pool=self.pool)
