import gpu
from gpu_extras.batch import batch_for_shader
import mathutils
import numpy as np


def render_uv_normal_map(depsgraph, obj, uv_layer_name: str,
                         width: int, height: int) -> np.ndarray:
    """Render a normal map in UV space using GPU offscreen rendering.

    Each pixel in the output corresponds to a UV coordinate. The normals
    are encoded as RGB: (normal * 0.5 + 0.5), so [0,0,1] (up) becomes
    [0.5, 0.5, 1.0] (blue-ish).

    Args:
        depsgraph: Evaluated dependency graph
        obj: Mesh object to render
        uv_layer_name: Name of the UV layer to use
        width: Output width in pixels
        height: Output height in pixels

    Returns:
        numpy array of shape (height, width, 4), float32 RGBA
    """
    # Get evaluated mesh (with modifiers applied)
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    if mesh is None:
        return np.full((height, width, 4), [0.5, 0.5, 1.0, 1.0], dtype=np.float32)

    mesh.transform(obj.matrix_world)
    mesh.calc_loop_triangles()

    # Get UV layer
    uv_layer = mesh.uv_layers.get(uv_layer_name)
    if uv_layer is None:
        eval_obj.to_mesh_clear()
        return np.full((height, width, 4), [0.5, 0.5, 1.0, 1.0], dtype=np.float32)

    tri_count = len(mesh.loop_triangles)
    if tri_count == 0:
        eval_obj.to_mesh_clear()
        return np.full((height, width, 4), [0.5, 0.5, 1.0, 1.0], dtype=np.float32)

    # Build per-vertex UV and normal arrays for all triangles
    uvs = np.empty((tri_count * 3, 2), dtype=np.float32)
    normals = np.empty((tri_count * 3, 3), dtype=np.float32)
    indices = np.arange(tri_count * 3, dtype=np.int32).reshape(-1, 3)

    # Get corner normals (Blender 4.2+ API)
    corner_normals = None
    if hasattr(mesh, 'corner_normals'):
        corner_normals = np.empty(len(mesh.loops) * 3, dtype=np.float32)
        mesh.corner_normals.foreach_get("vector", corner_normals)
        corner_normals = corner_normals.reshape(-1, 3)

    # Get all UV data
    uv_data = np.empty(len(uv_layer.uv) * 2, dtype=np.float32)
    uv_layer.uv.foreach_get("vector", uv_data)
    uv_data = uv_data.reshape(-1, 2)

    for i, tri in enumerate(mesh.loop_triangles):
        for j, loop_idx in enumerate(tri.loops):
            idx = i * 3 + j
            uvs[idx] = uv_data[loop_idx]

            if corner_normals is not None:
                normals[idx] = corner_normals[loop_idx]
            elif tri.use_smooth:
                # Fallback: use vertex normal for smooth shading
                normals[idx] = mesh.vertices[tri.vertices[j]].normal
            else:
                # Flat shading: use face normal
                normals[idx] = tri.normal

    eval_obj.to_mesh_clear()

    # Create shader
    vert_out = gpu.types.GPUStageInterfaceInfo("gentex_uv_normal_iface")
    vert_out.smooth('VEC3', "normalInterp")

    shader_info = gpu.types.GPUShaderCreateInfo()
    shader_info.vertex_in(0, 'VEC2', "uv")
    shader_info.vertex_in(1, 'VEC3', "normal")
    shader_info.vertex_out(vert_out)
    shader_info.fragment_out(0, 'VEC4', "fragColor")

    shader_info.vertex_source("""
void main()
{
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
    normalInterp = normal;
}
""")

    shader_info.fragment_source("""
void main()
{
    vec3 n = normalize(normalInterp);
    fragColor = vec4(n * 0.5 + 0.5, 1.0);
}
""")

    shader = gpu.shader.create_from_info(shader_info)

    # Render
    offscreen = gpu.types.GPUOffScreen(width, height)

    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        # Clear with default +Z normal (blue) for empty areas
        fb.clear(color=(0.5, 0.5, 1.0, 0.0))

        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(mathutils.Matrix.Identity(4))
            gpu.matrix.load_projection_matrix(mathutils.Matrix.Identity(4))

            batch = batch_for_shader(
                shader, 'TRIS',
                {"uv": uvs, "normal": normals},
                indices=indices,
            )
            batch.draw(shader)

        result = np.array(fb.read_color(0, 0, width, height, 4, 0, 'FLOAT').to_list())

    offscreen.free()
    return result
