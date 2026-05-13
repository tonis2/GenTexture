"""Shared helpers for pipeline nodes."""

import bpy

from ..tree import TREE_IDNAME


class GenTexPipelineNodeBase:
    """Mixin that hides nodes from non-pipeline trees and provides cache helpers.

    Nodes still subclass bpy.types.Node directly (Blender's metaclass requires
    that), but inherit this mixin for the poll + helpers.
    """

    # Default: evaluate() runs on Blender's main thread inline. Set True on
    # nodes whose evaluate() does slow non-Blender work (HTTP calls etc.) —
    # those get dispatched to a worker thread via utils.threading.run_async.
    # Anything touching bpy.data, bpy.context.active_object, or bpy.ops MUST
    # stay on the main thread; only true side-channel work belongs in async.
    runs_async: bool = False

    @classmethod
    def poll(cls, ntree):
        return getattr(ntree, "bl_idname", "") == TREE_IDNAME

    def cache_key(self, socket: bpy.types.NodeSocket) -> str:
        """Globally unique key for one output socket of this node.

        Uses Blender's persistent socket identifier so renaming or rewiring
        doesn't break the cache mid-run.
        """
        return f"{self.name}::{socket.identifier}"


def upstream_value(node: bpy.types.Node, input_name: str, ctx, *, default=None):
    """Read the value of an input socket: either follow its link into the
    upstream cache, or fall back to the socket's `default_value` for scalar
    socket types. For image sockets returns the cached PNG bytes (or default).
    """
    sock = node.inputs.get(input_name)
    if sock is None:
        return default
    if sock.is_linked:
        link = sock.links[0]
        upstream_node = link.from_node
        upstream_sock = link.from_socket
        key = f"{upstream_node.name}::{upstream_sock.identifier}"
        return ctx.cache.get(key, default)
    if hasattr(sock, "default_value"):
        return sock.default_value
    return default


def upstream_multi_input(node: bpy.types.Node, input_name: str, ctx) -> list[bytes]:
    """Collect cached bytes from every link on a multi-input socket.

    The Generate node's "References" socket is created with use_multi_input=True
    so the user can wire any number of upstream images into one slot. Returns
    the PNG bytes in connection order.
    """
    sock = node.inputs.get(input_name)
    if sock is None or not sock.is_linked:
        return []
    out = []
    for link in sock.links:
        key = f"{link.from_node.name}::{link.from_socket.identifier}"
        data = ctx.cache.get(key)
        if data is not None:
            out.append(data)
    return out
