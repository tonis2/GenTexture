"""Reference Image node — picks an existing Blender Image and outputs PNG bytes."""

import bpy

from ._base import GenTexPipelineNodeBase
from ...utils.image import bpy_to_np, np_to_png_bytes, load_image_file


class GenTexNodeReferenceImage(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeReferenceImage"
    bl_label = "Reference Image"
    bl_icon = "IMAGE_DATA"

    source: bpy.props.EnumProperty(
        name="Source",
        description="Where to read the reference image from",
        items=[
            ("DATABLOCK", "Datablock", "Pick an image already loaded in this .blend", "IMAGE_DATA", 0),
            ("FILE", "File", "Read an image directly from an external file path", "FILE_FOLDER", 1),
        ],
        default="DATABLOCK",
    )

    image: bpy.props.PointerProperty(
        name="Image",
        type=bpy.types.Image,
        description="Image datablock to feed into the pipeline",
    )

    filepath: bpy.props.StringProperty(
        name="File",
        description="Path to an external image file (PNG/JPEG/etc.)",
        subtype="FILE_PATH",
    )

    show_preview: bpy.props.BoolProperty(
        name="Preview",
        description="Show a thumbnail of the picked image in the node body",
        default=True,
    )

    preview_scale: bpy.props.FloatProperty(
        name="Preview Size",
        description="Height of the inline preview thumbnail (in UI units)",
        default=8.0, min=2.0, max=20.0,
    )

    def init(self, context):
        self.outputs.new("GenTexImageSocket", "Image")

    def draw_buttons(self, context, layout):
        layout.prop(self, "source", expand=True)

        if self.source == "DATABLOCK":
            layout.template_ID(self, "image", new="image.new", open="image.open")
        else:
            layout.prop(self, "filepath", text="")

        row = layout.row(align=True)
        row.prop(self, "show_preview", toggle=True)
        if self.show_preview:
            row.prop(self, "preview_scale", text="")

        if self.show_preview and self.source == "DATABLOCK" and self.image is not None:
            self.image.preview_ensure()
            if self.image.preview is not None:
                layout.template_icon(icon_value=self.image.preview.icon_id,
                                     scale=self.preview_scale)

    def evaluate(self, ctx):
        if self.source == "FILE":
            if not self.filepath:
                raise RuntimeError(f"{self.name}: no file path set")
            arr = load_image_file(self.filepath)
        else:
            if self.image is None:
                raise RuntimeError(f"{self.name}: no image picked")
            arr = bpy_to_np(self.image)
        png = np_to_png_bytes(arr)
        ctx.cache[self.cache_key(self.outputs[0])] = png
