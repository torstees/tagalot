"""Resolving an entity's thumbnail with its theme's provider chain (DESIGN.md §10).

:class:`ThumbnailResolver` runs in workers. For each entity it first tries the memoized
source (``entity.thumb_resource_id``); otherwise it walks the chain, trying each candidate
resource until one makes a picture, and remembers the winner through the DB writer. A chain
that ends with :class:`TitleCard` gives a title card (``ThumbnailResult.card``) for the UI to
paint. Pictures
come from ``thumbs.db`` when cached, and files are only read for resources that are online.
"""

import contextlib
import logging
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import Connection, Engine, Row, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.icons import type_icon
from tagalot.core.member_paths import PATH_MARK, member_name
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    ItemPicture,
    OnlinePicture,
    PictureFailure,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.roots import local_path
from tagalot.core.see_through import counts_in
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.archive import ArchiveError, read_member, read_pictures
from tagalot.core.thumbnails.cache import ThumbCache, online_key, thumb_key
from tagalot.core.thumbnails.image import Thumbnail, make_thumbnail
from tagalot.core.thumbnails.render import (
    Renderer,
    load_image,
    load_image_bytes,
    renderer_for,
)
from tagalot.core.thumbnails.title_card import Card, title_card
from tagalot.core.writer import DbWriter, WriterClosedError
from tagalot.themes.api import (
    Entity as ThemeEntity,
)
from tagalot.themes.api import (
    EntityRef,
    Icon,
    OnlineImage,
    ResourceInfo,
    Theme,
    ThumbnailProvider,
    TitleCard,
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
    Resource.parent_resource_id,
)

MEMBER_RENDERER, MEMBER_RENDERER_VERSION = "archive-member", 1
"""How a picture inside an archive is made, in its cache key (with its path inside)."""

MAX_MEMBER_BYTES = 256 * 1024 * 1024
"""The largest file inside an archive (a member resource, #524) drawn, as scans read."""

FALLBACK_ICON = "entity"
"""The icon shown when a chain ends without an :class:`Icon`."""


