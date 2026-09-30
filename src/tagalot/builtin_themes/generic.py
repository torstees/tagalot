"""Generic theme: one entity per file (DESIGN.md §9 "Built-in themes").

The reference implementation and the fallback: any folder of files becomes a searchable,
taggable collection. Each file is a ``File`` entity titled with its file name.

Identity is "the entity linked to this file", not the file's path: when a file is moved or
renamed, move detection carries its links to the new path, so its tags and edits follow it.
"""

import posixpath
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from tagalot.themes.api import (
    DetailView,
    Entity,
    IngestContext,
    ResourceInfo,
    SearchView,
    Section,
    SortBy,
    Theme,
    field,
    role,
)


class File(Entity):
    """Any file under a root. Its fields come from the file, so only its name can be edited
    by hand (which never renames the file)."""

    title_label = "Name"
    double_click = "open_file"
    extension: str = field("Extension", card=True, search="choice", editable=False)
    folder: str = field("Folder", card=True, search="text", editable=False)
    size: int | None = field("Size", card=True, search="range", editable=False, display="bytes")
    modified: datetime | None = field("Modified", card=True, search="range", editable=False)
    roles = [role("file", kinds={"any"}, primary=True, thumbnail=True)]


class GenericTheme(Theme):
    id, name, version = "generic", "Files", 1
    entities = [File]
    views = [
        SearchView("Files", [File], layout="list"),
        SearchView(
            "Recently modified",
            [File],
            layout="list",
            default_sort=[SortBy("modified", descending=True)],
        ),
        DetailView(File, [Section.fields(), Section.role("file")]),
    ]

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            if resource.kind != "file":
                continue
            title, values = file_values(resource)
            existing = ctx.entities_of(resource, "file")
            if existing:
                ctx.update(existing[0], title=title, **values)
            else:
                # The key is never reused: a new file at a path another file moved away from
                # must not take over that file's entity.
                entity = ctx.upsert(File, f"file:{uuid.uuid4().hex}", title=title, **values)
                ctx.link(entity, resource, "file")


def file_values(resource: ResourceInfo) -> tuple[str, dict[str, Any]]:
    """The title and the fields extracted for a file."""
    folder, name = posixpath.split(resource.relpath)
    modified = (
        datetime.fromtimestamp(resource.mtime_ns / 1e9, UTC)
        if resource.mtime_ns is not None
        else None
    )
    return name, {
        "extension": resource.ext,
        "folder": folder,
        "size": resource.size,
        "modified": modified,
    }
