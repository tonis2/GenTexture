"""Generate node — calls a provider (Gemini direct, fal, Stability, ...).

All settings (prompt/negative/width/height/strength/depth_scale) are exposed
as input sockets so they can be wired from upstream nodes or set inline as
socket default values. The provider/model selection lives on the node itself.

For reference images, the node grows extra "Ref" inputs in Geometry-Nodes
style: connecting the last one appends a new empty one; disconnecting trailing
empties trims them.
"""

import bpy

from ._base import GenTexPipelineNodeBase, upstream_value, upstream_multi_input
from ...preferences import ADDON_PKG
from ...providers import (
    GenerateRequest, get_provider, get_provider_class, has_provider, iter_providers,
)


def _provider_items(self, context):
    return [(pid, cls.label or pid, cls.__doc__ or "") for pid, cls in iter_providers()]


# Cache built enum item lists per provider so the (id, label, desc) string
# objects stay referenced — Blender can corrupt/crash on dynamic EnumProperty
# items whose strings get garbage-collected between the callback and use.
_model_items_cache: dict = {}


def _provider_models(provider_id):
    """Models declared by a provider, or [] if it has none / isn't registered."""
    if not has_provider(provider_id):
        return []
    return get_provider_class(provider_id).models()


def _model_items(self, context):
    pid = getattr(self, "provider", "") or ""
    items = [("", "Default", "Use the provider's configured default model")]
    for m in _provider_models(pid):
        items.append(tuple(m))
    _model_items_cache[pid] = items
    return _model_items_cache[pid]


class GenTexNodeGenerate(GenTexPipelineNodeBase, bpy.types.Node):
    bl_idname = "GenTexNodeGenerate"
    bl_label = "Generate"
    bl_icon = "RENDER_STILL"
    bl_width_default = 320
    bl_width_min = 220

    # The HTTP call to the provider can take 30s+. Must not block the main thread.
    runs_async = True

    provider: bpy.props.EnumProperty(
        name="Provider",
        description="Provider to call",
        items=_provider_items,
    )

    # Free-text override, shown for providers that don't declare a model list.
    model: bpy.props.StringProperty(
        name="Model",
        description=(
            "Override the provider's default model. "
            "Leave empty for the provider's default."
        ),
        default="",
    )

    # Dropdown override, shown for providers that declare models() (e.g. Gemini
    # direct). "" (the leading "Default" item) means use the provider default.
    model_enum: bpy.props.EnumProperty(
        name="Model",
        description="Model to use, from the provider's configured list",
        items=_model_items,
    )

    def init(self, context):
        sp = self.inputs.new("NodeSocketString", "Prompt")
        sp.default_value = ""
        sp.hide_value = True
        sn = self.inputs.new("NodeSocketString", "Negative")
        sn.default_value = ""
        sn.hide_value = True
        sw = self.inputs.new("NodeSocketInt", "Width")
        sw.default_value = 1024
        sh = self.inputs.new("NodeSocketInt", "Height")
        sh.default_value = 1024
        ss = self.inputs.new("NodeSocketFloat", "Strength")
        ss.default_value = 0.9
        sd = self.inputs.new("NodeSocketFloat", "Depth Scale")
        sd.default_value = 0.6

        self.inputs.new("GenTexImageSocket", "Init")
        self.inputs.new("GenTexImageSocket", "Mask")
        self.inputs.new("GenTexImageSocket", "Depth")
        # Multi-input socket: wire any number of reference images into one slot.
        self.inputs.new("GenTexImageSocket", "References", use_multi_input=True)

        self.outputs.new("GenTexImageSocket", "Image")

    def draw_buttons(self, context, layout):
        layout.prop(self, "provider")
        # Dropdown when the provider declares models, free-text otherwise.
        if _provider_models(self.provider):
            layout.prop(self, "model_enum")
        else:
            layout.prop(self, "model")

    def evaluate(self, ctx):
        if not has_provider(self.provider):
            raise RuntimeError(f"{self.name}: provider '{self.provider}' not registered")

        prefs = bpy.context.preferences.addons[ADDON_PKG].preferences
        settings = prefs.get_provider_settings(self.provider)

        prompt = str(upstream_value(self, "Prompt", ctx, default="") or "")
        if not prompt.strip():
            raise RuntimeError(f"{self.name}: empty prompt — wire a Text node into Prompt")

        negative = str(upstream_value(self, "Negative", ctx, default="") or "")
        width = int(upstream_value(self, "Width", ctx, default=1024) or 1024)
        height = int(upstream_value(self, "Height", ctx, default=1024) or 1024)
        strength = float(upstream_value(self, "Strength", ctx, default=0.9) or 0.9)
        depth_scale = float(upstream_value(self, "Depth Scale", ctx, default=0.6) or 0.6)

        init = upstream_value(self, "Init", ctx, default=None)
        mask = upstream_value(self, "Mask", ctx, default=None)
        depth = upstream_value(self, "Depth", ctx, default=None)
        refs = upstream_multi_input(self, "References", ctx)

        request = GenerateRequest(
            prompt=prompt,
            negative_prompt=negative,
            width=width, height=height,
            init_image=init if isinstance(init, (bytes, bytearray)) else None,
            mask_image=mask if isinstance(mask, (bytes, bytearray)) else None,
            depth_image=depth if isinstance(depth, (bytes, bytearray)) else None,
            depth_scale=depth_scale,
            strength=strength,
            reference_images=refs,
        )
        # Stash the per-node model override for providers that look for it.
        # gemini_direct uses request._model_override; other providers ignore it.
        # Providers with a model list use the dropdown; others the text field.
        if _provider_models(self.provider):
            chosen = self.model_enum  # "" = the leading "Default" item
        else:
            chosen = self.model.strip()
        if chosen:
            object.__setattr__(request, "_model_override", chosen)

        provider = get_provider(self.provider, settings)
        result = provider.generate(request)

        ctx.cache[self.cache_key(self.outputs["Image"])] = result.image_bytes
        ctx.last_result = result
