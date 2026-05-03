import bpy

from ..preferences import ADDON_PKG


class GENTEX_UL_Layers(bpy.types.UIList):
    """List view for the projected texture layer stack."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "visible", text="", icon='HIDE_OFF' if item.visible else 'HIDE_ON', emboss=False)
        row.prop(item, "name", text="", emboss=False)
        row.prop(item, "opacity", text="", slider=True)


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

        # Mode-specific settings
        in_edit = context.object is not None and context.object.mode == 'EDIT'
        if in_edit:
            # Projection settings (edit mode only)
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
        else:
            # UV-space settings (object mode)
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
            if in_edit:
                row.operator("gentex.project", icon='MOD_UVPROJECT')
            else:
                row.operator("gentex.generate_uv", icon='UV')
        else:
            # Validation
            api_key = prefs.get_api_key(prefs.provider) if prefs.provider else ""
            if not api_key:
                box = layout.box()
                box.label(text="No API key configured", icon='ERROR')
                box.operator("preferences.addon_show", text="Open Preferences").module = ADDON_PKG
            elif in_edit:
                col = layout.column(align=True)
                col.scale_y = 1.5
                col.operator("gentex.project_layer", icon='IMAGE_RGB_ALPHA')
                col.operator("gentex.project", icon='MOD_UVPROJECT')
            elif context.object is not None and context.object.type == 'MESH':
                if context.object.data.uv_layers:
                    row = layout.row()
                    row.scale_y = 1.5
                    row.operator("gentex.generate_uv", icon='UV')
                else:
                    box = layout.box()
                    box.label(text="Mesh needs a UV map", icon='INFO')
            else:
                box = layout.box()
                box.label(text="Select a mesh object", icon='INFO')


class GENTEX_PT_layers(bpy.types.Panel):
    bl_label = "Projected Layers"
    bl_idname = "GENTEX_PT_layers"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"
    bl_parent_id = "GENTEX_PT_project"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        obj = context.object

        if not obj.gentex_layers:
            layout.label(text="No layers yet. Use Project as New Layer.", icon='INFO')
            return

        layout.template_list(
            "GENTEX_UL_Layers", "",
            obj, "gentex_layers",
            obj, "gentex_active_layer_index",
            rows=4,
        )

        row = layout.row()
        row.operator("gentex.layer_remove", icon='X', text="Remove")
        row.operator("gentex.layer_clear", icon='TRASH', text="Clear")

        layout.separator()

        if obj.data.uv_layers:
            layout.prop_search(
                obj.data.uv_layers, "active",
                obj.data, "uv_layers",
                text="Bake Target UVs",
            )
            row = layout.row()
            row.scale_y = 1.4
            row.operator("gentex.bake_layers", icon='RENDER_RESULT')
