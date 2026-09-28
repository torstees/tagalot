"""Drag and drop payloads shared by the tagging panel and the result views."""

import json
from collections.abc import Iterable

from PySide6.QtCore import QMimeData

TAG_MIME = "application/x-tagalot-tag-ids"
"""Tags being dragged: a JSON list of tag ids."""


def tags_mime(tag_ids: Iterable[int]) -> QMimeData:
    """Mime data carrying ``tag_ids`` (in order, without duplicates)."""
    mime = QMimeData()
    ids = list(dict.fromkeys(int(t) for t in tag_ids))
    mime.setData(TAG_MIME, json.dumps(ids).encode("ascii"))
    return mime


def dragged_tags(mime: QMimeData | None) -> list[int]:
    """The tag ids carried by ``mime``, or an empty list if it carries none (or garbage)."""
    if mime is None or not mime.hasFormat(TAG_MIME):
        return []
    try:
        ids = json.loads(bytes(mime.data(TAG_MIME).data()).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(ids, list) or not all(isinstance(t, int) for t in ids):
        return []
    return ids
