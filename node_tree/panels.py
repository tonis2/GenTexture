"""Node Editor UI for the AI Texture Pipeline tree.

The primary entry point is the header bar — Run / Cancel / status are inline
in the Node Editor header so the user doesn't need a sidebar open. The N-panel
keeps the longer-form provider/api-key info.
"""

import bpy

from ..preferences import ADDON_PKG
from .tree import TREE_IDNAME


def _is_pipeline_editor(context) -> bool:
    space = context.space_data
    return space is not None and getattr(space, "tree_type", "") == TREE_IDNAME


def _draw_run_controls(layout, context, *, compact: bool):
    """Run / Cancel / status, used in both header and sidebar."""
    scene = context.scene
    if scene.gentex_progress > 0:
        if compact:
            layout.label(text=scene.gentex_info or "Running...", icon='SORTTIME')
            layout.operator("gentex.cancel_pipeline", text="", icon='CANCEL')
        else:
            box = layout.box()
            box.label(text=scene.gentex_info or "Running...", icon='SORTTIME')
            layout.operator("gentex.cancel_pipeline", icon='CANCEL')
        return

    if scene.gentex_info and scene.gentex_info.startswith("Error"):
        if compact:
            layout.label(text=scene.gentex_info, icon='ERROR')
        else:
            box = layout.box()
            box.label(text=scene.gentex_info, icon='ERROR')

    if compact:
        layout.operator("gentex.run_pipeline", text="Run", icon='PLAY')
    else:
        row = layout.row()
        row.scale_y = 1.5
        row.operator("gentex.run_pipeline", icon='PLAY')


def _header_draw(self, context):
    """Appended to the Node Editor header — only renders for our tree type."""
    if not _is_pipeline_editor(context):
        return
    layout = self.layout
    layout.separator_spacer()
    _draw_run_controls(layout, context, compact=True)


class GENTEX_PT_pipeline(bpy.types.Panel):
    """Sidebar panel — secondary; provider settings + status."""

    bl_label = "GenTexture Pipeline"
    bl_idname = "GENTEX_PT_pipeline"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "GenTexture"

    @classmethod
    def poll(cls, context):
        return _is_pipeline_editor(context)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.operator(
            "preferences.addon_show", text="Provider Settings", icon='PREFERENCES',
        ).module = ADDON_PKG

        layout.separator()
        _draw_run_controls(layout, context, compact=False)


def register_header():
    bpy.types.NODE_HT_header.append(_header_draw)


def unregister_header():
    bpy.types.NODE_HT_header.remove(_header_draw)
