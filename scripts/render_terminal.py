#!/usr/bin/env python3
"""
Render captured terminal output into a PNG screenshot.

El Verificador is a command-line tool, so its "screenshots" are real terminal
captures: the actual bytes the CLI wrote to stdout/stderr, replayed into a
macOS-style terminal window and rasterised to PNG.

This renderer understands the ANSI SGR subset that matters for CLI output
(colours, bold, dim, italic, underline, reverse, 256-colour and truecolour)
and lays the result out on a fixed character grid so the output looks exactly
as it did in the terminal it came from.

Usage:
    python scripts/render_terminal.py capture.txt -o shot.png --title "verify"
    python scripts/render_terminal.py capture.txt -o shot.png --theme light
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #

DARK_THEME = {
    "name": "dark",
    "window": "#2b2c35",
    "window_edge": "#1a1b21",
    "bg": "#12131a",
    "fg": "#d6dae3",
    "dim_fg": "#7d8290",
    "title": "#9aa0ae",
    "shadow": (0, 0, 0, 150),
    "lights": ("#ff5f57", "#febc2e", "#28c840"),
    "colors": {
        0: "#3a3f4b",
        1: "#e06c75",
        2: "#7fd18a",
        3: "#e5c07b",
        4: "#61afef",
        5: "#c678dd",
        6: "#56b6c2",
        7: "#abb2bf",
        8: "#5c6370",
        9: "#ef707a",
        10: "#9ee29e",
        11: "#f0d399",
        12: "#7cc4ff",
        13: "#d68ce6",
        14: "#6fd3de",
        15: "#e6e9ef",
    },
}

LIGHT_THEME = {
    "name": "light",
    "window": "#e6e6ea",
    "window_edge": "#c8c8cf",
    "bg": "#fbfbfd",
    "fg": "#2b2f3a",
    "dim_fg": "#8b90a0",
    "title": "#6b7080",
    "shadow": (0, 0, 0, 60),
    "lights": ("#ff5f57", "#febc2e", "#28c840"),
    "colors": {
        0: "#3a3f4b",
        1: "#c62839",
        2: "#2e7d32",
        3: "#a06a00",
        4: "#1565c0",
        5: "#8e24aa",
        6: "#00838f",
        7: "#5c6370",
        8: "#7a8090",
        9: "#e53950",
        10: "#43a047",
        11: "#fb8c00",
        12: "#1e88e5",
        13: "#ab47bc",
        14: "#00acc1",
        15: "#1f2430",
    },
}

# Menlo.ttc face indices.
MENLO_REGULAR = 0
MENLO_BOLD = 1
MENLO_ITALIC = 2
MENLO_BOLD_ITALIC = 3
MENLO_PATH = "/System/Library/Fonts/Menlo.ttc"
SYSTEM_FONT = "/System/Library/Fonts/SFNS.ttf"


# --------------------------------------------------------------------------- #
# ANSI parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Style:
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    dim: bool = False
    reverse: bool = False


@dataclass(frozen=True)
class Cell:
    char: str
    style: Style
    width: int = 1


def _xterm_256(index: int) -> str:
    if index < 16:
        return None  # handled by the base palette
    if index < 232:
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r = levels[(index // 36) % 6]
        g = levels[(index // 6) % 6]
        b = levels[index % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    grey = 8 + (index - 232) * 10
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def _apply_sgr(style: Style, params: list[int], theme: dict) -> Style:
    palette = theme["colors"]
    if not params:
        params = [0]

    i = 0
    while i < len(params):
        code = params[i]
        if code == 0:
            style = Style()
        elif code == 1:
            style = replace(style, bold=True)
        elif code == 2:
            style = replace(style, dim=True)
        elif code == 3:
            style = replace(style, italic=True)
        elif code == 4:
            style = replace(style, underline=True)
        elif code == 7:
            style = replace(style, reverse=True)
        elif code == 21:
            style = replace(style, bold=True)
        elif code == 22:
            style = replace(style, bold=False, dim=False)
        elif code == 23:
            style = replace(style, italic=False)
        elif code == 24:
            style = replace(style, underline=False)
        elif code == 27:
            style = replace(style, reverse=False)
        elif 30 <= code <= 37:
            style = replace(style, fg=palette[code - 30])
        elif code == 39:
            style = replace(style, fg=None)
        elif 40 <= code <= 47:
            style = replace(style, bg=palette[code - 40])
        elif code == 49:
            style = replace(style, bg=None)
        elif 90 <= code <= 97:
            style = replace(style, fg=palette[code - 90 + 8])
        elif 100 <= code <= 107:
            style = replace(style, bg=palette[code - 100 + 8])
        elif code in (38, 48):
            target = "fg" if code == 38 else "bg"
            if i + 1 < len(params) and params[i + 1] == 5:
                value = _xterm_256(params[i + 2]) if i + 2 < len(params) else None
                style = replace(style, **{target: value})
                i += 2
            elif i + 1 < len(params) and params[i + 1] == 2:
                if i + 4 < len(params):
                    r, g, b = params[i + 2 : i + 5]
                    style = replace(style, **{target: f"#{r:02x}{g:02x}{b:02x}"})
                i += 4
        i += 1
    return style


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def parse_ansi(text: str, theme: dict) -> list[list[Cell]]:
    """Split ANSI text into a grid of styled cells."""
    lines: list[list[Cell]] = [[]]
    style = Style()
    row = 0
    col = 0

    i = 0
    length = len(text)
    while i < length:
        ch = text[i]

        if ch == "\x1b":
            # CSI sequence
            if i + 1 < length and text[i + 1] == "[":
                j = i + 2
                while j < length and not ("@" <= text[j] <= "~"):
                    j += 1
                if j < length:
                    final = text[j]
                    body = text[i + 2 : j]
                    if final == "m":
                        raw = [p for p in body.split(";") if p != ""]
                        params = [int(p) for p in raw] if raw else [0]
                        style = _apply_sgr(style, params, theme)
                    i = j + 1
                    continue
            # OSC sequence (title) - consume until BEL or ST
            if i + 1 < length and text[i + 1] == "]":
                j = i + 2
                while j < length and text[j] not in ("\x07",):
                    if text[j] == "\x1b" and j + 1 < length and text[j + 1] == "\\":
                        j += 1
                        break
                    j += 1
                i = j + 1
                continue
            i += 1
            continue

        if ch == "\n":
            row += 1
            col = 0
            lines.append([])
            i += 1
            continue

        if ch == "\r":
            col = 0
            i += 1
            continue

        if ch == "\t":
            pad = 8 - (col % 8)
            for _ in range(pad):
                lines[row].append(Cell(" ", style, 1))
            col += pad
            i += 1
            continue

        if ch < " " and ch != "\t":
            i += 1
            continue

        width = char_width(ch)
        lines[row].append(Cell(ch, style, width))
        if width == 0:
            i += 1
            continue
        col += width
        if width == 2:
            lines[row].append(Cell("", style, 0))
        i += 1

    return lines


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _blend(color: str, background: str, alpha: float) -> str:
    def parts(c: str) -> tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    def mix(a: int, b: int) -> int:
        return int(round(a * alpha + b * (1 - alpha)))

    fr, fg_, fb = parts(color)
    br, bg_, bb = parts(background)
    return f"#{mix(fr, br):02x}{mix(fg_, bg_):02x}{mix(fb, bb):02x}"


def _load_font(size: int, bold: bool, italic: bool) -> ImageFont.FreeTypeFont:
    index = MENLO_REGULAR
    if bold and italic:
        index = MENLO_BOLD_ITALIC
    elif bold:
        index = MENLO_BOLD
    elif italic:
        index = MENLO_ITALIC
    return ImageFont.truetype(MENLO_PATH, size, index=index)


def _runs(row: Sequence[Cell]) -> list[tuple[int, list[Cell]]]:
    runs: list[tuple[int, list[Cell]]] = []
    start = 0
    for idx in range(1, len(row) + 1):
        if idx == len(row) or row[idx].style != row[start].style:
            runs.append((start, list(row[start:idx])))
            start = idx
    return runs


def _truncate(rows: list[list[Cell]], max_cols: int) -> list[list[Cell]]:
    """Clamp every row to `max_cols` display cells, marking the cut."""
    out: list[list[Cell]] = []
    for row in rows:
        used = 0
        kept: list[Cell] = []
        truncated = False
        for cell in row:
            if used + cell.width > max_cols:
                truncated = True
                break
            kept.append(cell)
            used += cell.width
        if truncated:
            ell = Cell("…", Style(fg="#8a8f9c"), 1)
            if used + 1 <= max_cols:
                kept.append(ell)
            elif kept:
                kept[-1] = Cell("…", Style(fg="#8a8f9c"), kept[-1].width)
        out.append(kept)
    return out


def render(
    lines: list[list[Cell]],
    theme: dict,
    title: str = "",
    font_size: int = 15,
    max_cols: int | None = None,
    padding: int = 18,
    chrome_height: int = 38,
    radius: int = 10,
) -> Image.Image:
    if max_cols:
        lines = _truncate(lines, max_cols)

    font = _load_font(font_size, False, False)
    cell_w = int(round(font.getlength("M")))
    ascent, descent = font.getmetrics()
    cell_h = ascent + descent

    cols = max((sum(c.width for c in row) for row in lines), default=1)
    rows = len(lines)
    if max_cols:
        cols = min(cols, max_cols)

    text_w = cols * cell_w
    text_h = rows * cell_h

    width = text_w + padding * 2
    height = chrome_height + text_h + padding * 2

    # --- soft shadow -------------------------------------------------- #
    shadow_pad = 26
    canvas = Image.new(
        "RGBA", (width + shadow_pad * 2, height + shadow_pad * 2), (0, 0, 0, 0)
    )
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        [shadow_pad, shadow_pad + 6, shadow_pad + width, shadow_pad + height + 6],
        radius=radius,
        fill=theme["shadow"],
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(shadow)

    # --- window -------------------------------------------------------- #
    win = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    wd = ImageDraw.Draw(win)
    wd.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=theme["window"])
    wd.rectangle([0, chrome_height, width - 1, height - 1], fill=theme["bg"])
    wd.rounded_rectangle(
        [0, 0, width - 1, height - 1], radius=radius, outline=theme["window_edge"], width=1
    )
    wd.line([(0, chrome_height), (width - 1, chrome_height)], fill=theme["window_edge"], width=1)

    # traffic lights
    light_r = 6
    ly = chrome_height // 2
    for idx, color in enumerate(theme["lights"]):
        cx = 20 + idx * 20
        wd.ellipse(
            [cx - light_r, ly - light_r, cx + light_r, ly + light_r], fill=color
        )

    # title
    if title:
        try:
            tfont = ImageFont.truetype(SYSTEM_FONT, 13)
        except OSError:
            tfont = font
        bbox = wd.textbbox((0, 0), title, font=tfont)
        tw = bbox[2] - bbox[0]
        wd.text(
            ((width - tw) / 2, ly - (bbox[3] - bbox[1]) / 2 - bbox[1]),
            title,
            font=tfont,
            fill=theme["title"],
        )

    canvas.alpha_composite(win, (shadow_pad, shadow_pad))

    # --- text ---------------------------------------------------------- #
    draw = ImageDraw.Draw(canvas)
    origin_x = shadow_pad + padding
    origin_y = shadow_pad + chrome_height + padding

    for r, row in enumerate(lines):
        y = origin_y + r * cell_h
        for start, run in _runs(row):
            style = run[0].style
            x = origin_x + sum(c.width for c in row[:start]) * cell_w

            bg = style.bg
            fg = style.fg or theme["fg"]
            if style.reverse:
                bg, fg = fg, bg or theme["bg"]
            if style.dim:
                fg = _blend(fg, bg or theme["bg"], 0.55)

            run_width = sum(c.width for c in run) * cell_w
            if bg:
                draw.rectangle([x, y, x + run_width, y + cell_h], fill=bg)

            rfont = _load_font(font_size, style.bold, style.italic)
            for cell in run:
                if cell.char and cell.char != " ":
                    draw.text((x, y + ascent), cell.char, font=rfont, fill=fg, anchor="ls")
                x += cell.width * cell_w

            if style.underline:
                draw.line(
                    [(origin_x + sum(c.width for c in row[:start]) * cell_w, y + cell_h - 2),
                     (origin_x + sum(c.width for c in row[:start]) * cell_w + run_width, y + cell_h - 2)],
                    fill=fg,
                    width=1,
                )

    # re-apply the rounded mask so the shadow does not show through corners
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [shadow_pad, shadow_pad, shadow_pad + width - 1, shadow_pad + height - 1],
        radius=radius,
        fill=255,
    )
    canvas.putalpha(Image.composite(canvas.getchannel("A"), mask, mask))
    return canvas


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render terminal output to PNG")
    parser.add_argument("input", help="Text file containing the captured output")
    parser.add_argument("-o", "--output", required=True, help="Output PNG path")
    parser.add_argument("--title", default="", help="Window title")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--font-size", type=int, default=15)
    parser.add_argument("--max-cols", type=int, default=None)
    parser.add_argument("--head", type=int, default=None, help="Keep only first N lines")
    parser.add_argument("--tail", type=int, default=None, help="Keep only last N lines")
    args = parser.parse_args(argv)

    theme = DARK_THEME if args.theme == "dark" else LIGHT_THEME
    text = Path(args.input).read_text(encoding="utf-8", errors="replace")

    lines = parse_ansi(text, theme)
    while lines and not lines[-1]:
        lines.pop()

    if args.head is not None:
        lines = lines[: args.head]
    if args.tail is not None:
        lines = lines[-args.tail :]

    image = render(
        lines,
        theme,
        title=args.title,
        font_size=args.font_size,
        max_cols=args.max_cols,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG", optimize=True)
    print(f"{out}  {image.width}x{image.height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
