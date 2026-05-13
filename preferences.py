"""Addon preferences, built dynamically from each provider's declarations.

Each provider returns a list of `PreferenceField` from `preference_fields()`.
We translate those into Blender properties on a single `GenTexPreferences`
class. Field names are namespaced as `<provider_id>__<field_name>` so two
providers can declare the same logical field (e.g. `api_key`) without
colliding.

For convenience, code can call `prefs.get_provider_settings(provider_id)` to
get back the flat `{field_name: value}` dict that Provider classes expect.
"""

from __future__ import annotations

import bpy

from .providers import PROVIDERS, PreferenceField
# Import provider modules so they self-register into PROVIDERS before we
# build the AddonPreferences class.
from .providers import stability as _stability  # noqa: F401
from .providers import fal as _fal  # noqa: F401
from .providers import gemini_direct as _gemini_direct  # noqa: F401


# Full package name. Same string for legacy addons and extensions; used as
# the lookup key for `bpy.context.preferences.addons[...]`.
ADDON_PKG = __package__


# ---------------------------------------------------------------------------
# Field-to-property translation
# ---------------------------------------------------------------------------
#
# History: there used to be an `update=` callback on every preference property
# that immediately ran `bpy.ops.wm.save_userpref()`. It silently wiped saved
# API keys: during addon reload (hot-reload or disable/enable cycles), Blender
# re-declares properties at their `default=""` *before* restoring the persisted
# value, and any update-callback firing on that transient default would write
# an empty key over the real one. Removed — relies on Blender's
# "Auto-Save Preferences" (default-on) plus the explicit Save button below.

def _to_bpy_prop(field: PreferenceField):
    name = field.label
    desc = field.description
    if field.kind == "string":
        return bpy.props.StringProperty(name=name, description=desc,
                                         default=str(field.default or ""))
    if field.kind == "password":
        return bpy.props.StringProperty(name=name, description=desc,
                                         default="", subtype='PASSWORD')
    if field.kind == "enum":
        return bpy.props.EnumProperty(name=name, description=desc,
                                       items=field.items or [],
                                       default=field.default)
    if field.kind == "int":
        return bpy.props.IntProperty(name=name, description=desc,
                                      default=int(field.default or 0))
    if field.kind == "float":
        return bpy.props.FloatProperty(name=name, description=desc,
                                        default=float(field.default or 0.0))
    if field.kind == "bool":
        return bpy.props.BoolProperty(name=name, description=desc,
                                       default=bool(field.default))
    raise ValueError(f"Unknown preference field kind: {field.kind}")


def _attr_name(provider_id: str, field_name: str) -> str:
    return f"{provider_id}__{field_name}"


# ---------------------------------------------------------------------------
# Class body methods (assigned during type() construction)
# ---------------------------------------------------------------------------

def _draw(self, context):
    layout = self.layout
    layout.use_property_split = True
    layout.use_property_decorate = False

    for pid, pcls in PROVIDERS.items():
        fields = pcls.preference_fields()
        if not fields:
            continue
        box = layout.box()
        box.label(text=pcls.label or pid, icon='SETTINGS')
        for f in fields:
            box.prop(self, _attr_name(pid, f.name))

    row = layout.row()
    row.operator("wm.save_userpref", text="Save Preferences", icon='FILE_TICK')


def _get_provider_settings(self, provider_id: str) -> dict:
    """Return a flat {field_name: value} dict for the given provider."""
    if provider_id not in PROVIDERS:
        return {}
    out = {}
    for f in PROVIDERS[provider_id].preference_fields():
        out[f.name] = getattr(self, _attr_name(provider_id, f.name), f.default)
    return out


def _get_api_key(self, provider_id: str) -> str:
    """Convenience accessor; returns "" if the provider has no api_key field."""
    return getattr(self, _attr_name(provider_id, "api_key"), "")


# ---------------------------------------------------------------------------
# Build the class dynamically
# ---------------------------------------------------------------------------

def _build_preferences_class():
    # Provider selection is now per-Generate-node (see node_tree/nodes/generate.py),
    # so the addon-prefs no longer needs a global provider enum.
    annotations: dict = {}

    for pid, pcls in PROVIDERS.items():
        for f in pcls.preference_fields():
            annotations[_attr_name(pid, f.name)] = _to_bpy_prop(f)

    return type(
        "GenTexPreferences",
        (bpy.types.AddonPreferences,),
        {
            "bl_idname": ADDON_PKG,
            "__annotations__": annotations,
            "draw": _draw,
            "get_provider_settings": _get_provider_settings,
            "get_api_key": _get_api_key,
        },
    )


# Build at import time. Providers must be registered before this module is
# imported (which is the case: __init__.py imports the provider modules
# before this preferences module).
GenTexPreferences = _build_preferences_class()
