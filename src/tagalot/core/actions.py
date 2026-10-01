"""Running theme actions (DESIGN.md §9 "Actions").

:func:`actions_for` lists the actions that apply to a type. :func:`run_action` runs one in
the DB writer's transaction with an :class:`ActionSession` (the ingest context plus file
lookups and outputs), recording every entity it touches so the run is one undo step
(:class:`ActionChange`, restored by :func:`restore_action`). Outputs (open a file, reveal
it, a message) are collected and returned, for the UI to carry out once the changes are
saved.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import Connection, select

from tagalot.core.entity_state import ChangeRecorder, EntitySnapshot, restore_states
from tagalot.core.ingest import IngestSession
from tagalot.core.models import Entity, EntityResource, Resource, ResourceStatus
from tagalot.core.roots import local_path
from tagalot.core.search_fields import contents_order
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import ActionSpec, EntityRef, ResourceInfo, Theme

logger = logging.getLogger(__name__)

OutputKind = Literal["open", "reveal", "message"]


@dataclass(frozen=True)
class ThemeAction:
    method: str
    """The theme method's name (stable; used to run it)."""
    label: str


@dataclass(frozen=True)
class ActionChange:
    """Everything needed to undo and redo one run of an action."""

    label: str
    before: dict[int, EntitySnapshot | None]
    after: dict[int, EntitySnapshot | None]


@dataclass
class ActionResult:
    change: ActionChange
    outputs: list[tuple[OutputKind, str]] = field(default_factory=list)
    """What to do now, in order: ``("open", path)``, ``("reveal", path)``, ``("message", text)``."""

    @property
    def changed(self) -> bool:
        return self.change.before != self.change.after

    def __bool__(self) -> bool:
        """True when the action changed something (the UI then refreshes)."""
        return self.changed

    @property
    def text(self) -> str:
        """For the status bar: the action's last message, else that it was done."""
        messages = [value for kind, value in self.outputs if kind == "message"]
        return messages[-1] if messages else f"{self.change.label}: done."


class ActionError(Exception):
    """An action couldn't run, or raised; the message is for the status bar."""


def actions_for(schema: ThemeSchema, type_id: str) -> list[ThemeAction]:
    """The theme's actions that apply to entities of ``type_id``, in declaration order."""
    try:
        entity = schema.by_type_id(type_id).entity
    except KeyError:
        return []
    roles = {r.name for r in entity.roles}
    return [
        ThemeAction(method, spec.label)
        for method, spec in schema.theme.actions().items()
        if _applies(spec, entity, roles)
    ]


def _applies(spec: ActionSpec, entity: type, roles: set[str]) -> bool:
    return any(t is entity if not isinstance(t, str) else t in roles for t in spec.applies_to)


class ActionSession(IngestSession):
    """``ctx`` for an action: the ingest context, plus file lookups and outputs."""

    def __init__(
        self,
        conn: Connection,
        schema: ThemeSchema,
        root_path: Callable[[str], str | None],
        temp_dir: Callable[[], Path],
    ) -> None:
        super().__init__(conn, schema)
        self._root_path = root_path
        self._temp_dir = temp_dir
        self.outputs: list[tuple[OutputKind, str]] = []

    def contents(self, entity: EntityRef) -> list[EntityRef]:
        children = super().contents(entity)
        if len(children) < 2:
            return children
        sort, fields = contents_order(self.schema, entity.type)
        keys = [fields[k.field].desc() if k.descending else fields[k.field] for k in sort]
        ordered = self.conn.scalars(
            select(Entity.id)
            .where(Entity.id.in_([c.id for c in children]))
            .order_by(*keys, Entity.id)
        ).all()
        by_id = {c.id: c for c in children}
        return [by_id[i] for i in ordered]

    def resources(self, entity: EntityRef, role: str | None = None) -> list[ResourceInfo]:
        query = (
            select(
                Resource.id,
                Resource.root_id,
                Resource.relpath,
                Resource.kind,
                Resource.ext,
                Resource.size,
                Resource.mtime_ns,
            )
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(EntityResource.entity_id == entity.id, Resource.status == ResourceStatus.OK)
            .order_by(EntityResource.sort_order, Resource.relpath, Resource.id)
        )
        if role is not None:
            query = query.where(EntityResource.role == role)
        found = []
        for rid, root_id, relpath, kind, ext, size, mtime in self.conn.execute(query):
            base = self._root_path(root_id)
            if base is not None:
                path = local_path(base, relpath)
                found.append(
                    ResourceInfo(rid, root_id, relpath, kind.value, ext, size, mtime, path)
                )
        return found

    def temp_path(self, name: str) -> str:
        safe = Path(name).name  # no folders: it stays in Tagalot's temp folder
        if not safe or safe in (".", ".."):
            raise ValueError(f"not a file name: {name!r}")
        folder = self._temp_dir()
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / safe)

    def open(self, path: str) -> None:
        self.outputs.append(("open", str(path)))

    def reveal(self, path: str) -> None:
        self.outputs.append(("reveal", str(path)))

    def message(self, text: str) -> None:
        self.outputs.append(("message", str(text)))


def run_action(
    conn: Connection,
    schema: ThemeSchema,
    theme: Theme,
    method: str,
    entity_ids: Sequence[int],
    *,
    root_path: Callable[[str], str | None],
    temp_dir: Callable[[], Path],
) -> ActionResult:
    """Run the action ``method`` on the entities it applies to among ``entity_ids``, in
    this transaction. Raises :class:`ActionError` (after logging) if the action fails, so
    the caller's transaction rolls back."""
    spec = type(theme).actions().get(method)
    if spec is None:
        raise ActionError(f"The {schema.theme.name} theme has no action {method!r}.")
    types = dict(
        conn.execute(select(Entity.id, Entity.type).where(Entity.id.in_(entity_ids))).all()
    )
    applies = {
        t for t in set(types.values()) if any(a.method == method for a in actions_for(schema, t))
    }
    entities = [EntityRef(i, types[i]) for i in entity_ids if types.get(i) in applies]
    if not entities:
        raise ActionError(f"{spec.label} doesn't apply to these items.")
    ctx = ActionSession(conn, schema, root_path, temp_dir)
    recorder = ChangeRecorder(conn, schema)
    ctx.recorder = recorder
    try:
        getattr(theme, method)(entities, ctx)
        ctx.flush()
    except Exception as e:
        logger.exception("Action %s failed", method)
        raise ActionError(f"{spec.label} failed: {e}") from e
    change = ActionChange(spec.label, recorder.before, recorder.after())
    return ActionResult(change, ctx.outputs)


def restore_action(
    conn: Connection, schema: ThemeSchema, change: ActionChange, *, forward: bool
) -> None:
    """Put the entities an action touched as they were before it (undo) or after (redo)."""
    restore_states(conn, schema, change.after if forward else change.before)
