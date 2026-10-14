"""Image preparation (JPEG/PNG uploads, and page images rendered from PDFs).

Why we resize: AI models bill images by size (~ width x height / 750 tokens).
A 4,000-pixel phone photo costs ~6x more than a 1,568-pixel one and is not read
any better. So every image is downscaled before it is sent to a model.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFilter, ImageOps

from app.core.errors import JobError

# Refuse "decompression bombs": tiny files that decode into gigantic images and
# exhaust memory. Pillow raises DecompressionBombError above this pixel count.
Image.MAX_IMAGE_PIXELS = 50_000_000

SCAN_MAX_EDGE = 1568      # scans and photos: enough detail for small print
DIGITAL_MAX_EDGE = 1000   # digital PDF pages: the text layer carries the detail
# Below this many pixels on the LONG side, the small print of a financial document is a few pixels
# tall: no model can read it, and models then GUESS plausible values (tested 2026-10-14 with
# 262x300 thumbnails of statements: rows came back with invented amounts and no warning). Such
# images are refused instead. An A4 page photographed or scanned at a readable size is far above it.
MIN_LONG_EDGE = 1000


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    """Encode as JPEG. Re-encoding also drops metadata such as GPS location."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def downscale(img: Image.Image, max_edge: int) -> Image.Image:
    """A copy no larger than `max_edge` pixels on its long side (aspect kept, never enlarged)."""
    img = img.copy()
    img.thumbnail((max_edge, max_edge))   # keeps the aspect ratio; never enlarges
    return img


def prepare_image(data: bytes) -> bytes:
    """Open an uploaded JPEG/PNG, fix its rotation, downscale it, return JPEG bytes."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()                         # decode now so errors surface here
    except Image.DecompressionBombError:
        raise JobError("file_too_complex") from None
    except Exception:
        raise JobError("corrupt_file") from None
    img = ImageOps.exif_transpose(img)     # phone photos store rotation in EXIF metadata
    if max(img.size) < MIN_LONG_EDGE:
        raise JobError("image_resolution_too_low")
    return to_jpeg(downscale(img, SCAN_MAX_EDGE))


def enhanced_view(jpeg: bytes) -> bytes:
    """A second, differently processed rendering of the same page (same size, so the same cost).

    Used for the SECOND reading of a scanned statement page: stretching the contrast and sharpening the
    edges changes how faint or blurred characters look, so a character the first reading misread because
    of noise is read from a different picture. (Two readings of the very same pixels by the same model
    tend to repeat the same slip.)
    """
    img = ImageOps.autocontrast(Image.open(io.BytesIO(jpeg)).convert("L"), cutoff=1)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))
    return to_jpeg(img, quality=88)


def enhanced_pages(pages: list[dict]) -> list[dict]:
    """The pages with their images replaced by enhanced_view (text layers, if any, are kept)."""
    return [{**p, "image": enhanced_view(p["image"])} if p.get("image") else p for p in pages]
