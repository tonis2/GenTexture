import bpy

from ..preferences import ADDON_PKG


# ──────────────────────────── UIList renderers ────────────────────────────


class GENTEX_UL_Layers(bpy.types.UIList):
    """Row in the projected-layer stack."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "visible", text="",
                 icon='HIDE_OFF' if item.visible else 'HIDE_ON', emboss=False)
        row.prop(item, "name", text="", emboss=False)
        row.prop(item, "opacity", text="", slider=True)


class GENTEX_UL_References(bpy.types.UIList):
    """Row in the reference-image list."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        layout.prop(item, "image", text="")


# ──────────────────────────── shared bits ────────────────────────────


def _has_api_key(prefs) -> bool:
    return bool(prefs.get_api_key(prefs.provider) if prefs.provider else "")


def _draw_api_key_warning(layout, prefs):
    box = layout.box()
    box.label(text="No API key configured", icon='ERROR')
    box.operator("preferences.addon_show", text="Open Preferences").module = ADDON_PKG


def _draw_status(layout, scene) -> bool:
    """If a generation is running or errored, draw a status box. Returns True
    when the caller should skip drawing the action button."""
    if scene.gentex_progress > 0:
        box = layout.box()
        box.label(text=scene.gentex_info or "Working...", icon='SORTTIME')
        layout.operator("gentex.cancel", icon='CANCEL')
        return True
    if scene.gentex_info and scene.gentex_info.startswith("Error:"):
        box = layout.box()
        box.label(text=scene.gentex_info, icon='ERROR')
    return False


def _draw_action(layout, op_id: str, icon: str = 'RENDER_STILL'):
    row = layout.row()
    row.scale_y = 1.5
    row.operator(op_id, icon=icon)


# ──────────────────────────── Image Editor panel ────────────────────────────


class GENTEX_PT_generate(bpy.types.Panel):
    """2D texture generation in the Image Editor sidebar."""

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
        prefs = context.preferences.addons[ADDON_PKG].preferences

        layout.prop(prefs, "provider")

        if not _has_api_key(prefs):
            _draw_api_key_warning(layout, prefs)
            return

        col = layout.column(align=True)
        col.label(text="Prompt:")
        col.prop(scene, "gentex_prompt", text="")
        col.label(text="Negative:")
        col.prop(scene, "gentex_negative_prompt", text="")

        row = layout.row(align=True)
        row.prop(scene, "gentex_width")
        row.prop(scene, "gentex_height")

        if context.space_data and getattr(context.space_data, 'image', None):
            layout.prop(scene, "gentex_strength")

        layout.separator()
        if not _draw_status(layout, scene):
            _draw_action(layout, "gentex.generate", icon='RENDER_STILL')


# ──────────────────────────── 3D Viewport: parent ────────────────────────────


class GENTEX_PT_main(bpy.types.Panel):
    """Parent panel for the projection workflow in the 3D viewport."""

    bl_label = "GenTexture"
    bl_idname = "GENTEX_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None or obj.type != 'MESH':
            return False
        # Show in Edit Mode (to project) and in Object Mode if there's
        # something to manage. Otherwise the panel quietly disappears.
        return obj.mode == 'EDIT' or bool(obj.gentex_layers)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        prefs = context.preferences.addons[ADDON_PKG].preferences
        layout.prop(prefs, "provider")
        if not _has_api_key(prefs):
            _draw_api_key_warning(layout, prefs)


# ──────────────────────────── Project Layer (Edit Mode) ────────────────────────────


class GENTEX_PT_project(bpy.types.Panel):
    """Primary action: project the prompt onto the selected faces as a new layer."""

    bl_label = "Project Layer"
    bl_idname = "GENTEX_PT_project"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"
    bl_parent_id = "GENTEX_PT_main"

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.mode == 'EDIT'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene
        prefs = context.preferences.addons[ADDON_PKG].preferences

        col = layout.column(align=True)
        col.label(text="Prompt:")
        col.prop(scene, "gentex_prompt", text="")
        col.label(text="Negative:")
        col.prop(scene, "gentex_negative_prompt", text="")

        if not _has_api_key(prefs):
            return

        # Projection relies on the AI returning pixels in the same positions
        # as the input — i.e. the inpaint contract (outside-mask pixels come
        # back identical, inside-mask pixels are repainted). Providers without
        # this capability will look misaligned even when the generation looks
        # great in isolation.
        if prefs.provider:
            from ..providers import PROVIDERS, CAP_INPAINT
            pcls = PROVIDERS.get(prefs.provider)
            if pcls and CAP_INPAINT not in pcls.capabilities():
                box = layout.box()
                box.label(text="Provider lacks inpaint", icon='ERROR')

        obj = context.object
        if obj and obj.data.uv_layers:
            layout.prop_search(
                obj.data.uv_layers, "active",
                obj.data, "uv_layers",
                text="Target UV",
            )

        layout.separator()
        if not _draw_status(layout, scene):
            _draw_action(layout, "gentex.project_layer", icon='IMAGE_RGB_ALPHA')


