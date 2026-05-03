import os

import bpy
from bpy_extras.io_utils import ImportHelper


class GENTEX_OT_ReferenceAdd(bpy.types.Operator):
    bl_idname = "gentex.reference_add"
    bl_label = "Add Reference Slot"
    bl_description = "Add an empty reference slot. Pick the image from the dropdown in the list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        scene.gentex_references.add()
        scene.gentex_active_reference_index = len(scene.gentex_references) - 1
        return {'FINISHED'}


class GENTEX_OT_ReferenceLoad(bpy.types.Operator, ImportHelper):
    bl_idname = "gentex.reference_load"
    bl_label = "Load Reference From Disk"
    bl_description = "Load an image from disk and add it as a reference"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.webp;*.tif;*.tiff;*.bmp;*.exr;*.hdr",
        options={'HIDDEN'},
    )

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({'ERROR'}, "No file selected.")
            return {'CANCELLED'}

        img = bpy.data.images.load(self.filepath, check_existing=True)
        ref = context.scene.gentex_references.add()
        ref.image = img
        context.scene.gentex_active_reference_index = len(context.scene.gentex_references) - 1
        return {'FINISHED'}


class GENTEX_OT_ReferenceAddFromActiveLayer(bpy.types.Operator):
    bl_idname = "gentex.reference_add_from_active_layer"
    bl_label = "Add Active Layer as Reference"
    bl_description = "Add the currently selected projected layer's image as a reference"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None:
            return False
        return (obj.gentex_layers
                and 0 <= obj.gentex_active_layer_index < len(obj.gentex_layers)
                and obj.gentex_layers[obj.gentex_active_layer_index].image is not None)

    def execute(self, context):
        obj = context.object
        layer = obj.gentex_layers[obj.gentex_active_layer_index]
        ref = context.scene.gentex_references.add()
        ref.image = layer.image
        context.scene.gentex_active_reference_index = len(context.scene.gentex_references) - 1
        return {'FINISHED'}


class GENTEX_OT_ReferenceRemove(bpy.types.Operator):
    bl_idname = "gentex.reference_remove"
    bl_label = "Remove Reference"
    bl_description = "Remove the active reference image"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene
        return s.gentex_references and 0 <= s.gentex_active_reference_index < len(s.gentex_references)

    def execute(self, context):
        s = context.scene
        idx = s.gentex_active_reference_index
        s.gentex_references.remove(idx)
        s.gentex_active_reference_index = max(0, idx - 1) if s.gentex_references else -1
        return {'FINISHED'}


class GENTEX_OT_ReferenceClear(bpy.types.Operator):
    bl_idname = "gentex.reference_clear"
    bl_label = "Clear References"
    bl_description = "Remove all reference images"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.gentex_references)

    def execute(self, context):
        context.scene.gentex_references.clear()
        context.scene.gentex_active_reference_index = -1
        return {'FINISHED'}
