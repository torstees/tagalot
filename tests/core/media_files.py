# mypy: disable-error-code="no-untyped-call"
# (mutagen is untyped)
"""Tiny media files made in tests: images, and audio with embedded art."""

import io
import struct
from collections.abc import Sequence
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from PIL import Image

MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
"""One silent MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz): enough for ``mutagen``."""


def png_bytes(size: tuple[int, int] = (40, 20), color: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def write_image(path: Path, size: tuple[int, int] = (200, 100), color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def write_mp3(path: Path, pictures: Sequence[tuple[int, bytes]] = ()) -> None:
    """An MP3 with ID3 ``APIC`` frames of the given (picture type, image bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAME * 20)
    if pictures:
        tags = ID3()
        for i, (kind, data) in enumerate(pictures):
            tags.add(APIC(encoding=3, mime="image/png", type=kind, desc=f"picture {i}", data=data))
        tags.save(path)


def write_flac(path: Path, pictures: Sequence[tuple[int, bytes]] = ()) -> None:
    """A FLAC stream header with no audio frames, plus the given pictures."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = (44100 << 44) | (1 << 41) | (15 << 36)  # 44.1 kHz, 2 channels, 16 bits
    info = struct.pack(">HH", 4096, 4096) + bytes(6) + stream.to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    if pictures:
        audio = FLAC(path)
        for kind, data in pictures:
            picture = Picture()
            picture.type, picture.mime, picture.data = kind, "image/png", data
            audio.add_picture(picture)
        audio.save()
