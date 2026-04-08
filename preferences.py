import bpy
from .providers import PROVIDERS

# Top-level package name, used as bl_idname and for addon lookup.
# Computed once at import time from this module's __package__.
ADDON_PKG = __package__.rsplit(".", 1)[0]


class GenTexPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_PKG

    provider: bpy.props.EnumProperty(
        name="Provider",
        description="Active image generation provider",
        items=lambda self, context: [
            (name, cls.name, "") for name, cls in PROVIDERS.items()
        ],
    )

    stability_api_key: bpy.props.StringProperty(
        name="Stability AI API Key",
        description="API key from platform.stability.ai",
        subtype='PASSWORD',
    )

    fal_api_key: bpy.props.StringProperty(
        name="fal.ai API Key",
        description="API key from fal.ai/dashboard/keys",
        subtype='PASSWORD',
    )

    def get_api_key(self, provider_name: str) -> str:
        key_map = {
            "stability": "stability_api_key",
            "fal": "fal_api_key",
        }
        attr = key_map.get(provider_name, "")
        return getattr(self, attr, "") if attr else ""

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.prop(self, "provider")

        box = layout.box()
        box.label(text="API Keys", icon='KEY_HLT')
        box.prop(self, "stability_api_key")
        box.prop(self, "fal_api_key")
