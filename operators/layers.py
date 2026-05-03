import bpy

from ..utils.material import rebuild_layer_stack, MARKER_KEY


# Mirror of the constants in `.bake_layers` — duplicated rather than imported
# so this module has no sibling-module load-order dependency. Both names are
# the canonical spellings written by the bake operator and the use-baked toggle.
BAKED_MAT_MARKER = "gentex_baked_material"
SNAPSHOT_KEY = "gentex_baked_face_idx"


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


def _remove_gentex_material_slots(obj):
    """Strip the layer-stack and baked-image materials from `obj`.

    Faces referencing these slots are reset to slot 0 first so material_index
    assignments stay valid after slots are popped. The material data blocks are
    orphaned (left to Blender's GC) — removing them outright would clobber any
    other object still referencing the same data block.
    """
    markers = (MARKER_KEY, BAKED_MAT_MARKER)
    victims = sorted(
        (i for i, slot in enumerate(obj.material_slots)
         if slot.material and any(slot.material.get(m) for m in markers)),
        reverse=True,
    )
    if not victims:
        return

    victim_set = set(victims)
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        for f in bm.faces:
            if f.material_index in victim_set:
                f.material_index = 0
        bmesh.update_edit_mesh(obj.data)
    else:
        for poly in obj.data.polygons:
            if poly.material_index in victim_set:
                poly.material_index = 0

    for i in victims:
        obj.data.materials.pop(index=i)


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
        "Wipe every GenTexture artifact on the active object: layers, projected "
        "UV maps, baked images, generated source images, our materials, and "
        "snapshot keys. Use to start a clean projection cycle"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        # Allow Clear even when there are no layers — the user might still have
        # stray baked images, orphan UV maps, or stale material slots from a
        # previous session that they want to wipe.
        obj = context.object
        if obj is None:
            return False
        if obj.gentex_layers:
            return True
        if obj.gentex_baked_image is not None:
            return True
        if any("Projected UVs" in uv.name for uv in obj.data.uv_layers):
            return True
        if any(s.material and (s.material.get(MARKER_KEY) or s.material.get(BAKED_MAT_MARKER))
               for s in obj.material_slots):
            return True
        return False

    def execute(self, context):
        obj = context.object

        # 1. Collect every image and material this addon owns BEFORE we drop
        #    references — we'll orphan-purge at the end.
        owned_images = set()
        owned_mats = set()

        for layer in obj.gentex_layers:
            if layer.image is not None:
                owned_images.add(layer.image.name)
            if layer.mask_image is not None:
                owned_images.add(layer.mask_image.name)
        if obj.gentex_baked_image is not None:
            owned_images.add(obj.gentex_baked_image.name)

        # Sweep any GenTex-named images that aren't referenced from this
        # object's layer list (intermediate bakes, stale layer images). They're
        # identifiable by the "GenTex" prefix written in project_layer/bake_layers.
        for img in bpy.data.images:
            if img.name.startswith("GenTex "):
                owned_images.add(img.name)

        for slot in obj.material_slots:
            m = slot.material
            if m and (m.get(MARKER_KEY) or m.get(BAKED_MAT_MARKER)):
                owned_mats.add(m.name)

        # 2. Free per-layer UV maps + the orphan "Projected UVs *" sweep.
        for layer in obj.gentex_layers:
            _remove_uv_layer(obj, layer.uv_name)
        orphan_uv_names = [uv.name for uv in obj.data.uv_layers
                           if "Projected UVs" in uv.name]
        for name in orphan_uv_names:
            _remove_uv_layer(obj, name)

        # 3. Strip the layer-stack and baked-material slots from the object.
        _remove_gentex_material_slots(obj)

        # 4. Reset all GenTex state on the object.
        obj.gentex_layers.clear()
        obj.gentex_active_layer_index = -1
        obj.gentex_baked_image = None
        obj.gentex_baked_uv = ""
        if obj.gentex_use_baked:
            obj.gentex_use_baked = False
        # Both old and new snapshot keys (they coexist briefly across versions).
        for key in (SNAPSHOT_KEY, "gentex_baked_face_orig_idx"):
            if key in obj.keys():
                del obj[key]

        # 5. Now that the object no longer references any of them, purge the
        #    image/material data-blocks from bpy.data so the file isn't bloated
        #    with `.001` / `.013` accumulations from repeated projections.
        for name in owned_images:
            img = bpy.data.images.get(name)
            if img is not None and img.users == 0:
                bpy.data.images.remove(img)
        for name in owned_mats:
            mat = bpy.data.materials.get(name)
            if mat is not None and mat.users == 0:
                bpy.data.materials.remove(mat)

        return {'FINISHED'}
