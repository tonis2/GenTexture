"""Reference Image node — picks an existing Blender Image and outputs PNG bytes."""

import bpy

from ._base import GenTexPipelineNodeBase
from ...utils.image import bpy_to_np, np_to_png_bytes


class GenTexNodeReferenceImage(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeReferenceImage"
    bl_label = "Reference Image"
    bl_icon = "IMAGE_DATA"

    image: bpy.props.PointerProperty(
        name="Image",
        type=bpy.types.Image,
        description="Image datablock to feed into the pipeline",
    )

    def init(self, context):
        self.outputs.new("GenTexImageSocket", "Image")

    def draw_buttons(self, context, layout):
        layout.template_ID(self, "image", new="image.new", open="image.open")

    def evaluate(self, ctx):
        if self.image is None:
            raise RuntimeError(f"{self.name}: no image picked")
        arr = bpy_to_np(self.image)
        png = np_to_png_bytes(arr)
        ctx.cache[self.cache_key(self.outputs[0])] = png
