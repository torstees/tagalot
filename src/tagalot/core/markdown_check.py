"""Problems in Markdown files' front matter (#438, DESIGN.md §9 *Notes*, §12 *Triage*).

Every Markdown file a scan reads (a book, a literature note, an item's note, a note in the
notes folder) is checked here too, whatever the theme makes of it: a key given twice (or
twice in different case, which Tagalot reads as one key), or front matter that doesn't
parse (``themes.readers.front_matter_problems``). Keys given twice go to the Activity panel
as the scan's warnings (front matter that doesn't parse is reported there already, by
whatever reads the file); all are kept (``markdown_problem``), replaced each time the file
is read, for Triage → Markdown. A file that can't be read is the scan's error, not this.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select

from tagalot.core.models import MarkdownProblem, Resource, ResourceStatus, Root
from tagalot.core.notes import NOTE_EXTENSIONS
from tagalot.themes.readers import MAX_FRONT_MATTER, front_matter_problems


def doesnt_parse(problem: str) -> bool:
    """A problem that the front matter doesn't parse: whatever reads the file (a theme, the
    notes) reports that already, so a scan's warnings leave it out; Triage lists it."""
    return problem.startswith(("The YAML front matter doesn't", "The TOML front matter doesn't"))


def is_markdown(ext: str) -> bool:
    return ext.lower() in NOTE_EXTENSIONS


def check_markdown(path: str) -> tuple[str, ...]:
    """The problems of the front matter of the Markdown file at ``path`` (none for one
    that can't be read: the scan reports that)."""
    try:
        with open(path, "rb") as file:
            raw = file.read(MAX_FRONT_MATTER)
    except OSError:
        return ()
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    return tuple(front_matter_problems(text))


def record_markdown_problems(conn: Connection, found: Mapping[int, Iterable[str]]) -> None:
    """Replace what these files' front matter had wrong with what was ``found``."""
    ids = list(found)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        conn.execute(delete(MarkdownProblem).where(MarkdownProblem.resource_id.in_(chunk)))
    rows = [{"resource_id": rid, "problems": list(p)} for rid, p in found.items() if p]
    if rows:
        conn.execute(insert(MarkdownProblem), rows)


@dataclass(frozen=True)
class MarkdownFileProblem:
    """A Markdown file whose front matter has problems, as Triage lists it."""

    resource_id: int
    root_id: str
    relpath: str
    status: ResourceStatus
    problems: tuple[str, ...]

    @property
    def reason(self) -> str:
        return " ".join(self.problems)


def markdown_problems(conn: Connection) -> list[MarkdownFileProblem]:
    """Markdown files with front-matter problems, by watched folder and path (not missing
    ones)."""
    query = (
        select(
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            Resource.status,
            MarkdownProblem.problems,
        )
        .join(Resource, Resource.id == MarkdownProblem.resource_id)
        .join(Root, Root.id == Resource.root_id)
        .where(Resource.status != ResourceStatus.MISSING)
        .order_by(Root.name, Resource.relpath)
    )
    return [
        MarkdownFileProblem(rid, root, rel, status, tuple(problems or ()))
        for rid, root, rel, status, problems in conn.execute(query)
    ]
