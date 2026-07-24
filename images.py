"""
Progress-photo handling: normalize inbound photos and compose before/after.

Mirrors charts.py — headless matplotlib rendering into an in-memory PNG. Pillow
ships as a matplotlib dependency, so no new requirement is added.

Everything here is deliberately defensive: inbound bytes are user-supplied body
photos. normalize() strips EXIF/GPS metadata (a privacy win) and bounds the
image so a decompression bomb or huge upload can't exhaust memory.
"""

import io
import math

import matplotlib

matplotlib.use("Agg")  # headless — must precede pyplot import

import matplotlib.pyplot as plt
from PIL import Image

import charts  # reuse the Discord dark-theme palette

# Bound decoded images. Pillow raises DecompressionBombError above ~178 MP by
# default; we tighten it and also cap the largest side after decoding.
Image.MAX_IMAGE_PIXELS = 40_000_000  # ~40 MP
MAX_DIMENSION = 1600


def normalize(raw_bytes: bytes) -> bytes:
    """Decode, strip metadata, bound the size, and re-encode as PNG.

    Converting to RGB and re-saving drops EXIF (including GPS), and thumbnail()
    caps the largest dimension. Returns PNG bytes safe to store and re-upload.
    """
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img = img.convert("RGB")  # flattens alpha, strips EXIF/orientation/GPS
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.getvalue()


def _panel(ax, png_bytes: bytes, caption: str) -> None:
    with Image.open(io.BytesIO(png_bytes)) as img:
        ax.imshow(img)
    ax.set_title(caption, color=charts.FG, fontsize=12, fontweight="bold", pad=10)
    ax.axis("off")


MAX_COLLAGE_PANELS = 9


def sample_timeline(items: list, limit: int = MAX_COLLAGE_PANELS) -> list:
    """Evenly spread `limit` items across `items`, always keeping the ends.

    A year of weekly photos is 52 panels — too slow to render and past
    Discord's attachment cap. Sampling keeps the grid readable while
    preserving the first and most recent shots, which carry the contrast.
    """
    if limit < 1:
        return []
    n = len(items)
    if n <= limit:
        return list(items)
    if limit == 1:
        return [items[-1]]
    # Fractional stride, rounded per index, so the picks stay evenly spaced and
    # the first and last elements are always included.
    step = (n - 1) / (limit - 1)
    picked = sorted({round(i * step) for i in range(limit)})
    return [items[i] for i in picked]


def render_collage(panels: list[tuple[bytes, str]]) -> io.BytesIO:
    """Grid of captioned progress photos as one PNG.

    `panels` is [(png_bytes, caption)], oldest first. Lays out a square-ish
    grid sized to the count; a single photo renders on its own rather than as
    a 1x1 "collage".
    """
    if not panels:
        raise ValueError("render_collage needs at least one panel")

    count = len(panels)
    cols = 1 if count == 1 else math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)

    fig, axes = plt.subplots(
        rows, cols, figsize=(4.5 * cols, 5.0 * rows), dpi=130, squeeze=False
    )
    fig.patch.set_facecolor(charts.BG)

    flat = [ax for row in axes for ax in row]
    for ax, (png, caption) in zip(flat, panels):
        _panel(ax, png, caption)
    # Blank out any unused cells so empty axes don't draw frames.
    for ax in flat[count:]:
        ax.axis("off")

    fig.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=charts.BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def compose_before_after(
    day1_bytes: bytes,
    now_bytes: bytes,
    day1_label: str,
    now_label: str,
) -> io.BytesIO:
    """Two side-by-side panels (Day 1 vs Now) as a single PNG for Discord."""
    fig, (ax_left, ax_right) = plt.subplots(1, 2, figsize=(9, 5.4), dpi=150)
    fig.patch.set_facecolor(charts.BG)

    _panel(ax_left, day1_bytes, day1_label)
    _panel(ax_right, now_bytes, now_label)

    fig.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=charts.BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf
