"""Music theme: artists, their albums, and the albums' songs (DESIGN.md §9).

```
Artist ⊃ Album (a folder)  ⊃ Song (one or more audio files: versions)
       ⊃ Song (a file directly in a root, with no album)
```

- **An album is a folder with audio** in it. Folders named like ``CD1``, ``Disc 2``, or
  ``Disk 3`` belong to their parent's album, and give their songs a disc number when the tags
  don't.
- **Songs** are keyed by album, disc, track, and title (ignoring case and punctuation), so
  ``01 So What.flac`` and ``01 So What.mp3`` are one song with two versions.
- **The album's artist** is its songs' ``ALBUMARTIST`` tag, else their ``ARTIST``; when its
  songs disagree it is by "Various Artists". A song whose own artist differs from its album's
  also sits under its own artist, so an artist's page finds their songs on compilations.
- **Thumbnails:** a song shows its embedded art, else its album's; an album its folder image
  (``folder.jpg``, ``cover.jpg``…), else its first songs' embedded art; an artist one of its
  albums' or songs'.
- Tags are read with mutagen in :meth:`MusicTheme.prepare` (the scan worker); a file without
  tags is named from its file name ("01 - So What.mp3": track 1, "So What").
"""

import logging
import os
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import mutagen

from tagalot.themes.api import (
    FOLDER_IMAGE_EXTENSIONS,
    KIND_EXTENSIONS,
    ActionContext,
    EmbeddedAudioArt,
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
    action,
    contains,
    field,
    kind_of,
    role,
    stat,
    top_values,
)

logger = logging.getLogger(__name__)

AUDIO = KIND_EXTENSIONS[Kind.AUDIO]
DISC_FOLDER = re.compile(r"^(?:cd|disc|disk)\s*[-_.]?\s*(\d+)$", re.IGNORECASE)
"""Folder names that hold one disc of their parent's album."""
LEADING_TRACK = re.compile(r"^\s*(?:(\d{1,2})[-.](?=\d))?(\d{1,3})(?:\s*[-._)]\s*|\s+)(.+)$")
"""``01 So What``, ``01 - So What``, ``1-03. So What`` (disc 1, track 3)."""
VARIOUS = "Various Artists"
"""The artist of an album whose songs are by different artists."""


class Artist(Entity):
    """Whoever an album or song is by."""

    title_label = "Name"
    contents_sort = (SortBy("year"), SortBy("title"))


class Album(Entity):
    """A folder of songs."""

    artist: str | None = field("Artist", card=True, search="choice", editable=False)
    year: int | None = field("Year", card=True, search="range")
    genre: str | None = field("Genre", search="choice")
    folder: str = field("Folder", search="text", editable=False)
    roles = [role("folder", kinds={"dir"}, primary=True)]
    card_lines = ("artist", "year")
    contents_sort = (SortBy("disc"), SortBy("track"), SortBy("title"))


class Song(Entity):
    """A song; its versions (FLAC, MP3…) are its audio files."""

    track: int | None = field("Track", card=True, search="range")
    disc: int | None = field("Disc", search="range")
    artist: str | None = field("Artist", card=True, search="choice")
    album_artist: str | None = field("Album artist", search="choice")
    album: str | None = field("Album", card=True, search="choice")
    year: int | None = field("Year", search="range")
    genre: str | None = field("Genre", search="choice")
    duration: float | None = field(
        "Length", card=True, search="range", editable=False, display="duration"
    )
    bitrate: int | None = field("Bitrate (kbps)", search="range", editable=False)
    """The best of its versions' bitrates, in kilobits a second (theme version 2)."""
    folder: str = field("Folder", search="text", editable=False)
    roles = [role("audio", kinds={"audio"}, many=True, primary=True)]
    double_click = "open_file"
    card_lines = ("artist", "duration")


class AlbumThumbnail(ThumbnailProvider):
    """A song shows its album's thumbnail (not that of an artist it also sits under)."""

    id = "music.album"

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        album = MusicTheme.type_id_of(Album)
        for parent in ctx.parents(entity):
            if parent.type == album:
                found = ctx.thumbnail_of(parent)
                if found is not None:
                    yield found


class ContentsThumbnail(ThumbnailProvider):
    """An artist shows the thumbnail of one of its first albums or songs."""

    id = "music.contents"
    tried = 5

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        for child in ctx.children(entity)[: self.tried]:
            found = ctx.thumbnail_of(child)
            if found is not None:
                yield found


