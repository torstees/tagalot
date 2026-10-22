"""Tags' pictures (#506, DESIGN.md §7 *Tag pictures*): a picture that says visually what a
tag means, as its description does in words, shown above the description in its tooltips.

The picture is copied into the keep (``tag_picture``), never linked to a file, so it can't
go missing or offline: :func:`prepare_picture` reads the chosen file and keeps a copy at
most :data:`STORED_SIZE` pixels on its longer side. The tag tree carries the pictures
(:meth:`~tagalot.core.tags.TagTree.picture`), for tooltips.

A tag's icon can be a small picture too (#507), in place of an emoji: :func:`prepare_icon`
keeps it at most :data:`ICON_SIZE` pixels, and an animated GIF stays animated (tag trees
play it; badges on items show its first frame).
"""

import io

from PIL import Image, ImageOps, ImageSequence
from sqlalchemy import Connection, delete, insert

from tagalot.core.models import TagPicture
from tagalot.core.tags import PictureData, TagError

STORED_SIZE = 480
"""The longest side of a kept picture: twice the tooltip's, for high-density screens."""
PICTURE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
"""Files a tag's picture can be chosen from."""
ICON_SIZE = 64
"""The longest side of a kept icon picture: a badge or a tree's icon is about a line of
text high, and this leaves room for high-density screens."""
MAX_ICON_FILE = 2 * 1024 * 1024
"""The biggest animated GIF kept as an icon, after shrinking."""


def prepare_picture(raw: bytes) -> PictureData:
    """A picture file's bytes as a tag keeps them: turned upright, at most
    :data:`STORED_SIZE` pixels on its longer side, PNG when it has transparency, else JPEG.
    Raises :class:`~tagalot.core.tags.TagError` for something that isn't a picture."""
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.seek(0)  # an animated picture: its first frame
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((STORED_SIZE, STORED_SIZE))
            clear = image.mode in ("RGBA", "LA", "P") and (
                image.mode != "P" or "transparency" in image.info
            )
            out = io.BytesIO()
            if clear:
                image.convert("RGBA").save(out, "PNG", optimize=True)
            else:
                image.convert("RGB").save(out, "JPEG", quality=88)
            width, height = image.size
    except (OSError, ValueError) as e:
        raise TagError(f"That isn't a picture Tagalot can read: {e}") from e
    return PictureData(out.getvalue(), width, height)


def prepare_icon(raw: bytes) -> PictureData:
    """A picture file's bytes as a tag's icon (#507): at most :data:`ICON_SIZE` pixels, as
    PNG; an animated GIF stays an animated GIF (as it is when it is small enough, else each
    frame shrunk). Raises :class:`~tagalot.core.tags.TagError` for something that isn't a
    picture, or an animation too big to keep."""
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.format == "GIF" and getattr(opened, "n_frames", 1) > 1:
                return _animated_icon(raw, opened)
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((ICON_SIZE, ICON_SIZE))
            out = io.BytesIO()
            image.convert("RGBA").save(out, "PNG", optimize=True)
            width, height = image.size
    except (OSError, ValueError, EOFError) as e:
        raise TagError(f"That isn't a picture Tagalot can read: {e}") from e
    return PictureData(out.getvalue(), width, height)


def _animated_icon(raw: bytes, opened: Image.Image) -> PictureData:
    width, height = opened.size
    if max(width, height) <= ICON_SIZE:
        data = raw  # small enough already: as it is
    else:
        frames, durations = [], []
        for frame in ImageSequence.Iterator(opened):
            shrunk = frame.convert("RGBA")
            shrunk.thumbnail((ICON_SIZE, ICON_SIZE))
            frames.append(shrunk)
            durations.append(frame.info.get("duration", 100))
        width, height = frames[0].size
        out = io.BytesIO()
        frames[0].save(
            out,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=opened.info.get("loop", 0),
            disposal=2,
        )
        data = out.getvalue()
    if len(data) > MAX_ICON_FILE:
        raise TagError("That animation is too big for an icon.")
    return PictureData(data, width, height)


def set_tag_picture(conn: Connection, tag_id: int, picture: PictureData | None) -> None:
    """Give a tag a picture, or clear it (``None``)."""
    conn.execute(delete(TagPicture).where(TagPicture.tag_id == tag_id))
    if picture is not None:
        conn.execute(
            insert(TagPicture).values(
                tag_id=tag_id, data=picture.data, width=picture.width, height=picture.height
            )
        )
