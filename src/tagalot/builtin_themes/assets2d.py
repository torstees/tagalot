"""2D assets theme: artists and their images, fonts, and archives (DESIGN.md §9).

```
Artist ⊃ Image    .png .jpg .psd …   width, height
       ⊃ Font     .ttf .otf .woff    family, style
       ⊃ Archive  .zip .7z .rar      images inside
```

The artist is a folder at a set level under a root: the ``artist_level`` option, 1 by
default (``Root/<Artist>/…``), which a keep or a single root can change. Files above that
level have no artist. When the level changes, the root is ingested again: assets move to
their new artists, and artists left empty (whose folder is no longer an artist folder) are
deleted. Each file is one entity, so tags stay with it when it moves, as in the
generic theme. File details (image sizes, font names, archive contents) are read in
:meth:`Assets2DTheme.prepare`, in the scan worker.
"""

import logging
import posixpath
import uuid
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import py7zr
import rarfile
from PIL import Image as PILImage
from PIL import ImageFont

from tagalot.themes.api import (
    KIND_EXTENSIONS,
    CardRows,
    DashboardContext,
    Entity,
    EntityRef,
    FolderImage,
    Icon,
    IngestContext,
    Kind,
    Record,
    ResourceInfo,
    SearchView,
    SortBy,
    Theme,
    ThumbnailContext,
    ThumbnailProvider,
    contains,
    dashboard_card,
    field,
    kind_of,
    option,
    role,
    stat,
    top_values,
)

logger = logging.getLogger(__name__)

ARTIST_LEVEL = option(
    "artist_level",
    1,
    label="Artist folder level",
    description="Which folder under a root names the artist: 1 is Root/<Artist>/…, "
    "2 is Root/<Store>/<Artist>/…",
)


class Artist(Entity):
    """Whoever made the assets in a folder."""

    title_label = "Name"
    roles = [role("folder", kinds={"dir"}, primary=True)]


class _Asset(Entity):
    """Fields every asset file has. Not a type of its own. They come from the file and
    its place, so they can't be edited by hand (a font's family and style can)."""

    title_label = "Name"
    double_click = "open_file"
    artist: str | None = field("Artist", card=True, search="choice", editable=False)
    extension: str = field("Extension", card=True, search="choice", editable=False)
    folder: str = field("Folder", search="text", editable=False)
    size: int | None = field("Size", card=True, search="range", editable=False, display="bytes")
    modified: datetime | None = field("Modified", search="range", editable=False)


class Image(_Asset):
    """A picture: PNG, JPEG, PSD, and the other image formats."""

    width: int | None = field("Width", search="range", editable=False)
    height: int | None = field("Height", search="range", editable=False)
    dimensions: str | None = field("Dimensions", card=True, editable=False)
    phash: str | None = field("Picture hash", editable=False, detail=False)
    """What the picture looks like: a 64-bit difference hash (16 hex digits) and its
    average color (6 more). Pictures that look alike have hashes that differ in few bits
    and similar colors (near-duplicates, DESIGN.md §13)."""
    roles = [role("file", kinds={"image"}, primary=True, thumbnail=True)]
    card_lines = ("dimensions",)


class Font(_Asset):
    """A font file; its thumbnail is a sample of the font."""

    family: str | None = field("Family", card=True, search="choice")
    style: str | None = field("Style", card=True, search="choice")
    roles = [role("file", kinds={"font"}, primary=True, thumbnail=True)]
    card_lines = ("family", "style")


class Archive(_Asset):
    """A zip, 7z, or rar archive; its thumbnail is the best image inside."""

    images: int | None = field("Images inside", card=True, search="range", editable=False)
    roles = [role("file", kinds={"archive"}, primary=True, thumbnail=True)]
    card_lines = ("images",)


ASSET_TYPES: dict[Kind, type[_Asset]] = {
    Kind.IMAGE: Image,
    Kind.FONT: Font,
    Kind.ARCHIVE: Archive,
}


class ContentsThumbnail(ThumbnailProvider):
    """An artist shows the thumbnail of one of its first assets."""

    id = "assets2d.contents"
    tried = 5

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        for child in ctx.children(entity)[: self.tried]:
            found = ctx.thumbnail_of(child)
            if found is not None:
                yield found