class MusicTheme(Theme):
    id, name, version = "music", "Music", 2
    # 2: songs record their bitrate; keeps read every file again once to fill it in.
    extensions = frozenset(AUDIO | FOLDER_IMAGE_EXTENSIONS)
    dirs = True
    entities = [Artist, Album, Song]
    containment = [contains(Artist, Album), contains(Album, Song), contains(Artist, Song)]

    # Tags on an album count for its songs (tag an album "Calm" and Songs with Calm lists
    # its songs); Browse also lists a matching album's songs, under it in the tree.
    def blocking_keys(self, entity_type: type[Entity], record: Record) -> Iterable[str]:
        """Songs by the same artist with the same title (ignoring case and punctuation)."""
        if entity_type is not Song:
            return ()
        artist = record.fields.get("artist") or ""
        return [f"{normalize(artist)}|{normalize(record.title)}"]

    def similarity(self, entity_type: type[Entity], a: Record, b: Record) -> float:
        """Same artist and title (the blocking key) on different albums: 1 when the lengths
        are within 2 seconds (or unknown), falling to 0 at 30 seconds apart (a live take, an
        edit)."""
        if normalize(a.title) != normalize(b.title):
            return 0.0
        # Within one album, versions of a song are already one song: two songs there with
        # the same title are different tracks (a box set's two "Intro"s).
        if (
            album_folder(a.fields.get("folder") or "")[0]
            == album_folder(b.fields.get("folder") or "")[0]
        ):
            return 0.0
        lengths = a.fields.get("duration"), b.fields.get("duration")
        if None in lengths:
            return 0.95
        apart = abs(lengths[0] - lengths[1])  # type: ignore[operator]
        if apart <= 2:
            return 1.0
        return max(0.0, 1 - float(apart) / 30)

    dashboard = [
        stat("Total running time", Song, "duration", "sum"),
        stat("Average song", Song, "duration", "avg"),
        top_values("Top genres", Album, "genre"),
        top_values("Top years", Album, "year"),
    ]
    views = [
        SearchView(
            "Browse",
            [Album, Song],
            inherit_tags=True,
            show_contained=True,
            layout="tree",
            default_sort=[SortBy("artist"), SortBy("year"), SortBy("title")],
        ),
        SearchView("Artists", [Artist]),
        SearchView(
            "Albums",
            [Album],
            inherit_tags=True,
            default_sort=[SortBy("artist"), SortBy("year"), SortBy("title")],
        ),
        SearchView(
            "Songs",
            [Song],
            inherit_tags=True,
            layout="list",
            default_sort=[
                SortBy("artist"),
                SortBy("album"),
                SortBy("disc"),
                SortBy("track"),
                SortBy("title"),
            ],
        ),
    ]

    # --- actions ---

    @action("Play album", [Album])
    def play_album(self, albums: Sequence[EntityRef], ctx: ActionContext) -> None:
        """Write the albums' songs, in disc and track order, to a playlist (``.m3u8``) in
        Tagalot's temp folder and open it with the program for playlists. A song's first
        version that's on this computer is played; songs with none are left out."""
        lines = ["#EXTM3U"]
        played = missing = 0
        for album in albums:
            for song in ctx.contents(album):
                files = ctx.resources(song, "audio")
                if not files:
                    missing += 1
                    continue
                record = ctx.get(song)
                seconds = record.fields.get("duration")
                artist = record.fields.get("artist")
                name = f"{artist} - {record.title}" if artist else record.title
                length = max(1, round(seconds)) if seconds else -1  # -1: unknown
                lines += [f"#EXTINF:{length},{name}", files[0].path]
                played += 1
        title = ctx.get(albums[0]).title if len(albums) == 1 else f"{len(albums)} albums"
        if not played:
            ctx.message(f"Nothing to play: {title} has no songs on this computer.")
            return
        path = playlist_path(ctx, title)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        ctx.open(path)
        text = f"Playing {played} {'song' if played == 1 else 'songs'} from {title}."
        if missing:
            text += f" {missing} not on this computer (offline or missing) were left out."
        ctx.message(text)

    def thumbnail_chain(self, entity_type: type[Entity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Song:
            return [EmbeddedAudioArt(), AlbumThumbnail(), Icon("audio")]
        if entity_type is Album:
            return [FolderImage(), EmbeddedAudioArt(), Icon("dir")]
        if entity_type is Artist:
            return [ContentsThumbnail(), Icon("entity")]
        return super().thumbnail_chain(entity_type)

    # --- reading files (scan worker) ---

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        found: dict[int, Any] = {}
        for resource in batch:
            try:
                if resource.kind == "dir":
                    found[resource.id] = {"has_audio": folder_has_audio(resource.path)}
                elif kind_of(resource) is Kind.AUDIO:
                    found[resource.id] = read_tags(resource.path)
            except Exception as e:  # a bad file is still a song, named from its file
                found[resource.id] = {"error": f"{type(e).__name__}: {e}"}
        return found

    # --- ingest (DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            if resource.kind == "dir":
                self._ingest_folder(resource, ctx)
            elif kind_of(resource) is Kind.AUDIO:
                self._ingest_song(resource, ctx)

    def _ingest_folder(self, resource: ResourceInfo, ctx: IngestContext) -> None:
        details = ctx.prepared(resource) or {}
        name = resource.relpath.rpartition("/")[2]
        if not details.get("has_audio") or DISC_FOLDER.match(name):
            return
        key = album_key(resource.relpath)
        album = ctx.upsert(Album, key, folder=resource.relpath)
        if not album_titled(ctx, album, key):
            ctx.update(album, title=name)
        ctx.link(album, resource, "folder")

    def _ingest_song(self, resource: ResourceInfo, ctx: IngestContext) -> None:
        tags = dict(ctx.prepared(resource) or {})
        error = tags.pop("error", None)
        if error:
            ctx.warn(resource, f"Couldn't read its tags: {error}")
        folder, _, filename = resource.relpath.rpartition("/")
        album_dir, folder_disc = album_folder(folder)
        guessed_disc, guessed_track, guessed_title = from_file_name(filename)
        title = tags.get("title") or guessed_title
        track = tags.get("track") or guessed_track
        disc = tags.get("disc") or folder_disc or guessed_disc
        artist = tags.get("artist")
        album_artist = tags.get("albumartist") or artist
        bitrate: int | None = tags.get("bitrate")
        values = {
            "track": track,
            "disc": disc,
            "artist": artist,
            "album_artist": tags.get("albumartist"),
            "album": tags.get("album") or (album_dir.rpartition("/")[2] if album_dir else None),
            "year": tags.get("year"),
            "genre": tags.get("genre"),
            "duration": tags.get("duration"),
            "folder": folder,
        }

        existing = ctx.entities_of(resource, "audio")
        if existing:
            song = existing[0]
            before = ctx.get(song).fields
            values["bitrate"] = best_bitrate(ctx, song, resource, before, bitrate)
            ctx.update(song, title=title, **values)
            if before.get("artist") and before["artist"] != artist:
                old_artist = artist_ref(before["artist"], ctx)
                ctx.uncontain(old_artist, song)
                delete_if_empty(old_artist, ctx)
            old_dir, _ = album_folder(before.get("folder") or "")
            if old_dir and old_dir != album_dir:  # moved to another album
                old_album = ctx.upsert(Album, album_key(old_dir))
                ctx.uncontain(old_album, song)
                if not delete_if_empty(old_album, ctx):
                    settle_album(old_album, ctx)
        else:
            key = song_key(album_dir, disc, track, title)
            song = ctx.upsert(Song, key, title=title)  # found, or made with no values yet
            values["bitrate"] = best_bitrate(ctx, song, resource, ctx.get(song).fields, bitrate)
            ctx.update(song, title=title, **values)
            ctx.link(song, resource, "audio", sort_order=0)

        if album_dir:
            key = album_key(album_dir)
            album = ctx.upsert(Album, key, folder=album_dir)
            if not album_titled(ctx, album, key):
                ctx.update(album, title=album_dir.rpartition("/")[2])
            album_values: dict[str, Any] = {}
            if tags.get("album"):
                album_values["title"] = tags["album"]
            for name in ("year", "genre"):
                if tags.get(name) is not None:
                    album_values[name] = tags[name]
            if album_values:
                ctx.update(album, **album_values)
            ctx.contain(album, song)
            settle_album(album, ctx)
        elif artist or album_artist:
            ctx.contain(artist_ref(artist or album_artist or "", ctx), song)


def settle_album(album: EntityRef, ctx: IngestContext) -> None:
    """Work out the album's artist from its songs and put it under them; songs by someone
    else also sit under their own artist."""
    songs = {song: ctx.get(song).fields for song in ctx.contents(album)}
    names = {f.get("album_artist") or f.get("artist") for f in songs.values()} - {None}
    artist = (names.pop() if len(names) == 1 else VARIOUS) if names else None
    previous = ctx.get(album).fields.get("artist")
    if artist != previous:
        ctx.update(album, artist=artist)
    if artist:
        ctx.contain(artist_ref(artist, ctx), album)
    for song, fields in songs.items():
        own = fields.get("artist")
        if own and own != artist:
            ctx.contain(artist_ref(own, ctx), song)
        elif own:
            ctx.uncontain(artist_ref(own, ctx), song)
    if previous and previous != artist:
        old = artist_ref(previous, ctx)
        ctx.uncontain(old, album)
        delete_if_empty(old, ctx)


# --- keys and names ---


def normalize(text: str) -> str:
    """Ignore case and punctuation: ``"So What!"`` and ``"so what"`` match."""
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def album_key(folder: str) -> str:
    return f"album:{folder}"


def song_key(album_dir: str, disc: int | None, track: int | None, title: str) -> str:
    return f"song:{album_dir}:{disc or ''}:{track or ''}:{normalize(title)}"


def artist_ref(name: str, ctx: IngestContext) -> EntityRef:
    return ctx.upsert(Artist, f"artist:{normalize(name)}", title=name)


def album_titled(ctx: IngestContext, album: EntityRef, key: str) -> bool:
    """Whether the album has a name yet (a new entity is named by its key until then)."""
    return ctx.get(album).title != key


def delete_if_empty(entity: EntityRef, ctx: IngestContext) -> bool:
    """An album or artist left with nothing inside and no folder is gone (returns whether it
    was deleted)."""
    if ctx.contents(entity) or ctx.linked(entity):
        return False
    ctx.delete(entity)
    return True


def album_folder(folder: str) -> tuple[str, int | None]:
    """The album's folder for songs in ``folder``, and a disc number if ``folder`` is a disc
    subfolder (``Kind of Blue/CD2`` → ``Kind of Blue``, 2)."""
    parent, _, name = folder.rpartition("/")
    found = DISC_FOLDER.match(name)
    if found and parent:
        return parent, int(found.group(1))
    return folder, None


def from_file_name(filename: str) -> tuple[int | None, int | None, str]:
    """Disc, track, and title guessed from a file name, for files without tags."""
    stem = posixpath.splitext(filename)[0]
    found = LEADING_TRACK.match(stem)
    if found is None:
        return None, None, stem.strip()
    disc, track, title = found.groups()
    return (int(disc) if disc else None), int(track), title.strip() or stem


def folder_has_audio(path: str) -> bool:
    """Whether a folder holds audio files, directly or in disc subfolders."""
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in AUDIO:
                return True
            if entry.is_dir() and DISC_FOLDER.match(entry.name):
                try:
                    if folder_has_audio(entry.path):
                        return True
                except OSError:
                    continue
    return False


def playlist_path(ctx: ActionContext, title: str) -> str:
    """A new playlist file named after ``title`` in the temp folder. A new name each time,
    so a player still holding the last one open doesn't stop the next."""
    stem = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', " ", title).strip(" .") or "Playlist"
    for n in range(1, 1000):
        path = ctx.temp_path(f"{stem}.m3u8" if n == 1 else f"{stem} ({n}).m3u8")
        if not os.path.exists(path):
            return path
    raise OSError(f"too many playlists named {stem!r}")


# --- tags ---


def best_bitrate(
    ctx: IngestContext,
    song: EntityRef,
    resource: ResourceInfo,
    before: Mapping[str, Any],
    read: int | None,
) -> int | None:
    """A song's bitrate after reading one of its files: the best of its versions. The
    value it had counts only while it has other files (a song's only file sets it)."""
    others = [r for r in ctx.linked(song, "audio") if r != resource.id]
    kept = before.get("bitrate") if others else None
    rates = [r for r in (kept, read) if r]
    return max(rates) if rates else None


def read_tags(path: str) -> dict[str, Any]:
    """An audio file's tags, with mutagen's common names."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("not an audio file mutagen knows")
    tags: dict[str, Any] = {}
    easy = audio.tags or {}

    def first(name: str) -> str | None:
        values = easy.get(name) if hasattr(easy, "get") else None
        if not values:
            return None
        value = str(values[0]).strip()
        return value or None

    for name in ("title", "artist", "albumartist", "album", "genre"):
        tags[name] = first(name)
    tags["track"] = _number(first("tracknumber"))
    tags["disc"] = _number(first("discnumber"))
    date = first("date")
    tags["year"] = int(date[:4]) if date and date[:4].isdigit() else None
    length = getattr(audio.info, "length", None)
    tags["duration"] = round(float(length), 1) if length else None
    bitrate = getattr(audio.info, "bitrate", None)
    tags["bitrate"] = round(bitrate / 1000) if bitrate else None  # bits a second -> kbps
    return tags


def _number(text: str | None) -> int | None:
    """``"3"`` or ``"3/12"`` → 3."""
    if not text:
        return None
    head = text.split("/")[0].strip()
    return int(head) if head.isdigit() else None
