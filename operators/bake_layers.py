"""Bake the projected-layer stack into a single UV-space texture.

For each layer, sample the layer's image through its projected UV layer and
write into the destination UV layer's space. Composite top-down: the topmost
visible layer wins where its mask is 1.

Equivalent to Modddif's mergeLayers: a per-pixel composite of all
ProjectedTextureLayers into a single UvmappedTextureLayer.
"""

import bpy
import bmesh
import numpy as np

from ..gpu.bake import bake_to_uv


class GENTEX_OT_BakeLayers(bpy.types.Operator):
    bl_idname = "gentex.bake_layers"
    bl_label = "Bake Layers"
    bl_description = "Composite all projected layers into a single texture in the active UV layout"
    bl_options = {'REGISTER'}

    width: bpy.props.IntProperty(name="Width", default=2048, min=256, max=8192)
    height: bpy.props.IntProperty(name="Height", default=2048, min=256, max=8192)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        if not obj.data.uv_layers:
            return False
        if not obj.gentex_layers:
            return False
        return True

    def execute(self, context):
        obj = context.active_object

        target_uv = obj.data.uv_layers.active
        if target_uv is None:
            self.report({'ERROR'}, "Active UV layer required.")
            return {'CANCELLED'}

        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')

        try:
            result = self._bake(obj, target_uv.name, self.width, self.height)
        finally:
            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')

        if result is None:
            self.report({'ERROR'}, "Baking failed.")
            return {'CANCELLED'}

        # Write the baked image as a Blender image
        from ..utils.image import np_to_bpy
        baked = np_to_bpy(result, f"GenTex Baked ({obj.name})")
        baked.update()

        self.report({'INFO'}, f"Baked {len(obj.gentex_layers)} layer(s) -> {baked.name}")
        return {'FINISHED'}

    def _bake(self, obj, target_uv_name, w, h):
        """Composite all visible layers into a (h, w, 4) RGBA image."""
        out = np.zeros((h, w, 4), dtype=np.float32)

        for layer in obj.gentex_layers:
            if not layer.visible or layer.image is None or layer.opacity <= 0.0:
                continue
            if layer.uv_name not in {uv.name for uv in obj.data.uv_layers}:
                continue

            color = self._bake_one(obj, layer.image, layer.uv_name, target_uv_name, w, h)
            if color is None:
                continue

            if layer.mask_image is not None:
                mask = self._bake_one(obj, layer.mask_image, layer.uv_name, target_uv_name, w, h)
                if mask is None:
                    mask = np.ones((h, w), dtype=np.float32)
                else:
                    mask = mask[..., 0]
            else:
                mask = color[..., 3]  # use image alpha

            # Combine with per-layer opacity
            f = (mask * layer.opacity).clip(0.0, 1.0)[..., None]
            out[..., :3] = f * color[..., :3] + (1.0 - f) * out[..., :3]
            out[..., 3] = (f[..., 0] + out[..., 3] * (1.0 - f[..., 0])).clip(0.0, 1.0)

        return out

    def _bake_one(self, obj, image, src_uv_name, dest_uv_name, w, h):
        """Bake a single image from src_uv -> dest_uv, returns (h, w, 4)."""
        # Build a temp BMesh in object space (we're in OBJECT mode here)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        # Triangulate so the bake shader's fan triangulation lines up with simple tris
        bmesh.ops.triangulate(bm, faces=bm.faces[:])

        src_layer = bm.loops.layers.uv.get(src_uv_name)
        dest_layer = bm.loops.layers.uv.get(dest_uv_name)
        if src_layer is None or dest_layer is None:
            bm.free()
            return None

        # Source pixels (RGBA float) - bottom-to-top in Blender, but bake shader
        # works in UV space directly so orientation just has to match the shader's
        # sampling convention. The current shader samples from a GPU texture
        # whose v=0 is at the bottom; Blender's image.pixels match that.
        src_w, src_h = image.size[0], image.size[1]
        src_pixels = np.empty(src_w * src_h * 4, dtype=np.float32)
        image.pixels.foreach_get(src_pixels)

        flat = bake_to_uv(
            src_pixels, src_w, src_h,
            bm, src_layer, dest_layer,
            w, h,
        )
        bm.free()
        # Bake reads bottom-to-top; flip to top-down to match np_to_bpy convention
        # (which itself flips again before storing in Blender).
        return np.flipud(flat.reshape(h, w, 4))
