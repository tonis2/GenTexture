"""Custom socket type used to wire image data between pipeline nodes.

The socket itself carries no value. Blender doesn't allow Image datablock
pointers on custom sockets, so the executor maintains a side-channel cache
keyed by output-socket identifier (see executor.py).
"""

import bpy


class GenTexImageSocket(bpy.types.NodeSocket):
    bl_idname = "GenTexImageSocket"
    bl_label = "Image"

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.4, 0.7, 1.0, 1.0)
