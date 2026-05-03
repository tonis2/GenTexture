import bpy

from ..preferences import ADDON_PKG


class GENTEX_UL_Layers(bpy.types.UIList):
    """List view for the projected texture layer stack."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "visible", text="", icon='HIDE_OFF' if item.visible else 'HIDE_ON', emboss=False)
        row.prop(item, "name", text="", emboss=False)
        row.prop(item, "opacity", text="", slider=True)


class GENTEX_UL_References(bpy.types.UIList):
    """List view for reference images fed alongside the prompt."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        # Image picker — works for any Blender image, including layer images
        row.prop(item, "image", text="")


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

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None or obj.type != 'MESH':
            return False
        if obj.mode == 'EDIT':
            return True
        # In Object Mode only show if there are layers to bake;
        # otherwise hide to keep things uncluttered.
        return bool(obj.gentex_layers)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene

        in_edit = context.object is not None and context.object.mode == 'EDIT'

        # Object Mode is only reachable here when there are existing layers
        # to bake (poll guard). Show a minimal view focused on baking.
        if not in_edit:
            box = layout.box()
            box.label(text=f"{len(context.object.gentex_layers)} layer(s) on this mesh",
                      icon='RENDERLAYERS')
            box.label(text="Use the Projected Layers panel below to bake.")
            return

        # ---------- Edit Mode: full projection UI ----------
        prefs = context.preferences.addons[ADDON_PKG].preferences
        layout.prop(prefs, "provider")

        layout.separator()

        col = layout.column(align=True)
        col.label(text="Prompt:")
        col.prop(scene, "gentex_prompt", text="")
        col.label(text="Negative:")
        col.prop(scene, "gentex_negative_prompt", text="")

        layout.separator()

        row = layout.row(align=True)
        row.prop(scene, "gentex_width")
        row.prop(scene, "gentex_height")

        layout.separator()

        layout.prop(scene, "gentex_depth_size")
        layout.prop(scene, "gentex_project_input")
        if scene.gentex_project_input == 'COLOR':
            layout.prop(scene, "gentex_strength")
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

        # ---------- Reference images ----------
        layout.separator()
        header = layout.row(align=True)
        header.label(text=f"References ({len(scene.gentex_references)})", icon='IMAGE_DATA')

        if scene.gentex_references:
            layout.template_list(
                "GENTEX_UL_References", "",
                scene, "gentex_references",
                scene, "gentex_active_reference_index",
                rows=2,
            )

        row = layout.row(align=True)
        row.operator("gentex.reference_add", text="Add Slot", icon='ADD')
        row.operator("gentex.reference_load", text="Load File...", icon='FILEBROWSER')
        if context.object and context.object.gentex_layers:
            layout.operator("gentex.reference_add_from_active_layer",
                            text="Use Active Layer", icon='RENDERLAYERS')
        if scene.gentex_references:
            row = layout.row(align=True)
            row.operator("gentex.reference_remove", text="Remove", icon='X')
            row.operator("gentex.reference_clear", text="Clear", icon='TRASH')

        # Provider hint: only providers declaring CAP_REFERENCE_IMAGES use refs
        if scene.gentex_references and prefs.provider:
            from ..providers import PROVIDERS, CAP_REFERENCE_IMAGES
            pcls = PROVIDERS.get(prefs.provider)
            if pcls and CAP_REFERENCE_IMAGES not in pcls.capabilities():
                box = layout.box()
                box.label(text="Active provider ignores reference images", icon='INFO')
                box.label(text="Switch to a provider with multi-image support")

        layout.separator()

        if scene.gentex_progress > 0:
            box = layout.box()
            box.label(text=scene.gentex_info or "Working...", icon='SORTTIME')
            layout.operator("gentex.cancel", icon='CANCEL')
        elif scene.gentex_info and scene.gentex_info.startswith("Error:"):
            box = layout.box()
            box.label(text=scene.gentex_info, icon='ERROR')
            row = layout.row()
            row.scale_y = 1.5
            row.operator("gentex.project_layer", icon='IMAGE_RGB_ALPHA')
        else:
            api_key = prefs.get_api_key(prefs.provider) if prefs.provider else ""
            if not api_key:
                box = layout.box()
                box.label(text="No API key configured", icon='ERROR')
                box.operator("preferences.addon_show", text="Open Preferences").module = ADDON_PKG
            else:
                col = layout.column(align=True)
                col.scale_y = 1.5
                col.operator("gentex.project_layer", icon='IMAGE_RGB_ALPHA')
                col.operator("gentex.project", icon='MOD_UVPROJECT')


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
        obj = context.object
        return obj is not None and obj.type == 'MESH' and bool(obj.gentex_layers)

    def draw(self, context):
        layout = self.layout
        obj = context.object

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

            if obj.gentex_baked_image is not None:
                box = layout.box()
                box.label(text=f"Baked: {obj.gentex_baked_image.name}", icon='IMAGE_DATA')
                box.prop(obj, "gentex_use_baked", toggle=True, icon='MATERIAL')
