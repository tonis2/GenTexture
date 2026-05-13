import struct
import zlib
import tempfile
import os

import bpy
import numpy as np


def np_to_bpy(
    array: np.ndarray,
    name: str,
    existing: bpy.types.Image | None = None,
    *,
    float_buffer: bool = False,
    pack: bool = True,
) -> bpy.types.Image:
    """Convert a numpy RGBA float32 array (H, W, 4) to a Blender image.

    Defaults to an 8-bit storage buffer — our LDR generator outputs and masks
    fit fine in uint8, and float buffers (16 B/px) make image creation and
    packing ~4× slower for no visual gain. Callers that need HDR can pass
    `float_buffer=True`.

    `pack=True` writes the pixel data into the .blend container so save/reload
    preserves it. Skip if the image is throwaway (e.g. an intermediate the
    caller is going to bake out of and immediately remove).
    """
    h, w = array.shape[:2]
    if existing is not None and existing.size[0] == w and existing.size[1] == h:
        image = existing
    else:
        if existing is not None:
            bpy.data.images.remove(existing)
        image = bpy.data.images.new(
            name, width=w, height=h, alpha=True, float_buffer=float_buffer,
        )

    pixels = array.astype(np.float32)
    # Blender stores pixels bottom-to-top, numpy is top-to-bottom
    pixels = np.flipud(pixels).ravel()
    image.pixels.foreach_set(pixels)
    image.update()
    if pack:
        image.pack()
    return image


def bpy_to_np(image: bpy.types.Image) -> np.ndarray:
    """Convert a Blender image to a numpy RGBA float32 array (H, W, 4)."""
    w, h = image.size
    pixels = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape((h, w, 4))
    # Flip to top-to-bottom order
    return np.flipud(pixels)


def np_to_png_bytes(array: np.ndarray) -> bytes:
    """Encode a numpy array as PNG bytes using only stdlib (struct + zlib).

    Accepts:
      - (H, W, 4) float32 RGBA 0-1 -> RGBA PNG
      - (H, W, 3) float32 RGB 0-1 -> RGB PNG
      - (H, W) float32 grayscale 0-1 -> Grayscale PNG
    """
    if array.ndim == 2:
        h, w = array.shape
        channels = 1
        color_type = 0  # Grayscale
    elif array.ndim == 3 and array.shape[2] == 4:
        h, w = array.shape[:2]
        channels = 4
        color_type = 6  # RGBA
    elif array.ndim == 3 and array.shape[2] == 3:
        h, w = array.shape[:2]
        channels = 3
        color_type = 2  # RGB
    else:
        raise ValueError(f"Unsupported array shape: {array.shape}")

    # Convert to uint8
    data = (np.clip(array, 0, 1) * 255).astype(np.uint8)
    if channels == 1:
        data = data.reshape(h, w, 1)

    # Build raw image data with filter byte (0 = None) per row
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter byte
        raw.extend(data[y].tobytes())

    # Compress
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
        c = chunk_type + chunk_data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(chunk_data)) + c + struct.pack(">I", crc)

    # Build PNG
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    return png


def load_image_bytes(data: bytes, name: str = "gentex_response") -> np.ndarray:
    """Load image bytes (PNG/JPEG) into a numpy RGBA float32 array via Blender."""
    # Detect format from magic bytes
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        ext = ".png"

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        image = bpy.data.images.load(tmp.name)
        result = bpy_to_np(image)
        bpy.data.images.remove(image)
        return result
    finally:
        os.unlink(tmp.name)
