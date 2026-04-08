import bpy

from ..preferences import ADDON_PKG


class GENTEX_PT_generate(bpy.types.Panel):
    bl_label = "GenTexture"
    bl_idname = "GENTEX_PT_generate"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "GenTexture"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene

        # Provider selection
        prefs = context.preferences.addons[ADDON_PKG].preferences
        layout.prop(prefs, "provider")

        layout.separator()

        # Prompt
        col = layout.column(align=True)
        col.label(text="Prompt:")
        col.prop(scene, "gentex_prompt", text="")
        col.label(text="Negative:")
        col.prop(scene, "gentex_negative_prompt", text="")

        layout.separator()

        # Size
        row = layout.row(align=True)
        row.prop(scene, "gentex_width")
        row.prop(scene, "gentex_height")

        # Init image strength
        if context.space_data and hasattr(context.space_data, 'image') and context.space_data.image:
            layout.prop(scene, "gentex_strength")

        layout.separator()

        # Actions
        if scene.gentex_progress > 0:
            box = layout.box()
            box.label(text=scene.gentex_info or "Working...", icon='SORTTIME')
            layout.operator("gentex.cancel", icon='CANCEL')
        elif scene.gentex_info and scene.gentex_info.startswith("Error:"):
            box = layout.box()
            box.label(text=scene.gentex_info, icon='ERROR')
            row = layout.row()
            row.scale_y = 1.5
            row.operator("gentex.generate", icon='RENDER_STILL')
        else:
            # Validation
            api_key = prefs.get_api_key(prefs.provider) if prefs.provider else ""
            if not api_key:
                box = layout.box()
                box.label(text="No API key configured", icon='ERROR')
                box.operator("preferences.addon_show", text="Open Preferences").module = ADDON_PKG
            else:
                row = layout.row()
                row.scale_y = 1.5
                row.operator("gentex.generate", icon='RENDER_STILL')


class GENTEX_PT_project(bpy.types.Panel):
    bl_label = "GenTexture"
    bl_idname = "GENTEX_PT_project"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene

        # Provider selection
        prefs = context.preferences.addons[ADDON_PKG].preferences
        layout.prop(prefs, "provider")

        layout.separator()

        # Prompt
        col = layout.column(align=True)
        col.label(text="Prompt:")
        col.prop(scene, "gentex_prompt", text="")
        col.label(text="Negative:")
        col.prop(scene, "gentex_negative_prompt", text="")

        layout.separator()

        # Size
        row = layout.row(align=True)
        row.prop(scene, "gentex_width")
        row.prop(scene, "gentex_height")

        layout.separator()

        # Projection settings
        layout.prop(scene, "gentex_depth_size")
        layout.prop(scene, "gentex_project_input")
        if scene.gentex_project_input == 'COLOR':
            layout.prop(scene, "gentex_strength")

        layout.prop(scene, "gentex_project_bake")
        if scene.gentex_project_bake:
            for obj in context.selected_objects:
                if hasattr(obj, "data") and hasattr(obj.data, "uv_layers") and len(obj.data.uv_layers) > 0:
                    layout.prop_search(
                        obj.data.uv_layers, "active",
                        obj.data, "uv_layers",
                        text=f"{obj.name} Target UVs"
                    )

        layout.separator()

        # Actions
        if scene.gentex_progress > 0:
            box = layout.box()
            box.label(text=scene.gentex_info or "Working...", icon='SORTTIME')
            layout.operator("gentex.cancel", icon='CANCEL')
        elif scene.gentex_info and scene.gentex_info.startswith("Error:"):
            box = layout.box()
            box.label(text=scene.gentex_info, icon='ERROR')
            row = layout.row()
            row.scale_y = 1.5
            row.operator("gentex.project", icon='MOD_UVPROJECT')
        else:
            # Validation
            api_key = prefs.get_api_key(prefs.provider) if prefs.provider else ""
            if not api_key:
                box = layout.box()
                box.label(text="No API key configured", icon='ERROR')
                box.operator("preferences.addon_show", text="Open Preferences").module = ADDON_PKG
            elif context.object is None or context.object.mode != 'EDIT':
                box = layout.box()
                box.label(text="Enter Edit Mode and select faces", icon='INFO')
            else:
                row = layout.row()
                row.scale_y = 1.5
                row.operator("gentex.project", icon='MOD_UVPROJECT')
