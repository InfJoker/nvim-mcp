"""Color mapping, font loading, and PNG rendering for pyte screens."""

from __future__ import annotations

import functools
import unicodedata

import pyte
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Color mapping for PNG rendering
# ---------------------------------------------------------------------------

_DEFAULT_FG = (204, 204, 204)
_DEFAULT_BG = (30, 30, 30)

_ANSI_COLOR_NAMES = (
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightbrown",
    "brightblue", "brightmagenta", "brightcyan", "brightwhite",
)

_ANSI_COLORS_BY_INDEX: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),          # 0  black
    (205, 49, 49),       # 1  red
    (13, 188, 121),      # 2  green
    (229, 229, 16),      # 3  brown/yellow
    (36, 114, 200),      # 4  blue
    (188, 63, 188),      # 5  magenta
    (17, 168, 205),      # 6  cyan
    (204, 204, 204),     # 7  white
    (118, 118, 118),     # 8  bright black
    (241, 76, 76),       # 9  bright red
    (35, 209, 139),      # 10 bright green
    (245, 245, 67),      # 11 bright brown/yellow
    (59, 142, 234),      # 12 bright blue
    (214, 112, 214),     # 13 bright magenta
    (41, 184, 219),      # 14 bright cyan
    (242, 242, 242),     # 15 bright white
)

_ANSI_COLORS = dict(zip(_ANSI_COLOR_NAMES, _ANSI_COLORS_BY_INDEX))


def _resolve_color(color: str, is_fg: bool) -> tuple[int, int, int]:
    """Convert a pyte color attribute to an RGB tuple."""
    if not color or color == "default":
        return _DEFAULT_FG if is_fg else _DEFAULT_BG
    # Named color
    if color in _ANSI_COLORS:
        return _ANSI_COLORS[color]
    # 256-color index (pyte stores as string like "196")
    if color.isdigit():
        idx = int(color)
        if idx < 16:
            return _ANSI_COLORS_BY_INDEX[idx]
        if idx < 232:
            # 6x6x6 color cube
            idx -= 16
            b = (idx % 6) * 51
            idx //= 6
            g = (idx % 6) * 51
            r = (idx // 6) * 51
            return (r, g, b)
        # Grayscale ramp
        v = 8 + (idx - 232) * 10
        return (v, v, v)
    # 24-bit hex (pyte may give "RRGGBB" or "#RRGGBB")
    hex_str = color.lstrip("#")
    if len(hex_str) == 6:
        try:
            r = int(hex_str[0:2], 16)
            g = int(hex_str[2:4], 16)
            b = int(hex_str[4:6], 16)
            return (r, g, b)
        except ValueError:
            pass
    return _DEFAULT_FG if is_fg else _DEFAULT_BG


# ---------------------------------------------------------------------------
# Font loading and PNG rendering
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=4)
def _load_font(size: int = 14) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a monospace font, trying platform-specific paths."""
    for name in [
        "/System/Library/Fonts/Menlo.ttc",               # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",  # Debian/Ubuntu
        "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",  # Fedora
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",       # Arch
        "DejaVuSansMono.ttf",                             # system path
    ]:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _char_width(ch: str) -> int:
    """Return the display width of a character (1 or 2 for wide/fullwidth)."""
    if len(ch) != 1:
        return 1
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ("W", "F") else 1


def _render_screen_to_png(screen: pyte.Screen, path: str) -> None:
    """Render a pyte screen to a PNG image."""
    font = _load_font(14)
    bbox = font.getbbox("M")
    char_w = bbox[2] - bbox[0]
    char_h = bbox[3] - bbox[1]
    line_h = char_h + 2
    y_offset = -bbox[1]  # baseline offset

    img = Image.new(
        "RGB",
        (screen.columns * char_w, screen.lines * line_h),
        _DEFAULT_BG,
    )
    draw = ImageDraw.Draw(img)

    for row in range(screen.lines):
        skip_next = False
        for col in range(screen.columns):
            if skip_next:
                skip_next = False
                continue
            char = screen.buffer[row][col]
            fg = _resolve_color(char.fg, is_fg=True)
            bg = _resolve_color(char.bg, is_fg=False)
            if char.reverse:
                fg, bg = bg, fg
            x = col * char_w
            y = row * line_h
            cw = _char_width(char.data) if char.data.strip() else 1
            cell_w = char_w * cw
            draw.rectangle([x, y, x + cell_w, y + line_h], fill=bg)
            if char.data.strip():
                draw.text((x, y + y_offset), char.data, font=font, fill=fg)
                if cw == 2:
                    skip_next = True

    img.save(path, "PNG")
