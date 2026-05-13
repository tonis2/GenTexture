import bpy


def _use_baked_changed(self, context):
    from .operators.bake_layers import apply_baked_toggle
    apply_baked_toggle(self, self.gentex_use_baked)


_REBAKE_GUARD = False


def _layer_changed(self, context):
    """Rebuild the host object's layer-stack material when a layer changes.

    When the object is currently displaying its baked composite, the layer-stack
    material isn't visible — so we also re-run the bake so toggles like
    `visible` and `opacity` actually show up in the viewport.
    """
    from .utils.material import rebuild_layer_stack
    obj = self.id_data
    if obj is None:
        return
    rebuild_layer_stack(obj)

    global _REBAKE_GUARD
    if obj.gentex_use_baked and obj.gentex_layers and not _REBAKE_GUARD:
        _REBAKE_GUARD = True
        try:
            prev_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            try:
                bpy.ops.gentex.bake_layers()
            except Exception as e:
                print(f"[GenTex] re-bake on layer change failed: {e}")
            finally:
                bpy.context.view_layer.objects.active = prev_active
        finally:
            _REBAKE_GUARD = False

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class GenTexLayer(bpy.types.PropertyGroup):
    """A single projected texture layer."""

    name: bpy.props.StringProperty(name="Name", default="Layer")

    image: bpy.props.PointerProperty(
        name="Image",
        type=bpy.types.Image,
        description="Generated color image for this layer",
        update=_layer_changed,
    )

    mask_image: bpy.props.PointerProperty(
        name="Mask",
        type=bpy.types.Image,
        description="Per-layer mask (white=visible, black=transparent). Same UVs as image",
        update=_layer_changed,
    )

    uv_name: bpy.props.StringProperty(
        name="UV Map",
        description="Name of the projected UV map captured at generation time",
        default="",
        update=_layer_changed,
    )

    opacity: bpy.props.FloatProperty(
        name="Opacity",
        default=1.0, min=0.0, max=1.0,
        update=_layer_changed,
    )

    visible: bpy.props.BoolProperty(
        name="Visible",
        default=True,
        update=_layer_changed,
    )

    seed: bpy.props.IntProperty(name="Seed", default=0)


def register():
    bpy.utils.register_class(GenTexLayer)
    bpy.types.Object.gentex_layers = bpy.props.CollectionProperty(type=GenTexLayer)
    bpy.types.Object.gentex_active_layer_index = bpy.props.IntProperty(default=-1)
    bpy.types.Object.gentex_baked_image = bpy.props.PointerProperty(
        name="Baked Image",
        type=bpy.types.Image,
        description="Most recent baked composite of all projected layers",
    )
    bpy.types.Object.gentex_baked_uv = bpy.props.StringProperty(
        name="Baked UV",
        description="UV layer the baked image was rendered into",
        default="",
    )
    bpy.types.Object.gentex_use_baked = bpy.props.BoolProperty(
        name="Use Baked Image",
        description="Show the baked composite instead of the layer-stack shader",
        default=False,
        update=_use_baked_changed,
    )


def unregister():
    del bpy.types.Object.gentex_layers
    del bpy.types.Object.gentex_active_layer_index
    del bpy.types.Object.gentex_baked_image
    del bpy.types.Object.gentex_baked_uv
    del bpy.types.Object.gentex_use_baked
    bpy.utils.unregister_class(GenTexLayer)
