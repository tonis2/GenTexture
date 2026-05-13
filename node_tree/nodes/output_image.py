"""Output Image node — terminal node that saves the result as a Blender Image.

The resulting datablock is named after the node's `output_name` property and
replaces any existing image with that name.
"""

import bpy

from ._base import GenTexPipelineNodeBase, upstream_value
from ...utils.image import load_image_bytes, np_to_bpy


class GenTexNodeOutputImage(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeOutputImage"
    bl_label = "Output Image"
    bl_icon = "OUTPUT"

    output_name: bpy.props.StringProperty(
        name="Image Name",
        description="Stored as bpy.data.images[<name>]. Replaces any existing image with the same name",
        default="GenTex Result",
    )

    def init(self, context):
        self.inputs.new("GenTexImageSocket", "Image")

    def draw_buttons(self, context, layout):
        layout.prop(self, "output_name")

    def evaluate(self, ctx):
        png = upstream_value(self, "Image", ctx, default=None)
        if not isinstance(png, (bytes, bytearray)):
            raise RuntimeError(f"{self.name}: no image on input")
        arr = load_image_bytes(bytes(png))
        existing = bpy.data.images.get(self.output_name)
        np_to_bpy(arr, self.output_name, existing=existing)
