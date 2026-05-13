"""GenTexPipelineNodeTree — the custom node tree type.

Users open a Node Editor, change its tree type to this, and build an AI
pipeline. Shift+A adds new nodes from the registered category.
"""

import bpy

try:
    # Blender ships nodeitems_utils as part of its scripts. Available across
    # the 4.x range.
    import nodeitems_utils
    from nodeitems_utils import NodeCategory, NodeItem
    _HAVE_NODEITEMS = True
except Exception:
    _HAVE_NODEITEMS = False


TREE_IDNAME = "GenTexPipelineNodeTree"


class GenTexPipelineNodeTree(bpy.types.NodeTree):
    bl_idname = TREE_IDNAME
    bl_label = "AI Texture Pipeline"
    bl_icon = "NODETREE"


class _GenTexNodeCategory(NodeCategory if _HAVE_NODEITEMS else object):
    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == TREE_IDNAME


_CATEGORIES_KEY = "GENTEX_PIPELINE_NODES"


def register_categories():
    if not _HAVE_NODEITEMS:
        return
    # Late import so the node classes have a chance to register first.
    cats = [
        _GenTexNodeCategory(_CATEGORIES_KEY, "AI Texture", items=[
            NodeItem("GenTexNodeText"),
            NodeItem("GenTexNodeReferenceImage"),
            NodeItem("GenTexNodeViewportCapture"),
            NodeItem("GenTexNodeGenerate"),
            NodeItem("GenTexNodeProjectLayer"),
            NodeItem("GenTexNodeOutputImage"),
        ]),
    ]
    try:
        nodeitems_utils.unregister_node_categories(_CATEGORIES_KEY)
    except Exception:
        pass
    nodeitems_utils.register_node_categories(_CATEGORIES_KEY, cats)


def unregister_categories():
    if not _HAVE_NODEITEMS:
        return
    try:
        nodeitems_utils.unregister_node_categories(_CATEGORIES_KEY)
    except Exception:
        pass
