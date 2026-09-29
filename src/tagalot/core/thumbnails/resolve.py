"""Resolving an entity's thumbnail with its theme's provider chain (DESIGN.md §10).

:class:`ThumbnailResolver` runs in workers. For each entity it first tries the memoized
source (``entity.thumb_resource_id``); otherwise it walks the chain, trying each candidate
resource until one makes a picture, and remembers the winner through the DB writer. Pictures
come from ``thumbs.db`` when cached, and files are only read for resources that are online.
"""

import contextlib
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine, Row, select, update

from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.roots import local_path
from tagalot.core.thumbnails.cache import ThumbCache, thumb_key
from tagalot.core.thumbnails.image import Thumbnail, make_thumbnail
from tagalot.core.thumbnails.render import renderer_for
from tagalot.core.writer import DbWriter, WriterClosedError
from tagalot.themes.api import (
    Entity as ThemeEntity,
)
from tagalot.themes.api import (
    EntityRef,
    Icon,
    ResourceInfo,
    Theme,
    ThumbnailProvider,
)

logger = logging.getLogger(__name__)

DEFAULT_THUMB_SIZE = 256
"""Thumbnail size in pixels (the longer side) unless the keep sets another."""

_RESOURCE_COLUMNS = (
    Resource.id,
    Resource.root_id,
    Resource.relpath,
    Resource.kind,
    Resource.ext,
    Resource.size,
    Resource.mtime_ns,
    Resource.status,
)

FALLBACK_ICON = "entity"
"""The icon shown when a chain ends without an :class:`Icon`."""


@dataclass(frozen=True)
class ThumbnailResult:
    """What to show for an entity: a picture, or else an icon name."""

    entity_id: int
    thumbnail: Thumbnail | None
    icon: str | None
    resource_id: int | None
    """The resource the picture came from."""
    problems: tuple[tuple[str, str], ...] = ()
    """(path, message) for files that couldn't be read, for the activity panel."""


