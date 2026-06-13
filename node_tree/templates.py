"""Pre-wired node-graph templates.

Two canonical workflows that would otherwise be tedious to wire by hand:

  - "Viewport Projection": Text -> Generate; Viewport Capture -> Generate's
    Init/Mask/Depth; Generate -> Project Layer; Viewport Capture -> Project
    Layer's Capture handle.
  - "PBR Material": an Albedo Text->Generate->Output chain whose generated
    image is fed as the reference into the Normal and Depth chains, so all
    three maps stay coherent with the base color.

Exposed both as buttons in the Node Editor sidebar and as a submenu under
Shift+A so users don't have to remember the wiring.
"""

import bpy

from .tree import TREE_IDNAME


def _link(tree, from_node, from_sock, to_node, to_sock):
    tree.links.new(from_node.outputs[from_sock], to_node.inputs[to_sock])


def _new_text(tree, x, y, text, label=None):
    n = tree.nodes.new("GenTexNodeText")
    n.location = (x, y)
    n.text = text
    if label:
        n.label = label
    return n


def build_projection_template(tree, ox, oy):
    prompt = _new_text(
        tree, ox, oy,
        "rusted metal plating, weathered, photoreal",
        label="Prompt",
    )

    cap = tree.nodes.new("GenTexNodeViewportCapture")
    cap.location = (ox, oy - 350)

    gen = tree.nodes.new("GenTexNodeGenerate")
    gen.location = (ox + 450, oy - 50)

    proj = tree.nodes.new("GenTexNodeProjectLayer")
    proj.location = (ox + 900, oy - 50)

    _link(tree, prompt, "Text", gen, "Prompt")
    _link(tree, cap, "Color", gen, "Init")
    _link(tree, cap, "Mask", gen, "Mask")
    _link(tree, cap, "Depth", gen, "Depth")
    _link(tree, gen, "Image", proj, "Image")
    _link(tree, cap, "Capture", proj, "Capture")
    return [prompt, cap, gen, proj]


def _new_generate(tree, x, y, label=None):
    n = tree.nodes.new("GenTexNodeGenerate")
    n.location = (x, y)
    n.provider = "gemini_direct"
    n.model_enum = "gemini-3-pro-image-preview"
    if label:
        n.label = label
    return n


def build_pbr_template(tree, ox, oy):
    """The three maps are generated as a chain, each feeding the next as a
    reference image: Albedo -> Depth -> Normal. Albedo is generated from its
    prompt alone, Depth takes the albedo result as its reference, and Normal
    takes the depth result. Each link is Text -> Generate -> Output Image."""

    rows = [
        ("Albedo", "Create albedo texture. It needs to work in game engine. it has to be seamless on all axes and unlit."),
        ("Depth",  "Create depth texture. It needs to work in game engine. it has to be seamless on all axes."),
        ("Normal", "Create normal texture. It needs to work in game engine. it has to be seamless on all axes."),
    ]

    created = []
    prev_out = None
    for i, (name, prompt_text) in enumerate(rows):
        row_y = oy - i * 450

        text_n = _new_text(tree, ox, row_y, prompt_text, label=f"{name} prompt")

        gen = _new_generate(tree, ox + 450, row_y, label=f"{name} generate")

        out = tree.nodes.new("GenTexNodeOutputImage")
        out.location = (ox + 900, row_y)
        out.output_name = f"PBR {name}"

        _link(tree, text_n, "Text", gen, "Prompt")
        _link(tree, gen, "Image", out, "Image")

        # Every map after Albedo takes the previous map's result as its
        # reference image, so the chain stays coherent end to end.
        if prev_out is not None:
            _link(tree, prev_out, "Image", gen, "References")

        created.extend([text_n, gen, out])
        prev_out = out

    return created


TEMPLATES = {
    "projection": ("Viewport Projection", build_projection_template),
    "pbr":        ("PBR Material",        build_pbr_template),
}


def _is_pipeline_editor(context) -> bool:
    space = context.space_data
    return space is not None and getattr(space, "tree_type", "") == TREE_IDNAME


def _origin_for_new_template(tree):
    """Place new templates below any existing nodes so they don't overlap."""
    if not tree.nodes:
        return (0, 0)
    min_x = min(n.location.x for n in tree.nodes)
    min_y = min(n.location.y for n in tree.nodes)
    return (min_x, min_y - 800)


class GENTEX_OT_AddTemplate(bpy.types.Operator):
    """Insert a pre-wired node template into the active pipeline tree."""

    bl_idname = "gentex.add_template"
    bl_label = "Add Template"
    bl_options = {'REGISTER', 'UNDO'}

    template: bpy.props.EnumProperty(
        name="Template",
        items=[(k, label, "") for k, (label, _) in TEMPLATES.items()],
    )

    @classmethod
    def poll(cls, context):
        return _is_pipeline_editor(context) and context.space_data.edit_tree is not None

    def execute(self, context):
        tree = context.space_data.edit_tree
        ox, oy = _origin_for_new_template(tree)

        _, builder = TEMPLATES[self.template]
        created = builder(tree, ox, oy)

        for n in tree.nodes:
            n.select = False
        for n in created:
            n.select = True
        if created:
            tree.nodes.active = created[-1]
        return {'FINISHED'}


class GENTEX_MT_template_menu(bpy.types.Menu):
    bl_idname = "GENTEX_MT_template_menu"
    bl_label = "Templates"

    def draw(self, context):
        layout = self.layout
        for key, (label, _) in TEMPLATES.items():
            op = layout.operator("gentex.add_template", text=label, icon='NODETREE')
            op.template = key


def _add_menu_draw(self, context):
    if not _is_pipeline_editor(context):
        return
    self.layout.menu(GENTEX_MT_template_menu.bl_idname, icon='NODETREE')


def register_add_menu():
    bpy.types.NODE_MT_add.append(_add_menu_draw)


def unregister_add_menu():
    bpy.types.NODE_MT_add.remove(_add_menu_draw)
