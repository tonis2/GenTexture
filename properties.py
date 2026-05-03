import bpy


def _layer_changed(self, context):
    """Rebuild the host object's layer-stack material when a layer changes."""
    # Lazy import to avoid a circular import at module load.
    from .utils.material import rebuild_layer_stack
    obj = self.id_data  # the Object that owns this PropertyGroup
    if obj is not None:
        rebuild_layer_stack(obj)
        # Force viewport redraw so the change is visible immediately.
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


class GenTexLayer(bpy.types.PropertyGroup):
    """A single projected texture layer.

    Mirrors Modddif's ProjectedTextureLayer: an AI-generated image projected
    through a frozen camera/UV snapshot, optionally masked by a per-layer mask
    image so the layer only contributes where it was generated.
    """

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


class GenTexReferenceImage(bpy.types.PropertyGroup):
    """A reference image fed alongside the prompt to keep style/theme consistent.

    Mirrors Modddif's `referenceImages[]` field on the worker job — a list of
    extra images the model conditions on (e.g. an existing layer's color, a
    mood-board picture). Most useful with Nano Banana (Gemini 2.5 Flash Image),
    which natively accepts multiple input images.
    """

    image: bpy.props.PointerProperty(
        name="Image",
        type=bpy.types.Image,
        description="Reference image. Pick any Blender image, including a layer's color image",
    )


def register():
    bpy.utils.register_class(GenTexLayer)
    bpy.utils.register_class(GenTexReferenceImage)
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
    bpy.types.Scene.gentex_references = bpy.props.CollectionProperty(type=GenTexReferenceImage)
    bpy.types.Scene.gentex_active_reference_index = bpy.props.IntProperty(default=-1)


def unregister():
    del bpy.types.Object.gentex_layers
    del bpy.types.Object.gentex_active_layer_index
    del bpy.types.Object.gentex_baked_image
    del bpy.types.Object.gentex_baked_uv
    del bpy.types.Scene.gentex_references
    del bpy.types.Scene.gentex_active_reference_index
    bpy.utils.unregister_class(GenTexReferenceImage)
    bpy.utils.unregister_class(GenTexLayer)
