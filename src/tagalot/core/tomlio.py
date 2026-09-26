"""Helpers for Tagalot's own hand-editable TOML files (``keep.toml``, ``settings.toml``)."""

import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

import tomli_w

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def toml_str(value: str) -> str:
    """Format a TOML string: a literal string if possible, else an escaped basic string.

    Literal strings keep Windows and UNC paths readable (``'\\\\nas\\music'``).
    """
    if "'" not in value and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return f"'{value}'"
    return tomli_w.dumps({"v": value}).removeprefix("v = ").rstrip("\n")


def toml_key(key: str) -> str:
    """Format a TOML key, quoting it unless it is a valid bare key."""
    return key if _BARE_KEY.fullmatch(key) else toml_str(key)


def toml_list(values: Iterable[str]) -> str:
    """Format a list of strings on one line."""
    return f"[{', '.join(toml_str(v) for v in values)}]"


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with LF endings via a temp file in the same folder, then replace.

    A crash mid-write leaves the previous file intact.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
