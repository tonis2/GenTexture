bl_info = {
    "name": "GenTexture",
    "description": "Generate textures for 3D meshes using AI image generation APIs",
    "author": "Tonis",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > GenTexture, Image Editor > Sidebar > GenTexture",
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

from . import preferences
from . import properties
from .utils import image as _img, threading as _thr, material as _mat
from .gpu import depth as _depth, bake as _bake, uv_normals as _uvn, mask as _mask, visible as _vis
from .operators import generate as _gen, project as _proj, generate_uv as _gen_uv
from .operators import project_layer as _pl, bake_layers as _bl, layers as _layers
from .operators import references as _refs
from .ui import panels as _panels

if _needs_reload:
    import importlib
    # Reload the API foundation first
    _api = importlib.reload(_api)
    _http_helper = importlib.reload(_http_helper)
    _providers_pkg = importlib.reload(_providers_pkg)
    _stability = importlib.reload(_stability)
    _fal = importlib.reload(_fal)
    # Then everything that depends on it
    preferences = importlib.reload(preferences)
    properties = importlib.reload(properties)
    _img = importlib.reload(_img)
    _thr = importlib.reload(_thr)
    _mat = importlib.reload(_mat)
    _depth = importlib.reload(_depth)
    _bake = importlib.reload(_bake)
    _uvn = importlib.reload(_uvn)
    _mask = importlib.reload(_mask)
    _vis = importlib.reload(_vis)
    _gen = importlib.reload(_gen)
    _proj = importlib.reload(_proj)
    _gen_uv = importlib.reload(_gen_uv)
    _pl = importlib.reload(_pl)
    _bl = importlib.reload(_bl)
    _layers = importlib.reload(_layers)
    _refs = importlib.reload(_refs)
    _panels = importlib.reload(_panels)


import bpy


classes = (
    preferences.GenTexPreferences,
    _gen.GENTEX_OT_Generate,
    _gen.GENTEX_OT_Cancel,
    _proj.GENTEX_OT_Project,
    _gen_uv.GENTEX_OT_GenerateUV,
    _pl.GENTEX_OT_ProjectLayer,
    _bl.GENTEX_OT_BakeLayers,
    _layers.GENTEX_OT_LayerRemove,
    _layers.GENTEX_OT_LayerClear,
    _refs.GENTEX_OT_ReferenceAdd,
    _refs.GENTEX_OT_ReferenceLoad,
    _refs.GENTEX_OT_ReferenceAddFromActiveLayer,
    _refs.GENTEX_OT_ReferenceRemove,
    _refs.GENTEX_OT_ReferenceClear,
    _panels.GENTEX_UL_Layers,
    _panels.GENTEX_UL_References,
    _panels.GENTEX_PT_generate,
    _panels.GENTEX_PT_main,
    _panels.GENTEX_PT_project,
    _panels.GENTEX_PT_settings,
    _panels.GENTEX_PT_references,
    _panels.GENTEX_PT_layers,
    _panels.GENTEX_PT_bake,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    properties.register()

    scene = bpy.types.Scene

    scene.gentex_prompt = bpy.props.StringProperty(
        name="Prompt",
        description="Text prompt for texture generation",
        default="",
    )
    scene.gentex_negative_prompt = bpy.props.StringProperty(
        name="Negative Prompt",
        description="What to avoid in the generated texture",
        default="",
    )
    scene.gentex_width = bpy.props.IntProperty(
        name="Width",
        description="Output image width",
        default=1024, min=512, max=2048, step=64,
    )
    scene.gentex_height = bpy.props.IntProperty(
        name="Height",
        description="Output image height",
        default=1024, min=512, max=2048, step=64,
    )
    scene.gentex_strength = bpy.props.FloatProperty(
        name="Strength",
        description="How much the AI can deviate from the input (lower = more faithful)",
        default=0.7, min=0.0, max=1.0,
    )
    scene.gentex_progress = bpy.props.IntProperty(
        name="Progress",
        default=0, min=0, max=100,
    )
    scene.gentex_info = bpy.props.StringProperty(
        name="Info",
        default="",
    )
    scene.gentex_depth_size = bpy.props.IntProperty(
        name="Depth Map Size",
        description="Resolution of the depth map sent to the AI. Larger may improve detail but is slower",
        default=512, min=128, max=2048, step=64,
    )
    scene.gentex_project_input = bpy.props.EnumProperty(
        name="Input",
        description="What viewport data to send to the AI",
        items=[
            ('DEPTH', 'Depth Only', 'Send only the depth map as guidance'),
            ('COLOR', 'Depth + Color', 'Send both depth and viewport color'),
        ],
        default='DEPTH',
    )
    scene.gentex_project_bake = bpy.props.BoolProperty(
        name="Bake to UV",
        description="Remap the projected texture to the active UV layout",
        default=False,
    )


def unregister():
    scene = bpy.types.Scene

    del scene.gentex_prompt
    del scene.gentex_negative_prompt
    del scene.gentex_depth_size
    del scene.gentex_width
    del scene.gentex_height
    del scene.gentex_strength
    del scene.gentex_progress
    del scene.gentex_info
    del scene.gentex_project_input
    del scene.gentex_project_bake

    properties.unregister()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
