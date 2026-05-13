"""Run / cancel the active GenTexPipelineNodeTree."""

import bpy

from ..node_tree import executor
from ..node_tree.tree import TREE_IDNAME


class GENTEX_OT_RunPipeline(bpy.types.Operator):
    bl_idname = "gentex.run_pipeline"
    bl_label = "Run Pipeline"
    bl_description = (
        "Walk the AI Texture Pipeline tree in topological order and run "
        "each node sequentially. The next API call starts only after the "
        "current one returns."
    )

    @classmethod
    def poll(cls, context):
        if executor.is_running():
            cls.poll_message_set("A pipeline is already running")
            return False
        space = context.space_data
        if space is None or space.type != 'NODE_EDITOR':
            cls.poll_message_set("Open the Node Editor")
            return False
        if getattr(space, "tree_type", "") != TREE_IDNAME:
            cls.poll_message_set("Switch tree type to AI Texture Pipeline")
            return False
        if space.node_tree is None:
            cls.poll_message_set("Create or pick a tree")
            return False
        return True

    def execute(self, context):
        tree = context.space_data.node_tree
        scene = context.scene
        scene.gentex_progress = 1

        def info(msg: str):
            scene.gentex_info = msg

        def on_finish():
            scene.gentex_progress = 0

        executor.run(tree, info, on_finish)
        return {'FINISHED'}


class GENTEX_OT_CancelPipeline(bpy.types.Operator):
    bl_idname = "gentex.cancel_pipeline"
    bl_label = "Cancel Pipeline"
    bl_description = "Cancel the running pipeline after the current node finishes"

    def execute(self, context):
        executor.cancel()
        return {'FINISHED'}
