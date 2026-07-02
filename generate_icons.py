#!/usr/bin/env python3
"""
Generiert icon-192.png, icon-512.png und apple-touch-icon.png
für /opt/project-insight/ aus dem eingebetteten Claude Remote App SVG.
Ausführen mit: python3 /opt/rename-webhook/generate_icons.py
"""

import subprocess
import sys
import os

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1a1a1a"/>
      <stop offset="100%" stop-color="#2d2d2d"/>
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="48%" r="42%">
      <stop offset="0%" stop-color="#CC785C" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="#CC785C" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="sym" x1="20%" y1="0%" x2="80%" y2="100%">
      <stop offset="0%" stop-color="#E8956D"/>
      <stop offset="100%" stop-color="#B85C3A"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#bg)"/>
  <ellipse cx="256" cy="248" rx="180" ry="165" fill="url(#glow)"/>
  <g transform="translate(256,248)">
    <rect x="-16" y="-148" width="32" height="90" rx="16" fill="url(#sym)" opacity="1.0"/>
    <rect x="-16" y="-148" width="32" height="82" rx="16" fill="url(#sym)" opacity="0.88" transform="rotate(45)"/>
    <rect x="-16" y="-148" width="32" height="74" rx="16" fill="url(#sym)" opacity="0.76" transform="rotate(90)"/>
    <rect x="-16" y="-148" width="32" height="66" rx="16" fill="url(#sym)" opacity="0.64" transform="rotate(135)"/>
    <rect x="-16" y="-148" width="32" height="58" rx="16" fill="url(#sym)" opacity="0.52" transform="rotate(180)"/>
    <rect x="-16" y="-148" width="32" height="50" rx="16" fill="url(#sym)" opacity="0.44" transform="rotate(225)"/>
    <rect x="-16" y="-148" width="32" height="42" rx="16" fill="url(#sym)" opacity="0.38" transform="rotate(270)"/>
    <rect x="-16" y="-148" width="32" height="34" rx="16" fill="url(#sym)" opacity="0.30" transform="rotate(315)"/>
    <circle r="28" fill="#CC785C"/>
    <circle r="18" fill="#1a1a1a"/>
  </g>
  <g transform="translate(380,390)" stroke="#CC785C" stroke-linecap="round" fill="none" opacity="0.7">
    <path d="M -28 0 A 28 28 0 0 1 28 0" stroke-width="9"/>
    <path d="M -17 -14 A 17 17 0 0 1 17 -14" stroke-width="7.5"/>
    <circle cx="0" cy="-27" r="5.5" fill="#CC785C" stroke="none"/>
  </g>
</svg>"""

TARGET_DIR = "/opt/project-insight"

TARGETS = [
    ("icon-512.png",         512),
    ("icon-192.png",         192),
    ("apple-touch-icon.png", 180),
]

def ensure_cairosvg():
    try:
        import cairosvg
        return cairosvg
    except ImportError:
        print("cairosvg nicht gefunden – installiere...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "cairosvg"],
            stdout=subprocess.DEVNULL
        )
        import cairosvg
        return cairosvg

def main():
    cairosvg = ensure_cairosvg()
    svg_bytes = SVG.encode("utf-8")

    for filename, size in TARGETS:
        out_path = os.path.join(TARGET_DIR, filename)
        cairosvg.svg2png(
            bytestring=svg_bytes,
            write_to=out_path,
            output_width=size,
            output_height=size,
        )
        kb = os.path.getsize(out_path) // 1024
        print(f"  ✓ {filename} ({size}×{size}px, {kb} KB)")

    print("\nFertig! Alle Icons wurden ersetzt.")
    os.remove(os.path.abspath(__file__))
    print("Script gelöscht.")

if __name__ == "__main__":
    main()
