"""Bounded decoder subprocess. Receives bytes on stdin and emits a metadata-free JPEG."""

import io
import sys
import warnings

from PIL import Image, ImageOps


def main():
    Image.MAX_IMAGE_PIXELS = 16_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    raw = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise ValueError("Image size")
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
            raise ValueError("Image format")
        if max(source.size) > 8000 or source.width * source.height > 16_000_000:
            raise ValueError("Image dimensions")
        source.load()
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1600, 1600))
        clean = Image.new("RGB", image.size)
        clean.paste(image)
        result = io.BytesIO()
        clean.save(result, format="JPEG", quality=90)
        sys.stdout.buffer.write(result.getvalue())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(2) from None
