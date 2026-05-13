bl_info = {
    "name": "GenTexture",
    "description": "Generate textures for 3D meshes using AI image generation APIs",
    "author": "Tonis",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "Node Editor > AI Texture Pipeline; View3D > Sidebar > GenTexture (Layers)",
    "category": "Material",
}

_needs_reload = "preferences" in locals()

# Import order matters: providers must register (via @register_provider in
# their module bodies) BEFORE preferences.py builds its dynamic AddonPreferences
# class at import time.
from . import providers as _providers_pkg
from .providers import api as _api
from .providers import _http as _http_helper
from .providers import stability as _stability
from .providers import fal as _fal
from .providers import local_server as _local_server
from .providers import gemini_direct as _gemini_direct

from . import preferences
from . import properties
from .utils import image as _img, threading as _thr, material as _mat
from .gpu import bake as _bake, mask as _mask, visible as _vis, depth as _depth

# Node tree (custom NodeTree, sockets, nodes, panel)
from .node_tree import sockets as _nt_sockets
from .node_tree import tree as _nt_tree
from .node_tree.nodes import _base as _nt_base
from .node_tree.nodes import reference_image as _nt_ref_image
from .node_tree.nodes import text as _nt_text
from .node_tree.nodes import viewport_capture as _nt_viewport_capture
from .node_tree.nodes import generate as _nt_generate
from .node_tree.nodes import output_image as _nt_output_image
from .node_tree.nodes import project_layer as _nt_project_layer
from .node_tree import nodes as _nt_nodes
from .node_tree import executor as _nt_executor
from .node_tree import templates as _nt_templates
from .node_tree import panels as _nt_panels

# Operators (layers & bake & run-pipeline)
from .operators import bake_layers as _bl, layers as _layers
from .operators import run_pipeline as _run_pipeline

from .ui import panels as _panels

# MCP server (TCP JSON command dispatcher, optional)
from . import mcp_server as _mcp_server
from .mcp_server import operators as _mcp_ops


if _needs_reload:
    import importlib
    _api = importlib.reload(_api)
    _http_helper = importlib.reload(_http_helper)
    _providers_pkg = importlib.reload(_providers_pkg)
    _stability = importlib.reload(_stability)
    _fal = importlib.reload(_fal)
    _local_server = importlib.reload(_local_server)
    _gemini_direct = importlib.reload(_gemini_direct)
    preferences = importlib.reload(preferences)
    properties = importlib.reload(properties)
    _img = importlib.reload(_img)
    _thr = importlib.reload(_thr)
    _mat = importlib.reload(_mat)
    _bake = importlib.reload(_bake)
    _mask = importlib.reload(_mask)
    _vis = importlib.reload(_vis)
    _depth = importlib.reload(_depth)
    _nt_sockets = importlib.reload(_nt_sockets)
    _nt_tree = importlib.reload(_nt_tree)
    _nt_base = importlib.reload(_nt_base)
    _nt_ref_image = importlib.reload(_nt_ref_image)
    _nt_text = importlib.reload(_nt_text)
    _nt_viewport_capture = importlib.reload(_nt_viewport_capture)
    _nt_generate = importlib.reload(_nt_generate)
    _nt_output_image = importlib.reload(_nt_output_image)
    _nt_project_layer = importlib.reload(_nt_project_layer)
    _nt_nodes = importlib.reload(_nt_nodes)
    _nt_executor = importlib.reload(_nt_executor)
    _nt_templates = importlib.reload(_nt_templates)
    _nt_panels = importlib.reload(_nt_panels)
    _bl = importlib.reload(_bl)
    _layers = importlib.reload(_layers)
    _run_pipeline = importlib.reload(_run_pipeline)
    _panels = importlib.reload(_panels)
    _mcp_server = importlib.reload(_mcp_server)
    _mcp_ops = importlib.reload(_mcp_ops)


import bpy


classes = (
    preferences.GenTexPreferences,
    # Node tree, sockets, nodes
    _nt_tree.GenTexPipelineNodeTree,
    _nt_sockets.GenTexImageSocket,
    _nt_ref_image.GenTexNodeReferenceImage,
    _nt_text.GenTexNodeText,
    _nt_viewport_capture.GenTexNodeViewportCapture,
    _nt_generate.GenTexNodeGenerate,
    _nt_output_image.GenTexNodeOutputImage,
    _nt_project_layer.GenTexNodeProjectLayer,
    # Operators
    _bl.GENTEX_OT_BakeLayers,
    _layers.GENTEX_OT_LayerRemove,
    _layers.GENTEX_OT_LayerClear,
    _run_pipeline.GENTEX_OT_RunPipeline,
    _run_pipeline.GENTEX_OT_CancelPipeline,
    _nt_templates.GENTEX_OT_AddTemplate,
    _nt_templates.GENTEX_MT_template_menu,
    # Panels (3D View — layer stack only)
    _panels.GENTEX_UL_Layers,
    _panels.GENTEX_PT_main,
    _panels.GENTEX_PT_layers,
    _panels.GENTEX_PT_bake,
    # Node editor sidebar
    _nt_panels.GENTEX_PT_pipeline,
    # MCP server start/stop operators
    *_mcp_ops.classes,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    properties.register()

    scene = bpy.types.Scene
    scene.gentex_progress = bpy.props.IntProperty(
        name="Progress",
        default=0, min=0, max=100,
    )
    scene.gentex_info = bpy.props.StringProperty(
        name="Info",
        default="",
    )

    _nt_tree.register_categories()
    _nt_panels.register_header()
    _nt_templates.register_add_menu()

    # Auto-start the MCP server if the user previously enabled it.
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        if getattr(prefs, "mcp_enabled", False):
            _mcp_server.start_server(prefs.mcp_host, int(prefs.mcp_port))
    except Exception as e:
        print(f"GenTexture: MCP auto-start skipped: {e}")


def unregister():
    try:
        _mcp_server.stop_server()
    except Exception as e:
        print(f"GenTexture: MCP stop on unregister failed: {e}")

    _nt_templates.unregister_add_menu()
    _nt_panels.unregister_header()
    _nt_tree.unregister_categories()

    scene = bpy.types.Scene
    del scene.gentex_progress
    del scene.gentex_info

    properties.unregister()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
