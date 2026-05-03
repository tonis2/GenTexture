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
from ..providers import PROVIDERS, GenerateRequest, CAP_INPAINT
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
            cls.poll_message_set("A generation is already running")
            return False
        if context.object is None or context.object.mode != 'EDIT':
            cls.poll_message_set("Enter Edit Mode on a mesh")
            return False

        # objects_in_mode is the right API: in Edit Mode, selected_objects
        # may be empty even though the active mesh is being edited.
        edit_meshes = [o for o in context.objects_in_mode
                       if o.type == 'MESH' and o.data.is_editmode]
        if not edit_meshes:
            cls.poll_message_set("No mesh currently in Edit Mode")
            return False

        for obj in edit_meshes:
            try:
                bm = bmesh.from_edit_mesh(obj.data)
                if any(f.select for f in bm.faces):
                    return True
            except Exception:
                continue

        cls.poll_message_set(
            "Select faces to texture. Tip: switch to Face Select mode (3) "
            "and click a face"
        )
        return False

    def execute(self, context):
        global _active_task

        prefs = context.preferences.addons[ADDON_PKG].preferences
        provider_name = prefs.provider
        settings = prefs.get_provider_settings(provider_name)
        api_key = settings.get("api_key", "")
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
        target_w = scene.gentex_width
        target_h = scene.gentex_height

        scene.gentex_info = "Capturing viewport..."
        scene.gentex_progress = 1

        # Render the viewport and selection mask at the viewport's native size
        # — that's what the viewport projection matrix is set up for. Then
        # bilinearly resize to the AI's target dimensions. Rendering directly
        # at target size would mismatch the projection matrix's aspect ratio
        # and squish the mask relative to the visible image.
        try:
            visible = render_visible_image(area, region_w, region_h)
        except Exception as e:
            scene.gentex_progress = 0
            scene.gentex_info = f"Error: {e}"
            return {'CANCELLED'}

        edit_objs = [o for o in context.objects_in_mode
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

        # Snapshot screen-space UVs into a new UV layer per object, and assign
        # those faces to the layer-stack material now (in edit mode). UVs are
        # captured against the live viewport region, not the AI render size —
        # they're normalised so the AI image dimensions don't matter.
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

        scene.gentex_info = "Generating..."
        visible_resized = _bilinear_resize(visible, target_w, target_h)
        mask_resized = _bilinear_resize(mask, target_w, target_h)

        # Gather reference images (any Image data-block, including layer images
        # picked from the panel). Each is encoded once here on the main thread.
        reference_pngs = []
        for ref in scene.gentex_references:
            if ref.image is None:
                continue
            try:
                from ..utils.image import bpy_to_np
                arr = bpy_to_np(ref.image)
                reference_pngs.append(np_to_png_bytes(arr))
            except Exception:
                pass

        request = GenerateRequest(
            prompt=prompt,
            negative_prompt=scene.gentex_negative_prompt,
            width=target_w, height=target_h,
            init_image=np_to_png_bytes(visible_resized),
            mask_image=np_to_png_bytes(mask_resized),
            reference_images=reference_pngs,
            strength=scene.gentex_strength,
        )

        provider_cls = PROVIDERS[provider_name]
        provider = provider_cls(settings)
        # Capabilities are declared by the provider; falls back to
        # client-side mask composite if it can't do real inpaint.
        provider_supports_inpaint = CAP_INPAINT in provider_cls.capabilities()

        if not provider_supports_inpaint:
            # Provider can't do real inpaint; we'll do client-side composite below
            request.mask_image = None

        # Capture data needed by the callback
        captured_mask = mask_resized
        captured_visible = visible_resized
        captured_per_obj = per_obj_uv
        composite_locally = not provider_supports_inpaint

        def do_generate():
            return provider.generate(request)

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

                for obj, uv_name, face_indices in captured_per_obj:
                    layer = obj.gentex_layers.add()
                    layer.name = base_name
                    layer.image = color_img
                    layer.mask_image = mask_img
                    layer.uv_name = uv_name
                    layer.opacity = 1.0
                    layer.visible = True
                    layer.seed = result.seed
                    # Store which faces this layer covers so later cleanup can
                    # restore them when the layer is removed.
                    layer["face_indices"] = list(face_indices)
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


def _bilinear_resize(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    """Bilinear resize for (H, W) or (H, W, C) float arrays.

    Avoids the row-repetition / stair-step artifacts of nearest-neighbour when
    upscaling, and the aliasing of NN downscaling. Pure numpy — no extra deps.
    """
    src_h = arr.shape[0]
    src_w = arr.shape[1]
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


# Back-compat alias: existing callers expected nearest-neighbour, but bilinear
# is strictly better for the smooth signals we resize here (visible image,
# AI output, mask).
_nn_resize = _bilinear_resize
