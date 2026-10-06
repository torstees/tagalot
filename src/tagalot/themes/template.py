"""TODO: say what your theme organizes (docs/THEMES.md walks through every part of this file).

A working theme to start from: ``tagalot --new-theme my_theme`` copies it into your themes
folder with its id and name filled in. As it is, it turns each folder directly under a root
into a **Group**, and each file into an **Item** in it; text files also get a headline
(their first line). Change the TODO parts to fit your files, then open a keep made with it.

Rules (DESIGN.md §9): import only ``tagalot.themes.api``; never write, move, or delete
anything under a root; read files in ``prepare``, not ``ingest``.
"""

import posixpath
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tagalot.themes.api import (
    ActionContext,
    DetailView,
    Entity,
    EntityRef,
    IngestContext,
    ResourceInfo,
    SearchView,
    Section,
    SortBy,
    Theme,
    action,
    contains,
    field,
    option,
    role,
    stat,
    top_values,
)

TEXT = frozenset({".txt", ".md"})
"""Files whose first line becomes an item's headline (read in ``prepare``)."""


# --- Entity types: what you browse and tag ---


class Group(Entity):
    """A folder directly under a root, holding items. TODO: rename to what yours are."""

    title_label = "Name"
    roles = [role("folder", kinds={"dir"}, primary=True)]
    contents_sort = (SortBy("title"),)


class Item(Entity):
    """One file. TODO: your fields; each is a column, a filter, and a line on the page."""

    title_label = "Name"
    double_click = "open_file"  # double-click opens the file; Ctrl+Enter its page
    card_lines = ("extension", "headline")
    extension: str = field("Extension", card=True, search="choice", editable=False)
    headline: str | None = field("Headline", search="text")
    size: int | None = field("Size", search="range", editable=False, display="bytes")
    modified: datetime | None = field("Modified", search="range", editable=False)
    roles = [role("file", kinds={"any"}, primary=True, thumbnail=True)]


# --- The theme ---


class TemplateTheme(Theme):
    """TODO: a one-line description, shown nowhere yet but kind to future you."""

    id = "template"  # TODO: a unique lowercase identifier (keeps store it)
    name = "Template"  # TODO: what people see in the new-keep dialog
    version = 1  # raise it when ingest reads new things: keeps read their files once again
    api_version = 6  # the theme API this needs; Tagalots with an older one refuse the theme
    extensions = frozenset()  # TODO: e.g. {".txt", ".md", ".pdf"}; empty = every file
    dirs = True  # folders become resources (needed for Group's folder role)
    entities = [Group, Item]
    containment = [contains(Group, Item)]
    options = [
        option(
            "group_level",
            1,
            label="Group folder level",
            description="Which folder under the root is a group (1 = the first)",
        )
    ]
    views = [
        SearchView("Items", [Item], inherit_tags=True),  # a tag on a group counts for its items
        SearchView("Groups", [Group]),
        SearchView("Recent", [Item], layout="list", default_sort=[SortBy("modified", True)]),
        DetailView(Item, [Section.fields(), Section.role("file")]),
    ]
    dashboard = [
        stat("Total size", Item, "size"),
        top_values("Kinds of file", Item, "extension"),
    ]

    # --- prepare: read files here (a worker; no database) ---

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        """``{resource id: what ingest needs}``; ``ingest`` gets it with ``ctx.prepared``."""
        found: dict[int, Any] = {}
        for resource in batch:
            if resource.kind == "file" and resource.ext in TEXT:
                try:
                    found[resource.id] = {"headline": first_line(resource.path)}
                except OSError as e:  # one bad file never stops a scan
                    found[resource.id] = {"error": str(e)}
        return found

    # --- ingest: files and folders -> items, groups, links (the DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        level = ctx.option("group_level")
        for resource in batch:
            if resource.kind == "dir":
                if resource.relpath.count("/") + 1 == level:  # a group's own folder
                    group = group_ref(resource.relpath, ctx)
                    ctx.link(group, resource, "folder")
                continue
            details = ctx.prepared(resource) or {}
            if details.get("error"):
                ctx.warn(resource, f"Couldn't read it: {details['error']}")
            values = item_values(resource)
            if "headline" in details:
                values["headline"] = details["headline"]
            existing = ctx.entities_of(resource, "file")
            if existing:  # the same file (perhaps moved): update its item
                item = existing[0]
                ctx.update(item, title=posixpath.basename(resource.relpath), **values)
            else:  # a never-reused key, so a new file at a vacated path is a new item
                title = posixpath.basename(resource.relpath)
                item = ctx.upsert(Item, f"item:{uuid.uuid4().hex}", title=title, **values)
                ctx.link(item, resource, "file")
            folder = group_folder(resource.relpath, level)
            if folder is not None:
                ctx.contain(group_ref(folder, ctx), item)

    # --- an action: a command on items' menus and pages ---

    @action("Count", [Item, Group])
    def count(self, items: Sequence[EntityRef], ctx: ActionContext) -> None:
        """Say how many items (and groups' items) are selected, and their total size."""
        found = [i for e in items for i in ([e] if e.type.endswith(".item") else ctx.contents(e))]
        total = sum(ctx.get(i).fields.get("size") or 0 for i in found)
        ctx.message(f"{len(found)} items, {total:,} bytes")


def group_ref(folder: str, ctx: IngestContext) -> EntityRef:
    """The group of a folder, made if new (keyed by its path: one group per folder)."""
    return ctx.upsert(Group, f"group:{folder.casefold()}", title=posixpath.basename(folder))


def group_folder(relpath: str, level: int) -> str | None:
    """The group folder a file is in (``level`` folders deep), if it is deep enough."""
    parts = relpath.split("/")[:-1]
    return "/".join(parts[:level]) if len(parts) >= level else None


def item_values(resource: ResourceInfo) -> dict[str, Any]:
    modified = (
        datetime.fromtimestamp(resource.mtime_ns / 1e9, UTC)
        if resource.mtime_ns is not None
        else None
    )
    return {"extension": resource.ext, "size": resource.size, "modified": modified}


def first_line(path: str, limit: int = 200) -> str | None:
    """A text file's first non-empty line, at most ``limit`` characters."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                return line.strip()[:limit]
    return None
