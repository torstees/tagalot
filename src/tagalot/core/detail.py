"""What an entity's detail page shows (DESIGN.md §12 "Detail page").

:func:`load_detail` reads everything a page needs in one read transaction, in a worker: the
entity's fields, the files in each role, related entities, and how many items it contains,
following the theme's :class:`~tagalot.themes.api.DetailView` for the type, or
:func:`default_detail_view` when the theme declares none.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Connection, func, select, union

from tagalot.core.fields import user_fields
from tagalot.core.ingest import TITLE, merged_into
from tagalot.core.keywords import ItemKeyword, item_keywords
from tagalot.core.models import (
    LAST_POSITION,
    Entity,
    EntityContains,
    EntityResource,
    Resource,
    ResourceStatus,
    Root,
)
from tagalot.core.roots import local_path
from tagalot.core.tags import PATH_SEPARATOR, TagTree
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import (
    DetailView,
    Section,
    entity_fields,
    entity_label,
    entity_plural,
)
from tagalot.themes.api import Entity as ThemeEntity


@dataclass(frozen=True)
class FileRow:
    """A resource shown in a role section."""

    resource_id: int
    root_name: str
    relpath: str
    kind: str
    """``"file"`` or ``"dir"``."""
    size: int | None
    status: ResourceStatus
    path: str | None
    """This machine's path, or ``None`` if its root isn't configured here."""
    role: str | None = None
    """The role it is linked in."""
    skipped: bool = False
    """Scans leave it out now (an exclude pattern); its status is as last seen."""
    by_user: bool = False
    """Linked by hand (#243); it can be unlinked from the item's page."""


@dataclass(frozen=True)
class EntityRow:
    """A related or contained entity, shown as a link to its page."""

    id: int
    type: str
    title: str


@dataclass(frozen=True)
class FieldRow:
    name: str
    label: str
    value: Any
    type: type = str
    """The field's Python type (``str``, ``int``, ``float``, ``bool``, ``date``,
    ``datetime``)."""
    nullable: bool = True
    editable: bool = False
    edited: bool = False
    """The user set this value by hand (provenance ``user``): scans leave it alone."""
    display: str | None = None
    """The field's display format (``"bytes"``, ``"duration"``), if any."""


@dataclass(frozen=True)
class DetailSection:
    """One section of the page, in the theme's order."""

    kind: str
    """``fields``, ``role``, ``gallery``, ``related``, ``contents``, or ``custom``."""
    title: str
    fields: tuple[FieldRow, ...] = ()
    files: tuple[FileRow, ...] = ()
    entities: tuple[EntityRow, ...] = ()
    count: int = 0
    """Contents: how many items the entity directly contains."""
    relationship: str | None = None
    """Related: the relationship's name, for adding and removing by hand (#260)."""
    other_type: str | None = None
    """Related: the type of the items on the other side."""
    side: str | None = None
    """Related: ``"a"`` when this item is the relationship's ``a`` side, ``"b"`` its ``b``
    side, ``None`` for both (a symmetric relationship of a type with itself, #322)."""


@dataclass(frozen=True)
class EntityDetail:
    id: int
    type: str
    type_label: str
    title: str
    sections: tuple[DetailSection, ...] = field(default_factory=tuple)
    title_label: str = "Title"
    title_edited: bool = False
    """The user renamed it by hand."""
    extra: tuple[tuple[str, Any], ...] = ()
    """The user's own fields (``entity.extra``), by name."""
    breadcrumbs: tuple[EntityRow, ...] = ()
    """The containers above it, outermost first (for a song: its artist, then its album)."""
    other_parents: int = 0
    """How many more containers hold it directly, besides the one the crumbs follow."""
    merged_from: int | None = None
    """The id asked for, when that item was merged into this one (§13)."""
    keywords: tuple[ItemKeyword, ...] = ()
    """What its files say it is about, and what became of each (§7 "File keywords")."""
    keyword_tags: tuple[tuple[int, str], ...] = ()
    """``(tag id, path)`` for the tags those keywords match, to name them."""


PATH_MARK = "\u203a"
"""Between a root's name and a path inside it."""

MAX_ROWS = 200
"""Files or entities listed per section; the section says how many more there are."""


def default_detail_view(entity: type[ThemeEntity], is_container: bool) -> DetailView:
    """The page for a type the theme gave no :class:`DetailView`: its fields, its primary
    role's files, and its contents if it can contain anything."""
    sections = [Section.fields()]
    primary = next((r for r in entity.roles if r.primary), None)
    if primary is not None:
        sections.append(Section.role(primary.name))
    if is_container:
        sections.append(Section.contents())
    return DetailView(entity, sections)


def detail_view_for(schema: ThemeSchema, entity: type[ThemeEntity]) -> DetailView:
    theme = schema.theme
    for view in theme.views:
        if isinstance(view, DetailView) and view.type is entity:
            return view
    return default_detail_view(entity, any(c.parent is entity for c in theme.containment))


def load_detail(
    conn: Connection,
    schema: ThemeSchema,
    entity_id: int,
    root_path: Callable[[str], str | None],
) -> EntityDetail | None:
    """The detail page of an entity, or ``None`` if it no longer exists. ``root_path``
    gives this machine's path for a root id (``None`` if unknown)."""
    row = conn.execute(
        select(Entity.type, Entity.title, Entity.extra).where(Entity.id == entity_id)
    ).first()
    if row is None:
        into = merged_into(conn, [entity_id]).get(entity_id)
        found = load_detail(conn, schema, into, root_path) if into is not None else None
        return replace(found, merged_from=entity_id) if found is not None else None
    crumbs, others = breadcrumbs(conn, entity_id)
    extra = tuple(sorted((row.extra or {}).items(), key=lambda item: item[0].casefold()))
    try:
        table = schema.by_type_id(row.type)
    except KeyError:
        return EntityDetail(
            entity_id, row.type, row.type, row.title, breadcrumbs=crumbs, other_parents=others
        )
    entity = table.entity
    edited = user_fields(conn, entity_id)
    sections: list[DetailSection] = []
    for section in detail_view_for(schema, entity).sections:
        if section.kind == "related":  # one section per side it is on
            sections += _related_sections(conn, schema, entity, entity_id, section.name or "")
            continue
        loaded = _load_section(conn, schema, entity, entity_id, section, root_path, edited)
        if loaded is not None:
            sections.append(loaded)
    tree = TagTree.load(conn)
    keywords = tuple(item_keywords(conn, tree, entity_id))
    named = {k.tag_id for k in keywords if k.tag_id is not None and k.tag_id in tree}
    return EntityDetail(
        entity_id,
        row.type,
        entity_label(entity),
        row.title,
        tuple(sections),
        breadcrumbs=crumbs,
        other_parents=others,
        title_label=entity.title_label,
        title_edited=TITLE in edited,
        extra=extra,
        keywords=keywords,
        keyword_tags=tuple((t, PATH_SEPARATOR.join(tree.path(t))) for t in sorted(named)),
    )


MAX_DEPTH = 64
"""Breadcrumbs stop here (containment is shallow; this only guards against bad data)."""


def breadcrumbs(conn: Connection, entity_id: int) -> tuple[tuple[EntityRow, ...], int]:
    """The chain of containers above an entity, outermost first, and how many other
    direct containers it has. Where an entity has several containers, the chain follows the
    first by title, so the crumbs are stable."""
    chain: list[EntityRow] = []
    seen = {entity_id}
    others = 0
    current = entity_id
    for depth in range(MAX_DEPTH):
        parents = conn.execute(
            select(Entity.id, Entity.type, Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == current)
            .order_by(Entity.title, Entity.id)
        ).all()
        if not parents:
            break
        if depth == 0:
            others = len(parents) - 1
        parent = EntityRow(*parents[0])
        if parent.id in seen:
            break  # a cycle: bad data, but no endless loop
        seen.add(parent.id)
        chain.append(parent)
        current = parent.id
    return tuple(reversed(chain)), others


def _load_section(
    conn: Connection,
    schema: ThemeSchema,
    entity: type[ThemeEntity],
    entity_id: int,
    section: Section,
    root_path: Callable[[str], str | None],
    edited: set[str],
) -> DetailSection | None:
    match section.kind:
        case "fields":
            fields = _fields(conn, schema, entity, entity_id, edited)
            return DetailSection("fields", "Details", fields=fields)
        case "role" | "gallery":
            role = next((r for r in entity.roles if r.name == section.name), None)
            if role is None:
                return None
            title = role.label or (section.name or "").replace("_", " ").capitalize()
            files = role_files(conn, entity_id, role.name, root_path)
            return DetailSection(section.kind, title, files=files)
        case "contents":
            count = (
                conn.scalar(select(func.count()).where(EntityContains.parent_id == entity_id)) or 0
            )
            return DetailSection("contents", "Contents", count=int(count))
        case _:
            return DetailSection(section.kind, "")


def _fields(
    conn: Connection,
    schema: ThemeSchema,
    entity: type[ThemeEntity],
    entity_id: int,
    edited: set[str],
) -> tuple[FieldRow, ...]:
    table = schema.entities[entity].table
    shown = [f for f in entity_fields(entity) if f.spec.detail]
    if not shown:
        return ()
    values = conn.execute(
        select(*(table.c[f.name] for f in shown)).where(table.c.id == entity_id)
    ).first()
    return tuple(
        FieldRow(
            f.name,
            f.spec.label,
            values[i] if values is not None else None,
            f.type,
            f.nullable,
            f.spec.editable,
            f.name in edited,
            f.spec.display,
        )
        for i, f in enumerate(shown)
    )


def role_files(
    conn: Connection,
    entity_id: int,
    role: str | None,
    root_path: Callable[[str], str | None],
    *,
    resource_id: int | None = None,
) -> tuple[FileRow, ...]:
    """The entity's files in ``role`` (every role if ``None``), in their sort order; with
    ``resource_id``, only that one of them."""
    query = (
        select(
            Resource.id,
            Resource.root_id,
            Root.name,
            Resource.relpath,
            Resource.kind,
            Resource.size,
            Resource.status,
            EntityResource.role,
            Resource.skipped,
            EntityResource.by_user,
        )
        .join(EntityResource, EntityResource.resource_id == Resource.id)
        .join(Root, Root.id == Resource.root_id)
        .where(EntityResource.entity_id == entity_id)
        .order_by(EntityResource.sort_order, Resource.relpath)
        .limit(MAX_ROWS)
    )
    if role is not None:
        query = query.where(EntityResource.role == role)
    if resource_id is not None:
        query = query.where(Resource.id == resource_id)
    found = []
    for row in conn.execute(query):
        base = root_path(row.root_id)
        found.append(
            FileRow(
                row.id,
                row.name,
                row.relpath,
                row.kind.value,
                row.size,
                row.status,
                local_path(base, row.relpath) if base is not None else None,
                row.role,
                row.skipped,
                row.by_user,
            )
        )
    return tuple(found)


def _related_sections(
    conn: Connection,
    schema: ThemeSchema,
    entity: type[ThemeEntity],
    entity_id: int,
    name: str,
) -> list[DetailSection]:
    """The related sections of one relationship: the side the item is on; both sides,
    separately, for a relationship of a type with itself (Cites, Cited by); or one section
    of both sides for a symmetric one (Related)."""
    link = schema.relationships.get(name)
    if link is None:
        return []
    rel = link.relationship
    if rel.a is entity and rel.b is entity:
        if rel.symmetric:
            return [_related(conn, schema, link, entity_id, None)]
        return [
            _related(conn, schema, link, entity_id, "a"),
            _related(conn, schema, link, entity_id, "b"),
        ]
    if rel.a is entity:
        return [_related(conn, schema, link, entity_id, "a")]
    if rel.b is entity:
        return [_related(conn, schema, link, entity_id, "b")]
    return []


def _related(
    conn: Connection, schema: ThemeSchema, link: Any, entity_id: int, side: str | None
) -> DetailSection:
    rel = link.relationship
    t = link.table
    if side == "a":
        title, default = rel.label, entity_plural(rel.b)
        other_type = schema.theme.type_id_of(rel.b)
        others: Any = select(t.c.b_id.label("other")).where(t.c.a_id == entity_id)
    elif side == "b":
        title, default = rel.reverse_label, entity_plural(rel.a)
        other_type = schema.theme.type_id_of(rel.a)
        others = select(t.c.a_id.label("other")).where(t.c.b_id == entity_id)
    else:  # symmetric: either way
        title, default = rel.label, entity_plural(rel.a)
        other_type = schema.theme.type_id_of(rel.a)
        others = union(
            select(t.c.b_id.label("other")).where(t.c.a_id == entity_id),
            select(t.c.a_id.label("other")).where(t.c.b_id == entity_id),
        )
    order: list[Any] = [Entity.title, Entity.id]
    query = select(Entity.id, Entity.type, Entity.title).where(Entity.id.in_(others))
    if rel.ordered and side == "b":  # a paper's authors, in their order (#317)
        query = query.join(t, (t.c.a_id == Entity.id) & (t.c.b_id == entity_id))
        order.insert(0, func.coalesce(t.c.position, LAST_POSITION))
    rows = conn.execute(query.order_by(*order).limit(MAX_ROWS)).all()
    return DetailSection(
        "related",
        title or default,
        entities=tuple(EntityRow(*r) for r in rows),
        relationship=rel.name,
        other_type=other_type,
        side=side,
    )


# --- the preview strip (#90) ---


@dataclass(frozen=True)
class Preview:
    """What the preview strip shows for one selected entity."""

    id: int
    type_label: str
    title: str
    facts: tuple[FieldRow, ...]
    """Its card fields, in declaration order."""
    file: str | None
    """Its primary file: the root's name, then the path inside it."""


def load_preview(conn: Connection, schema: ThemeSchema, entity_id: int) -> Preview | None:
    """The preview of one entity, or ``None`` if it no longer exists."""
    row = conn.execute(select(Entity.type, Entity.title).where(Entity.id == entity_id)).first()
    if row is None:
        return None
    try:
        table = schema.by_type_id(row.type)
    except KeyError:
        return Preview(entity_id, row.type, row.title, (), None)
    entity = table.entity
    shown = [f for f in entity_fields(entity) if f.spec.card]
    facts: tuple[FieldRow, ...] = ()
    if shown:
        values = conn.execute(
            select(*(table.table.c[f.name] for f in shown)).where(table.table.c.id == entity_id)
        ).first()
        if values is not None:
            facts = tuple(
                FieldRow(f.name, f.spec.label, values[i], display=f.spec.display)
                for i, f in enumerate(shown)
            )
    primary = next((r for r in entity.roles if r.primary), None)
    file = None
    if primary is not None:
        found = conn.execute(
            select(Root.name, Resource.relpath)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .join(Root, Root.id == Resource.root_id)
            .where(EntityResource.entity_id == entity_id, EntityResource.role == primary.name)
            .order_by(EntityResource.sort_order, Resource.relpath)
            .limit(1)
        ).first()
        if found is not None:
            file = f"{found.name} {PATH_MARK} {found.relpath}" if found.relpath else found.name
    return Preview(entity_id, entity_label(entity), row.title, facts, file)
