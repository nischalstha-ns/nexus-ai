"""Minimal stdlib PNG encoder/decoder (8-bit RGB/RGBA, no interlace).

Enough to generate test screenshots and to validate captures without
Pillow. Not a general image library — keep it small and correct.
"""
from __future__ import annotations

import struct
import zlib


def _chunk(ctype: bytes, data: bytes) -> bytes:
    out = struct.pack(">I", len(data)) + ctype + data
    out += struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    return out


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode raw RGB bytes (width*height*3) as PNG."""
    assert len(rgb) == width * height * 3, "rgb buffer size mismatch"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b""))


def decode_png_size(png: bytes) -> tuple[int, int]:
    """Read width/height from the IHDR chunk. Raises ValueError if invalid."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    length = struct.unpack(">I", png[8:12])[0]
    ctype = png[12:16]
    if ctype != b"IHDR" or length != 13:
        raise ValueError("bad IHDR")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def test_card(width: int = 320, height: int = 200) -> bytes:
    """Generate a deterministic gradient test image (for mocks/tests)."""
    px = bytearray()
    for y in range(height):
        for x in range(width):
            px += bytes((x * 255 // width, y * 255 // height,
                         (x + y) * 255 // (width + height)))
    return encode_png(width, height, bytes(px))
