"""Tags' pictures (#506, DESIGN.md §7 *Tag pictures*): a picture that says visually what a
tag means, as its description does in words, shown above the description in its tooltips.

The picture is copied into the keep (``tag_picture``), never linked to a file, so it can't
go missing or offline: :func:`prepare_picture` reads the chosen file and keeps a copy at
most :data:`STORED_SIZE` pixels on its longer side. The tag tree carries the pictures
(:meth:`~tagalot.core.tags.TagTree.picture`), for tooltips.
"""

import io

from PIL import Image, ImageOps
from sqlalchemy import Connection, delete, insert

from tagalot.core.models import TagPicture
from tagalot.core.tags import PictureData, TagError

STORED_SIZE = 480
"""The longest side of a kept picture: twice the tooltip's, for high-density screens."""
PICTURE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
"""Files a tag's picture can be chosen from."""


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


def set_tag_picture(conn: Connection, tag_id: int, picture: PictureData | None) -> None:
    """Give a tag a picture, or clear it (``None``)."""
    conn.execute(delete(TagPicture).where(TagPicture.tag_id == tag_id))
    if picture is not None:
        conn.execute(
            insert(TagPicture).values(
                tag_id=tag_id, data=picture.data, width=picture.width, height=picture.height
            )
        )
