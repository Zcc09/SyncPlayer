#!/usr/bin/env python3
"""Generate SyncPlayer's app icon (icon.png 256x256 + icon.ico) with pure stdlib.

Design: dark rounded tile, two overlapping rounded rectangles (blue = movie,
teal = reaction) with a play triangle in the middle (sync player).
"""
import os
import struct
import zlib

SIZE = 256

BG = (20, 22, 28, 255)          # near-black
TILE_A = (79, 156, 249, 255)    # blue
TILE_B = (57, 217, 142, 255)    # teal
WHITE = (255, 255, 255, 255)


def in_rounded_rect(px, py, x, y, w, h, r):
    if not (x <= px < x + w and y <= py < y + h):
        return False
    cx = min(max(px, x + r), x + w - r - 1)
    cy = min(max(py, y + r), y + h - r - 1)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def in_triangle(px, py, a, b, c):
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1 = sign((px, py), a, b)
    d2 = sign((px, py), b, c)
    d3 = sign((px, py), c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def pixel(px, py):
    # background tile
    if not in_rounded_rect(px, py, 6, 6, SIZE - 12, SIZE - 12, 56):
        return (0, 0, 0, 0)
    # two video tiles
    in_a = in_rounded_rect(px, py, 38, 54, 130, 148, 26)
    in_b = in_rounded_rect(px, py, 88, 54, 130, 148, 26)
    if in_a and in_b:
        # overlap: blend the two accent colors
        r = (TILE_A[0] + TILE_B[0]) // 2
        g = (TILE_A[1] + TILE_B[1]) // 2
        b = (TILE_A[2] + TILE_B[2]) // 2
        c = (r, g, b, 255)
    elif in_a:
        c = TILE_A
    elif in_b:
        c = TILE_B
    else:
        return BG
    # play triangle centered on the overlap
    if in_triangle(px, py, (112, 96), (112, 160), (168, 128)):
        return WHITE
    # subtle darker top edge on the tiles for depth
    if py < 70 and (in_a or in_b):
        c = (int(c[0] * 0.82), int(c[1] * 0.82), int(c[2] * 0.82), 255)
    return c


def render():
    rows = []
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            r, g, b, a = pixel(px, py)
            row += bytes((r, g, b, a))
        rows.append(bytes(row))
    return b"".join(rows)


def png_encode(raw_rgba, w, h):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"".join(b"\x00" + raw_rgba[y * w * 4:(y + 1) * w * 4] for y in range(h)), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def ico_encode(png_bytes, size):
    header = struct.pack("<HHH", 0, 1, 1)
    dim = 0 if size >= 256 else size
    entry = struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png_bytes), 6 + 16)
    return header + entry + png_bytes


HERE = os.path.dirname(os.path.abspath(__file__))
held = render()
png = png_encode(held, SIZE, SIZE)
with open(os.path.join(HERE, "icon.png"), "wb") as f:
    f.write(png)
with open(os.path.join(HERE, "icon.ico"), "wb") as f:
    f.write(ico_encode(png, SIZE))
print("icon.png", len(png), "bytes ; icon.ico", len(png) + 22, "bytes")