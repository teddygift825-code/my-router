#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate app_icon.ico + app_icon.png — stdlib only (no PIL).

Draws: dark rounded square, Wi-Fi arcs, two antennas, router body
with status LEDs.  .ico bundles 16/24/32/48/64/128/256 px.
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))

NAVY      = (13, 22, 38)      # RGB, bottom of gradient
NAVY_TOP  = (24, 40, 66)      # RGB, top of gradient
TEAL_FILL = (18, 70, 110)
TEAL_EDGE = (76, 194, 255)
GREEN     = (63, 185, 80)
AMBER     = (227, 179, 65)
GREY      = (159, 179, 200)
GOLD      = (240, 170, 60)

SIZES = (16, 24, 32, 48, 64, 128, 256)


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _alpha_at(x, y, size, r):
    """255 inside the rounded square, 0 in the four cut corners."""
    corners = ((0, 0), (size, 0), (0, size), (size, size))
    for cx, cy in corners:
        bx0 = 0.0 if cx == 0 else size - r
        by0 = 0.0 if cy == 0 else size - r
        if bx0 <= x <= bx0 + r and by0 <= y <= by0 + r:
            ccx = bx0 + (r if cx == 0 else 0.0)
            ccy = by0 + (r if cy == 0 else 0.0)
            dx, dy = x - ccx, y - ccy
            if dx * dx + dy * dy > r * r:
                return 0
    return 255


def _clamp(v):
    return 0 if v < 0 else 255 if v > 255 else int(v)


def pixel(tx, ty, size):
    """BGRA bytes for pixel (tx, ty), ty measured top-down."""
    u = size / 16.0
    x, y = tx + 0.5, ty + 0.5
    r = 3.0 * u
    alpha = _alpha_at(x, y, size, r)
    if alpha == 0:
        return (0, 0, 0, 0)

    rgb = _lerp(NAVY_TOP, NAVY, min(1.0, ty / max(1.0, size - 1)))

    # Wi-Fi arcs centred above the router body
    cx, cy = 8.0 * u, 4.4 * u
    d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    for rad0, rad1 in ((1.8 * u, 2.5 * u), (3.0 * u, 3.7 * u),
                       (4.2 * u, 4.9 * u)):
        if rad0 <= d <= rad1 and (cy - y) >= 0.45 * d and y <= cy:
            rgb = GOLD
    # antenna posts + gold tips
    for ax in (4.0 * u, 12.0 * u):
        if abs(x - ax) <= 0.45 * u:
            if 4.2 * u <= y <= 10.4 * u:
                rgb = GREY
            elif 3.2 * u <= y < 4.2 * u:
                rgb = GOLD
    # router body
    bx0, bx1, by0, by1 = 2.2 * u, 13.8 * u, 10.4 * u, 13.8 * u
    if bx0 <= x <= bx1 and by0 <= y <= by1:
        edge = min(x - bx0, bx1 - x, y - by0, by1 - y)
        rgb = TEAL_EDGE if edge <= 0.55 * u else TEAL_FILL
        # status LEDs
        for lx, col in ((4.6 * u, GREEN), (11.4 * u, AMBER)):
            if (x - lx) ** 2 + (y - 12.1 * u) ** 2 <= (0.62 * u) ** 2:
                rgb = col

    return (_clamp(rgb[2]), _clamp(rgb[1]), _clamp(rgb[0]), alpha)


def render_bgra(size):
    """Raw 32-bpp BMP pixel data (bottom-up rows) + 1-bpp AND mask."""
    stride = size * 4
    topdown = bytearray(stride * size)
    for ty in range(size):
        base = ty * stride
        for tx in range(size):
            off = base + tx * 4
            topdown[off:off + 4] = bytes(pixel(tx, ty, size))
    rows = bytearray()
    for ty in range(size - 1, -1, -1):          # BMP rows go bottom-up
        rows += topdown[ty * stride:(ty + 1) * stride]
    mask_stride = ((size + 31) // 32) * 4       # 1-bpp rows, 32-bit aligned
    mask = bytearray()
    for ty in range(size - 1, -1, -1):
        line = bytearray(mask_stride)
        for tx in range(size):
            if pixel(tx, ty, size)[3] == 0:
                line[tx // 8] |= 0x80 >> (tx % 8)
        mask += line
    return bytes(rows), bytes(mask)


def bitmap_header(size, image_bytes):
    return struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                       len(image_bytes), 0, 0, 0, 0)


def write_ico(path):
    blocks, entries, offset = [], [], 6 + 16 * len(SIZES)
    for size in SIZES:
        pix, mask = render_bgra(size)
        blob = bitmap_header(size, pix + mask) + pix + mask
        entries.append((size, len(blob), offset))
        blocks.append(blob)
        offset += len(blob)
    ico = struct.pack("<HHH", 0, 1, len(SIZES))
    for size, blen, boff in entries:
        ico += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                           1, 32, blen, boff)
    for blob in blocks:
        ico += blob
    with open(path, "wb") as fh:
        fh.write(ico)


def _png_chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload +
            struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def write_png(path, size=128):
    raw = bytearray()
    for ty in range(size):
        raw.append(0)                            # filter: none
        for tx in range(size):
            raw += bytes(pixel(tx, ty, size))
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6,
                                             0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + _png_chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def main():
    ico_path = os.path.join(HERE, "app_icon.ico")
    png_path = os.path.join(HERE, "app_icon.png")
    write_ico(ico_path)
    write_png(png_path)
    print(f"OK wrote {ico_path} ({os.path.getsize(ico_path)} bytes)")
    print(f"OK wrote {png_path} ({os.path.getsize(png_path)} bytes)")


if __name__ == "__main__":
    main()
