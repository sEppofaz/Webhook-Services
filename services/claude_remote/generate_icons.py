#!/usr/bin/env python3
"""Generiert icon-192.png und icon-512.png für Claude Remote.
Ausführen auf dem Server nach dem Deployen:
  cd /opt/rename-webhook && python3 services/claude_remote/generate_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "static"

BG = (124, 58, 237)
FG = (255, 255, 255)


def make_canvas(size):
    return [BG] * (size * size)


def set_pixel(canvas, size, x, y, color):
    if 0 <= x < size and 0 <= y < size:
        canvas[y * size + x] = color


def fill_circle(canvas, size, cx, cy, r, color):
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                set_pixel(canvas, size, cx + dx, cy + dy, color)


def draw_arc(canvas, size, cx, cy, radius, stroke, color):
    for angle_deg in range(225, 495):
        angle = math.radians(angle_deg)
        for t in range(-stroke // 2, stroke // 2 + 1):
            r = radius + t
            x = int(round(cx + r * math.cos(angle)))
            y = int(round(cy - r * math.sin(angle)))
            set_pixel(canvas, size, x, y, color)


def draw_wifi(canvas, size):
    cx = size // 2
    cy = int(size * 0.58)
    dot_r = max(3, size // 20)
    stroke = max(2, size // 28)
    gaps = [size // 10, size // 6, size // 4]

    fill_circle(canvas, size, cx, cy, dot_r, FG)
    for radius in gaps:
        draw_arc(canvas, size, cx, cy, radius, stroke, FG)


def encode_png(canvas, size):
    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b""
    for row in range(size):
        raw += b"\x00"
        for col in range(size):
            raw += bytes(canvas[row * size + col])

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    print("Generiere Claude-Remote-Icons ...")
    for size in (192, 512):
        canvas = make_canvas(size)
        draw_wifi(canvas, size)
        data = encode_png(canvas, size)
        out = OUT / f"icon-{size}.png"
        out.write_bytes(data)
        print(f"  {out.name} ({size}x{size}, {len(data)} Bytes)")
    print("Fertig.")
