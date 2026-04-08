import bpy
import numpy as np

from ..preferences import ADDON_PKG
from ..providers import PROVIDERS, GenerateRequest
from ..utils.image import np_to_bpy, np_to_png_bytes, load_image_bytes
from ..utils.threading import run_async, AsyncTask
from ..gpu.uv_normals import render_uv_normal_map


_active_task: AsyncTask | None = None


class GENTEX_OT_GenerateUV(bpy.types.Operator):
    bl_idname = "gentex.generate_uv"
    bl_label = "Generate UV Texture"
    bl_description = "Generate a texture in UV space using normal map conditioning. Works in Object mode, covers full UV layout"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if context.scene.gentex_progress > 0:
            return False
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        if not obj.data.uv_layers or not obj.data.uv_layers.active:
            return False
        return True

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

        obj = context.active_object
        uv_name = obj.data.uv_layers.active.name

        scene.gentex_info = "Generating texture..."
        scene.gentex_progress = 1

        # Generate a texture purely from the prompt (no geometric conditioning).
        # UV-space textures map directly to UVs, so the prompt drives the look
        # and the UV layout handles placement.
        request = GenerateRequest(
            prompt=prompt,
            negative_prompt=scene.gentex_negative_prompt,
            width=scene.gentex_width,
            height=scene.gentex_height,
            strength=scene.gentex_strength,
        )

        provider = PROVIDERS[provider_name]()

        # Capture references for the callback
        active_obj = obj
        active_uv_name = uv_name

        def do_generate():
            return provider.generate(request, api_key)

        def on_complete(result):
            global _active_task
            _active_task = None

            # Decode image on main thread
            image_array = load_image_bytes(result.image_bytes)
            texture = np_to_bpy(image_array, f"GenTexture UV ({result.seed})")

            # Set up material with the generated texture
            _setup_material(active_obj, texture, active_uv_name)

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


def _setup_material(obj, texture, uv_name):
    """Assign the generated texture to the object's material."""
    # Try to reuse the active material
    mat = None
    if obj.active_material and obj.active_material.use_nodes:
        mat = obj.active_material

    if mat:
        # Find existing Image Texture node or create one
        image_node = None
        uv_node = None
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE':
                image_node = node
            if node.type == 'UVMAP':
                uv_node = node

        if image_node is None:
            image_node = mat.node_tree.nodes.new("ShaderNodeTexImage")
            principled = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled:
                mat.node_tree.links.new(image_node.outputs[0], principled.inputs[0])

        image_node.image = texture

        if uv_node is None:
            uv_node = mat.node_tree.nodes.new("ShaderNodeUVMap")
            mat.node_tree.links.new(uv_node.outputs[0], image_node.inputs[0])
        uv_node.uv_map = uv_name
    else:
        # Create new material
        mat = bpy.data.materials.new(name="gentex-uv-material")
        mat.use_nodes = True

        image_node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = texture

        principled = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
        mat.node_tree.links.new(image_node.outputs[0], principled.inputs[0])

        uv_node = mat.node_tree.nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = uv_name
        mat.node_tree.links.new(uv_node.outputs[0], image_node.inputs[0])

        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
