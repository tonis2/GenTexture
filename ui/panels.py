"""Per-object N-panel UI for inspecting and baking projected layers.

Action panels (Generate, Project, Settings, References) have been replaced
by the AI Texture Pipeline node editor — open a Node Editor and switch its
tree type to "AI Texture Pipeline" to wire up generations.
"""

import bpy

from ..preferences import ADDON_PKG


class GENTEX_UL_Layers(bpy.types.UIList):
    """Row in the projected-layer stack."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "visible", text="",
                 icon='HIDE_OFF' if item.visible else 'HIDE_ON', emboss=False)
        row.prop(item, "name", text="", emboss=False)
        row.prop(item, "opacity", text="", slider=True)


class GENTEX_PT_main(bpy.types.Panel):
    """Parent panel — appears only when the active mesh has projected layers.

    All pipeline actions live in the Node Editor (header + sidebar). This 3D-View
    panel exists purely to inspect/manage the per-object layer stack that
    Project Layer nodes write into.
    """

    bl_label = "GenTexture"
    bl_idname = "GENTEX_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GenTexture"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == 'MESH' and bool(obj.gentex_layers)

    def draw(self, context):
        layout = self.layout
        layout.label(
            text="Pipeline lives in the Node Editor (AI Texture Pipeline)",
            icon='NODETREE',
        )


class GENTEX_PT_layers(bpy.types.Panel):
    """Layer stack management."""

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
        obj = context.object
        n = len(obj.gentex_layers) if obj is not None else 0
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