class Assets2DTheme(Theme):
    # Version 2 reads a picture hash from images: existing keeps read their files again once.
    id, name, version = "assets2d", "2D assets", 2
    extensions = frozenset(
        KIND_EXTENSIONS[Kind.IMAGE] | KIND_EXTENSIONS[Kind.FONT] | KIND_EXTENSIONS[Kind.ARCHIVE]
    )
    entities = [Artist, Image, Font, Archive]
    containment = [contains(Artist, Image), contains(Artist, Font), contains(Artist, Archive)]
    # Asset views count an artist's tags as its assets' own (tag an artist "Pixel art" and
    # its assets match), so tagging a whole artist is one step.
    views = [
        SearchView("Assets", [Image, Font, Archive], inherit_tags=True),
        SearchView("Artists", [Artist]),
        SearchView("Images", [Image], inherit_tags=True),
        SearchView(
            "Fonts",
            [Font],
            inherit_tags=True,
            default_sort=[SortBy("family"), SortBy("style"), SortBy("title")],
        ),
        SearchView("Archives", [Archive], inherit_tags=True),
    ]
    thumbnail_max = 1024
    thumbnail_default = 256
    options = [ARTIST_LEVEL]
    dashboard = [
        stat("Space used by images", Image, "size", "sum"),
        top_values("Image types", Image, "extension"),
    ]

    near_duplicate_threshold = 58 / 64  # at most 6 of the 64 bits differ

    def blocking_keys(self, entity_type: type[Entity], record: Record) -> Iterable[str]:
        """An image's difference hash in four quarters: hashes within 3 bits of each other
        always share a quarter, and most within 6 do."""
        phash = record.fields.get("phash")
        if entity_type is not Image or not phash:
            return ()
        return [f"{n}:{phash[n * 4 : n * 4 + 4]}" for n in range(4)]

    def similarity(self, entity_type: type[Entity], a: Record, b: Record) -> float:
        """The share of the difference hashes' bits that match, for pictures of about the
        same average color: the hash only sees light and dark, so a recolored picture of
        the same layout would otherwise count as the same."""
        first, second = a.fields.get("phash"), b.fields.get("phash")
        if not first or not second:
            return 0.0
        if color_distance(first, second) > MAX_COLOR_DISTANCE:
            return 0.0
        return 1 - hash_distance(first, second) / 64

    @dashboard_card("Biggest artists", description="Artists with the most assets")
    def biggest_artists(self, ctx: DashboardContext) -> CardRows:
        counts = []
        for artist in ctx.find(Artist):
            name = ctx.get(artist).title
            n = sum(ctx.count(kind, artist=name) for kind in (Image, Font, Archive))
            counts.append((n, name))
        counts.sort(key=lambda c: (-c[0], c[1].casefold()))
        if not counts:
            return "No artists yet."
        return [(name, f"{n:,} assets") for n, name in counts[:5]]

    dirs = True  # every folder: ingest decides which are artists (the level can change)

    def thumbnail_chain(self, entity_type: type[Entity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Artist:
            return [FolderImage(), ContentsThumbnail(), Icon("dir")]
        return super().thumbnail_chain(entity_type)

    # --- reading files (scan worker) ---

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        found: dict[int, Any] = {}
        for resource in batch:
            kind = kind_of(resource)
            try:
                if kind is Kind.IMAGE:
                    found[resource.id] = read_image(resource.path)
                elif kind is Kind.FONT:
                    found[resource.id] = read_font(resource.path)
                elif kind is Kind.ARCHIVE:
                    found[resource.id] = read_archive(resource.path)
            except Exception as e:  # a bad file still becomes an asset, just without details
                found[resource.id] = {"error": f"{type(e).__name__}: {e}"}
        return found

    # --- ingest (DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        level: int = ctx.option(ARTIST_LEVEL.name)
        for resource in batch:
            if resource.kind == "dir":
                self._ingest_folder(resource, level, ctx)
                continue
            kind = kind_of(resource)
            asset_type = ASSET_TYPES.get(kind) if kind is not None else None
            if asset_type is None:
                continue
            details = dict(ctx.prepared(resource) or {})
            error = details.pop("error", None)
            if error:
                ctx.warn(resource, f"Couldn't read its details: {error}")
            artist = artist_of(resource.relpath, level)
            title, values = file_values(resource)
            values.update(details, artist=artist)

            existing = ctx.entities_of(resource, "file")
            if existing:
                asset = existing[0]
                previous = ctx.get(asset).fields.get("artist")
                ctx.update(asset, title=title, **values)
                if previous != artist and previous is not None:
                    old = ctx.upsert(Artist, artist_key(previous))
                    ctx.uncontain(old, asset)
                    _delete_if_empty(old, ctx)
            else:
                # Never-reused keys: a new file where another used to be is a new asset.
                asset = ctx.upsert(asset_type, f"file:{uuid.uuid4().hex}", title=title, **values)
                ctx.link(asset, resource, "file")
            if artist is not None:
                ctx.contain(ctx.upsert(Artist, artist_key(artist), title=artist), asset)

    def _ingest_folder(self, resource: ResourceInfo, level: int, ctx: IngestContext) -> None:
        """An artist's folder becomes its ``folder`` link; a folder at another level stops
        being one (after the level changed)."""
        if resource.relpath.count("/") == level - 1:
            name = resource.relpath.rpartition("/")[2]
            artist = ctx.upsert(Artist, artist_key(name), title=name)
            ctx.link(artist, resource, "folder")
            return
        for artist in ctx.entities_of(resource, "folder"):
            ctx.unlink(artist, resource, "folder")
            _delete_if_empty(artist, ctx)


def _delete_if_empty(artist: EntityRef, ctx: IngestContext) -> None:
    """An artist with no assets whose folder is no longer an artist folder is gone."""
    if not ctx.contents(artist) and not ctx.linked(artist, "folder"):
        ctx.delete(artist)


def artist_of(relpath: str, level: int = 1) -> str | None:
    """The artist folder's name for a file, or ``None`` if the file isn't that deep."""
    parts = relpath.split("/")
    return parts[level - 1] if len(parts) > level else None


def artist_key(name_or_path: str) -> str:
    """Artists are keyed by folder name, so one artist's folders in two roots merge."""
    return "artist:" + name_or_path.rpartition("/")[2].casefold()


def file_values(resource: ResourceInfo) -> tuple[str, dict[str, Any]]:
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


# --- file details ---


def read_image(path: str) -> dict[str, Any]:
    """An image's size, and a hash of what it looks like (:func:`picture_hash`)."""
    with PILImage.open(path) as image:
        width, height = image.size
        try:
            color = picture_color(image)  # first: it decodes in color
            phash: str | None = picture_hash(image) + color
        except Exception:  # an odd format Pillow sizes but can't decode: no hash
            phash = None
    return {
        "width": width,
        "height": height,
        "dimensions": f"{width} \u00d7 {height}",
        "phash": phash,
    }


def picture_hash(image: PILImage.Image) -> str:
    """A 64-bit difference hash, as 16 hex digits: the picture shrunk to 9 x 8 grey
    pixels, one bit for whether each pixel is brighter than its right-hand neighbour.
    Resizing, recompressing, or small edits change few bits."""
    image.draft("L", (64, 64))  # JPEGs decode small: much faster
    small = image.convert("L").resize((9, 8), PILImage.Resampling.LANCZOS)
    pixels = small.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (left > right)
    return f"{bits:016x}"


def picture_color(image: PILImage.Image) -> str:
    """The picture's average color as 6 hex digits (``"3c2878"``); transparent parts
    count as mid grey."""
    image.draft("RGB", (64, 64))  # JPEGs decode small: much faster
    rgba = image.convert("RGBA")
    grey = PILImage.new("RGBA", rgba.size, (128, 128, 128, 255))
    average = (
        PILImage.alpha_composite(grey, rgba).convert("RGB").resize((1, 1), PILImage.Resampling.BOX)
    )
    red, green, blue = average.getpixel((0, 0))  # type: ignore[misc]
    return f"{red:02x}{green:02x}{blue:02x}"


MAX_COLOR_DISTANCE = 40
"""How far apart (in RGB) two pictures' average colors can be and still look alike."""


def hash_distance(a: str, b: str) -> int:
    """How many of two picture hashes' 64 bits differ (their first 16 hex digits)."""
    return (int(a[:16], 16) ^ int(b[:16], 16)).bit_count()


def color_distance(a: str, b: str) -> float:
    """How far apart two picture hashes' average colors are (0 when either has none)."""
    if len(a) < 22 or len(b) < 22:
        return 0.0
    first = [int(a[16 + n : 18 + n], 16) for n in (0, 2, 4)]
    second = [int(b[16 + n : 18 + n], 16) for n in (0, 2, 4)]
    return float(sum((x - y) ** 2 for x, y in zip(first, second, strict=True)) ** 0.5)


def read_font(path: str) -> dict[str, Any]:
    """A font's family and style names."""
    family, style = ImageFont.truetype(path, 12).getname()
    return {"family": family, "style": style}


def read_archive(path: str) -> dict[str, Any]:
    """How many images an archive holds (from its listing; nothing is extracted)."""
    return {"images": sum(1 for name in archive_names(path) if is_image_name(name))}


def archive_names(path: str) -> list[str]:
    with open(path, "rb") as file:
        head = file.read(8)
    if head.startswith(b"PK"):
        with zipfile.ZipFile(path) as zf:
            return [i.filename for i in zf.infolist() if not i.is_dir()]
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        with py7zr.SevenZipFile(path) as sz:
            return [i.filename for i in sz.list() if not i.is_directory]
    if head.startswith(b"Rar!\x1a\x07"):
        with rarfile.RarFile(path) as rf:
            return [i.filename for i in rf.infolist() if not i.is_dir()]
    raise ValueError("not a zip, 7z, or rar archive")


def is_image_name(name: str) -> bool:
    """An image inside an archive, not a dotfile or macOS metadata."""
    parts = name.split("/")
    if any(p.startswith(".") or p == "__MACOSX" for p in parts):
        return False
    return posixpath.splitext(parts[-1])[1].lower() in KIND_EXTENSIONS[Kind.IMAGE]
