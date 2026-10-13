"""Image path: JPG/PNG uploads, rendered PDF pages, and pictures inside DOCX files.

What happens to every image before the model sees it:

1. Size check BEFORE decoding. Pillow reads only the header first, so we can refuse an
   image that claims to be, say, 30000 x 30000 pixels without ever unpacking it. A small
   file can expand into gigabytes of memory ("decompression bomb").
2. Rotate using the EXIF orientation flag. Phones often store photos sideways with a flag
   saying "rotate 90°"; the model must see the page upright.
3. Convert to RGB on a white background (transparent PNGs would otherwise turn black).
4. Shrink so the longest side is at most SLM_MAX_IMAGE_SIDE (1600 px). Enough to read
   printed text; fewer image tokens = faster model.
5. Re-encode as PNG. This also DROPS all EXIF metadata, including the GPS location where a
   photo was taken (privacy).
"""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.modules.extraction.errors import ExtractionError

# We do our own pixel check below (with a clear error). Disable Pillow's built-in one, which
# only warns between 1x and 2x of its default limit.
Image.MAX_IMAGE_PIXELS = None


def to_model_png(img: Image.Image, settings: Settings) -> bytes:
    """Steps 3-5 for an already-open image. Also used for rendered PDF pages."""
    if img.mode in ("RGBA", "LA", "P"):
        # Paste onto white so transparent areas become white, not black.
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((settings.max_image_side, settings.max_image_side))  # keeps proportions; never enlarges
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def image_bytes_to_png(data: bytes, settings: Settings) -> tuple[bytes, tuple[int, int]]:
    """Steps 1-5 for raw image bytes. Returns (PNG bytes, (width, height) after resizing)."""
    try:
        img = Image.open(io.BytesIO(data))  # reads the header only
    except (UnidentifiedImageError, OSError) as exc:
        raise ExtractionError("corrupt_file", "The image could not be read.") from exc
    width, height = img.size
    if width * height > settings.max_image_pixels:
        raise ExtractionError("file_too_large", f"The image is {width}x{height} pixels; the limit is 40 megapixels.")
    try:
        img = ImageOps.exif_transpose(img)  # decodes the pixels and applies the rotation flag
        png = to_model_png(img, settings)
    except OSError as exc:  # truncated or damaged image data
        raise ExtractionError("corrupt_file", "The image could not be read.") from exc
    final = Image.open(io.BytesIO(png)).size
    return png, final


def prepare_image_file(data: bytes, kind, settings: Settings):
    """A JPG/PNG upload: one image, one page, no text layer."""
    from app.modules.extraction.preprocess import PreparedDocument

    png, (w, h) = image_bytes_to_png(data, settings)
    return PreparedDocument(
        kind=kind,
        pages=1,
        text=None,
        images=[png],
        route=f"{kind.value.upper()} image, upright and resized to {w}x{h} px",
    )
