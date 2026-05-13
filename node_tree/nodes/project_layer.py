"""Project Layer node — terminal node that writes the generated image onto
the mesh as a new projected layer.

Requires a Viewport Capture node upstream (anywhere in the chain) — it reads
`ctx.captured_per_obj` for the per-mesh UV name + face indices, and
`ctx.captured_mask`/`captured_visible` for local-mask composite fallback when
the provider lacked native inpaint.
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

    auto_bake: bpy.props.BoolProperty(
        name="Auto Bake",
        description="After projecting, bake the layer stack into the object's real UVs",
        default=True,
    )

    def init(self, context):
        self.inputs.new("GenTexImageSocket", "Image")
        self.inputs.new("GenTexImageSocket", "Capture")

    def draw_buttons(self, context, layout):
        layout.prop(self, "auto_bake")
        layout.label(text="Needs Viewport Capture upstream", icon='INFO')

    def evaluate(self, ctx):
        png = upstream_value(self, "Image", ctx, default=None)
        if not isinstance(png, (bytes, bytearray)):
            raise RuntimeError(f"{self.name}: no image on input")
        if not ctx.captured_per_obj:
            raise RuntimeError(
                f"{self.name}: no Viewport Capture state found. Wire a "
                "Viewport Capture node into this chain."
            )

        ai_image = load_image_bytes(bytes(png))
        captured_w, captured_h = ctx.captured_size
        ai_h, ai_w = ai_image.shape[:2]
        if (ai_h, ai_w) != (captured_h, captured_w):
            ai_image = _bilinear_resize(ai_image, captured_w, captured_h)

        # Force pixels outside the selection mask back to the captured visible
        # image. If the provider already inpainted exactly, this is a no-op;
        # if it ignored the mask (Gemini etc.), this preserves the surroundings.
        if ctx.captured_visible is not None:
            m = ctx.captured_mask[..., None]
            ai_image = m * ai_image[..., :4] + (1.0 - m) * ctx.captured_visible[..., :4]

        first_obj = ctx.captured_per_obj[0][0]
        layer_index = len(first_obj.gentex_layers)
        seed = getattr(ctx.last_result, "seed", 0) if ctx.last_result else 0
        base_name = f"GenTex L{layer_index + 1} ({seed})"
        color_img = np_to_bpy(ai_image, base_name)

        mask_rgba = np.stack(
            [ctx.captured_mask] * 3 + [np.ones_like(ctx.captured_mask)], axis=-1,
        )
        mask_img = np_to_bpy(mask_rgba, base_name + " Mask")

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

        if self.auto_bake:
            for obj, _, _ in ctx.captured_per_obj:
                if obj.data.uv_layers.active is None:
                    continue
                prev_active = bpy.context.view_layer.objects.active
                bpy.context.view_layer.objects.active = obj
                try:
                    bpy.ops.gentex.bake_layers()
                    obj.gentex_use_baked = True
                except Exception as bake_err:
                    print(f"[GenTex] auto-bake failed: {bake_err}")
                finally:
                    bpy.context.view_layer.objects.active = prev_active

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
