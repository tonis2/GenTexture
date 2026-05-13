"""Pre-wired node-graph templates.

Two canonical workflows that would otherwise be tedious to wire by hand:

  - "Viewport Projection": Text -> Generate; Viewport Capture -> Generate's
    Init/Mask/Depth; Generate -> Project Layer; Viewport Capture -> Project
    Layer's Capture handle.
  - "PBR Material": one Reference Image fans out into three Generate->Output
    chains, each with its own prompt (albedo / normal / roughness).

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


def build_pbr_template(tree, ox, oy):
    ref = tree.nodes.new("GenTexNodeReferenceImage")
    ref.location = (ox, oy)
    ref.label = "Concept / Source"

    rows = [
        ("Albedo",    "clean albedo map, flat even lighting, no shadows, no highlights"),
        ("Normal",    "tangent-space normal map, OpenGL convention, RGB encodes XYZ"),
        ("Roughness", "grayscale roughness map, white = rough, black = smooth"),
    ]

    created = [ref]
    for i, (name, prompt_text) in enumerate(rows):
        row_y = oy - i * 450

        text_n = _new_text(tree, ox + 400, row_y, prompt_text, label=f"{name} prompt")

        gen = tree.nodes.new("GenTexNodeGenerate")
        gen.location = (ox + 850, row_y)
        gen.label = f"{name} generate"

        out = tree.nodes.new("GenTexNodeOutputImage")
        out.location = (ox + 1300, row_y)
        out.output_name = f"PBR {name}"

        _link(tree, text_n, "Text", gen, "Prompt")
        _link(tree, ref, "Image", gen, "References")
        _link(tree, gen, "Image", out, "Image")

        created.extend([text_n, gen, out])

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
