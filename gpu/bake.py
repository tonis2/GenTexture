"""UV-to-UV texture remapper.

Given a source image with per-loop "source" UVs (e.g. screen-space projection)
and "destination" UVs (target UV layout), rasterize each triangle into the
destination UV space, sampling the source image via barycentric interpolation.

Implemented in numpy on CPU — robust across Blender GPU API revisions, and
fast enough at typical mesh + texture sizes (a few thousand tris, 2k texture).
"""

import numpy as np


def bake_to_uv(src_pixels: np.ndarray, src_width: int, src_height: int,
               bmesh_data, src_uv_layer, dest_uv_layer,
               dest_width: int, dest_height: int) -> np.ndarray:
    """Remap a texture from source UVs to destination UVs.

    Args:
        src_pixels: Flat float32 RGBA, bottom-to-top (Blender's image.pixels order)
        bmesh_data: BMesh (triangulated)
        src_uv_layer / dest_uv_layer: BMesh UV layers
        dest_width / dest_height: Output size
    Returns:
        Flat float32 RGBA, bottom-to-top, length = dest_w * dest_h * 4.
    """
    src_img = src_pixels.reshape(src_height, src_width, 4)
    out = np.zeros((dest_height, dest_width, 4), dtype=np.float32)

    tri_count = 0
    drawn_pixels = 0

    for face in bmesh_data.faces:
        loops = list(face.loops)
        if len(loops) < 3:
            continue
        for i in range(1, len(loops) - 1):
            tri = [loops[0], loops[i], loops[i + 1]]
            s = np.array([list(l[src_uv_layer].uv) for l in tri], dtype=np.float32)
            d = np.array([list(l[dest_uv_layer].uv) for l in tri], dtype=np.float32)
            tri_count += 1
            drawn_pixels += _rasterize_tri(
                out, src_img, s, d,
                dest_width, dest_height, src_width, src_height,
            )

    if tri_count == 0:
        print("[GenTex bake] no triangles found")
    elif drawn_pixels == 0:
        print(f"[GenTex bake] {tri_count} triangles, but 0 pixels drawn — "
              f"check that destination UV layout has non-degenerate UVs")
    else:
        # Edge-pad the bake. Without this, bilinear filtering at runtime
        # samples across UV island boundaries into alpha=0 pixels, which the
        # baked material's alpha-mix renders as the fallback grey — visible as
        # dashed seams along every UV island border. Dilating by ~8px hides
        # those seams without noticeably affecting the painted regions.
        _dilate_painted(out, iterations=8)

    return out.ravel()


def _dilate_painted(out: np.ndarray, iterations: int = 8):
    """Bleed painted pixels outward into adjacent transparent ones.

    For each transparent pixel that touches at least one painted (alpha>0)
    neighbor, copy the average of those painted neighbors' colors and set
    alpha=1. Repeat `iterations` times. In-place.
    """
    H, W = out.shape[:2]
    for _ in range(iterations):
        alpha = out[..., 3] > 0
        if alpha.all():
            return
        padded = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode='constant')
        padded_a = np.pad(alpha, ((1, 1), (1, 1)), mode='constant').astype(np.float32)
        accum = np.zeros_like(out)
        count = np.zeros((H, W), dtype=np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            n_rgba = padded[1 + dy:1 + dy + H, 1 + dx:1 + dx + W]
            n_a = padded_a[1 + dy:1 + dy + H, 1 + dx:1 + dx + W]
            accum += n_rgba * n_a[..., None]
            count += n_a
        fill = (~alpha) & (count > 0)
        if not fill.any():
            return
        avg = accum / np.maximum(count, 1.0)[..., None]
        avg[..., 3] = 1.0
        out[fill] = avg[fill]


def _rasterize_tri(out: np.ndarray, src_img: np.ndarray,
                   s: np.ndarray, d: np.ndarray,
                   dw: int, dh: int, sw: int, sh: int) -> int:
    """Rasterize a single triangle into `out` (bottom-up), sampling src_img.

    Returns number of pixels written.
    """
    # Destination pixel coordinates (bottom-up: y = v * H)
    px = d[:, 0] * dw
    py = d[:, 1] * dh

    x0 = max(0, int(np.floor(px.min())))
    x1 = min(dw, int(np.ceil(px.max())) + 1)
    y0 = max(0, int(np.floor(py.min())))
    y1 = min(dh, int(np.ceil(py.max())) + 1)
    if x1 <= x0 or y1 <= y0:
        return 0

    xs = np.arange(x0, x1, dtype=np.float32) + 0.5
    ys = np.arange(y0, y1, dtype=np.float32) + 0.5
    gy, gx = np.meshgrid(ys, xs, indexing='ij')

    x0p, y0p = px[0], py[0]
    x1p, y1p = px[1], py[1]
    x2p, y2p = px[2], py[2]
    denom = (y1p - y2p) * (x0p - x2p) + (x2p - x1p) * (y0p - y2p)
    if abs(denom) < 1e-9:
        return 0

    l1 = ((y1p - y2p) * (gx - x2p) + (x2p - x1p) * (gy - y2p)) / denom
    l2 = ((y2p - y0p) * (gx - x2p) + (x0p - x2p) * (gy - y2p)) / denom
    l3 = 1.0 - l1 - l2
    inside = (l1 >= -1e-5) & (l2 >= -1e-5) & (l3 >= -1e-5)
    if not inside.any():
        return 0

    # Interpolate source UVs at inside pixels
    u_src = l1 * s[0, 0] + l2 * s[1, 0] + l3 * s[2, 0]
    v_src = l1 * s[0, 1] + l2 * s[1, 1] + l3 * s[2, 1]

    # Bilinear sample source (bottom-up storage). Nearest-neighbour produced
    # visible blockiness wherever the dest face was larger than the src face's
    # screen-space footprint (i.e. faces near the silhouette of the projection
    # camera, where one screen pixel maps to several dest pixels).
    sx_f = np.clip(u_src * sw - 0.5, 0, sw - 1)
    sy_f = np.clip(v_src * sh - 0.5, 0, sh - 1)
    x0i = np.floor(sx_f).astype(np.int32)
    y0i = np.floor(sy_f).astype(np.int32)
    x1i = np.minimum(x0i + 1, sw - 1)
    y1i = np.minimum(y0i + 1, sh - 1)
    fx = (sx_f - x0i)[..., None]
    fy = (sy_f - y0i)[..., None]
    a = src_img[y0i, x0i]
    b = src_img[y0i, x1i]
    c = src_img[y1i, x0i]
    d = src_img[y1i, x1i]
    top = a + (b - a) * fx
    bot = c + (d - c) * fx
    sampled = top + (bot - top) * fy

    region = out[y0:y1, x0:x1]
    region[inside] = sampled[inside]
    out[y0:y1, x0:x1] = region
    return int(inside.sum())
