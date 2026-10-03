"""Tagging from the UI: apply, remove, undo, and redo, off the GUI thread (DESIGN.md §7, §12).

Tag operations wait for the DB writer, so they run in a worker. They run one at a time, in
the order the user asked for them, on a pool of one thread: undo history is a stack, and two
drops finishing out of order would record them out of order.
"""

import logging
from collections.abc import Callable, Iterable

import shiboken6
from PySide6.QtCore import QObject, QThreadPool, Signal

from tagalot.core.actions import ActionError, ActionResult
from tagalot.core.fields import FieldEditError
from tagalot.core.links import LinkError
from tagalot.core.reextract import ReextractReport
from tagalot.core.session import KeepSession
from tagalot.core.tags import DeleteMode, TagError, count_tagged_entities, subtree_usage
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

    # --- tag operations (the tag manager, §7) ---

    def add_tag(self, parent_id: int | None, name: str) -> None:
        """Create a tag under ``parent_id`` (``None``: the top level)."""
        self._run(
            lambda: self.session.tags.add(parent_id, name),
            lambda _: f"Added tag {' '.join(name.split())!r}.",
        )

    def rename(self, tag_id: int, name: str, old_name: str) -> None:
        new = " ".join(name.split())

        def work() -> bool:
            if new == old_name:
                return False  # nothing to do
            self.session.tags.rename(tag_id, name)
            return True

        self._run(work, lambda changed: f"Renamed {old_name!r} to {new!r}." if changed else "")

    def move(self, tag_id: int, parent_id: int | None, name: str, target: str | None) -> None:
        """Move a tag (with its sub-tags) under ``parent_id``; the message says how many
        items carry the moved tags (§7: "Show the affected item count")."""
        session = self.session

        def work() -> int:
            with session.reader.connect() as conn:
                affected = subtree_usage(conn, tag_id)
            session.tags.reparent(tag_id, parent_id)
            return affected + 1  # truthy even when no items are affected

        where = f"under {target!r}" if target is not None else "to the top level"
        self._run(work, lambda n: f"Moved {name!r} {where} ({items_text(n - 1)}).")

    def change(self, work: Callable[[], object], message: str) -> None:
        """Run a tag operation that returns nothing useful (set a color, add an alias…);
        ``message`` goes to the status bar once it's done."""

        def run() -> bool:
            work()
            return True

        self._run(run, lambda _: message)

    def merge(self, source_id: int, target_id: int, source: str, target: str) -> None:
        """Fold ``source_id`` into ``target_id`` (§7); the message says how many items
        were tagged with it."""
        session = self.session

        def work() -> int:
            with session.reader.connect() as conn:
                affected = count_tagged_entities(conn, [source_id])
            session.tags.merge(source_id, target_id)
            return affected + 1  # truthy even when no items are affected

        self._run(
            work, lambda n: f"Merged {source!r} into {target!r} ({items_text(n - 1)} retagged)."
        )

    def delete(self, tag_id: int, mode: DeleteMode | None, name: str) -> None:
        """Delete a tag; with ``DeleteMode.SUBTREE`` its sub-tags go too, with
        ``PROMOTE`` they move up. The message says how many items lost a tag."""
        session = self.session

        def work() -> int:
            with session.reader.connect() as conn:
                if mode is DeleteMode.SUBTREE:
                    affected = subtree_usage(conn, tag_id)
                else:
                    affected = count_tagged_entities(conn, [tag_id])
            session.tags.delete(tag_id, mode)
            return affected + 1

        what = f"{name!r} and its sub-tags" if mode is DeleteMode.SUBTREE else repr(name)
        self._run(work, lambda n: f"Deleted {what} ({items_text(n - 1)} lost a tag).")

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

    # --- field edits (detail pages, §12) ---

    def edit_field(self, entity_id: int, name: str, value: object) -> None:
        """Set a field (or ``"title"``) by hand; one undo step."""
        self._run(
            lambda: self.session.tags.edit_field(entity_id, name, value),
            lambda change: f"{change.label}.",
        )

    def edit_extra(self, entity_id: int, name: str, value: object, new: bool) -> None:
        """Add (``new``), change, or remove (``None``) an extra field; one undo step."""
        text = value if isinstance(value, str) else None
        self._run(
            lambda: self.session.tags.edit_extra(entity_id, name, text, new=new),
            lambda change: f"{change.label}.",
        )

    def reread(self, entity_ids: Iterable[int], replace_edits: bool) -> None:
        """Read these items' files again now (keeping, or replacing, what the user edited);
        one undo step."""
        ids = list(entity_ids)
        if not ids:
            return

        def describe(report: ReextractReport) -> str:
            items = items_text(len(ids))
            text = f"Re-read {items} from their files"
            text += ", replacing your edits." if replace_edits else "."
            if report.offline:
                text += f" {items_text(report.offline)} couldn't be reached (offline or missing)."
            if report.errors:
                text += f" {len(report.errors)} files couldn't be read."
            return text

        self._run(lambda: self.session.reextract(ids, replace_edits=replace_edits), describe)

    def run_action(
        self,
        method: str,
        entity_ids: Iterable[int],
        on_outputs: Callable[[ActionResult], None],
    ) -> None:
        """Run a theme action on these items in a worker; one undo step if it changed
        anything. ``on_outputs`` then carries out what it asked for (opening files)."""
        ids = list(entity_ids)
        if not ids:
            return

        def describe(result: ActionResult) -> str:
            on_outputs(result)
            return result.text

        self._run(lambda: self.session.run_action(method, ids), describe)

    def dismiss(self, name: str, ids: list[int]) -> None:
        """Hide files or items from a triage list until they change; one undo step."""

        def describe(count: int) -> str:
            what = "file" if name == "unlinked" else "item"
            return f"Dismissed {count:,} {what}{'' if count == 1 else 's'} until they change."

        self._run(lambda: self.session.dismiss(name, ids), describe)

    def delete_items(self, ids: list[int]) -> None:
        """Delete items (never their files); one undo step."""
        self._run(
            lambda: self.session.delete_items(ids),
            lambda count: f"Deleted {items_text(count)}. Edit \u2192 Undo brings them back.",
        )

    def link_files(self, entity_id: int, resource_ids: list[int], role: str) -> None:
        """Link files to an item by hand; one undo step."""
        self._run(
            lambda: self.session.link_files(entity_id, resource_ids, role),
            lambda label: f"{label}.",
        )

    def merge_items(self, keep_id: int, other_ids: list[int], choices: dict[str, int]) -> None:
        """Merge items into one (never their files); one undo step."""
        self._run(
            lambda: self.session.merge_items(keep_id, other_ids, choices),
            lambda label: f"{label}. Edit \u2192 Undo separates them again.",
        )

    def set_not_duplicate(self, entries: list[tuple[str, str, str]], dismissed: bool) -> None:
        """Hide groups or pairs from Dedupe as not duplicates, or show them again; one
        undo step."""
        self._run(
            lambda: self.session.set_not_duplicate(entries, dismissed),
            lambda label: f"{label}.",
        )

    def unlink_file(self, entity_id: int, resource_id: int, role: str) -> None:
        """Remove a link made by hand; one undo step."""
        self._run(
            lambda: self.session.unlink_file(entity_id, resource_id, role),
            lambda label: f"{label}.",
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
            if isinstance(error, TagError | FieldEditError | ActionError | LinkError):
                self.message.emit(str(error))
            else:
                logger.error("Tagging failed", exc_info=error)
                self.message.emit(f"Tagging failed: {error}")

        run_in_pool(work, on_done=done, on_error=failed, pool=self.pool)
