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

        # Don't switch modes — calling `bpy.ops.object.mode_set` from inside
        # an operator's execute (especially when this operator is itself
        # invoked from another operator's callback like project_layer's
        # on_complete) is unreliable: the switch can be deferred or crash
        # Blender. Instead, read UVs directly from the live edit-mesh bmesh
        # when in Edit Mode, and from the mesh data otherwise.
        result = self._bake(obj, target_uv.name, self.width, self.height)

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
        # In Edit Mode, the live bmesh holds the truth — `obj.data.uv_layers`
        # is stale until the user exits edit mode. Iterate the editing bmesh
        # directly. `bake_to_uv` does its own per-face fan-triangulation, so
        # the mesh doesn't need a destructive triangulate pass.
        owned = False
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            owned = True

        try:
            src_layer = bm.loops.layers.uv.get(src_uv_name)
            dest_layer = bm.loops.layers.uv.get(dest_uv_name)
            if src_layer is None or dest_layer is None:
                return None

            src_w, src_h = image.size[0], image.size[1]
            src_pixels = np.empty(src_w * src_h * 4, dtype=np.float32)
            image.pixels.foreach_get(src_pixels)

            flat = bake_to_uv(
                src_pixels, src_w, src_h,
                bm, src_layer, dest_layer,
                w, h,
            )
            return np.flipud(flat.reshape(h, w, 4))
        finally:
            if owned:
                bm.free()


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
    out.location = (900, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (600, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.location = (0, 0)
    tex.image = image
    # A single projection only covers a fraction of the UV layout; everywhere
    # the bake didn't reach has alpha=0. Without this mix, those regions would
    # render pure black. Fall back to neutral grey so unbaked faces look like
    # plain unpainted material instead of "broken texture".
    fallback = nt.nodes.new("ShaderNodeRGB")
    fallback.location = (0, -250)
    fallback.outputs[0].default_value = (0.5, 0.5, 0.5, 1.0)
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.location = (300, 0)
    mix.blend_type = 'MIX'
    nt.links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
    nt.links.new(fallback.outputs[0], mix.inputs[1])
    nt.links.new(tex.outputs["Color"], mix.inputs[2])
    nt.links.new(mix.outputs[0], bsdf.inputs["Base Color"])
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


SNAPSHOT_PER_FACE_KEY = "gentex_baked_face_orig_idx"


def apply_baked_toggle(obj, enabled: bool):
    """Switch the mesh between its native shading and the single baked image.

    On enable: snapshot every face's current `material_index` and reassign
    every face to the baked material. The baked material's alpha-driven mix
    falls back to neutral grey on unbaked UV regions, so faces that never had
    a real material to begin with no longer show as Blender's pink "missing
    material" default.

    On disable: restore each face to its snapshotted slot.

    Mode-aware: in Edit Mode it goes through bmesh + `bmesh.update_edit_mesh`,
    otherwise direct writes to mesh polygons are silently overwritten by the
    bmesh sync.

    No-ops when there's no baked image (toggle UI shouldn't appear in that case).
    """
    if obj is None or obj.type != 'MESH':
        return
    baked = obj.gentex_baked_image
    if baked is None:
        return

    in_edit = obj.mode == 'EDIT'

    if enabled:
        uv_name = obj.gentex_baked_uv
        if not uv_name and obj.data.uv_layers.active:
            uv_name = obj.data.uv_layers.active.name
        baked_mat = _get_or_create_baked_material(obj, baked, uv_name)
        baked_idx = _ensure_slot(obj, baked_mat)

        if in_edit:
            import bmesh
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            orig = [f.material_index for f in bm.faces]
            for f in bm.faces:
                f.material_index = baked_idx
            bmesh.update_edit_mesh(obj.data)
        else:
            polys = obj.data.polygons
            orig = [p.material_index for p in polys]
            for p in polys:
                p.material_index = baked_idx

        obj[SNAPSHOT_PER_FACE_KEY] = orig
        # Keep the legacy key cleared so we never accidentally fall back to
        # the old "only restore the listed faces to the layer slot" path.
        if SNAPSHOT_KEY in obj.keys():
            del obj[SNAPSHOT_KEY]
        obj.active_material_index = baked_idx
    else:
        orig = obj.get(SNAPSHOT_PER_FACE_KEY)
        if orig is None:
            # No snapshot — fall back to leaving faces as they are. Clean up
            # any stale snapshot keys so we don't trip up on next enable.
            for k in (SNAPSHOT_PER_FACE_KEY, SNAPSHOT_KEY):
                if k in obj.keys():
                    del obj[k]
            return

        if in_edit:
            import bmesh
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            for f in bm.faces:
                if f.index < len(orig):
                    f.material_index = int(orig[f.index])
            bmesh.update_edit_mesh(obj.data)
        else:
            polys = obj.data.polygons
            for i, p in enumerate(polys):
                if i < len(orig):
                    p.material_index = int(orig[i])

        del obj[SNAPSHOT_PER_FACE_KEY]
        if SNAPSHOT_KEY in obj.keys():
            del obj[SNAPSHOT_KEY]

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
