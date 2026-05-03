"""Modddif-style multi-layer projection.

Pipeline (per generation):
  1. Render the current viewport (overlays off) -> visibleImage
  2. Render the selection-mask through the same camera
  3. Send (visibleImage, mask, prompt) to the provider as inpaint
     (or img2img + client-side mask composite if provider lacks inpaint)
  4. Snapshot the screen-space UVs of the selected faces into a NEW UV layer
     "Projected UVs N" and a NEW layer entry on the object
  5. Save the AI image and the mask as Blender images, attach to the layer
  6. Rebuild the object's layer-stack material so the new layer composites
     above all previous ones, masked by its own mask
"""

import bpy
import bmesh
from bpy_extras import view3d_utils
import numpy as np

from ..preferences import ADDON_PKG
from ..providers import PROVIDERS, GenerateRequest
from ..utils.image import np_to_bpy, np_to_png_bytes, load_image_bytes
from ..utils.threading import run_async, AsyncTask
from ..utils.material import rebuild_layer_stack, get_or_create_layer_material
from ..gpu.mask import render_selection_mask
from ..gpu.visible import render_visible_image


_active_task: AsyncTask | None = None


def _get_view3d(context):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
                    return area, region, space
    return None, None, None


def _next_layer_uv_name(obj) -> str:
    existing = {uv.name for uv in obj.data.uv_layers}
    i = 1
    while f"Projected UVs {i}" in existing:
        i += 1
    return f"Projected UVs {i}"


def _capture_projected_uvs_and_assign_material(obj, region, space_3d, region_w, region_h,
                                                uv_layer_name, material):
    """Bake screen-space UVs of selected faces into a new UV layer and reassign
    those faces to the layer-stack material.

    Returns the list of selected face indices.
    """
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()

    uv_layer = bm.loops.layers.uv.get(uv_layer_name)
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.new(uv_layer_name)

    # Resolve material slot index (append slot if missing)
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


