import bpy


MATERIAL_NAME = "gentex-layer-stack"
MARKER_KEY = "gentex_layer_stack"


def get_or_create_layer_material(obj) -> bpy.types.Material:
    """Return the layer-stack material on `obj`, creating one if absent.

    The material is identified by a custom property `gentex_layer_stack=True`
    so we don't clobber unrelated materials the user might have.
    """
    for slot in obj.material_slots:
        m = slot.material
        if m and m.get(MARKER_KEY):
            return m

    mat = bpy.data.materials.new(name=MATERIAL_NAME)
    mat.use_nodes = True
    mat[MARKER_KEY] = True
    obj.data.materials.append(mat)
    return mat


def rebuild_layer_stack(obj):
    """Rewire the layer-stack material's node tree from `obj.gentex_layers`.

    Build order, bottom-up:
        out = (0, 0, 0, 0)
        for layer in layers (bottom -> top):
            f = (mask.r if mask else 1.0) * opacity
            out = mix_rgb(f, out, layer_image.rgb)
        out -> Principled BSDF base color
    """
    mat = get_or_create_layer_material(obj)
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (1400, 0)

    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (1100, 0)
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    layers = [l for l in obj.gentex_layers if l.image is not None and l.visible]
    if not layers:
        return mat

    prev_color = None  # socket
    x = -1200

    for i, layer in enumerate(layers):
        uv = nodes.new("ShaderNodeUVMap")
        uv.location = (x, i * -350 - 100)
        uv.uv_map = layer.uv_name

        img_tex = nodes.new("ShaderNodeTexImage")
        img_tex.location = (x + 200, i * -350)
        img_tex.image = layer.image
        img_tex.extension = 'CLIP'
        links.new(uv.outputs["UV"], img_tex.inputs["Vector"])

        mask_factor_socket = None
        if layer.mask_image is not None:
            mask_tex = nodes.new("ShaderNodeTexImage")
            mask_tex.location = (x + 200, i * -350 - 200)
            mask_tex.image = layer.mask_image
            mask_tex.image.colorspace_settings.name = 'Non-Color'
            mask_tex.extension = 'CLIP'
            links.new(uv.outputs["UV"], mask_tex.inputs["Vector"])
            mask_factor_socket = mask_tex.outputs["Color"]
        else:
            # Use the image's own alpha as the mask
            mask_factor_socket = img_tex.outputs["Alpha"]

        # Multiply mask by opacity
        mul = nodes.new("ShaderNodeMath")
        mul.location = (x + 500, i * -350 - 100)
        mul.operation = 'MULTIPLY'
        mul.inputs[1].default_value = layer.opacity
        links.new(mask_factor_socket, mul.inputs[0])

        if prev_color is None:
            prev_color = img_tex.outputs["Color"]
            # First (bottom) layer: factor scales toward black background
            # So actually, mix from black -> img to honour mask on bottom layer too.
            mix_first = nodes.new("ShaderNodeMixRGB")
            mix_first.location = (x + 750, i * -350)
            mix_first.blend_type = 'MIX'
            mix_first.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)
            links.new(mul.outputs[0], mix_first.inputs[0])
            links.new(img_tex.outputs["Color"], mix_first.inputs[2])
            prev_color = mix_first.outputs[0]
        else:
            mix = nodes.new("ShaderNodeMixRGB")
            mix.location = (x + 750, i * -350)
            mix.blend_type = 'MIX'
            links.new(mul.outputs[0], mix.inputs[0])
            links.new(prev_color, mix.inputs[1])
            links.new(img_tex.outputs["Color"], mix.inputs[2])
            prev_color = mix.outputs[0]

        x += 350

    if prev_color is not None:
        links.new(prev_color, principled.inputs["Base Color"])

    return mat
