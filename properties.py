import bpy


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
    )

    mask_image: bpy.props.PointerProperty(
        name="Mask",
        type=bpy.types.Image,
        description="Per-layer mask (white=visible, black=transparent). Same UVs as image",
    )

    uv_name: bpy.props.StringProperty(
        name="UV Map",
        description="Name of the projected UV map captured at generation time",
        default="",
    )

    opacity: bpy.props.FloatProperty(
        name="Opacity",
        default=1.0, min=0.0, max=1.0,
    )

    visible: bpy.props.BoolProperty(
        name="Visible",
        default=True,
    )

    seed: bpy.props.IntProperty(name="Seed", default=0)


def register():
    bpy.utils.register_class(GenTexLayer)
    bpy.types.Object.gentex_layers = bpy.props.CollectionProperty(type=GenTexLayer)
    bpy.types.Object.gentex_active_layer_index = bpy.props.IntProperty(default=-1)


def unregister():
    del bpy.types.Object.gentex_layers
    del bpy.types.Object.gentex_active_layer_index
    bpy.utils.unregister_class(GenTexLayer)