class GENTEX_OT_ProjectLayer(bpy.types.Operator):
    bl_idname = "gentex.project_layer"
    bl_label = "Project as New Layer"
    bl_description = (
        "Generate a texture from the current viewport (visible image + selection mask) "
        "and add it as a new projected layer on top of existing ones"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if context.scene.gentex_progress > 0:
            return False
        if context.object is None or context.object.mode != 'EDIT':
            return False
        for obj in context.selected_objects:
            if obj.type != 'MESH' or not obj.data.is_editmode:
                continue
            try:
                bm = bmesh.from_edit_mesh(obj.data)
                if any(f.select for f in bm.faces):
                    return True
            except Exception:
                continue
        return False

    def execute(self, context):
        global _active_task

        prefs = context.preferences.addons[ADDON_PKG].preferences
        provider_name = prefs.provider
        api_key = prefs.get_api_key(provider_name)
        if not api_key:
            self.report({'ERROR'}, "No API key configured.")
            return {'CANCELLED'}
        if provider_name not in PROVIDERS:
            self.report({'ERROR'}, "No provider selected.")
            return {'CANCELLED'}

        scene = context.scene
        prompt = scene.gentex_prompt
        if not prompt.strip():
            self.report({'ERROR'}, "Enter a prompt.")
            return {'CANCELLED'}

        area, region, space_3d = _get_view3d(context)
        if area is None:
            self.report({'ERROR'}, "No 3D viewport found.")
            return {'CANCELLED'}

        region_w, region_h = region.width, region.height

        scene.gentex_info = "Capturing viewport..."
        scene.gentex_progress = 1

        # 1. Visible image (the textured mesh as user sees it)
        try:
            visible = render_visible_image(area, region_w, region_h)
        except Exception as e:
            scene.gentex_progress = 0
            scene.gentex_info = f"Error: {e}"
            return {'CANCELLED'}

        # 2. Selection mask through same camera
        edit_objs = [o for o in context.selected_objects
                     if o.type == 'MESH' and o.data.is_editmode]
        mask = render_selection_mask(
            region_w, region_h,
            view_matrix=space_3d.region_3d.view_matrix,
            projection_matrix=space_3d.region_3d.window_matrix,
            objects=edit_objs,
        )
        # GPU buffer is bottom-up; flip to match top-down convention used for PNG
        mask = np.flipud(mask)

        if mask.max() <= 0.0:
            scene.gentex_progress = 0
            scene.gentex_info = "Error: empty mask"
            self.report({'ERROR'}, "Selection produced an empty mask.")
            return {'CANCELLED'}

        # 3. Snapshot screen-space UVs into a new UV layer per object,
        #    and assign those faces to the layer-stack material now (in edit mode)
        scene.gentex_info = "Snapshotting UVs..."
        per_obj_uv = []  # (obj, uv_name, selected_face_indices)
        for obj in edit_objs:
            uv_name = _next_layer_uv_name(obj)
            mat = get_or_create_layer_material(obj)
            face_idx = _capture_projected_uvs_and_assign_material(
                obj, region, space_3d, region_w, region_h, uv_name, mat,
            )
            if face_idx:
                per_obj_uv.append((obj, uv_name, face_idx))

        if not per_obj_uv:
            scene.gentex_progress = 0
            scene.gentex_info = ""
            self.report({'ERROR'}, "No selected faces.")
            return {'CANCELLED'}

        # 4. Build provider request
        scene.gentex_info = "Generating..."

        # Resize visible/mask to provider target dimensions
        target_w = scene.gentex_width
        target_h = scene.gentex_height
        visible_resized = _nn_resize(visible, target_w, target_h)
        mask_resized = _nn_resize(mask, target_w, target_h)

        request = GenerateRequest(
            prompt=prompt,
            negative_prompt=scene.gentex_negative_prompt,
            width=target_w, height=target_h,
            init_image=np_to_png_bytes(visible_resized),
            mask_image=np_to_png_bytes(mask_resized),
            strength=scene.gentex_strength,
        )

        provider_cls = PROVIDERS[provider_name]
        provider = provider_cls()
        # Read inpaint support from the instance: some providers (fal) decide
        # this dynamically based on the configured model.
        provider_supports_inpaint = getattr(provider, "supports_inpaint", False)

        if not provider_supports_inpaint:
            # Provider can't do real inpaint; we'll do client-side composite below
            request.mask_image = None

        # Capture data needed by the callback
        captured_mask = mask_resized
        captured_visible = visible_resized
        captured_per_obj = per_obj_uv
        composite_locally = not provider_supports_inpaint

        def do_generate():
            return provider.generate(request, api_key)

        def on_complete(result):
            global _active_task
            _active_task = None
            try:
                ai_image = load_image_bytes(result.image_bytes)
                # Match viewport orientation
                ai_h, ai_w = ai_image.shape[:2]
                if (ai_h, ai_w) != (target_h, target_w):
                    ai_image = _nn_resize(ai_image, target_w, target_h)

                # Composite locally if provider couldn't inpaint:
                # final = mask * ai_image + (1 - mask) * visible
                if composite_locally:
                    m = captured_mask[..., None]
                    ai_image = m * ai_image[..., :4] + (1.0 - m) * captured_visible[..., :4]

                layer_index = len(bpy.context.object.gentex_layers) if bpy.context.object else 0
                # Layer is per-object; we add one entry per object so the per-object UV
                # snapshot is consistent. The image and mask are shared.
                base_name = f"GenTex L{layer_index + 1} ({result.seed})"
                color_img = np_to_bpy(ai_image, base_name)

                mask_arr = captured_mask
                # Mask as RGBA grayscale for storage
                mask_rgba = np.stack([mask_arr] * 3 + [np.ones_like(mask_arr)], axis=-1)
                mask_img = np_to_bpy(mask_rgba, base_name + " Mask")

                for obj, uv_name, _face_indices in captured_per_obj:
                    layer = obj.gentex_layers.add()
                    layer.name = base_name
                    layer.image = color_img
                    layer.mask_image = mask_img
                    layer.uv_name = uv_name
                    layer.opacity = 1.0
                    layer.visible = True
                    layer.seed = result.seed
                    obj.gentex_active_layer_index = len(obj.gentex_layers) - 1
                    rebuild_layer_stack(obj)

                scene.gentex_info = ""
                scene.gentex_progress = 0
                for window in bpy.context.window_manager.windows:
                    for a in window.screen.areas:
                        if a.type == 'VIEW_3D':
                            a.tag_redraw()
            except Exception as e:
                scene.gentex_progress = 0
                scene.gentex_info = f"Error: {e}"
                import traceback
                traceback.print_exc()

        def on_error(err):
            global _active_task
            _active_task = None
            scene.gentex_progress = 0
            scene.gentex_info = f"Error: {err}"

        _active_task = run_async(do_generate, on_complete, on_error)
        return {'FINISHED'}


def _nn_resize(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    """Nearest-neighbour resize. Avoids pulling in a heavy dep just for this."""
    src_h = arr.shape[0]
    src_w = arr.shape[1]
    if src_w == w and src_h == h:
        return arr
    row_idx = (np.linspace(0, src_h - 1, h)).astype(int)
    col_idx = (np.linspace(0, src_w - 1, w)).astype(int)
    if arr.ndim == 2:
        return arr[np.ix_(row_idx, col_idx)]
    return arr[np.ix_(row_idx, col_idx)]
