import bpy
from .providers import PROVIDERS

# Full package name, used as bl_idname and for `context.preferences.addons[...]`.
# For legacy addons this is "GenTexture"; for extensions it is
# "bl_ext.user_default.gen_texture" (or whichever repo). Same lookup either way.
ADDON_PKG = __package__


class GenTexPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_PKG

    provider: bpy.props.EnumProperty(
        name="Provider",
        description="Active image generation provider",
        items=lambda self, context: [
            (name, cls.name, "") for name, cls in PROVIDERS.items()
        ],
    )

    fal_model: bpy.props.EnumProperty(
        name="fal Model",
        description="Which model to use when the fal provider is active",
        items=[
            ('flux', "FLUX",
             "Black Forest Labs FLUX. Supports text2img, img2img, depth/normal control, real inpainting"),
            ('nano_banana', "Nano Banana (Gemini 2.5 Flash Image)",
             "Google Gemini 2.5 Flash Image. Strong at multi-view consistency. "
             "No mask channel — masking falls back to a client-side composite"),
        ],
        default='flux',
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
        if self.provider == "fal":
            layout.prop(self, "fal_model")

        box = layout.box()
        box.label(text="API Keys", icon='KEY_HLT')
        box.prop(self, "stability_api_key")
        box.prop(self, "fal_api_key")
