"""A minimal GeoTIFF reader for sampling single pixels of a float32 grid.

Covers the NOHRSC National Snowfall Analysis files: one band, `float32`,
strips, no compression or LZW, no predictor, georeferenced by
`ModelTiepointTag` + `ModelPixelScaleTag`, nodata in `GDAL_NODATA`. Only the
strips holding the requested pixels are decoded, so a lookup at a handful of
points needs no array library. Anything else raises `TiffError`.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any

# TIFF field types -> (struct code, size)
_TYPES: dict[int, tuple[str, int]] = {
    1: ("B", 1),
    2: ("c", 1),
    3: ("H", 2),
    4: ("I", 4),
    5: ("II", 8),
    11: ("f", 4),
    12: ("d", 8),
    16: ("Q", 8),
}

WIDTH, HEIGHT, BITS, COMPRESSION = 256, 257, 258, 259
STRIP_OFFSETS, SAMPLES, ROWS_PER_STRIP, STRIP_BYTES = 273, 277, 278, 279
PLANAR, PREDICTOR, TILE_WIDTH, SAMPLE_FORMAT = 284, 317, 322, 339
PIXEL_SCALE, TIEPOINT, GDAL_NODATA = 33550, 33922, 42113

NO_COMPRESSION, LZW = 1, 5
LZW_CLEAR, LZW_EOI = 256, 257


class TiffError(ValueError):
    """The file is not a GeoTIFF this reader supports."""


def lzw_decode(data: bytes) -> bytes:
    """Decode TIFF LZW (MSB-first codes, 9 to 12 bits, early change)."""
    out = bytearray()
    table: list[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    nbits = 9
    prev: bytes | None = None
    total_bits = len(data) * 8
    pos = 0
    while pos + nbits <= total_bits:
        start = pos >> 3
        chunk = int.from_bytes(data[start : start + 4].ljust(4, b"\0"), "big")
        code = (chunk >> (32 - (pos & 7) - nbits)) & ((1 << nbits) - 1)
        pos += nbits
        if code == LZW_CLEAR:
            del table[258:]
            nbits = 9
            prev = None
            continue
        if code == LZW_EOI:
            break
        if prev is None:
            if code >= len(table):
                raise TiffError(f"LZW code {code} before any table entry")
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            table.append(entry)
        else:
            raise TiffError(f"LZW code {code} out of range")
        out += entry
        prev = entry
        if len(table) >= (1 << nbits) - 1 and nbits < 12:
            nbits += 1
    return bytes(out)


@dataclass(frozen=True)
class Grid:
    """A parsed single-band float32 GeoTIFF; pixels decode on demand."""

    data: bytes
    order: str  # "<" or ">"
    width: int
    height: int
    compression: int
    rows_per_strip: int
    strip_offsets: tuple[int, ...]
    strip_bytes: tuple[int, ...]
    origin_x: float  # longitude of the upper-left corner of pixel (0, 0)
    origin_y: float  # latitude of the upper-left corner of pixel (0, 0)
    scale_x: float
    scale_y: float
    nodata: float | None

    def pixel_at(self, lat: float, lon: float) -> tuple[int, int] | None:
        """The (row, col) whose cell contains the point, or None outside the grid."""
        col = math.floor((lon - self.origin_x) / self.scale_x)
        row = math.floor((self.origin_y - lat) / self.scale_y)
        if 0 <= row < self.height and 0 <= col < self.width:
            return row, col
        return None

    def _strip(self, index: int) -> bytes:
        offset, size = self.strip_offsets[index], self.strip_bytes[index]
        raw = self.data[offset : offset + size]
        return lzw_decode(raw) if self.compression == LZW else raw

    def value(self, row: int, col: int) -> float | None:
        strip = self._strip(row // self.rows_per_strip)
        at = ((row % self.rows_per_strip) * self.width + col) * 4
        if at + 4 > len(strip):
            raise TiffError(f"strip for row {row} is short ({len(strip)} bytes)")
        (value,) = struct.unpack_from(f"{self.order}f", strip, at)
        if math.isnan(value) or (self.nodata is not None and value == self.nodata):
            return None
        return float(value)

    def sample(self, lat: float, lon: float) -> float | None:
        """The value of the cell containing the point; None for nodata or outside."""
        pixel = self.pixel_at(lat, lon)
        return None if pixel is None else self.value(*pixel)


def _tags(data: bytes, order: str) -> dict[int, tuple[Any, ...]]:
    (ifd,) = struct.unpack_from(f"{order}I", data, 4)
    (count,) = struct.unpack_from(f"{order}H", data, ifd)
    tags: dict[int, tuple[Any, ...]] = {}
    for i in range(count):
        tag, kind, n = struct.unpack_from(f"{order}HHI", data, ifd + 2 + 12 * i)
        if kind not in _TYPES:
            continue
        code, size = _TYPES[kind]
        nbytes = size * n
        at = ifd + 2 + 12 * i + 8
        if nbytes > 4:
            (at,) = struct.unpack_from(f"{order}I", data, at)
        if kind == 2:
            tags[tag] = (data[at : at + n].rstrip(b"\0").decode("ascii", "replace"),)
        else:
            tags[tag] = struct.unpack_from(f"{order}{code * n}", data, at)
    return tags


def read(data: bytes) -> Grid:
    """Parse the header of a single-band float32 GeoTIFF."""
    if data[:4] == b"II*\0":
        order = "<"
    elif data[:4] == b"MM\0*":
        order = ">"
    else:
        raise TiffError("not a classic TIFF file")
    tags = _tags(data, order)

    def one(tag: int, default: int | None = None) -> int:
        if tag not in tags:
            if default is None:
                raise TiffError(f"missing TIFF tag {tag}")
            return default
        return int(tags[tag][0])

    if TILE_WIDTH in tags:
        raise TiffError("tiled TIFFs are not supported")
    if one(SAMPLES, 1) != 1 or one(PLANAR, 1) != 1:
        raise TiffError("only single-band TIFFs are supported")
    if one(BITS) != 32 or one(SAMPLE_FORMAT, 1) != 3:
        raise TiffError("only float32 samples are supported")
    compression = one(COMPRESSION, NO_COMPRESSION)
    if compression not in (NO_COMPRESSION, LZW):
        raise TiffError(f"compression {compression} is not supported")
    if one(PREDICTOR, 1) != 1:
        raise TiffError("TIFF predictors are not supported")
    if PIXEL_SCALE not in tags or TIEPOINT not in tags:
        raise TiffError("not georeferenced (no pixel scale or tiepoint)")
    scale = [float(v) for v in tags[PIXEL_SCALE]]
    tie = [float(v) for v in tags[TIEPOINT]]
    nodata: float | None = None
    if GDAL_NODATA in tags:
        try:
            nodata = float(str(tags[GDAL_NODATA][0]).strip())
        except ValueError:
            nodata = None
    height = one(HEIGHT)
    return Grid(
        data=data,
        order=order,
        width=one(WIDTH),
        height=height,
        compression=compression,
        rows_per_strip=min(one(ROWS_PER_STRIP, height), height),
        strip_offsets=tuple(int(v) for v in tags[STRIP_OFFSETS]),
        strip_bytes=tuple(int(v) for v in tags[STRIP_BYTES]),
        origin_x=tie[3] - tie[0] * scale[0],
        origin_y=tie[4] + tie[1] * scale[1],
        scale_x=scale[0],
        scale_y=scale[1],
        nodata=nodata,
    )
