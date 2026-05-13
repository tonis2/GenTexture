"""Viewport Capture node — renders the current 3D viewport selection into:

    - Color image of the visible geometry (the AI's ``init_image``)
    - Selection mask (the AI's ``mask_image``)
    - Depth map (linear depth ControlNet conditioning)
    - Capture handle (an opaque marker that downstream Project Layer reads)

Also snapshots screen-space UVs of the selected faces into a new UV layer on
each edit-mode mesh, so the eventually-generated image lines up with geometry.

Must be executed while at least one mesh is in Edit Mode with selected faces.
The executor validates context once up-front before any expensive work runs.
"""

import bmesh
import bpy
import numpy as np
from bpy_extras import view3d_utils

from ._base import GenTexPipelineNodeBase, upstream_value
from ...gpu.depth import render_depth_map
from ...gpu.mask import render_selection_mask
from ...gpu.visible import render_visible_image
from ...utils.image import np_to_png_bytes
from ...utils.material import get_or_create_layer_material


def _next_layer_uv_name(obj) -> str:
    existing = {uv.name for uv in obj.data.uv_layers}
    i = 1
    while f"Projected UVs {i}" in existing:
        i += 1
    return f"Projected UVs {i}"


def _capture_projected_uvs_and_assign_material(
    obj, region, space_3d, region_w, region_h, uv_layer_name, material,
):
    """Bake screen-space UVs of selected faces into a new UV layer and reassign
    those faces to the layer-stack material. Returns selected face indices.
    """
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()

    uv_layer = bm.loops.layers.uv.get(uv_layer_name)
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.new(uv_layer_name)

    mat_index = -1
    for i, slot in enumerate(obj.material_slots):
        if slot.material == material:
            mat_index = i
            break
    if mat_index < 0:
        obj.data.materials.append(material)
        mat_index = len(obj.material_slots) - 1

    rv3d = space_3d.region_3d
    selected_faces = []

    for face in bm.faces:
        if not face.select:
            continue
        selected_faces.append(face.index)
        face.material_index = mat_index
        for loop in face.loops:
            world_co = obj.matrix_world @ loop.vert.co
            screen = view3d_utils.location_3d_to_region_2d(region, rv3d, world_co)
            if screen is None:
                u, v = 0.5, 0.5
            else:
                u = max(0.0, min(1.0, screen[0] / region_w))
                v = max(0.0, min(1.0, screen[1] / region_h))
            loop[uv_layer].uv = (u, v)

    bmesh.update_edit_mesh(obj.data)
    return selected_faces


class GenTexNodeViewportCapture(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeViewportCapture"
    bl_label = "Viewport Capture"
    bl_icon = "VIEW_CAMERA"

    width: bpy.props.IntProperty(name="Width", default=1024, min=64, max=4096)
    height: bpy.props.IntProperty(name="Height", default=1024, min=64, max=4096)

    def init(self, context):
        win = self.inputs.new("NodeSocketInt", "Width")
        win.default_value = 1024
        hin = self.inputs.new("NodeSocketInt", "Height")
        hin.default_value = 1024
        self.outputs.new("GenTexImageSocket", "Color")
        self.outputs.new("GenTexImageSocket", "Mask")
        self.outputs.new("GenTexImageSocket", "Depth")
        self.outputs.new("GenTexImageSocket", "Capture")

    def draw_buttons(self, context, layout):
        layout.label(text="Renders from 3D viewport", icon='INFO')
        layout.label(text="Needs Edit Mode + faces selected")

    def evaluate(self, ctx):
        if ctx.area is None or ctx.region is None or ctx.space_3d is None:
            raise RuntimeError(
                f"{self.name}: no 3D viewport found. Open a 3D View and run again."
            )
        if not ctx.edit_objs:
            raise RuntimeError(
                f"{self.name}: no mesh in Edit Mode. Enter Edit Mode on a mesh."
            )

        target_w = int(upstream_value(self, "Width", ctx, default=self.width) or self.width)
        target_h = int(upstream_value(self, "Height", ctx, default=self.height) or self.height)

        actual_w, actual_h = ctx.region.width, ctx.region.height
        if target_w > 0 and target_h > 0 and actual_w > 0 and actual_h > 0:
            scale = min(target_w / actual_w, target_h / actual_h)
            render_w = max(1, int(actual_w * scale))
            render_h = max(1, int(actual_h * scale))
        else:
            render_w, render_h = actual_w, actual_h

        # Hide non-edit meshes so the viewport capture isolates the target.
        edit_obj_names = {o.name for o in ctx.edit_objs}
        hide_restore = {}
        for o in bpy.data.objects:
            if o.type == 'MESH' and o.name not in edit_obj_names:
                hide_restore[o.name] = o.hide_viewport
                o.hide_viewport = True

        try:
            visible = render_visible_image(ctx.area, render_w, render_h)
        finally:
            for name, prev in hide_restore.items():
                if name in bpy.data.objects:
                    bpy.data.objects[name].hide_viewport = prev

        mask = render_selection_mask(
            render_w, render_h,
            view_matrix=ctx.space_3d.region_3d.view_matrix,
            projection_matrix=ctx.space_3d.region_3d.window_matrix,
            objects=ctx.edit_objs,
        )
        mask = np.flipud(mask)

        depth = render_depth_map(
            render_w, render_h,
            view_matrix=ctx.space_3d.region_3d.view_matrix,
            projection_matrix=ctx.space_3d.region_3d.window_matrix,
            objects=ctx.edit_objs,
        )
        depth = np.flipud(depth)

        if mask.max() <= 0.0:
            raise RuntimeError(
                f"{self.name}: empty mask — no faces selected on the edit-mode mesh."
            )

        # Snapshot UVs against the actual viewport region size.
        captured = []
        for obj in ctx.edit_objs:
            uv_name = _next_layer_uv_name(obj)
            mat = get_or_create_layer_material(obj)
            face_idx = _capture_projected_uvs_and_assign_material(
                obj, ctx.region, ctx.space_3d, actual_w, actual_h, uv_name, mat,
            )
            if face_idx:
                captured.append((obj, uv_name, face_idx))

        if not captured:
            raise RuntimeError(f"{self.name}: no faces selected.")

        ctx.captured_per_obj = captured
        ctx.captured_visible = visible
        ctx.captured_mask = mask
        ctx.captured_depth = depth
        ctx.captured_size = (render_w, render_h)

        mask_rgba = np.stack([mask] * 3 + [np.ones_like(mask)], axis=-1)
        depth_rgba = np.stack([depth] * 3 + [np.ones_like(depth)], axis=-1)

        ctx.cache[self.cache_key(self.outputs["Color"])] = np_to_png_bytes(visible)
        ctx.cache[self.cache_key(self.outputs["Mask"])] = np_to_png_bytes(mask_rgba)
        ctx.cache[self.cache_key(self.outputs["Depth"])] = np_to_png_bytes(depth_rgba)
        # The Capture handle is just a marker — downstream Project Layer uses
        # ctx.captured_per_obj. We still cache a tiny byte string so the socket
        # registers as "populated" in the executor.
        ctx.cache[self.cache_key(self.outputs["Capture"])] = b"capture"
