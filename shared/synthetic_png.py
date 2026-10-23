"""Deterministic synthetic PNG images (no imaging library needed).

`blurry_document_png` draws a grey page with smeared, unreadable "text" bands, standing in
for a badly photographed bill. Same input, same bytes, same SHA-256.
"""

from __future__ import annotations

import math
import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_grayscale(width: int, height: int, pixel: callable) -> bytes:  # type: ignore[valid-type]
    """Encode an 8-bit greyscale image whose pixels come from `pixel(x, y) -> 0..255`."""
    raw = b"".join(b"\x00" + bytes(max(0, min(255, int(pixel(x, y)))) for x in range(width)) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def blurry_document_png(width: int = 360, height: int = 240) -> bytes:
    """A smeared, unreadable 'invoice photo'."""
    def pixel(x: int, y: int) -> float:
        paper = 205 + 18 * math.sin(x / 47.0) * math.cos(y / 39.0)
        band = (y // 22) % 2 == 0 and 30 < x < width - 30 and 20 < y < height - 20
        smear = 60 * (0.5 + 0.5 * math.sin(x / 6.5 + y / 9.0)) if band else 0
        return paper - smear
    return png_grayscale(width, height, pixel)
