import bpy

from ..preferences import ADDON_PKG
from ..providers import PROVIDERS, GenerateRequest
from ..utils.image import np_to_bpy, bpy_to_np, np_to_png_bytes, load_image_bytes
from ..utils.threading import run_async, AsyncTask


# Module-level task reference for cancellation
_active_task: AsyncTask | None = None


class GENTEX_OT_Generate(bpy.types.Operator):
    bl_idname = "gentex.generate"
    bl_label = "Generate Texture"
    bl_description = "Generate a texture using the AI provider"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.scene.gentex_info == "" and context.scene.gentex_progress == 0

    def execute(self, context):
        global _active_task

        prefs = context.preferences.addons[ADDON_PKG].preferences
        provider_name = prefs.provider
        settings = prefs.get_provider_settings(provider_name)
        api_key = settings.get("api_key", "")

        if not api_key:
            self.report({'ERROR'}, "No API key configured. Set it in addon preferences.")
            return {'CANCELLED'}

        if not provider_name or provider_name not in PROVIDERS:
            self.report({'ERROR'}, "No provider selected.")
            return {'CANCELLED'}

        scene = context.scene
        prompt = scene.gentex_prompt
        if not prompt.strip():
            self.report({'ERROR'}, "Enter a prompt.")
            return {'CANCELLED'}

        # Build request
        request = GenerateRequest(
            prompt=prompt,
            negative_prompt=scene.gentex_negative_prompt,
            width=scene.gentex_width,
            height=scene.gentex_height,
            strength=scene.gentex_strength,
        )

        # Optional init image from active image editor
        if context.space_data and hasattr(context.space_data, 'image') and context.space_data.image:
            init_img = context.space_data.image
            array = bpy_to_np(init_img)
            request.init_image = np_to_png_bytes(array)

        provider = PROVIDERS[provider_name](settings)
        scene.gentex_info = "Generating..."
        scene.gentex_progress = 1

        def do_generate():
            return provider.generate(request)

        def on_complete(result):
            global _active_task
            _active_task = None
            scene.gentex_info = ""
            scene.gentex_progress = 0

            # Decode image on main thread (thread-safe bpy access)
            image_array = load_image_bytes(result.image_bytes)
            image = np_to_bpy(image_array, f"GenTexture ({result.seed})")

            # Show in image editor if possible
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        area.spaces.active.image = image
                        area.tag_redraw()
                        break

        def on_error(error):
            global _active_task
            _active_task = None
            scene.gentex_info = ""
            scene.gentex_progress = 0
            # Report to all areas since we can't use self.report from timer
            print(f"GenTexture Error: {error}")
            scene.gentex_info = f"Error: {error}"

        _active_task = run_async(do_generate, on_complete, on_error)
        return {'FINISHED'}


class GENTEX_OT_Cancel(bpy.types.Operator):
    bl_idname = "gentex.cancel"
    bl_label = "Cancel"
    bl_description = "Cancel the current generation"

    @classmethod
    def poll(cls, context):
        return _active_task is not None

    def execute(self, context):
        global _active_task
        if _active_task is not None:
            _active_task.cancel()
            _active_task = None
        context.scene.gentex_info = ""
        context.scene.gentex_progress = 0
        return {'FINISHED'}
