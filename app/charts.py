"""Extensible report chart rendering.

The current rich-message Bot API deployment has no documented SVG/media field,
so callers get an accessible textual fallback.  `svg()` is real SVG and is kept
separate so it can be attached as rich media once Telegram exposes a media ID
or SVG attachment contract.
"""
from __future__ import annotations

import html
import struct
import zlib
from decimal import Decimal


class ReportChartService:
    def png(self, rows: list[tuple[str, Decimal]]) -> bytes | None:
        """Render a small, dependency-free PNG bar chart for RichMessage media."""
        if not rows:
            return None
        width, height = 640, max(160, min(440, 48 * min(len(rows), 8) + 48))
        pixels = bytearray([255, 255, 255, 255]) * (width * height)

        def paint(x: int, y: int, color: tuple[int, int, int, int]) -> None:
            if 0 <= x < width and 0 <= y < height:
                offset = (y * width + x) * 4
                pixels[offset : offset + 4] = bytes(color)

        maximum = max(value for _, value in rows) or Decimal(1)
        for index, (_, value) in enumerate(rows[:8]):
            top = 28 + index * 48
            bar_width = max(1, int((value / maximum) * 440))
            for y in range(top, top + 26):
                for x in range(170, 170 + bar_width):
                    paint(x, y, (46, 125, 91, 255))
            for x in range(160, 165):
                for y in range(top, top + 26):
                    paint(x, y, (210, 215, 220, 255))
        raw = b"".join(b"\x00" + bytes(pixels[y * width * 4 : (y + 1) * width * 4]) for y in range(height))
        signature = b"\x89PNG\r\n\x1a\n"

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

        return signature + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")

    def svg(self, rows: list[tuple[str, Decimal]]) -> str:
        maximum = max((value for _, value in rows), default=Decimal(1)) or Decimal(1)
        bars = []
        for index, (name, value) in enumerate(rows[:8]):
            width = int((value / maximum) * 240)
            y = 24 + index * 28
            bars.append(f'<text x="0" y="{y + 14}">{html.escape(name[:18])}</text><rect x="150" y="{y}" width="{width}" height="16" fill="#4f8cff"/>')
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="250">' + "".join(bars) + "</svg>"

    def fallback_html(self, rows: list[tuple[str, Decimal]]) -> str:
        # This remains useful to clients that cannot render attached SVG yet.
        if not rows:
            return ""
        maximum = max(value for _, value in rows) or Decimal(1)
        lines = []
        for name, value in rows[:8]:
            bars = "▰" * max(1, round(float(value / maximum) * 12))
            lines.append(f"{html.escape(name[:18])} {bars}")
        return "<p><b>График расходов</b><br/><code>" + "<br/>".join(lines) + "</code></p>"
