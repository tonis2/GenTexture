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

        obj.gentex_baked_image = baked
        obj.gentex_baked_uv = target_uv.name

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
                mask = color[..., 3]

            f = (mask * layer.opacity).clip(0.0, 1.0)[..., None]
            out[..., :3] = f * color[..., :3] + (1.0 - f) * out[..., :3]
            out[..., 3] = (f[..., 0] + out[..., 3] * (1.0 - f[..., 0])).clip(0.0, 1.0)

        return out

    def _bake_one(self, obj, image, src_uv_name, dest_uv_name, w, h):
        """Bake a single image from src_uv -> dest_uv, returns (h, w, 4)."""
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])

        src_layer = bm.loops.layers.uv.get(src_uv_name)
        dest_layer = bm.loops.layers.uv.get(dest_uv_name)
        if src_layer is None or dest_layer is None:
            bm.free()
            return None

        src_w, src_h = image.size[0], image.size[1]
        src_pixels = np.empty(src_w * src_h * 4, dtype=np.float32)
        image.pixels.foreach_get(src_pixels)

        flat = bake_to_uv(
            src_pixels, src_w, src_h,
            bm, src_layer, dest_layer,
            w, h,
        )
        bm.free()
        return np.flipud(flat.reshape(h, w, 4))


BAKED_MAT_MARKER = "gentex_baked_material"


def _get_or_create_baked_material(obj, image, uv_name):
    mat = None
    for slot in obj.material_slots:
        m = slot.material
        if m and m.get(BAKED_MAT_MARKER):
            mat = m
            break
    if mat is None:
        mat = bpy.data.materials.new(name="gentex-baked-material")
        mat.use_nodes = True
        mat[BAKED_MAT_MARKER] = True
        obj.data.materials.append(mat)

    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (300, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.location = (0, 0)
    tex.image = image
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    if uv_name:
        uv = nt.nodes.new("ShaderNodeUVMap")
        uv.location = (-300, 0)
        uv.uv_map = uv_name
        nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    return mat


SNAPSHOT_KEY = "gentex_baked_face_idx"


def _ensure_slot(obj, material) -> int:
    for i, slot in enumerate(obj.material_slots):
        if slot.material == material:
            return i
    obj.data.materials.append(material)
    return len(obj.material_slots) - 1


def _find_layer_stack_material(obj):
    from ..utils.material import MARKER_KEY as LAYER_MARKER
    for slot in obj.material_slots:
        m = slot.material
        if m and m.get(LAYER_MARKER):
            return m
    return None


def apply_baked_toggle(obj, enabled: bool):
    """Switch the mesh between layer-stack shading and the single baked image.

    On enable: snapshot the face indices currently assigned to the layer-stack
    material and reassign them to the baked material. On disable: restore those
    same faces back to the layer-stack material.

    No-ops when there's no baked image (toggle UI shouldn't appear in that case).
    """
    if obj is None or obj.type != 'MESH':
        return
    baked = obj.gentex_baked_image
    if baked is None:
        return

    polys = obj.data.polygons
    layer_mat = _find_layer_stack_material(obj)

    if enabled:
        uv_name = obj.gentex_baked_uv
        if not uv_name and obj.data.uv_layers.active:
            uv_name = obj.data.uv_layers.active.name
        baked_mat = _get_or_create_baked_material(obj, baked, uv_name)
        baked_idx = _ensure_slot(obj, baked_mat)

        if layer_mat is not None:
            layer_idx = _ensure_slot(obj, layer_mat)
            moved = [i for i, p in enumerate(polys) if p.material_index == layer_idx]
        else:
            moved = list(range(len(polys)))

        for i in moved:
            polys[i].material_index = baked_idx
        obj[SNAPSHOT_KEY] = moved
        obj.active_material_index = baked_idx
    else:
        moved = obj.get(SNAPSHOT_KEY)
        if moved is None or layer_mat is None:
            if SNAPSHOT_KEY in obj.keys():
                del obj[SNAPSHOT_KEY]
            return
        layer_idx = _ensure_slot(obj, layer_mat)
        for i in moved:
            if 0 <= i < len(polys):
                polys[i].material_index = layer_idx
        del obj[SNAPSHOT_KEY]
        obj.active_material_index = layer_idx

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
