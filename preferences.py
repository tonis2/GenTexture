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
# Notes on persistence:
#   - Two earlier bugs are recorded here so we don't reintroduce them.
#   1) An `update=` callback that ran `wm.save_userpref()` on every change
#      silently wiped API keys: during addon (re)registration, properties
#      are briefly held at their `default=""` before Blender restores the
#      persisted value, and the callback firing on that transient default
#      would overwrite the on-disk value with empty. Don't auto-save in an
#      update callback.
#   2) Blender deliberately does NOT serialize `subtype='PASSWORD'` string
#      properties to userpref.blend — they're session-only secrets, so API
#      keys would disappear every restart even after a manual save. Hence
#      api_key uses plain StringProperty (no PASSWORD subtype). Visible in
#      the prefs panel, but actually persists.

def _to_bpy_prop(field: PreferenceField):
    name = field.label
    desc = field.description
    if field.kind == "string":
        return bpy.props.StringProperty(name=name, description=desc,
                                         default=str(field.default or ""))
    if field.kind == "password":
        # See note above: PASSWORD subtype skips userpref serialization, so
        # we use a plain StringProperty. The field still functions as a key
        # store — it's just not masked in the UI.
        return bpy.props.StringProperty(name=name, description=desc,
                                         default="")
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

    # MCP server section
    from . import mcp_server
    box = layout.box()
    box.label(text="MCP Server", icon='NETWORK_DRIVE')
    box.prop(self, "mcp_enabled")
    row = box.row()
    row.prop(self, "mcp_host")
    row.prop(self, "mcp_port")
    status_row = box.row()
    if mcp_server.is_running():
        host, port = mcp_server.get_address()
        status_row.label(text=f"Running on {host}:{port}", icon='CHECKMARK')
        status_row.operator("gentex.mcp_stop", text="Stop", icon='PAUSE')
    else:
        err = mcp_server.get_last_error()
        if err:
            status_row.label(text=err, icon='ERROR')
        else:
            status_row.label(text="Stopped", icon='RADIOBUT_OFF')
        status_row.operator("gentex.mcp_start", text="Start", icon='PLAY')

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
    annotations: dict = {
        # MCP server (TCP JSON command dispatcher hosted inside Blender).
        # Off by default — flip it on, hit Start, and an external Claude Code /
        # OpenCode session can drive the generation APIs through a stdio
        # bridge script (see mcp/gentex-mcp-server.py).
        "mcp_enabled": bpy.props.BoolProperty(
            name="Enable MCP Server",
            description=(
                "Run a TCP command server inside Blender so external AI "
                "agents (Claude Code, OpenCode, ...) can call GenTexture's "
                "image-generation APIs via MCP"
            ),
            default=False,
        ),
        "mcp_host": bpy.props.StringProperty(
            name="Host",
            description="Bind address. Keep at 127.0.0.1 unless you know what you're doing",
            default="127.0.0.1",
        ),
        "mcp_port": bpy.props.IntProperty(
            name="Port",
            description="TCP port the in-Blender MCP server listens on",
            default=9877, min=1024, max=65535,
        ),
    }

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
