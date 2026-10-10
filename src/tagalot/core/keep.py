"""Open and create keeps; ``keep.toml``."""

import logging
import re
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tagalot.core.fsinfo import is_network_path
from tagalot.core.tomlio import (
    toml_inline_table,
    toml_key,
    toml_list,
    toml_str,
    toml_value,
    write_atomic,
)

logger = logging.getLogger(__name__)

KEEP_TOML = "keep.toml"
KEEP_DB = "keep.db"
THUMBS_DB = "thumbs.db"
UI_STATE_JSON = "ui_state.json"
KEEP_FORMAT_VERSION = 15
"""Core format version written to new keeps (DESIGN.md §4)."""

NETWORK_WARNING = (
    "This keep is on a network drive. SQLite locking over network shares is unreliable, "
    "so the keep's database could be damaged. Keep it on a local disk if you can."
)


class KeepError(Exception):
    """A keep cannot be created or opened. The message is suitable for showing to the user."""


CONTENTS_INDEXES = ("words", "substrings")
"""What ``[contents] index`` can say (§8 *Search inside documents*); absent is Off."""
ONLINE_ANSWERS = ("allow", "never")
"""What ``[online] lookups`` can say (§9 *Online details*)."""


class KeepConfigError(KeepError):
    """A ``keep.toml`` file is missing, unreadable, or invalid."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


@dataclass
class ThemeRef:
    """The theme a keep uses and the theme schema version its database was built with."""

    id: str
    version: int


DEFAULT_EXCLUDES: tuple[str, ...] = (
    # macOS: folder metadata, AppleDouble "._" companions, and volume housekeeping.
    "**/.DS_Store",
    "**/._*",
    "**/.AppleDouble/**",
    "**/.Spotlight-V100/**",
    "**/.Trashes/**",
    "**/.fseventsd/**",
    "**/.TemporaryItems/**",
    # Windows: thumbnail caches, folder settings, and recycle bins.
    "**/Thumbs.db",
    "**/desktop.ini",
    "**/$RECYCLE.BIN/**",
    "**/System Volume Information/**",
    # NAS: Synology thumbnails and recycle bins.
    "**/@eaDir/**",
    "**/#recycle/**",
)
"""Exclude patterns new roots start with: operating-system and NAS leftovers, never user
content. They are written into ``keep.toml``, where the user can edit them."""


@dataclass
class RootConfig:
    """A watched directory. ``path`` is kept as written: it may be a UNC or drive path."""

    id: str
    name: str
    path: str
    exclude: list[str] = field(default_factory=list)
    exclude_notes: dict[str, str] = field(default_factory=dict)
    """Why a Skip pattern is there (#334): ``{pattern: note}``, written by the user, or by
    Tagalot when it skips a file itself (an export saved here, Triage's Skip). Saved as
    ``[roots.exclude_notes]``, so ``exclude`` stays a plain list an older build reads."""
    options: dict[str, Any] = field(default_factory=dict)
    """Theme options for this root only (``options = { … }``), over the keep's."""
    watched: bool = True
    """``watched = false``: not scanned; its items stay, shown offline, until it is
    watched again."""
    writable: bool = False
    """``writable = true``: **Write to file…** may change files here (DESIGN.md §4 *Writing
    back to files*). Off by default; no other root is ever written to."""


@dataclass
class KeepConfig:
    """The contents of ``keep.toml``: identity, theme, and roots."""

    id: uuid.UUID
    name: str
    theme: ThemeRef
    format_version: int = KEEP_FORMAT_VERSION
    roots: list[RootConfig] = field(default_factory=list)
    theme_options: dict[str, Any] = field(default_factory=dict)
    """``[theme.options]``: the keep's settings for its theme's options."""
    thumbnail_max: int | None = None
    """``[thumbnails] max_size``: overrides the theme's ``thumbnail_max`` for this keep."""
    thumbnails_after_scan: bool = True
    """``[thumbnails] after_scan``: make the thumbnails a scan affects in the background."""
    online_lookups: str | None = None
    contents_index: str | None = None
    """``[contents] index``: ``"words"`` or ``"substrings"`` to search inside documents;
    ``None``, Off (nothing is read)."""
    """``[online] lookups``: ``"allow"`` or ``"never"`` once the user answered (§9 *Online
    details*); ``None``, not asked yet, so nothing is looked up."""


def load_keep_config(path: Path) -> KeepConfig:
    """Read and validate a ``keep.toml`` file.

    Unknown keys are ignored with a warning so that older builds can open keeps written by
    newer ones. Raises :class:`KeepConfigError` for anything missing or malformed.
    """
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise KeepConfigError(path, f"cannot read file: {e.strerror or e}") from e
    except tomllib.TOMLDecodeError as e:
        raise KeepConfigError(path, f"invalid TOML: {e}") from e
    return _parse(path, data)


def save_keep_config(config: KeepConfig, path: Path) -> None:
    """Write ``config`` to ``path`` atomically (write a temp file, then replace)."""
    write_atomic(path, dump_keep_config(config))


def dump_keep_config(config: KeepConfig) -> str:
    """Serialize ``config`` as TOML, preferring literal strings so paths stay hand-editable."""
    lines = [
        "[keep]",
        f"id = {toml_str(str(config.id))}",
        f"name = {toml_str(config.name)}",
        f"format_version = {config.format_version}",
        "",
        "[theme]",
        f"id = {toml_str(config.theme.id)}",
        f"version = {config.theme.version}",
    ]
    if config.theme_options:
        lines += ["", "[theme.options]"]
        lines += [f"{toml_key(k)} = {toml_value(v)}" for k, v in config.theme_options.items()]
    thumbnails = []
    if config.thumbnail_max is not None:
        thumbnails.append(f"max_size = {config.thumbnail_max}")
    if not config.thumbnails_after_scan:
        thumbnails.append("after_scan = false")
    if thumbnails:
        lines += ["", "[thumbnails]", *thumbnails]
    if config.online_lookups is not None:
        lines += ["", "[online]", f"lookups = {toml_str(config.online_lookups)}"]
    if config.contents_index is not None:
        lines += ["", "[contents]", f"index = {toml_str(config.contents_index)}"]
    for root in config.roots:
        lines += [
            "",
            "[[roots]]",
            f"id = {toml_str(root.id)}",
            f"name = {toml_str(root.name)}",
            f"path = {toml_str(root.path)}",
            f"exclude = {toml_list(root.exclude)}",
        ]
        if root.options:
            lines.append(f"options = {toml_inline_table(root.options)}")
        if not root.watched:
            lines.append("watched = false")
        if root.writable:
            lines.append("writable = true")
        notes = {p: n for p, n in root.exclude_notes.items() if p in root.exclude and n}
        if notes:
            lines += ["", "[roots.exclude_notes]"]
            lines += [f"{toml_key(p)} = {toml_str(n)}" for p, n in notes.items()]
    return "\n".join(lines) + "\n"


def _parse(path: Path, data: Mapping[str, Any]) -> KeepConfig:
    reader = _Reader(path)
    reader.warn_unknown(
        data, {"keep", "theme", "roots", "thumbnails", "online", "contents"}, "top level"
    )

    keep = reader.table(data, "keep")
    reader.warn_unknown(keep, {"id", "name", "format_version"}, "[keep]")
    raw_id = reader.string(keep, "id", "[keep]")
    try:
        keep_id = uuid.UUID(raw_id)
    except ValueError as e:
        raise KeepConfigError(path, f"[keep] id is not a UUID: {raw_id!r}") from e
    name = reader.string(keep, "name", "[keep]")
    format_version = reader.integer(keep, "format_version", "[keep]")

    theme_table = reader.table(data, "theme")
    reader.warn_unknown(theme_table, {"id", "version", "options"}, "[theme]")
    theme = ThemeRef(
        id=reader.string(theme_table, "id", "[theme]"),
        version=reader.integer(theme_table, "version", "[theme]"),
    )
    theme_options = reader.options(theme_table, "[theme.options]")

    thumbnail_max = None
    after_scan = True
    if "thumbnails" in data:
        thumbnails = reader.table(data, "thumbnails")
        reader.warn_unknown(thumbnails, {"max_size", "after_scan"}, "[thumbnails]")
        if "max_size" in thumbnails:
            thumbnail_max = reader.integer(thumbnails, "max_size", "[thumbnails]")
        after_scan = thumbnails.get("after_scan", True)
        if not isinstance(after_scan, bool):
            raise KeepConfigError(path, "[thumbnails] after_scan must be true or false")

    online_lookups = None
    if "online" in data:
        online = reader.table(data, "online")
        reader.warn_unknown(online, {"lookups"}, "[online]")
        online_lookups = online.get("lookups")
        if online_lookups not in (None, *ONLINE_ANSWERS):
            raise KeepConfigError(path, '[online] lookups must be "allow" or "never"')

    contents_index = None
    if "contents" in data:
        contents = reader.table(data, "contents")
        reader.warn_unknown(contents, {"index"}, "[contents]")
        contents_index = contents.get("index")
        if contents_index not in (None, *CONTENTS_INDEXES):
            raise KeepConfigError(path, '[contents] index must be "words" or "substrings"')

    raw_roots = data.get("roots", [])
    if not isinstance(raw_roots, list) or not all(isinstance(r, dict) for r in raw_roots):
        raise KeepConfigError(path, "roots must be written as [[roots]] tables")
    roots: list[RootConfig] = []
    for i, raw in enumerate(raw_roots, start=1):
        where = f"[[roots]] #{i}"
        known = {"id", "name", "path", "exclude", "exclude_notes", "options", "watched", "writable"}
        reader.warn_unknown(raw, known, where)
        watched, writable = raw.get("watched", True), raw.get("writable", False)
        if not isinstance(watched, bool):
            raise KeepConfigError(path, f"{where} watched must be true or false")
        if not isinstance(writable, bool):
            raise KeepConfigError(path, f"{where} writable must be true or false")
        exclude = raw.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
            raise KeepConfigError(path, f"{where} exclude must be a list of strings")
        notes = raw.get("exclude_notes", {})
        if not isinstance(notes, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in notes.items()
        ):
            raise KeepConfigError(path, f"{where} exclude_notes must map patterns to notes")
        roots.append(
            RootConfig(
                id=reader.string(raw, "id", where),
                name=reader.string(raw, "name", where),
                path=reader.string(raw, "path", where),
                exclude=list(exclude),
                exclude_notes=dict(notes),
                options=reader.options(raw, f"{where} options"),
                watched=watched,
                writable=writable,
            )
        )
    seen: set[str] = set()
    for root in roots:
        if root.id in seen:
            raise KeepConfigError(path, f"duplicate root id {root.id!r}")
        seen.add(root.id)

    return KeepConfig(
        id=keep_id,
        name=name,
        theme=theme,
        format_version=format_version,
        roots=roots,
        theme_options=theme_options,
        thumbnail_max=thumbnail_max,
        thumbnails_after_scan=after_scan,
        online_lookups=online_lookups,
        contents_index=contents_index,
    )


class _Reader:
    """Typed accessors that raise :class:`KeepConfigError` with the file path and location."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def table(self, data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise KeepConfigError(self.path, f"missing [{key}] table")
        return value

    def string(self, data: Mapping[str, Any], key: str, where: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise KeepConfigError(self.path, f"{where} {key} must be a non-empty string")
        return value

    def integer(self, data: Mapping[str, Any], key: str, where: str) -> int:
        value = data.get(key)
        # bool is a subclass of int; `version = true` is a mistake, not 1.
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise KeepConfigError(self.path, f"{where} {key} must be a positive integer")
        return value

    def options(self, data: Mapping[str, Any], where: str) -> dict[str, Any]:
        """The ``options`` table of ``data``: names and scalar values. The theme checks
        the names and types when the keep opens."""
        value = data.get("options", {})
        if not isinstance(value, dict) or not all(
            isinstance(v, bool | int | float | str) for v in value.values()
        ):
            raise KeepConfigError(
                self.path, f"{where} must be a table of numbers, true/false, or strings"
            )
        return dict(value)

    def warn_unknown(self, data: Mapping[str, Any], known: set[str], where: str) -> None:
        for key in sorted(set(data) - known):
            logger.warning("%s: ignoring unknown key %r in %s", self.path, key, where)


@dataclass(frozen=True)
class Keep:
    """An open keep: its folder, its configuration, and where its files live."""

    dir: Path
    config: KeepConfig
    on_network: bool
    """The keep folder is on a network share; the UI should show :data:`NETWORK_WARNING`."""

    @property
    def toml_path(self) -> Path:
        return self.dir / KEEP_TOML

    @property
    def db_path(self) -> Path:
        return self.dir / KEEP_DB

    @property
    def thumbs_path(self) -> Path:
        return self.dir / THUMBS_DB

    @property
    def ui_state_path(self) -> Path:
        return self.dir / UI_STATE_JSON


def create_keep(
    keep_dir: Path,
    name: str,
    theme: ThemeRef,
    roots: list[RootConfig] | None = None,
    *,
    contents_index: str | None = None,
) -> Keep:
    """Create a new keep in ``keep_dir``, which must be missing or empty.

    Writes ``keep.toml`` with a fresh keep id. Raises :class:`KeepError` if the folder is
    unusable, the configuration is invalid, or the keep folder and a root contain each other
    (the scanner would index the keep's own files, and Tagalot never writes under a root).
    """
    keep_dir = keep_dir.absolute()
    if not name.strip():
        raise KeepError("A keep needs a name.")
    if keep_dir.exists():
        if not keep_dir.is_dir():
            raise KeepError(f"{keep_dir} exists and is not a folder.")
        if any(keep_dir.iterdir()):
            raise KeepError(f"{keep_dir} is not empty. Choose a new or empty folder.")
    config = KeepConfig(
        id=uuid.uuid4(),
        name=name,
        theme=theme,
        roots=list(roots or []),
        contents_index=contents_index,
    )
    _validate(config, keep_dir / KEEP_TOML)
    for root in config.roots:
        if _nested(keep_dir, Path(root.path)):
            raise KeepError(
                f"The keep folder {keep_dir} and root {root.name!r} ({root.path}) are inside "
                "one another. Put the keep somewhere outside its roots."
            )
    try:
        keep_dir.mkdir(parents=True, exist_ok=True)
        save_keep_config(config, keep_dir / KEEP_TOML)
    except OSError as e:
        raise KeepError(f"Cannot create keep in {keep_dir}: {e.strerror or e}") from e
    logger.info("Created keep %r (%s) in %s", name, config.id, keep_dir)
    return _opened(keep_dir, config)


def open_keep(keep_dir: Path) -> Keep:
    """Open the keep in ``keep_dir``. Raises :class:`KeepError` if it is not a valid keep."""
    keep_dir = keep_dir.absolute()
    if not keep_dir.is_dir():
        raise KeepError(f"{keep_dir} does not exist or is not a folder.")
    if not (keep_dir / KEEP_TOML).is_file():
        raise KeepError(f"{keep_dir} is not a keep: it has no {KEEP_TOML}.")
    config = load_keep_config(keep_dir / KEEP_TOML)
    logger.info("Opened keep %r (%s) in %s", config.name, config.id, keep_dir)
    return _opened(keep_dir, config)


def _opened(keep_dir: Path, config: KeepConfig) -> Keep:
    on_network = is_network_path(keep_dir)
    if on_network:
        logger.warning("%s: %s", keep_dir, NETWORK_WARNING)
    return Keep(dir=keep_dir, config=config, on_network=on_network)


def _validate(config: KeepConfig, path: Path) -> None:
    """Apply the same checks as loading, before anything is written."""
    _parse(path, tomllib.loads(dump_keep_config(config)))


validate_keep_config = _validate


def folder_name(folder: str) -> str:
    r"""The last segment of a folder as typed, with either slash style.

    ``Path`` can't be used: for a share root like ``\\nas\music`` its ``name`` is empty
    (Windows treats the share as a drive root).
    """
    parts = [p for p in re.split(r"[\\/]+", folder.strip()) if p]
    return parts[-1] if parts else folder.strip()


def root_id_for(folder: str, taken: Iterable[str] = ()) -> str:
    """A stable, readable root id from a folder: ``"D:/My Photos"`` -> ``"my-photos"``;
    ``-2``, ``-3``… are added to avoid the ids in ``taken``."""
    slug = re.sub(r"[^a-z0-9]+", "-", folder_name(folder).lower()).strip("-") or "root"
    used = set(taken)
    candidate, n = slug, 1
    while candidate in used:
        n += 1
        candidate = f"{slug}-{n}"
    return candidate


def nested_paths(a: Path, b: Path) -> bool:
    """Whether either path is inside (or equal to) the other, compared as the OS would."""
    return _nested(a, b)


def _nested(a: Path, b: Path) -> bool:
    """Whether either path is inside (or equal to) the other, compared as the OS would."""
    try:
        ra, rb = a.resolve(), b.resolve()
    except OSError:
        ra, rb = a.absolute(), b.absolute()
    return ra == rb or ra.is_relative_to(rb) or rb.is_relative_to(ra)
