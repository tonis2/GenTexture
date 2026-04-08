import gpu
from gpu_extras.batch import batch_for_shader
import mathutils
import numpy as np


def bake_to_uv(src_pixels: np.ndarray, src_width: int, src_height: int,
               bmesh_data, src_uv_layer, dest_uv_layer,
               dest_width: int, dest_height: int) -> np.ndarray:
    """Remap a texture from projected UVs to a target UV layout using GPU.

    Args:
        src_pixels: Source image as flat float32 array (RGBA, length = W*H*4)
        src_width: Source image width
        src_height: Source image height
        bmesh_data: BMesh with UV layers
        src_uv_layer: Source UV layer (projected UVs)
        dest_uv_layer: Destination UV layer (target layout)
        dest_width: Output image width
        dest_height: Output image height

    Returns:
        numpy array of shape (dest_height * dest_width * 4) flat RGBA float32
    """
    # Build UV coordinate arrays
    src_uvs = []
    dest_uvs = []
    tri_indices = []

    # Triangulate faces and collect UV data
    vert_idx = 0
    for face in bmesh_data.faces:
        loops = list(face.loops)
        if len(loops) < 3:
            continue
        # Fan triangulation
        for i in range(1, len(loops) - 1):
            for loop in [loops[0], loops[i], loops[i + 1]]:
                src_uvs.append(list(loop[src_uv_layer].uv))
                dest_uvs.append(list(loop[dest_uv_layer].uv))
                tri_indices.append(vert_idx)
                vert_idx += 1

    if not src_uvs:
        return np.zeros(dest_width * dest_height * 4, dtype=np.float32)

    src_uvs = np.array(src_uvs, dtype=np.float32)
    dest_uvs = np.array(dest_uvs, dtype=np.float32)
    tri_indices = np.array(tri_indices, dtype='i').reshape(-1, 3)

    # Create shader
    vert_out = gpu.types.GPUStageInterfaceInfo("gentex_bake_iface")
    vert_out.smooth('VEC2', "uvInterp")

    shader_info = gpu.types.GPUShaderCreateInfo()
    shader_info.sampler(0, 'FLOAT_2D', "image")
    shader_info.vertex_in(0, 'VEC2', "src_uv")
    shader_info.vertex_in(1, 'VEC2', "dest_uv")
    shader_info.vertex_out(vert_out)
    shader_info.fragment_out(0, 'VEC4', "fragColor")

    shader_info.vertex_source("""
void main()
{
    gl_Position = vec4(dest_uv * 2.0 - 1.0, 0.0, 1.0);
    uvInterp = src_uv;
}
""")

    shader_info.fragment_source("""
void main()
{
    fragColor = texture(image, uvInterp);
}
""")

    shader = gpu.shader.create_from_info(shader_info)

    # Create texture from source pixels
    buffer = gpu.types.Buffer('FLOAT', len(src_pixels), src_pixels.tolist())
    texture = gpu.types.GPUTexture(size=(src_width, src_height), data=buffer, format='RGBA16F')

    # Render
    offscreen = gpu.types.GPUOffScreen(dest_width, dest_height)

    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))

        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(mathutils.Matrix.Identity(4))
            gpu.matrix.load_projection_matrix(mathutils.Matrix.Identity(4))

            batch = batch_for_shader(
                shader, 'TRIS',
                {"src_uv": src_uvs, "dest_uv": dest_uvs},
                indices=tri_indices,
            )
            shader.uniform_sampler("image", texture)
            batch.draw(shader)

        result = np.array(fb.read_color(0, 0, dest_width, dest_height, 4, 0, 'FLOAT').to_list())

    offscreen.free()
    return result.ravel()
