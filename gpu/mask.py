import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np


def render_selection_mask(width: int, height: int, view_matrix, projection_matrix,
                          objects=None, all_faces: bool = False) -> np.ndarray:
    """Render selected faces of edit-mode meshes as a white-on-black mask.

    Selected faces are rasterized in white through the given camera; everything
    else stays black. Also depth-tested against unselected faces, so faces hidden
    behind the model don't bleed through.

    Args:
        width, height: output size
        view_matrix, projection_matrix: typically region_3d.view_matrix / window_matrix
        objects: list of mesh objects in edit mode. If None, uses bpy.context's edit objects.
        all_faces: when True, treat every face as "selected" — useful for the
            whole-mesh mode of the projection operator, where the user hasn't
            highlighted faces but the mask still needs to mark every visible
            triangle. Self-occlusion is preserved via the depth test.

    Returns:
        (H, W) float32 array, 0..1
    """
    if objects is None:
        objects = [obj for obj in bpy.context.selected_objects
                   if obj.type == 'MESH' and obj.data.is_editmode]

    selected_geom = []   # (verts, indices) of SELECTED triangles
    occluder_geom = []   # (verts, indices) of UNSELECTED triangles (depth-only)

    for obj in objects:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()

        mw = obj.matrix_world
        v_world = np.empty((len(bm.verts), 3), dtype='f')
        for i, v in enumerate(bm.verts):
            co = mw @ v.co
            v_world[i] = (co.x, co.y, co.z)

        sel_idx = []
        unsel_idx = []
        for f in bm.faces:
            verts = [l.vert.index for l in f.loops]
            if len(verts) < 3:
                continue
            for i in range(1, len(verts) - 1):
                tri = (verts[0], verts[i], verts[i + 1])
                if all_faces or f.select:
                    sel_idx.append(tri)
                else:
                    unsel_idx.append(tri)

        if sel_idx:
            selected_geom.append((v_world, np.array(sel_idx, dtype='i')))
        if unsel_idx:
            occluder_geom.append((v_world, np.array(unsel_idx, dtype='i')))

    offscreen = gpu.types.GPUOffScreen(width, height)
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 1.0), depth=1.0)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)

        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(view_matrix)
            gpu.matrix.load_projection_matrix(projection_matrix)

            shader = gpu.shader.from_builtin('UNIFORM_COLOR')

            # First write occluders to depth only (color disabled)
            gpu.state.color_mask_set(False, False, False, False)
            for verts, idx in occluder_geom:
                batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=idx)
                batch.draw(shader)

            # Then draw selected faces in white with depth-test
            gpu.state.color_mask_set(True, True, True, True)
            shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
            for verts, idx in selected_geom:
                batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=idx)
                batch.draw(shader)

        result = np.array(fb.read_color(0, 0, width, height, 4, 0, 'FLOAT').to_list())

    gpu.state.depth_test_set('NONE')
    offscreen.free()
    # Take red channel as the mask
    return result[:, :, 0].astype(np.float32)
