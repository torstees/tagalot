"""Making thumbnail images (DESIGN.md §10 "Cache").

:func:`make_thumbnail` turns a decoded image into the bytes the cache stores: fitted inside a
square of the thumbnail size (never enlarged), turned upright by its EXIF orientation, with
transparency kept, encoded as WebP (PNG if this Pillow can't write WebP).
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, features

WEBP_QUALITY = 80
"""Lossy WebP quality: small files, no visible loss at thumbnail sizes."""


@dataclass(frozen=True)
class Thumbnail:
    """An encoded thumbnail: its bytes, format (``"webp"`` or ``"png"``), and pixel size."""

    data: bytes
    format: str
    width: int
    height: int


def output_format() -> str:
    """The format thumbnails are written in: WebP when this Pillow supports it."""
    return "webp" if features.check("webp") else "png"


def make_thumbnail(image: Image.Image, size: int) -> Thumbnail:
    """Fit ``image`` inside a ``size`` by ``size`` square of pixels and encode it.

    The image is turned upright first (EXIF orientation), then shrunk keeping its aspect
    ratio (at least 1 pixel on each side); a smaller image isn't enlarged. Images with
    transparency stay transparent; other modes (CMYK, 16-bit, palette) become RGB or RGBA.
    """
    if size < 1:
        raise ValueError(f"thumbnail size must be positive, not {size}")
    image = ImageOps.exif_transpose(image) or image
    image = _displayable(image)
    image.thumbnail((size, size), Image.Resampling.LANCZOS, reducing_gap=3.0)
    fmt = output_format()
    buffer = io.BytesIO()
    if fmt == "webp":
        image.save(buffer, "WEBP", quality=WEBP_QUALITY, method=4)
    else:
        image.save(buffer, "PNG", optimize=True)
    return Thumbnail(buffer.getvalue(), fmt, image.width, image.height)


def _displayable(image: Image.Image) -> Image.Image:
    """``image`` as RGB, or RGBA when it has any transparency."""
    if image.mode in ("RGB", "RGBA"):
        return image
    has_alpha = image.mode in ("LA", "PA", "La") or (
        image.mode == "P" and "transparency" in image.info
    )
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16/32-bit grayscale: scale into 8 bits rather than clipping to white.
        image = image.point(lambda value: value / 256).convert("L")
    return image.convert("RGBA" if has_alpha else "RGB")