# ──────────────────────────── Settings (collapsed) ────────────────────────────


class GENTEX_PT_settings(bpy.types.Panel):
    """Output size, conditioning input, strength — hidden by default."""

    bl_label = "Settings"
    bl_idname = "GENTEX_PT_settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"
    bl_parent_id = "GENTEX_PT_project"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene

        row = layout.row(align=True)
        row.prop(scene, "gentex_width")
        row.prop(scene, "gentex_height")
        layout.prop(scene, "gentex_strength")
        layout.prop(scene, "gentex_depth_size")
        layout.prop(scene, "gentex_project_input")


# ──────────────────────────── References (collapsed) ────────────────────────────


class GENTEX_PT_references(bpy.types.Panel):
    """Reference images fed alongside the prompt."""

    bl_label = "References"
    bl_idname = "GENTEX_PT_references"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"
    bl_parent_id = "GENTEX_PT_project"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        n = len(context.scene.gentex_references)
        if n:
            self.layout.label(text=f"({n})")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = context.preferences.addons[ADDON_PKG].preferences

        if scene.gentex_references:
            layout.template_list(
                "GENTEX_UL_References", "",
                scene, "gentex_references",
                scene, "gentex_active_reference_index",
                rows=2,
            )
            row = layout.row(align=True)
            row.operator("gentex.reference_remove", text="Remove", icon='X')
            row.operator("gentex.reference_clear", text="Clear", icon='TRASH')

        row = layout.row(align=True)
        row.operator("gentex.reference_add", text="Add Slot", icon='ADD')
        row.operator("gentex.reference_load", text="Load File", icon='FILEBROWSER')
        if context.object and context.object.gentex_layers:
            row.operator("gentex.reference_add_from_active_layer",
                         text="From Layer", icon='RENDERLAYERS')

        if scene.gentex_references and prefs.provider:
            from ..providers import PROVIDERS, CAP_REFERENCE_IMAGES
            pcls = PROVIDERS.get(prefs.provider)
            if pcls and CAP_REFERENCE_IMAGES not in pcls.capabilities():
                box = layout.box()
                box.label(text="Active provider ignores references", icon='INFO')


# ──────────────────────────── Layers ────────────────────────────


class GENTEX_PT_layers(bpy.types.Panel):
    """Layer stack management + bake to a single UV-space texture."""

    bl_label = "Layers"
    bl_idname = "GENTEX_PT_layers"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"
    bl_parent_id = "GENTEX_PT_main"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == 'MESH' and bool(obj.gentex_layers)

    def draw_header(self, context):
        n = len(context.object.gentex_layers)
        self.layout.label(text=f"({n})")

    def draw(self, context):
        layout = self.layout
        obj = context.object

        layout.template_list(
            "GENTEX_UL_Layers", "",
            obj, "gentex_layers",
            obj, "gentex_active_layer_index",
            rows=4,
        )

        row = layout.row(align=True)
        row.operator("gentex.layer_remove", text="Remove", icon='X')
        row.operator("gentex.layer_clear", text="Clear", icon='TRASH')


class GENTEX_PT_bake(bpy.types.Panel):
    """Bake the layer stack into a single UV-space texture, then optionally
    swap the mesh's material to that flat texture."""

    bl_label = "Bake to UV"
    bl_idname = "GENTEX_PT_bake"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"
    bl_parent_id = "GENTEX_PT_layers"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and bool(obj.data.uv_layers)

    def draw(self, context):
        layout = self.layout
        obj = context.object

        layout.prop_search(
            obj.data.uv_layers, "active",
            obj.data, "uv_layers",
            text="Target UV",
        )
        row = layout.row()
        row.scale_y = 1.4
        row.operator("gentex.bake_layers", text="Bake Layers", icon='RENDER_RESULT')

        if obj.gentex_baked_image is not None:
            col = layout.column(align=True)
            col.label(text=obj.gentex_baked_image.name, icon='IMAGE_DATA')
            col.prop(obj, "gentex_use_baked", toggle=True, icon='MATERIAL')
