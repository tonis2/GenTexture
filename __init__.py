bl_info = {
    "name": "GenTexture",
    "author": "GenTexture",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > GenTexture, Image Editor > Sidebar > GenTexture",
    "description": "Generate textures for 3D meshes using AI image generation APIs",
    "category": "Material",
}

# Support F3 > Reload Scripts: reload all submodules before importing
if "bpy" in locals():
    import importlib
    from . import preferences
    from . import utils
    from .utils import image, threading
    from . import providers
    from .providers import stability, fal
    from . import operators
    from .operators import generate, project
    from . import ui
    from .ui import panels
    from . import gpu
    from .gpu import depth, bake

    importlib.reload(image)
    importlib.reload(threading)
    importlib.reload(utils)
    importlib.reload(depth)
    importlib.reload(bake)
    importlib.reload(gpu)
    importlib.reload(providers)    # reload base first (resets PROVIDERS dict)
    importlib.reload(stability)    # then providers re-register into it
    importlib.reload(fal)
    importlib.reload(preferences)
    importlib.reload(generate)
    importlib.reload(project)
    importlib.reload(operators)
    importlib.reload(panels)
    importlib.reload(ui)

import bpy

from .preferences import GenTexPreferences
from .operators.generate import GENTEX_OT_Generate, GENTEX_OT_Cancel
from .operators.project import GENTEX_OT_Project
from .ui.panels import GENTEX_PT_generate, GENTEX_PT_project

# Ensure providers are registered
from .providers import stability  # noqa: F401
from .providers import fal  # noqa: F401


classes = (
    GenTexPreferences,
    GENTEX_OT_Generate,
    GENTEX_OT_Cancel,
    GENTEX_OT_Project,
    GENTEX_PT_generate,
    GENTEX_PT_project,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

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

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
