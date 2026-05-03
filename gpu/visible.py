import os
import tempfile

import bpy
import numpy as np

from ..utils.image import bpy_to_np


def render_visible_image(area, region_width: int, region_height: int) -> np.ndarray:
    """Render the current 3D viewport via OpenGL with overlays disabled.

    This produces the "visible image" in Modddif's sense: what the user is seeing
    of the textured mesh from the active camera. Used as init_image for img2img
    so generations stay consistent with already-textured layers.

    Args:
        area: a VIEW_3D area
        region_width, region_height: target render size (matches viewport region)

    Returns:
        (H, W, 4) float32 RGBA, top-to-bottom
    """
    scene = bpy.context.scene
    res_x = scene.render.resolution_x
    res_y = scene.render.resolution_y
    render_filepath = scene.render.filepath
    file_format = scene.render.image_settings.file_format

    scene.render.resolution_x = region_width
    scene.render.resolution_y = region_height

    hidden_spaces = []
    for sp in area.spaces:
        if sp.type == 'VIEW_3D' and sp.overlay.show_overlays:
            hidden_spaces.append(sp)
            sp.overlay.show_overlays = False

    # Per-object wire display (`obj.show_wire = True` or `display_type='WIRE'`)
    # forces a wireframe in solid shading regardless of the overlay toggle, so
    # FLUX preserves those edges outside the mask. Force the active object to
    # plain solid display for the render and restore after.
    obj = bpy.context.active_object
    show_wire_prev = None
    display_type_prev = None
    if obj is not None:
        show_wire_prev = obj.show_wire
        display_type_prev = obj.display_type
        obj.show_wire = False
        if obj.display_type in {'WIRE', 'BOUNDS'}:
            obj.display_type = 'SOLID'

    # Edit Mode bakes the mesh wireframe straight into `render.opengl` output
    # regardless of the overlay toggle — FLUX then preserves that wire pattern
    # outside the mask, polluting every subsequent UV sample. Drop to Object
    # Mode for the render so the mesh comes back clean.
    was_edit = obj is not None and obj.mode == 'EDIT'
    if was_edit:
        bpy.ops.object.mode_set(mode='OBJECT')

    out_path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = out_path
    try:
        bpy.ops.render.opengl(write_still=True, view_context=True)
        img = bpy.data.images.load(out_path)
        try:
            arr = bpy_to_np(img)
        finally:
            bpy.data.images.remove(img)
    finally:
        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')
        for sp in hidden_spaces:
            sp.overlay.show_overlays = True
        if obj is not None and show_wire_prev is not None:
            obj.show_wire = show_wire_prev
            obj.display_type = display_type_prev
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
        scene.render.filepath = render_filepath
        scene.render.image_settings.file_format = file_format
        try:
            os.unlink(out_path)
        except OSError:
            pass

    return arr
