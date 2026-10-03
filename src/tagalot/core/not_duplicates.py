"""\"Not a duplicate\": hiding identical-file groups and similar pairs from Dedupe
(DESIGN.md §12 "Dedupe", §13; #121).

A dismissal is stored in ``dedupe_dismissal`` by kind and key:

- **identical files** (``exact``): keyed by the group's fingerprint, with the group's
  files as its marker. The group shows again once its files differ (a third copy, one
  deleted), since the user only judged the copies there were.
- **similar items** (``similar``): keyed by the pair's two item ids. A pair stays hidden;
  an item merged away takes its pairs with it.

Dismissing and restoring are each one undo step (:class:`NotDuplicateChange`).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select, tuple_

from tagalot.core.dedupe import DuplicateGroup, NearPair
from tagalot.core.models import DedupeDismissal

EXACT = "exact"
SIMILAR = "similar"

Entry = tuple[str, str, str]
"""(kind, key, marker)."""


def group_entry(group: DuplicateGroup) -> Entry:
    """How an identical-files group is dismissed: by fingerprint, marked by its files."""
    files = ",".join(str(i) for i in sorted(f.resource_id for f in group.files))
    return EXACT, group.fingerprint.hex(), files


def pair_entry(pair: NearPair) -> Entry:
    """How a similar pair is dismissed: by its two items, in either order."""
    low, high = sorted((pair.a[0], pair.b[0]))
    return SIMILAR, f"{low}:{high}", ""


@dataclass(frozen=True)
class Dismissals:
    """What the user marked as not a duplicate."""

    rows: frozenset[Entry] = frozenset()

    def hides(self, entry: Entry) -> bool:
        """The group or pair is dismissed, and (for a group) still has the same files."""
        return entry in self.rows


def load_dismissals(conn: Connection) -> Dismissals:
    rows = conn.execute(
        select(DedupeDismissal.kind, DedupeDismissal.key, DedupeDismissal.marker)
    ).all()
    return Dismissals(frozenset((k, key, m) for k, key, m in rows))


@dataclass(frozen=True)
class NotDuplicateChange:
    """Everything needed to undo and redo marking (or unmarking) as not a duplicate."""

    label: str
    before: frozenset[Entry]
    """Rows for the touched keys before."""
    after: frozenset[Entry]


def set_not_duplicate(
    conn: Connection, entries: Sequence[Entry], *, dismissed: bool = True
) -> NotDuplicateChange:
    """Mark these groups or pairs as not duplicates (hidden), or restore them."""
    keys = list(dict.fromkeys((kind, key) for kind, key, _ in entries))
    before = _rows(conn, keys)
    _delete(conn, keys)
    if dismissed:
        unique = {(kind, key): marker for kind, key, marker in entries}
        conn.execute(
            insert(DedupeDismissal),
            [{"kind": k, "key": key, "marker": m} for (k, key), m in unique.items()],
        )
    n = len(keys)
    groups = sum(k == EXACT for k, _ in keys)
    what = (
        ("group" if n == 1 else "groups")
        if groups == n
        else ("pair" if n == 1 else "pairs")
        if groups == 0
        else "entries"
    )
    count = f"{n} {what}" if n != 1 else f"1 {what}"
    label = (
        f"Mark {count} as not a duplicate" if dismissed else f"Show {count} as a duplicate again"
    )
    return NotDuplicateChange(label, before, _rows(conn, keys))


def restore_not_duplicates(conn: Connection, change: NotDuplicateChange, *, forward: bool) -> None:
    """Undo (``forward=False``) or redo a change."""
    keys = list({(k, key) for k, key, _ in change.before | change.after})
    _delete(conn, keys)
    rows = change.after if forward else change.before
    if rows:
        conn.execute(
            insert(DedupeDismissal),
            [{"kind": k, "key": key, "marker": m} for k, key, m in rows],
        )


def _rows(conn: Connection, keys: Iterable[tuple[str, str]]) -> frozenset[Entry]:
    wanted = list(keys)
    if not wanted:
        return frozenset()
    rows = conn.execute(
        select(DedupeDismissal.kind, DedupeDismissal.key, DedupeDismissal.marker).where(
            tuple_(DedupeDismissal.kind, DedupeDismissal.key).in_(wanted)
        )
    )
    return frozenset((k, key, m) for k, key, m in rows)


def _delete(conn: Connection, keys: list[tuple[str, str]]) -> None:
    if keys:
        conn.execute(
            delete(DedupeDismissal).where(
                tuple_(DedupeDismissal.kind, DedupeDismissal.key).in_(keys)
            )
        )
