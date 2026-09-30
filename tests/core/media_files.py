# mypy: disable-error-code="no-untyped-call"
# (mutagen is untyped)
"""Tiny media files made in tests: images, and audio with embedded art."""

import base64
import inspect
import io
import re
import struct
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from PIL import Image, ImageFont

MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
"""One silent MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz): enough for ``mutagen``."""


def png_bytes(size: tuple[int, int] = (40, 20), color: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def write_image(path: Path, size: tuple[int, int] = (200, 100), color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def write_mp3(
    path: Path,
    pictures: Sequence[tuple[int, bytes]] = (),
    tags: Mapping[str, str] | None = None,
    frames: int = 20,
) -> None:
    """An MP3 with ID3 ``APIC`` frames of the given (picture type, image bytes), and ``tags``
    by mutagen's easy names (``title``, ``albumartist``, ``tracknumber``…). Each frame is
    about 26 ms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAME * frames)
    if pictures:
        id3 = ID3()
        for i, (kind, data) in enumerate(pictures):
            id3.add(APIC(encoding=3, mime="image/png", type=kind, desc=f"picture {i}", data=data))
        id3.save(path)
    if tags:
        easy = EasyID3(path) if pictures else EasyID3()
        for name, value in tags.items():
            easy[name] = value
        easy.save(path)


def write_flac(
    path: Path,
    pictures: Sequence[tuple[int, bytes]] = (),
    tags: Mapping[str, str] | None = None,
    seconds: int = 0,
) -> None:
    """A FLAC stream header with no audio frames (claiming ``seconds`` of audio), plus the
    given pictures and Vorbis comment ``tags``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = (44100 << 44) | (1 << 41) | (15 << 36) | (seconds * 44100)  # 44.1 kHz, 2 ch, 16 bit
    info = struct.pack(">HH", 4096, 4096) + bytes(6) + stream.to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    if pictures:
        audio = FLAC(path)
        for kind, data in pictures:
            picture = Picture()
            picture.type, picture.mime, picture.data = kind, "image/png", data
            audio.add_picture(picture)
        audio.save()
    if tags:
        audio = FLAC(path)
        for name, value in tags.items():
            audio[name] = value
        audio.save()


def font_bytes() -> bytes:
    """A small TrueType font (Aileron Regular, SIL Open Font License), the one Pillow embeds
    for ``ImageFont.load_default()``, so tests need no font file of their own."""
    source = inspect.getsource(ImageFont.load_default)
    found = re.search(r'b"""(.*?)"""', source, re.DOTALL)
    assert found is not None, "Pillow no longer embeds its default font"
    return base64.b64decode(found.group(1))


def write_font(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(font_bytes())


def psd_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    """A minimal Photoshop file: 8-bit RGB, no layers, an uncompressed composite image (what
    Pillow reads). Pillow can't write PSD, so the bytes are assembled here."""
    width, height = size
    header = b"8BPS" + struct.pack(">H6sHIIHH", 1, bytes(6), 3, height, width, 8, 3)
    empty_sections = struct.pack(">III", 0, 0, 0)  # color mode, resources, layers
    planes = b"".join(bytes([c]) * (width * height) for c in color)
    return header + empty_sections + struct.pack(">H", 0) + planes


def woff_bytes(ttf: bytes) -> bytes:
    """A TrueType font rewrapped as WOFF 1.0 (zlib-compressed tables), per the W3C spec."""
    flavor, count = struct.unpack(">IH", ttf[:6])
    tables = []
    for i in range(count):
        tag, checksum, offset, length = struct.unpack(">4sIII", ttf[12 + 16 * i : 28 + 16 * i])
        tables.append((tag, checksum, ttf[offset : offset + length]))

    def padded(n: int) -> int:
        return (n + 3) & ~3

    start = 44 + 20 * count
    directory, blobs = b"", b""
    for tag, checksum, data in tables:
        packed = zlib.compress(data)
        if len(packed) >= len(data):
            packed = data  # stored as is when compression doesn't help
        directory += struct.pack(
            ">4sIIII", tag, start + len(blobs), len(packed), len(data), checksum
        )
        blobs += packed + bytes(padded(len(packed)) - len(packed))
    sfnt_size = 12 + 16 * count + sum(padded(len(d)) for _, _, d in tables)
    header = struct.pack(
        ">4sIIHHIHHIIIII",
        b"wOFF",
        flavor,
        44 + len(directory) + len(blobs),
        count,
        0,
        sfnt_size,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return header + directory + blobs
