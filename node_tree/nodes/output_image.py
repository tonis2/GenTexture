"""Output Image node — saves the result as a Blender Image.

The resulting datablock is named after the node's `output_name` property and
replaces any existing image with that name.

It also passes the image straight through on an "Image" output socket, so the
same result can be fed into a later step (e.g. wired into a downstream Generate
node's Init or References) without round-tripping through a datablock. The
executor topo-sorts by links, so a downstream connection runs after this node.

When the Image input is connected and a result datablock exists, the node draws
a live thumbnail of it in its body via the image's preview icon.
"""

import bpy

from ._base import GenTexPipelineNodeBase, upstream_value
from ...utils.image import load_image_bytes, np_to_bpy


def _tag_node_editors_redraw():
    """Repaint every node-editor area so the inline preview updates at once."""
    wm = bpy.context.window_manager
    if not wm:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


class GenTexNodeOutputImage(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeOutputImage"
    bl_label = "Output Image"
    bl_icon = "OUTPUT"

    output_name: bpy.props.StringProperty(
        name="Image Name",
        description="Stored as bpy.data.images[<name>]. Replaces any existing image with the same name",
        default="GenTex Result",
    )

    show_preview: bpy.props.BoolProperty(
        name="Preview",
        description="Show a thumbnail of the result in the node body",
        default=True,
    )

    preview_scale: bpy.props.FloatProperty(
        name="Preview Size",
        description="Height of the inline preview thumbnail (in UI units)",
        default=8.0, min=2.0, max=20.0,
    )

    def init(self, context):
        self.inputs.new("GenTexImageSocket", "Image")
        self.outputs.new("GenTexImageSocket", "Image")

    def draw_buttons(self, context, layout):
        layout.prop(self, "output_name")

        row = layout.row(align=True)
        row.prop(self, "show_preview", toggle=True)
        if self.show_preview:
            row.prop(self, "preview_scale", text="")

        if not self.show_preview:
            return

        # Only show a thumbnail once there's something to show: the input must
        # be wired and the result datablock must already exist (i.e. a run has
        # produced it). preview_ensure() is a no-op once the preview is built.
        img = bpy.data.images.get(self.output_name)
        if self.inputs["Image"].is_linked and img is not None:
            img.preview_ensure()
            if img.preview is not None:
                layout.template_icon(icon_value=img.preview.icon_id,
                                     scale=self.preview_scale)

    def evaluate(self, ctx):
        png = upstream_value(self, "Image", ctx, default=None)
        if not isinstance(png, (bytes, bytearray)):
            raise RuntimeError(f"{self.name}: no image on input")
        arr = load_image_bytes(bytes(png))
        existing = bpy.data.images.get(self.output_name)
        img = np_to_bpy(arr, self.output_name, existing=existing)
        # Refresh the preview thumbnail so the node body reflects the new pixels
        # rather than a stale cached icon, then repaint the node editor.
        try:
            img.preview_ensure()
            if img.preview is not None:
                img.preview.reload()
            _tag_node_editors_redraw()
        except Exception:
            # Preview is cosmetic — never let it break a pipeline run.
            pass
        # Pass the original PNG through so downstream nodes can reuse it.
        out = self.outputs.get("Image")
        if out is not None:
            ctx.cache[self.cache_key(out)] = bytes(png)
