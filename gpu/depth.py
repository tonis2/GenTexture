"""Linear depth map for the selected mesh, rendered through the viewport camera.

Output convention matches what depth ControlNets expect: white = close to
camera, black = far. Background is black (no geometry sampled there).

Returned as a flat (H, W) float32 array in [0, 1], top-to-bottom — the same
layout as `render_selection_mask` after its caller flips it.
"""

import bmesh
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader


def render_depth_map(width: int, height: int,
                     view_matrix, projection_matrix,
                     objects) -> np.ndarray:
    """Render a normalized linear depth map of `objects` from the given camera.

    Args:
        width, height: output size
        view_matrix, projection_matrix: region_3d.view_matrix / window_matrix
        objects: list of mesh objects (any mode). Unselected faces still
            occlude (z-test) so back faces don't bleed through.

    Returns:
        (H, W) float32 in [0,1]. White = near camera, black = far / empty.
    """
    if not objects:
        return np.zeros((height, width), dtype=np.float32)

    vm = np.array(view_matrix)
    all_verts = []
    all_idx = []
    all_view_z = []
    vert_offset = 0

    for obj in objects:
        if obj.type != 'MESH':
            continue
        owns_bm = False
        if obj.data.is_editmode:
            bm = bmesh.from_edit_mesh(obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            owns_bm = True
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()

        mw = obj.matrix_world
        v_world = np.empty((len(bm.verts), 3), dtype=np.float32)
        for i, v in enumerate(bm.verts):
            co = mw @ v.co
            v_world[i] = (co.x, co.y, co.z)

        v_hom = np.hstack([v_world, np.ones((len(v_world), 1), dtype=np.float32)])
        view_pos = (vm @ v_hom.T).T  # (N, 4)
        view_z = -view_pos[:, 2].astype(np.float32)  # positive distance ahead

        idx = []
        for f in bm.faces:
            verts = [l.vert.index for l in f.loops]
            if len(verts) < 3:
                continue
            for i in range(1, len(verts) - 1):
                idx.append((
                    verts[0] + vert_offset,
                    verts[i] + vert_offset,
                    verts[i + 1] + vert_offset,
                ))

        all_verts.append(v_world)
        all_view_z.append(view_z)
        if idx:
            all_idx.append(np.array(idx, dtype=np.int32))
        vert_offset += len(bm.verts)

        if owns_bm:
            bm.free()

    if not all_idx:
        return np.zeros((height, width), dtype=np.float32)

    verts = np.concatenate(all_verts, axis=0)
    view_z = np.concatenate(all_view_z, axis=0)
    indices = np.concatenate(all_idx, axis=0)

    # Pad min/max slightly so the front/back faces aren't pure 0 / 1 clipped.
    z_near = max(1e-3, float(view_z.min()) * 0.98)
    z_far = max(z_near + 1e-3, float(view_z.max()) * 1.02)

    t = (view_z - z_near) / (z_far - z_near)
    t = np.clip(t, 0.0, 1.0)
    intensity = 1.0 - t  # near=1, far=0
    colors = np.zeros((len(verts), 4), dtype=np.float32)
    colors[:, 0] = intensity
    colors[:, 1] = intensity
    colors[:, 2] = intensity
    colors[:, 3] = 1.0

    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    offscreen = gpu.types.GPUOffScreen(width, height)
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 1.0), depth=1.0)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)

        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(view_matrix)
            gpu.matrix.load_projection_matrix(projection_matrix)
            batch = batch_for_shader(
                shader, 'TRIS',
                {"pos": verts, "color": colors},
                indices=indices,
            )
            batch.draw(shader)

        result = np.array(fb.read_color(0, 0, width, height, 4, 0, 'FLOAT').to_list())

    gpu.state.depth_test_set('NONE')
    offscreen.free()

    return result[:, :, 0].astype(np.float32)
