import bpy

from ..utils.material import rebuild_layer_stack, MARKER_KEY


def _find_layer_stack_slot(obj) -> int:
    for i, slot in enumerate(obj.material_slots):
        if slot.material and slot.material.get(MARKER_KEY):
            return i
    return -1


def _layer_face_set(obj, layer) -> set:
    """Faces covered by this layer.

    Prefers the stored `face_indices` ID-property (written when the layer was
    projected). Falls back to inferring from the layer's UV map: any face with
    at least one non-(0,0) loop is considered part of the layer. Inferred sets
    are cached back onto the layer.
    """
    raw = layer.get("face_indices")
    if raw is not None:
        return {int(i) for i in raw}

    uv_name = layer.uv_name
    if not uv_name:
        return set()
    inferred = set()
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        uv = bm.loops.layers.uv.get(uv_name)
        if uv is None:
            return inferred
        for f in bm.faces:
            for loop in f.loops:
                u, v = loop[uv].uv
                if u != 0.0 or v != 0.0:
                    inferred.add(f.index)
                    break
    else:
        uv = obj.data.uv_layers.get(uv_name)
        if uv is None:
            return inferred
        for poly in obj.data.polygons:
            for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
                u, v = uv.data[li].uv
                if u != 0.0 or v != 0.0:
                    inferred.add(poly.index)
                    break
    layer["face_indices"] = list(inferred)
    return inferred


def _reassign_faces(obj, from_slot: int, to_slot: int,
                    only_indices: set | None = None,
                    exclude: set | None = None):
    """Move faces currently on `from_slot` to `to_slot`. Mode-aware so it works
    in both Edit Mode (via bmesh) and Object Mode."""
    exclude = exclude or set()
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        for f in bm.faces:
            if f.material_index != from_slot:
                continue
            if only_indices is not None and f.index not in only_indices:
                continue
            if f.index in exclude:
                continue
            f.material_index = to_slot
        bmesh.update_edit_mesh(obj.data)
    else:
        for poly in obj.data.polygons:
            if poly.material_index != from_slot:
                continue
            if only_indices is not None and poly.index not in only_indices:
                continue
            if poly.index in exclude:
                continue
            poly.material_index = to_slot


def _remove_uv_layer(obj, name: str):
    if not name:
        return
    uv = obj.data.uv_layers.get(name)
    if uv is None:
        return
    try:
        obj.data.uv_layers.remove(uv)
    except RuntimeError:
        # Blender 5.x sometimes mangles attribute names (.pn. prefix) when the
        # public name collides with the underlying attribute system. Leave the
        # stray map in place rather than crash the whole clear operation.
        pass


class GENTEX_OT_LayerRemove(bpy.types.Operator):
    bl_idname = "gentex.layer_remove"
    bl_label = "Remove Layer"
    bl_description = (
        "Remove the active projected layer, free its UV map, and restore the "
        "uncovered faces' material assignment"
    )

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.gentex_layers and obj.gentex_active_layer_index >= 0

    def execute(self, context):
        obj = context.object
        idx = obj.gentex_active_layer_index
        if idx < 0 or idx >= len(obj.gentex_layers):
            return {'CANCELLED'}
        layer = obj.gentex_layers[idx]

        this_set = _layer_face_set(obj, layer)
        keep_set = set()
        for i, l in enumerate(obj.gentex_layers):
            if i != idx:
                keep_set |= _layer_face_set(obj, l)

        layer_slot = _find_layer_stack_slot(obj)
        if layer_slot >= 0 and not obj.gentex_use_baked:
            # If we know which faces this layer covered, only reset those that
            # aren't still covered by another layer. If we don't know (legacy),
            # fall back to "any face on the layer-stack slot that no other
            # layer claims" — that's the only safe inference.
            only = (this_set - keep_set) if this_set else None
            _reassign_faces(obj, from_slot=layer_slot, to_slot=0,
                            only_indices=only, exclude=keep_set)

        _remove_uv_layer(obj, layer.uv_name)

        obj.gentex_layers.remove(idx)
        obj.gentex_active_layer_index = max(0, idx - 1) if obj.gentex_layers else -1

        rebuild_layer_stack(obj)
        return {'FINISHED'}


class GENTEX_OT_LayerClear(bpy.types.Operator):
    bl_idname = "gentex.layer_clear"
    bl_label = "Clear All Layers"
    bl_description = (
        "Remove every projected layer, free their UV maps, and restore the "
        "mesh's material assignment"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and bool(obj.gentex_layers)

    def execute(self, context):
        obj = context.object

        for layer in obj.gentex_layers:
            _remove_uv_layer(obj, layer.uv_name)

        # Sweep any leftover "Projected UVs *" layers (from earlier sessions
        # where remove didn't clean up). Collect names first — iterating uv
        # references and removing them in place invalidates the rest.
        orphan_names = [uv.name for uv in obj.data.uv_layers
                        if "Projected UVs" in uv.name]
        for name in orphan_names:
            _remove_uv_layer(obj, name)

        layer_slot = _find_layer_stack_slot(obj)
        if layer_slot >= 0 and not obj.gentex_use_baked:
            _reassign_faces(obj, from_slot=layer_slot, to_slot=0)

        obj.gentex_layers.clear()
        obj.gentex_active_layer_index = -1

        rebuild_layer_stack(obj)
        return {'FINISHED'}
