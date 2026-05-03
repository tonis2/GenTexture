import tempfile

import bpy
import bmesh
from bpy_extras import view3d_utils
import numpy as np

from ..preferences import ADDON_PKG
from ..providers import PROVIDERS, GenerateRequest
from ..utils.image import np_to_bpy, np_to_png_bytes, load_image_bytes
from ..utils.threading import run_async, AsyncTask
from ..gpu.depth import render_depth_map
from ..gpu.bake import bake_to_uv


_active_task: AsyncTask | None = None


def _get_view3d_region(context):
    """Find the VIEW_3D region dimensions."""
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    return region.width, region.height, region, area
    return None, None, None, None


class GENTEX_OT_Project(bpy.types.Operator):
    bl_idname = "gentex.project"
    bl_label = "Project Texture"
    bl_description = "Generate a texture guided by viewport depth and project it onto selected faces"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if context.scene.gentex_progress > 0:
            cls.poll_message_set("A generation is already running. Use Reset Status if stuck")
            return False
        if context.object is None or context.object.mode != 'EDIT':
            cls.poll_message_set("Enter Edit Mode on a mesh")
            return False

        edit_meshes = [o for o in context.objects_in_mode
                       if o.type == 'MESH' and o.data.is_editmode]
        if not edit_meshes:
            cls.poll_message_set("No mesh currently in Edit Mode")
            return False

        for obj in edit_meshes:
            try:
                mesh = bmesh.from_edit_mesh(obj.data)
                if any(f.select for f in mesh.faces):
                    return True
            except Exception:
                continue
        cls.poll_message_set(
            "Select faces. Tip: switch to Face Select (press 3) and click a face"
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

        # Get viewport dimensions and space
        region_width, region_height, region, area = _get_view3d_region(context)
        if region_width is None:
            self.report({'ERROR'}, "No 3D viewport found.")
            return {'CANCELLED'}

        # Get the 3D viewport space (context.space_data may not be VIEW_3D)
        space_3d = None
        for sp in area.spaces:
            if sp.type == 'VIEW_3D':
                space_3d = sp
                break
        if space_3d is None:
            self.report({'ERROR'}, "No 3D viewport space found.")
            return {'CANCELLED'}

        scene.gentex_info = "Rendering viewport..."
        scene.gentex_progress = 1

        # --- Step 1: Optional viewport color capture ---
        init_img_path = None
        if scene.gentex_project_input == 'COLOR':
            scene.gentex_info = "Rendering viewport color..."
            res_x = scene.render.resolution_x
            res_y = scene.render.resolution_y
            scene.render.resolution_x = region_width
            scene.render.resolution_y = region_height

            # Hide overlays temporarily
            hidden_spaces = []
            for sp in area.spaces:
                if sp.type == 'VIEW_3D' and sp.overlay.show_overlays:
                    hidden_spaces.append(sp)
                    sp.overlay.show_overlays = False

            init_img_path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
            render_filepath = scene.render.filepath
            file_format = scene.render.image_settings.file_format
            scene.render.image_settings.file_format = 'PNG'
            scene.render.filepath = init_img_path
            bpy.ops.render.opengl(write_still=True, view_context=True)

            # Restore
            for sp in hidden_spaces:
                sp.overlay.show_overlays = True
            scene.render.resolution_x = res_x
            scene.render.resolution_y = res_y
            scene.render.filepath = render_filepath
            scene.render.image_settings.file_format = file_format

        # --- Step 2: Reuse or create material, set up projected UVs ---
        scene.gentex_info = "Setting up UVs and material..."

        use_bake = True
        target_objects = []

        for obj in context.objects_in_mode:
            if obj.type != 'MESH' or not obj.data.is_editmode:
                continue

            # Reuse existing material on selected faces, or create a new one
            mesh_bm = bmesh.from_edit_mesh(obj.data)
            active_face = mesh_bm.faces.active
            if active_face is None:
                # Find first selected face
                for f in mesh_bm.faces:
                    if f.select:
                        active_face = f
                        break

            existing_mat_index = active_face.material_index if active_face else -1
            existing_mat = None
            if existing_mat_index >= 0 and existing_mat_index < len(obj.material_slots):
                existing_mat = obj.material_slots[existing_mat_index].material

            if existing_mat and existing_mat.use_nodes:
                # Reuse existing material - find or add the texture node
                material = existing_mat
                material_index = existing_mat_index

                image_texture_node = None
                uv_map_node = None
                for node in material.node_tree.nodes:
                    if node.type == 'TEX_IMAGE':
                        image_texture_node = node
                    if node.type == 'UVMAP':
                        uv_map_node = node

                if image_texture_node is None:
                    image_texture_node = material.node_tree.nodes.new("ShaderNodeTexImage")
                    principled_node = next((n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
                    if principled_node:
                        material.node_tree.links.new(image_texture_node.outputs[0], principled_node.inputs[0])

                if uv_map_node is None:
                    uv_map_node = material.node_tree.nodes.new("ShaderNodeUVMap")
                    material.node_tree.links.new(uv_map_node.outputs[0], image_texture_node.inputs[0])

                uv_map_node.uv_map = "Projected UVs"
            else:
                # Create new material
                material = bpy.data.materials.new(name="gentex-material")
                material.use_nodes = True
                image_texture_node = material.node_tree.nodes.new("ShaderNodeTexImage")
                principled_node = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
                material.node_tree.links.new(image_texture_node.outputs[0], principled_node.inputs[0])

                uv_map_node = material.node_tree.nodes.new("ShaderNodeUVMap")
                uv_map_node.uv_map = "Projected UVs"
                material.node_tree.links.new(uv_map_node.outputs[0], image_texture_node.inputs[0])

                material_index = len(obj.material_slots)
                obj.data.materials.append(material)

            mesh = bmesh.from_edit_mesh(obj.data)
            mesh.verts.ensure_lookup_table()
            mesh.verts.index_update()

            # Get or create projected UVs layer
            uv_layer = None
            uv_layer_index = 0
            for i, layer in enumerate(mesh.loops.layers.uv):
                if layer.name.lower() == "projected uvs":
                    uv_layer = layer
                    uv_layer_index = i
                    break
            if uv_layer is None:
                uv_layer = mesh.loops.layers.uv.new("Projected UVs")
                uv_layer_index = len(mesh.loops.layers.uv) - 1

            # Project vertices to screen space UVs (clamp off-screen to edge)
            def vert_to_uv(v):
                world_co = obj.matrix_world @ v.co
                screen_pos = view3d_utils.location_3d_to_region_2d(
                    region, space_3d.region_3d, world_co
                )
                if screen_pos is None:
                    # Vertex is behind camera - try to project with clip=False
                    screen_pos = view3d_utils.location_3d_to_region_2d(
                        region, space_3d.region_3d, world_co, default=None
                    )
                if screen_pos is None:
                    return None
                # Clamp to viewport bounds (allow slightly off-screen vertices)
                u = max(0.0, min(1.0, screen_pos[0] / region_width))
                v = max(0.0, min(1.0, screen_pos[1] / region_height))
                return (u, v)

            # Copy mesh for baking (with split edges)
            if use_bake:
                bm = mesh.copy()
                bm.select_mode = {'FACE'}
                bmesh.ops.split_edges(bm, edges=bm.edges)
                bmesh.ops.delete(bm, geom=[f for f in bm.faces if not f.select], context='FACES')
                target_objects.append((bm, bm.loops.layers.uv[uv_layer_index]))

            # Set projected UVs on selected faces
            mesh.faces.ensure_lookup_table()
            for face in mesh.faces:
                if face.select:
                    uvs = []
                    valid_count = 0
                    for loop in face.loops:
                        uv = vert_to_uv(mesh.verts[loop.vert.index])
                        if uv is not None:
                            valid_count += 1
                        uvs.append(uv)
                    # Accept face if at least half the vertices projected
                    if valid_count >= len(uvs) // 2 + 1:
                        for loop, uv in zip(face.loops, uvs):
                            if uv is not None:
                                loop[uv_layer].uv = uv
                            else:
                                # Fallback: use average of valid UVs
                                valid_uvs = [u for u in uvs if u is not None]
                                avg = (sum(u[0] for u in valid_uvs) / len(valid_uvs),
                                       sum(u[1] for u in valid_uvs) / len(valid_uvs))
                                loop[uv_layer].uv = avg
                        face.material_index = material_index

            bmesh.update_edit_mesh(obj.data)

        # --- Step 3: Render depth map ---
        scene.gentex_info = "Rendering depth map..."

        depth = np.flipud(render_depth_map(
            context.evaluated_depsgraph_get(),
            width=region_width,
            height=region_height,
            view_matrix=space_3d.region_3d.view_matrix,
            projection_matrix=space_3d.region_3d.window_matrix,
        ))

        # --- Step 4: Build request and call API ---
        scene.gentex_info = "Generating texture..."

        # Resize depth map to the configured depth map size
        depth_size = scene.gentex_depth_size
        if depth.shape[1] != depth_size or depth.shape[0] != depth_size:
            row_idx = np.linspace(0, depth.shape[0] - 1, depth_size).astype(int)
            col_idx = np.linspace(0, depth.shape[1] - 1, depth_size).astype(int)
            depth = depth[np.ix_(row_idx, col_idx)]

        depth_png = np_to_png_bytes(depth)

        request = GenerateRequest(
            prompt=prompt,
            negative_prompt=scene.gentex_negative_prompt,
            width=scene.gentex_width,
            height=scene.gentex_height,
            depth_image=depth_png,
            strength=scene.gentex_strength,
        )

        # Load viewport color if captured
        if init_img_path is not None:
            init_image = bpy.data.images.load(init_img_path)
            w, h = init_image.size
            pixels = np.empty(w * h * 4, dtype=np.float32)
            init_image.pixels.foreach_get(pixels)
            init_array = pixels.reshape((h, w, 4))
            request.init_image = np_to_png_bytes(np.flipud(init_array))
            bpy.data.images.remove(init_image)

        provider = PROVIDERS[provider_name](settings)

        # Capture the bake target UV name now (context may be stale in callback)
        bake_target_uv_name = None
        if use_bake and context.objects_in_mode:
            active_uv = context.objects_in_mode[0].data.uv_layers.active
            if active_uv:
                bake_target_uv_name = active_uv.name

        def do_generate():
            return provider.generate(request)

        def on_complete(result):
            global _active_task
            _active_task = None

            # Decode image on main thread (thread-safe bpy access)
            image_array = load_image_bytes(result.image_bytes)
            texture = np_to_bpy(image_array, f"GenTexture Projected ({result.seed})")
            image_texture_node.image = texture

            # Bake if requested
            if use_bake and target_objects:
                scene.gentex_info = "Baking to UV layout..."
                for bm, src_uv_layer in target_objects:
                    dest = bpy.data.images.new(
                        name=f"{texture.name} (Baked)",
                        width=texture.size[0],
                        height=texture.size[1],
                        alpha=True,
                        float_buffer=True,
                    )
                    dest_uv_layer = bm.loops.layers.uv.active
                    src_h, src_w = image_array.shape[:2]
                    baked_pixels = bake_to_uv(
                        image_array.ravel().astype(np.float32),
                        src_w, src_h,
                        bm, src_uv_layer, dest_uv_layer,
                        dest.size[0], dest.size[1],
                    )
                    dest.pixels[:] = baked_pixels
                    dest.update()
                    dest.pack()
                    image_texture_node.image = dest

                # Update UV map node to use the target UV name (captured at execute time)
                if bake_target_uv_name:
                    uv_map_node.uv_map = bake_target_uv_name

            scene.gentex_info = ""
            scene.gentex_progress = 0

            # Redraw viewports
            for window in bpy.context.window_manager.windows:
                for a in window.screen.areas:
                    if a.type == 'VIEW_3D':
                        a.tag_redraw()

        def on_error(error):
            global _active_task
            _active_task = None
            scene.gentex_progress = 0
            scene.gentex_info = f"Error: {error}"
            print(f"GenTexture Error: {error}")

        _active_task = run_async(do_generate, on_complete, on_error)
        return {'FINISHED'}
