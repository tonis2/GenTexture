"""Pipeline node classes.

Each module defines one bpy.types.Node subclass. They share the contract:

    class SomeNode(bpy.types.Node):
        bl_idname = "GenTexNodeXxx"
        bl_label = "..."

        def init(self, context):
            # define sockets

        def evaluate(self, ctx):
            # runs inside the executor's worker thread
            # write outputs to ctx.cache keyed by self.output_key(socket)
"""

from .reference_image import GenTexNodeReferenceImage  # noqa: F401
from .viewport_capture import GenTexNodeViewportCapture  # noqa: F401
from .generate import GenTexNodeGenerate  # noqa: F401
from .output_image import GenTexNodeOutputImage  # noqa: F401
from .project_layer import GenTexNodeProjectLayer  # noqa: F401


NODE_CLASSES = (
    GenTexNodeReferenceImage,
    GenTexNodeViewportCapture,
    GenTexNodeGenerate,
    GenTexNodeOutputImage,
    GenTexNodeProjectLayer,
)
