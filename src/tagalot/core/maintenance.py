"""Repair actions for derived data (the keep configuration window's "Repair", M14).

Both the containment closure and the text-search index are derived from other tables, so
they can always be rebuilt without losing anything the user entered.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import Connection

from tagalot.core import closure
from tagalot.core.fts import TextSource, index_is_consistent, no_text_fields, rebuild_search_index

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairReport:
    closure_was_ok: bool
    """Whether the closure matched the edges before the repair."""
    closure_rows: int
    search_index_was_ok: bool
    search_rows: int

    @property
    def found_problems(self) -> bool:
        return not (self.closure_was_ok and self.search_index_was_ok)


def repair_indexes(conn: Connection, text_source: TextSource = no_text_fields) -> RepairReport:
    """Check, then rebuild, the containment closure and the search index. Run in the DB
    writer; everything happens in the caller's single transaction."""
    closure_ok = closure.verify(conn).ok
    search_ok = index_is_consistent(conn)
    report = RepairReport(
        closure_was_ok=closure_ok,
        closure_rows=closure.rebuild_all(conn),
        search_index_was_ok=search_ok,
        search_rows=rebuild_search_index(conn, text_source),
    )
    if report.found_problems:
        logger.warning("Repaired derived data: %s", report)
    return report