class ThumbnailResolver:
    """Resolves thumbnails for one open keep. Safe to use from several worker threads."""

    def __init__(
        self,
        reader: Engine,
        writer: DbWriter,
        theme: type[Theme],
        cache: ThumbCache,
        root_path: Callable[[str], str],
        size: int = DEFAULT_THUMB_SIZE,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.cache = cache
        self.root_path = root_path
        self.size = size
        self._theme = theme()
        self._types = {theme.type_id_of(e): e for e in theme.entities}
        self._chains: dict[str, list[ThumbnailProvider]] = {}
        self._failed: set[str] = set()
        """Cache keys that failed to render this session: the same file isn't retried
        until it changes (which changes its key)."""
        self._lock = threading.Lock()

    def resolve(self, entity_id: int) -> ThumbnailResult:
        """The thumbnail of an entity, making and caching it if needed. Does I/O: call it
        from a worker."""
        with self.reader.connect() as conn:
            return _Resolution(self, conn).resolve(entity_id)

    # --- used by _Resolution ---

    def chain(self, type_id: str) -> list[ThumbnailProvider]:
        with self._lock:
            if type_id not in self._chains:
                entity = self._types.get(type_id)
                chain = list(self._theme.thumbnail_chain(entity)) if entity else []
                self._chains[type_id] = chain
            return self._chains[type_id]

    def entity_type(self, type_id: str) -> type[ThemeEntity] | None:
        return self._types.get(type_id)

    def failed(self, key: str) -> bool:
        with self._lock:
            return key in self._failed

    def mark_failed(self, key: str) -> None:
        with self._lock:
            self._failed.add(key)

    def remember(self, entity_id: int, resource_id: int | None) -> None:
        """Store the chain's result in ``entity.thumb_resource_id`` (queued, not awaited)."""

        def work(conn: Connection) -> None:
            conn.execute(
                update(Entity).where(Entity.id == entity_id).values(thumb_resource_id=resource_id)
            )

        # If the keep is closing, the chain simply runs again next time.
        with contextlib.suppress(WriterClosedError):
            self.writer.submit(work)


@dataclass
class _Resolution:
    """One call of :meth:`ThumbnailResolver.resolve`, and the :class:`ThumbnailContext`
    its providers see. Entities it resolves on the way (parents) are resolved once."""

    owner: ThumbnailResolver
    conn: Connection
    _status: dict[int, ResourceStatus] = field(default_factory=dict)
    _results: dict[int, ThumbnailResult] = field(default_factory=dict)
    _resolving: set[int] = field(default_factory=set)
    _problems: list[tuple[str, str]] = field(default_factory=list)

    def resolve(self, entity_id: int) -> ThumbnailResult:
        if entity_id in self._results:
            return self._results[entity_id]
        self._resolving.add(entity_id)
        try:
            result = self._resolve(entity_id)
        finally:
            self._resolving.discard(entity_id)
        self._results[entity_id] = result
        return result

    def _resolve(self, entity_id: int) -> ThumbnailResult:
        row = self.conn.execute(
            select(Entity.type, Entity.thumb_resource_id).where(Entity.id == entity_id)
        ).first()
        if row is None:
            return ThumbnailResult(entity_id, None, FALLBACK_ICON, None)
        entity = EntityRef(entity_id, row.type)
        memo: int | None = row.thumb_resource_id
        if memo is not None:
            resource = self._resource(memo)
            if resource is not None:
                picture = self._picture(resource)
                if picture is not None:
                    return self._result(entity, picture, None, resource.id)
                if self._status.get(memo) == ResourceStatus.OFFLINE:
                    # Offline isn't gone: keep the memo and show the icon for now.
                    return self._result(entity, None, self._icon(entity), None)
        picture, icon, source = self._run_chain(entity)
        if source != memo:
            self.owner.remember(entity_id, source)
        return self._result(entity, picture, icon, source)

    def _run_chain(self, entity: EntityRef) -> tuple[Thumbnail | None, str | None, int | None]:
        tried: set[int] = set()
        for provider in self.owner.chain(entity.type):
            if isinstance(provider, Icon):
                return None, provider.name, None
            try:
                for candidate in provider.candidates(entity, self):
                    if candidate.id in tried:
                        continue
                    tried.add(candidate.id)
                    picture = self._picture(candidate)
                    if picture is not None:
                        return picture, None, candidate.id
            except Exception as e:  # a theme's provider must not break thumbnails
                logger.warning("Thumbnail provider %r failed for entity %d", provider, entity.id)
                self._problems.append((f"entity {entity.id}", f"{provider!r} failed: {e}"))
        return None, FALLBACK_ICON, None

    def _icon(self, entity: EntityRef) -> str:
        icons = [p for p in self.owner.chain(entity.type) if isinstance(p, Icon)]
        return icons[0].name if icons else FALLBACK_ICON

    def _result(
        self, entity: EntityRef, picture: Thumbnail | None, icon: str | None, source: int | None
    ) -> ThumbnailResult:
        return ThumbnailResult(entity.id, picture, icon, source, tuple(self._problems))

    # --- pictures ---

    def _picture(self, resource: ResourceInfo) -> Thumbnail | None:
        """The resource's thumbnail: from the cache, else read from the file when it's
        online. ``None`` when it has no picture or can't be read."""
        renderer = renderer_for(resource)
        if renderer is None:
            return None
        size = self.owner.size
        key = thumb_key(
            resource.id, resource.size, resource.mtime_ns, renderer.id, renderer.version, size
        )
        cached = self.owner.cache.get(key)
        if cached is not None:
            return cached
        if self._status.get(resource.id) != ResourceStatus.OK or self.owner.failed(key):
            return None
        try:
            image = renderer.load(resource.path, size)
            picture = None if image is None else make_thumbnail(image, size)
        except Exception as e:  # unreadable, corrupt, or unsupported: try the next one
            logger.warning("Can't make a thumbnail of %s: %s", resource.path, e)
            self._problems.append((resource.path, f"{type(e).__name__}: {e}"))
            picture = None
        if picture is None:
            self.owner.mark_failed(key)
            return None
        self.owner.cache.put(key, size, picture)
        return picture

    # --- ThumbnailContext ---

    def resources(self, entity: EntityRef, role: str | None = None) -> list[ResourceInfo]:
        query = (
            select(*_RESOURCE_COLUMNS)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(
                EntityResource.entity_id == entity.id,
                Resource.status != ResourceStatus.MISSING,
            )
            .order_by(EntityResource.sort_order, Resource.relpath, Resource.id)
        )
        if role is not None:
            query = query.where(EntityResource.role == role)
        rows = self.conn.execute(query).all()
        return [info for info in map(self._info, rows) if info is not None]

    def primary_role(self, entity: EntityRef) -> str | None:
        declared = self.owner.entity_type(entity.type)
        if declared is None:
            return None
        return next((r.name for r in declared.roles if r.primary), None)

    def parents(self, entity: EntityRef) -> list[EntityRef]:
        rows = self.conn.execute(
            select(Entity.id, Entity.type)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity.id)
            .order_by(Entity.id)
        ).all()
        return [EntityRef(row.id, row.type) for row in rows]

    def children(self, entity: EntityRef) -> list[EntityRef]:
        rows = self.conn.execute(
            select(Entity.id, Entity.type)
            .join(EntityContains, EntityContains.child_id == Entity.id)
            .where(EntityContains.parent_id == entity.id)
            .order_by(Entity.title, Entity.id)
        ).all()
        return [EntityRef(row.id, row.type) for row in rows]

    def folder_files(
        self, entity: EntityRef, extensions: Iterable[str] | None = None
    ) -> list[ResourceInfo]:
        primary = self.primary_role(entity)
        own = self.resources(entity, primary) if primary else []
        if not own:
            return []
        first = own[0]
        folder = first.relpath if first.kind == "dir" else first.relpath.rpartition("/")[0]
        prefix = f"{folder}/" if folder else ""
        query = select(*_RESOURCE_COLUMNS).where(
            Resource.root_id == first.root_id,
            Resource.kind == ResourceKind.FILE,
            Resource.status != ResourceStatus.MISSING,
            Resource.parent_resource_id.is_(None),
        )
        if prefix:
            query = query.where(Resource.relpath.startswith(prefix, autoescape=True))
        if extensions is not None:
            query = query.where(Resource.ext.in_(sorted(extensions)))
        rows = self.conn.execute(query.order_by(Resource.relpath)).all()
        inside = [row for row in rows if "/" not in row.relpath[len(prefix) :]]
        return [info for info in map(self._info, inside) if info is not None]

    def thumbnail_of(self, entity: EntityRef) -> ResourceInfo | None:
        if entity.id in self._resolving:  # a containment cycle: don't loop
            return None
        source = self.resolve(entity.id).resource_id
        return None if source is None else self._resource(source)

    # --- resources ---

    def _resource(self, resource_id: int) -> ResourceInfo | None:
        row = self.conn.execute(
            select(*_RESOURCE_COLUMNS).where(Resource.id == resource_id)
        ).first()
        return None if row is None else self._info(row)

    def _info(self, row: Row[*tuple[Any, ...]]) -> ResourceInfo | None:
        try:
            root = self.owner.root_path(row.root_id)
        except (KeyError, StopIteration):  # a root no longer in keep.toml
            return None
        self._status[row.id] = row.status
        return ResourceInfo(
            row.id,
            row.root_id,
            row.relpath,
            row.kind.value,
            row.ext,
            row.size,
            row.mtime_ns,
            local_path(root, row.relpath),
        )
