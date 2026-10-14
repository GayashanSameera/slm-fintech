"""Image preparation (JPEG/PNG uploads, and page images rendered from PDFs).

Why we resize: AI models bill images by size (~ width x height / 750 tokens).
A 4,000-pixel phone photo costs ~6x more than a 1,568-pixel one and is not read
any better. So every image is downscaled before it is sent to a model.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from app.core.errors import JobError

# Refuse "decompression bombs": tiny files that decode into gigantic images and
# exhaust memory. Pillow raises DecompressionBombError above this pixel count.
Image.MAX_IMAGE_PIXELS = 50_000_000

SCAN_MAX_EDGE = 1568      # scans and photos: enough detail for small print
DIGITAL_MAX_EDGE = 1000   # digital PDF pages: the text layer carries the detail


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    """Encode as JPEG. Re-encoding also drops metadata such as GPS location."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def downscale(img: Image.Image, max_edge: int) -> Image.Image:
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
    return to_jpeg(downscale(img, SCAN_MAX_EDGE))