@dataclass(frozen=True)
class ThumbnailResult:
    """What to show for an entity: a picture, else a title card, else an icon name."""

    entity_id: int
    thumbnail: Thumbnail | None
    icon: str | None
    resource_id: int | None
    """The resource the picture came from."""
    problems: tuple[tuple[str, str], ...] = ()
    """(path, message) for files that couldn't be read, for the activity panel."""
    card: Card | None = None
    """The title card to paint when there's no picture (:class:`TitleCard`)."""


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
        schema: ThemeSchema | None = None,
    ) -> None:
        self.reader = reader
        self.schema = schema
        """The theme's tables, for a title card's subtitle field."""
        self.writer = writer
        self.cache = cache
        self.root_path = root_path
        self.size = size
        self.report: Callable[[Sequence[tuple[str, str]]], None] | None = None
        """Told about files that couldn't be read (``(path, message)``), from workers."""
        self.see_through: Callable[[], frozenset[str]] = frozenset
        """The keep's see-through folder names, case-folded (#459): a folder's files include
        those in its see-through folders."""
        self._theme = theme()
        self._types = {theme.type_id_of(e): e for e in theme.entities}
        self._type_icons = {
            type_id: icon
            for type_id, entity in self._types.items()
            if (icon := type_icon(theme, entity)) is not None
        }
        """Each type's own icon (#556): it shows instead of its chain's ``Icon``."""
        self.icon_of: Callable[[str], str | None] = self._type_icons.get
        """A type's icon by type id: the theme's, or the keep's choice (the session sets
        it, #557)."""
        self._chains: dict[str, list[ThumbnailProvider]] = {}
        self._failed: set[str] = set()
        """Cache keys that failed to render this session: the same file isn't retried
        until it changes (which changes its key)."""
        self._lock = threading.Lock()

    def resolve(self, entity_id: int) -> ThumbnailResult:
        """The thumbnail of an entity, making and caching it if needed. Does I/O: call it
        from a worker."""
        with self.reader.connect() as conn:
            result = _Resolution(self, conn).resolve(entity_id)
        if result.problems and self.report is not None:
            self.report(result.problems)
        return result

    def resource_thumbnail(self, resource_id: int) -> Thumbnail | None:
        """The thumbnail of one resource (a detail page's gallery), made and cached if
        needed; ``None`` if it has no picture. Does I/O: call it from a worker."""
        with self.reader.connect() as conn:
            resolution = _Resolution(self, conn)
            resource = resolution.resource(resource_id)
            picture = None if resource is None else resolution.picture(resource)
        if resolution._problems and self.report is not None:
            self.report(resolution._problems)
        return picture

    def archive_thumbnails(
        self, resource_id: int, names: Sequence[str]
    ) -> list[tuple[str, Thumbnail | None]]:
        """Thumbnails of files inside an archive (a gallery's archive group, #386; Choose
        what to read inside, #519), each made and cached if needed, by the archive, the
        member's path, and the archive's size and time; ``None`` for one that can't be read
        or that nothing draws (:func:`member_renderer`). Does I/O: call it from a worker. A
        damaged archive is reported, never raised."""
        with self.reader.connect() as conn:
            archive = _Resolution(self, conn).resource(resource_id)
        if archive is None or not archive.path:
            return [(name, None) for name in names]
        size = self.size
        keys = {
            name: thumb_key(
                archive.id,
                archive.size,
                archive.mtime_ns,
                f"{MEMBER_RENDERER}:{name}",
                MEMBER_RENDERER_VERSION,
                size,
            )
            for name in names
        }
        found: dict[str, Thumbnail | None] = {}
        wanted = []
        for name in names:
            cached = self.cache.get(keys[name])
            if cached is not None or self.failed(keys[name]):
                found[name] = cached
            else:
                wanted.append(name)
        problems: list[tuple[str, str]] = []
        try:
            for name, data in read_pictures(archive.path, wanted):
                picture = None
                renderer = member_renderer(name)
                if data is not None and renderer is not None:
                    try:
                        image = _draw_member(data, name, renderer, size)
                        picture = None if image is None else make_thumbnail(image, size)
                    except Exception as e:  # corrupt or unsupported: the others still show
                        problems.append(
                            (f"{archive.path} \u203a {name}", f"{type(e).__name__}: {e}")
                        )
                if picture is None:
                    self.mark_failed(keys[name])
                else:
                    self.cache.put(keys[name], size, picture)
                found[name] = picture
        except (ArchiveError, OSError) as e:
            logger.warning("Can't read pictures in %s: %s", archive.path, e)
            problems.append((archive.path, f"{type(e).__name__}: {e}"))
        if problems and self.report is not None:
            self.report(problems)
        return [(name, found.get(name)) for name in names]

    # --- used by _Resolution ---

    def chain(self, type_id: str) -> list[ThumbnailProvider]:
        with self._lock:
            if type_id not in self._chains:
                entity = self._types.get(type_id)
                chain = list(self._theme.thumbnail_chain(entity)) if entity else []
                self._chains[type_id] = chain
            return self._chains[type_id]

    def type_icon(self, type_id: str) -> str | None:
        return self.icon_of(type_id)

    def entity_type(self, type_id: str) -> type[ThemeEntity] | None:
        return self._types.get(type_id)

    def failed(self, key: str) -> bool:
        with self._lock:
            return key in self._failed

    def mark_failed(self, key: str) -> None:
        with self._lock:
            self._failed.add(key)

    def forget_failures(self) -> None:
        """Try files that failed this session again (after the cache is cleared)."""
        with self._lock:
            self._failed.clear()

    def remember(self, entity_id: int, resource_id: int | None) -> None:
        """Store the chain's result in ``entity.thumb_resource_id`` (queued, not awaited)."""

        def work(conn: Connection) -> None:
            conn.execute(
                update(Entity).where(Entity.id == entity_id).values(thumb_resource_id=resource_id)
            )

        # If the keep is closing, the chain simply runs again next time.
        with contextlib.suppress(WriterClosedError):
            self.writer.submit(work)

    def record_failure(self, resource: ResourceInfo, error: str | None) -> None:
        """Remember that a picture couldn't be drawn (``error``), for Triage, or that it was
        drawn after all (``None``) (#426). Queued, not awaited."""

        def work(conn: Connection) -> None:
            if error is None:
                drawn = PictureFailure.resource_id == resource.id
                conn.execute(delete(PictureFailure).where(drawn))
                return
            values = {"size": resource.size, "mtime_ns": resource.mtime_ns, "error": error}
            conn.execute(
                sqlite_insert(PictureFailure)
                .values(resource_id=resource.id, **values)
                .on_conflict_do_update(index_elements=["resource_id"], set_=values)
            )

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
            resource = self.resource(memo)
            if resource is not None:
                picture = self.picture(resource)
                if picture is not None:
                    return self._result(entity, picture, None, resource.id)
                if self._status.get(memo) == ResourceStatus.OFFLINE:
                    # Offline isn't gone: keep the memo and show the icon for now.
                    return self._result(entity, None, self._icon(entity), None)
        picture, icon, source, card = self._run_chain(entity)
        if source != memo:
            self.owner.remember(entity_id, source)
        return self._result(entity, picture, icon, source, card)

    def _run_chain(
        self, entity: EntityRef
    ) -> tuple[Thumbnail | None, str | None, int | None, Card | None]:
        tried: set[int] = set()
        own = self._own(entity, tried)  # the user's choice, or its note's (#439)
        if own is not None:
            return own[0], None, own[1], None
        gallery_tried = False
        for provider in self.owner.chain(entity.type):
            if isinstance(provider, Icon | TitleCard) and not gallery_tried:
                gallery_tried = True  # no picture from the chain: its gallery's first (#439)
                found = self._gallery(entity, tried)
                if found is not None:
                    return found[0], None, found[1], None
                online = self._online(entity)  # then one from a service (#483)
                if online is not None:
                    return online, None, None, None
            if isinstance(provider, OnlineImage):  # placed by the theme (#487)
                online = self._online(entity)
                if online is not None:
                    return online, None, None, None
                continue
            if isinstance(provider, Icon):
                return None, self.owner.type_icon(entity.type) or provider.name, None, None
            if isinstance(provider, TitleCard):
                card = self._card(entity, provider)
                if card is not None:
                    return None, None, None, card
                continue
            try:
                for candidate in provider.candidates(entity, self):
                    if candidate.id in tried:
                        continue
                    tried.add(candidate.id)
                    picture = self.picture(candidate)
                    if picture is not None:
                        return picture, None, candidate.id, None
            except Exception as e:  # a theme's provider must not break thumbnails
                logger.warning("Thumbnail provider %r failed for entity %d", provider, entity.id)
                self._problems.append((f"entity {entity.id}", f"{provider!r} failed: {e}"))
        if not gallery_tried:
            found = self._gallery(entity, tried)
            if found is not None:
                return found[0], None, found[1], None
            online = self._online(entity)
            if online is not None:
                return online, None, None, None
        return None, self.owner.type_icon(entity.type) or FALLBACK_ICON, None, None

    def _online(self, entity: EntityRef) -> Thumbnail | None:
        """The item's picture from an online service (#483): only when it has none of its
        own, so it is never remembered as the item's source."""
        row = self.conn.execute(
            select(OnlinePicture.data, OnlinePicture.fetched_at).where(
                OnlinePicture.entity_id == entity.id, OnlinePicture.data.is_not(None)
            )
        ).first()
        if row is None or row.data is None:
            return None
        size = self.owner.size
        key = online_key(entity.id, row.fetched_at, size)
        cached = self.owner.cache.get(key)
        if cached is not None:
            return cached
        if self.owner.failed(key):
            return None
        try:
            picture = make_thumbnail(load_image_bytes(row.data, size), size)
        except Exception as e:  # not a picture after all: the card or icon shows
            logger.warning("Can't draw the online picture of entity %d: %s", entity.id, e)
            self.owner.mark_failed(key)
            return None
        self.owner.cache.put(key, size, picture)
        return picture

    def _own(self, entity: EntityRef, tried: set[int]) -> tuple[Thumbnail, int] | None:
        """The item's own picture (``item_picture``), when it can be drawn."""
        resource_id = self.conn.scalar(  # the user's, else its note's
            select(ItemPicture.resource_id)
            .where(ItemPicture.entity_id == entity.id)
            .order_by(ItemPicture.source != "user")
            .limit(1)
        )
        resource = None if resource_id is None else self.resource(resource_id)
        if resource is None:
            return None
        tried.add(resource.id)
        picture = self.picture(resource)
        return None if picture is None else (picture, resource.id)

    def _gallery(self, entity: EntityRef, tried: set[int]) -> tuple[Thumbnail, int] | None:
        """The first picture of the item's first gallery role that can be drawn."""
        declared = self.owner.entity_type(entity.type)
        role = next((r for r in declared.roles if r.gallery), None) if declared else None
        if role is None:
            return None
        for resource in self.resources(entity, role.name):
            if resource.id in tried:
                continue
            tried.add(resource.id)
            picture = self.picture(resource)
            if picture is not None:
                return picture, resource.id
        return None

    def _card(self, entity: EntityRef, provider: TitleCard) -> Card | None:
        """The entity's title card; ``None`` when it has no title."""
        title = self.conn.scalar(select(Entity.title).where(Entity.id == entity.id))
        if not title or not title.strip():
            return None
        subtitle = ""
        schema = self.owner.schema
        if provider.subtitle and schema is not None:
            try:
                table = schema.by_type_id(entity.type).table
                column = table.c[provider.subtitle]
            except KeyError:  # not a field of this type: no subtitle
                logger.warning("%r: %s has no field %r", provider, entity.type, provider.subtitle)
            else:
                value = self.conn.scalar(select(column).where(table.c.id == entity.id))
                subtitle = "" if value is None else str(value)
        return title_card(title, subtitle, provider.aspect)

    def _icon(self, entity: EntityRef) -> str:
        own = self.owner.type_icon(entity.type)
        if own is not None:
            return own
        icons = [p for p in self.owner.chain(entity.type) if isinstance(p, Icon)]
        return icons[0].name if icons else FALLBACK_ICON

    def _result(
        self,
        entity: EntityRef,
        picture: Thumbnail | None,
        icon: str | None,
        source: int | None,
        card: Card | None = None,
    ) -> ThumbnailResult:
        return ThumbnailResult(entity.id, picture, icon, source, tuple(self._problems), card)

    # --- pictures ---

    def picture(self, resource: ResourceInfo) -> Thumbnail | None:
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
            if resource.archive is not None and resource.archive_path:
                image = _member_image(resource, renderer, size)
            else:
                image = renderer.load(resource.path, size)
            picture = None if image is None else make_thumbnail(image, size)
        except Exception as e:  # unreadable, corrupt, or unsupported: try the next one
            where = _where(resource)
            logger.warning("Can't make a thumbnail of %s: %s", where, e)
            self._problems.append((where, f"{type(e).__name__}: {e}"))
            self.owner.record_failure(resource, str(e) or type(e).__name__)
            picture = None
        if picture is None:
            self.owner.mark_failed(key)
            return None
        self.owner.cache.put(key, size, picture)
        failed = select(PictureFailure.resource_id).where(PictureFailure.resource_id == resource.id)
        if self.conn.scalar(failed) is not None:  # it was drawn after all
            self.owner.record_failure(resource, None)
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
        names = self.owner.see_through()
        if names:  # then those in its see-through folders (#459)
            inside += [
                row
                for row in rows
                if "/" in row.relpath[len(prefix) :] and counts_in(row.relpath, names) == folder
            ]
        return [info for info in map(self._info, inside) if info is not None]

    def thumbnail_of(self, entity: EntityRef) -> ResourceInfo | None:
        if entity.id in self._resolving:  # a containment cycle: don't loop
            return None
        source = self.resolve(entity.id).resource_id
        return None if source is None else self.resource(source)

    # --- resources ---

    def resource(self, resource_id: int) -> ResourceInfo | None:
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
        if row.parent_resource_id is not None:  # inside an archive (#524)
            archive = self.conn.scalar(
                select(Resource.relpath).where(Resource.id == row.parent_resource_id)
            )
            if archive is None:
                return None
            return ResourceInfo(
                row.id,
                row.root_id,
                row.relpath,
                row.kind.value,
                row.ext,
                row.size,
                row.mtime_ns,
                "",
                archive=archive,
                archive_path=local_path(root, archive),
            )
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


