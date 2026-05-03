import bpy

from ..utils.material import rebuild_layer_stack


class GENTEX_OT_LayerRemove(bpy.types.Operator):
    bl_idname = "gentex.layer_remove"
    bl_label = "Remove Layer"
    bl_description = "Remove the active projected layer"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.gentex_layers and obj.gentex_active_layer_index >= 0

    def execute(self, context):
        obj = context.object
        idx = obj.gentex_active_layer_index
        if idx < 0 or idx >= len(obj.gentex_layers):
            return {'CANCELLED'}

        obj.gentex_layers.remove(idx)
        obj.gentex_active_layer_index = max(0, idx - 1) if obj.gentex_layers else -1

        rebuild_layer_stack(obj)
        return {'FINISHED'}


class GENTEX_OT_LayerClear(bpy.types.Operator):
    bl_idname = "gentex.layer_clear"
    bl_label = "Clear All Layers"
    bl_description = "Remove every projected layer from the active object"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and bool(obj.gentex_layers)

    def execute(self, context):
        obj = context.object
        obj.gentex_layers.clear()
        obj.gentex_active_layer_index = -1
        rebuild_layer_stack(obj)
        return {'FINISHED'}
