"""Text node — a string source.

Edits a multi-line text in a big text field and feeds it into any
NodeSocketString downstream (Prompt / Negative on Generate, etc.).
"""

import bpy

from ._base import GenTexPipelineNodeBase


class GenTexNodeText(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeText"
    bl_label = "Text"
    bl_icon = "FILE_TEXT"
    bl_width_default = 320
    bl_width_min = 200

    text: bpy.props.StringProperty(
        name="Text",
        description="String emitted on the output socket",
        default="",
    )

    def init(self, context):
        self.outputs.new("NodeSocketString", "Text")

    def draw_buttons(self, context, layout):
        row = layout.row()
        row.scale_y = 4.0
        row.prop(self, "text", text="")

    def evaluate(self, ctx):
        ctx.cache[self.cache_key(self.outputs[0])] = self.text