def _member_image(resource: ResourceInfo, renderer: Renderer, size: int) -> Image.Image | None:
    """Draw a file inside an archive (#524): copied out of the archive into a temporary
    folder for the renderer, which reads files, and deleted after. The archive is only
    read."""
    assert resource.archive is not None
    assert resource.archive_path is not None
    name = member_name(resource.archive, resource.relpath)
    data = read_member(resource.archive_path, name, MAX_MEMBER_BYTES)
    return _draw_member(data, name, renderer, size)


def _ext(name: str) -> str:
    base = name.rpartition("/")[2]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def member_renderer(name: str) -> Renderer | None:
    """The renderer for a file inside an archive, by its extension (an image, a font, a
    PDF...); ``None`` when nothing draws its kind."""
    return renderer_for(ResourceInfo(0, "", name, "file", _ext(name), None, None, ""))


def _draw_member(data: bytes, name: str, renderer: Renderer, size: int) -> Image.Image | None:
    """Draw a file read out of an archive: an image from memory, anything else from a copy
    in a temporary folder for the renderer, which reads files, deleted after."""
    if renderer.load is load_image:
        return load_image_bytes(data, size)
    with tempfile.TemporaryDirectory(prefix="tagalot-thumb-") as folder:
        copy = Path(folder) / f"member{_ext(name)}"
        copy.write_bytes(data)
        image = renderer.load(str(copy), size)
        if image is not None:
            image.load()  # read it all before the copy goes
        return image


def _where(resource: ResourceInfo) -> str:
    """The file's path for messages: a member's archive's, then its path inside it."""
    if resource.archive is not None and resource.archive_path:
        return (
            f"{resource.archive_path} {PATH_MARK} {member_name(resource.archive, resource.relpath)}"
        )
    return resource.path
