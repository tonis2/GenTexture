import bpy
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np


def render_depth_map(depsgraph, width: int, height: int, view_matrix, projection_matrix) -> np.ndarray:
    """Render a depth map from the given viewpoint using GPU offscreen rendering.

    Args:
        depsgraph: Evaluated dependency graph
        width: Output width in pixels
        height: Output height in pixels
        view_matrix: Camera view matrix (e.g. region_3d.view_matrix)
        projection_matrix: Camera projection matrix (e.g. region_3d.window_matrix)

    Returns:
        numpy array of shape (height, width) with normalized depth values 0-1,
        where 0 is closest and 1 is farthest.
    """
    offscreen = gpu.types.GPUOffScreen(width, height)

    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0), depth=1.0)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)

        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(view_matrix)
            gpu.matrix.load_projection_matrix(projection_matrix)

            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))

            for instance in depsgraph.object_instances:
                obj = instance.object
                if obj.type != 'MESH':
                    continue
                try:
                    mesh = obj.to_mesh()
                    if mesh is None:
                        continue

                    mesh.transform(instance.matrix_world)
                    mesh.calc_loop_triangles()

                    vert_count = len(mesh.vertices)
                    tri_count = len(mesh.loop_triangles)
                    if vert_count == 0 or tri_count == 0:
                        obj.to_mesh_clear()
                        continue

                    vertices = np.empty((vert_count, 3), dtype='f')
                    indices = np.empty((tri_count, 3), dtype='i')

                    mesh.vertices.foreach_get("co", vertices.ravel())
                    mesh.loop_triangles.foreach_get("vertices", indices.ravel())

                    batch = batch_for_shader(
                        shader, 'TRIS',
                        {"pos": vertices},
                        indices=indices,
                    )
                    batch.draw(shader)
                    obj.to_mesh_clear()
                except Exception:
                    continue

        # Read depth buffer
        depth = np.array(fb.read_depth(0, 0, width, height).to_list())

        # Read color to get a mask of rendered pixels
        mask = np.array(fb.read_color(0, 0, width, height, 4, 0, 'UBYTE').to_list())[:, :, 3]

    gpu.state.depth_test_set('NONE')
    offscreen.free()

    # Invert so close=0, far=1 becomes close=1, far=0 (more intuitive for AI)
    depth = 1.0 - depth
    # Mask out background
    depth *= mask

    # Normalize to 0-1 range (ignoring background zeros)
    masked = np.ma.masked_equal(depth, 0, copy=False)
    if masked.count() > 0:
        depth = np.interp(depth, [masked.min(), depth.max()], [0, 1]).clip(0, 1)

    return depth
