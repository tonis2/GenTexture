"""Project Layer node — terminal node that writes the generated image onto
the mesh as a new projected layer.

Requires a Viewport Capture node upstream (anywhere in the chain) — it reads
`ctx.captured_per_obj` for the per-mesh UV name + face indices, and
`ctx.captured_mask` / `captured_visible` for the client-side mask composite
that keeps pixels outside the selection unchanged.
"""

import bpy
import numpy as np

from ._base import GenTexPipelineNodeBase, upstream_value
from ...utils.image import load_image_bytes, np_to_bpy
from ...utils.material import rebuild_layer_stack


def _bilinear_resize(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    src_h, src_w = arr.shape[:2]
    if src_w == w and src_h == h:
        return arr

    y_idx = np.linspace(0, src_h - 1, h, dtype=np.float32)
    x_idx = np.linspace(0, src_w - 1, w, dtype=np.float32)
    y0 = np.floor(y_idx).astype(np.int32)
    x0 = np.floor(x_idx).astype(np.int32)
    y1 = np.minimum(y0 + 1, src_h - 1)
    x1 = np.minimum(x0 + 1, src_w - 1)
    fy = y_idx - y0
    fx = x_idx - x0

    a = arr[y0[:, None], x0[None, :]]
    b = arr[y0[:, None], x1[None, :]]
    c = arr[y1[:, None], x0[None, :]]
    d = arr[y1[:, None], x1[None, :]]

    if arr.ndim == 3:
        fy_b = fy[:, None, None]
        fx_b = fx[None, :, None]
    else:
        fy_b = fy[:, None]
        fx_b = fx[None, :]

    top = a + (b - a) * fx_b
    bot = c + (d - c) * fx_b
    return (top + (bot - top) * fy_b).astype(np.float32)


class GenTexNodeProjectLayer(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeProjectLayer"
    bl_label = "Project Layer"
    bl_icon = "IMAGE_RGB_ALPHA"

    def init(self, context):
        self.inputs.new("GenTexImageSocket", "Image")
        self.inputs.new("GenTexImageSocket", "Capture")

    def draw_buttons(self, context, layout):
        layout.label(text="Needs Viewport Capture upstream", icon='INFO')
        layout.label(text="Bake from the Layers panel", icon='INFO')

    def evaluate(self, ctx):
        png = upstream_value(self, "Image", ctx, default=None)
        if not isinstance(png, (bytes, bytearray)):
            raise RuntimeError(f"{self.name}: no image on input")
        if not ctx.captured_per_obj:
            raise RuntimeError(
                f"{self.name}: no Viewport Capture state found. Wire a "
                "Viewport Capture node into this chain."
            )

        scene = bpy.context.scene

        scene.gentex_info = f"{self.name}: decoding image..."
        ai_image = load_image_bytes(bytes(png))
        captured_w, captured_h = ctx.captured_size
        ai_h, ai_w = ai_image.shape[:2]
        if (ai_h, ai_w) != (captured_h, captured_w):
            ai_image = _bilinear_resize(ai_image, captured_w, captured_h)

        if ctx.captured_visible is not None:
            m = ctx.captured_mask[..., None]
            ai_image = m * ai_image[..., :4] + (1.0 - m) * ctx.captured_visible[..., :4]

        scene.gentex_info = f"{self.name}: creating layer images..."
        first_obj = ctx.captured_per_obj[0][0]
        layer_index = len(first_obj.gentex_layers)
        seed = getattr(ctx.last_result, "seed", 0) if ctx.last_result else 0
        base_name = f"GenTex L{layer_index + 1} ({seed})"
        color_img = np_to_bpy(ai_image, base_name)

        mask_rgba = np.stack(
            [ctx.captured_mask] * 3 + [np.ones_like(ctx.captured_mask)], axis=-1,
        )
        mask_img = np_to_bpy(mask_rgba, base_name + " Mask")

        scene.gentex_info = f"{self.name}: building material..."
        for obj, uv_name, face_indices in ctx.captured_per_obj:
            layer = obj.gentex_layers.add()
            layer.name = base_name
            layer.image = color_img
            layer.mask_image = mask_img
            layer.uv_name = uv_name
            layer.opacity = 1.0
            layer.visible = True
            layer.seed = int(seed) & 0x7fffffff
            layer["face_indices"] = list(face_indices)
            obj.gentex_active_layer_index = len(obj.gentex_layers) - 1
            rebuild_layer_stack(obj)

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
