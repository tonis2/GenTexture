"""run_pipeline / list_pipelines / describe_pipeline_schema — JSON wrapper
around node_tree/executor.py.

These commands let an external agent author and execute a GenTexture
pipeline as a single tool call instead of stringing together text2img +
img2img + project + bake. The executor itself is reused unmodified; we
only add a thin JSON ↔ live `GenTexPipelineNodeTree` builder and a
run-and-wait wrapper that converts the executor's callback-style
on_finish into a blocking command.
"""

from __future__ import annotations

import base64
import threading
import uuid

import bpy

from ..node_tree import executor
from ..node_tree.tree import TREE_IDNAME
from . import main_thread
from .commands import _image_to_png, _resolve_image_arg, _store_image


_pipeline_lock = threading.Lock()


_NODE_TYPES = (
    "GenTexNodeText",
    "GenTexNodeReferenceImage",
    "GenTexNodeViewportCapture",
    "GenTexNodeGenerate",
    "GenTexNodeProjectLayer",
    "GenTexNodeOutputImage",
)

_TERMINAL_IDNAMES = {"GenTexNodeOutputImage", "GenTexNodeProjectLayer"}


# ---------------------------------------------------------------------------
# Build (JSON → live tree). Main thread only.
# ---------------------------------------------------------------------------

def _resolve_socket(sockets, key):
    if isinstance(key, int):
        if key < 0 or key >= len(sockets):
            raise ValueError(
                f"Socket index {key} out of range (0..{len(sockets) - 1})"
            )
        return sockets[key]
    for s in sockets:
        if s.name == key or s.identifier == key:
            return s
    raise ValueError(
        f"No socket named '{key}'; available: {[s.name for s in sockets]}"
    )


def _set_node_prop(node, key, value):
    # Special case: Reference Image's `image` is a PointerProperty to
    # bpy.types.Image. Accept either an existing datablock name or a base64
    # PNG; in the latter case load it into bpy.data.images first.
    if key == "image" and node.bl_idname == "GenTexNodeReferenceImage":
        if isinstance(value, str) and value in bpy.data.images:
            node.image = bpy.data.images[value]
            return
        png = _resolve_image_arg(value)
        if png is None:
            raise ValueError(
                "GenTexNodeReferenceImage 'image' is neither a datablock name "
                "nor valid base64 PNG"
            )
        img_name = _store_image(png, f"__mcp_ref_{uuid.uuid4().hex[:8]}")
        node.image = bpy.data.images[img_name]
        return
    setattr(node, key, value)


def _clear_nodes(tree):
    while tree.nodes:
        tree.nodes.remove(tree.nodes[0])


def _build_tree_from_json(graph: dict):
    """Returns (tree, created_flag, layer_baseline). Main thread only."""
    name = graph.get("name") or f"__mcp_{uuid.uuid4().hex[:8]}"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.bl_idname != TREE_IDNAME:
            raise ValueError(
                f"Node group '{name}' exists but is not a {TREE_IDNAME}"
            )
        tree = existing
        created = False
        _clear_nodes(tree)
    else:
        tree = bpy.data.node_groups.new(name=name, type=TREE_IDNAME)
        created = True

    id_to_node = {}
    for spec in graph.get("nodes") or []:
        nid = spec.get("id")
        ntype = spec.get("type")
        if not nid or not ntype:
            raise ValueError(f"Node spec missing id/type: {spec!r}")
        if ntype not in _NODE_TYPES:
            raise ValueError(
                f"Unknown node type '{ntype}'. Available: {list(_NODE_TYPES)}"
            )
        node = tree.nodes.new(type=ntype)
        # Setting .name may collide (Blender will mangle), so honour whatever
        # name the datablock ends up with and accept both keys downstream.
        try:
            node.name = nid
        except Exception:
            pass
        id_to_node[nid] = node
        id_to_node[node.name] = node

        for k, v in (spec.get("props") or {}).items():
            try:
                _set_node_prop(node, k, v)
            except Exception as e:
                raise ValueError(f"Node '{nid}'.{k}: {e}") from e

    for link in graph.get("links") or []:
        try:
            src_id, src_sock = link["from"]
            dst_id, dst_sock = link["to"]
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Bad link spec {link!r}: {e}") from e
        if src_id not in id_to_node:
            raise ValueError(f"Link references unknown node id '{src_id}'")
        if dst_id not in id_to_node:
            raise ValueError(f"Link references unknown node id '{dst_id}'")
        src = _resolve_socket(id_to_node[src_id].outputs, src_sock)
        dst = _resolve_socket(id_to_node[dst_id].inputs, dst_sock)
        tree.links.new(src, dst)

    # Snapshot per-object layer counts so we can diff after the run and
    # report which objects gained projected layers.
    baseline = {}
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and getattr(obj, "gentex_layers", None) is not None:
            baseline[obj.name] = len(obj.gentex_layers)

    return tree, created, baseline


# ---------------------------------------------------------------------------
# Harvest. Main thread only.
# ---------------------------------------------------------------------------

def _harvest_outputs(tree, layer_baseline: dict) -> dict:
    outputs = []
    project_node_names = []

    for node in tree.nodes:
        if node.bl_idname == "GenTexNodeOutputImage":
            img_name = node.output_name
            img = bpy.data.images.get(img_name)
            entry = {"node": node.name, "kind": "image", "image_name": img_name}
            if img is not None:
                png = _image_to_png(img)
                entry["image_base64"] = base64.b64encode(png).decode("ascii")
                entry["width"] = img.size[0]
                entry["height"] = img.size[1]
            outputs.append(entry)
        elif node.bl_idname == "GenTexNodeProjectLayer":
            project_node_names.append(node.name)

    # For ProjectLayer terminals, diff layer counts against the pre-run
    # baseline. Report each object that gained layers; if there are multiple
    # ProjectLayer nodes we attribute all new layers to the first one (the
    # executor doesn't expose per-node mutations).
    if project_node_names:
        first = project_node_names[0]
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue
            layers = getattr(obj, "gentex_layers", None)
            if layers is None:
                continue
            now = len(layers)
            before = layer_baseline.get(obj.name, 0)
            if now <= before:
                continue
            new_layers = [
                {
                    "index": i,
                    "name": layers[i].name,
                    "image": layers[i].image.name if layers[i].image else None,
                }
                for i in range(before, now)
            ]
            outputs.append({
                "node": first,
                "kind": "layer",
                "object": obj.name,
                "new_layers": new_layers,
            })

    return {"outputs": outputs}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run_pipeline(params: dict) -> dict:
    graph = params.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("'graph' (object) is required")
    timeout = float(params.get("timeout", 600.0))

    tree, created, baseline = main_thread.run_on_main(_build_tree_from_json, graph)

    done = threading.Event()
    info_buf: list[str] = []
    err_holder: dict = {}

    def info_setter(msg: str):
        info_buf.append(msg)
        if isinstance(msg, str) and msg.startswith("Error"):
            err_holder.setdefault("msg", msg)

    def on_finish():
        done.set()

    try:
        with _pipeline_lock:
            if main_thread.run_on_main(executor.is_running):
                raise RuntimeError("Another pipeline is already running")
            main_thread.run_on_main(executor.run, tree, info_setter, on_finish)
            if not done.wait(timeout):
                main_thread.run_on_main(executor.cancel)
                raise TimeoutError(f"Pipeline exceeded {timeout}s")

        if "msg" in err_holder:
            raise RuntimeError(err_holder["msg"])

        result = main_thread.run_on_main(_harvest_outputs, tree, baseline)
        result["info_log"] = info_buf
        result["tree_name"] = tree.name
        result["tree_kept"] = bool(graph.get("keep_tree") or not created)
        return result
    finally:
        if created and not graph.get("keep_tree", False):
            def _remove():
                try:
                    bpy.data.node_groups.remove(tree)
                except (ReferenceError, RuntimeError):
                    pass
            try:
                main_thread.run_on_main(_remove)
            except Exception:
                pass


def cmd_list_pipelines(params: dict) -> dict:
    def _read():
        out = []
        for ng in bpy.data.node_groups:
            if ng.bl_idname != TREE_IDNAME:
                continue
            terminals = [n.name for n in ng.nodes if n.bl_idname in _TERMINAL_IDNAMES]
            out.append({
                "name": ng.name,
                "node_count": len(ng.nodes),
                "terminals": terminals,
            })
        return out
    return {"pipelines": main_thread.run_on_main(_read)}


def _introspect_props(cls) -> list[dict]:
    out = []
    anns = getattr(cls, "__annotations__", {}) or {}
    for prop_name, descriptor in anns.items():
        info: dict = {"name": prop_name}
        # bpy.props descriptors expose .function (the property constructor)
        # and .keywords (the kwargs the user passed). On modern Blender the
        # descriptor is a `_PropertyDeferred`.
        fn = getattr(descriptor, "function", None)
        if fn is not None:
            info["type"] = getattr(fn, "__name__", "Unknown")
        kw = getattr(descriptor, "keywords", None)
        if isinstance(kw, dict):
            for k in ("name", "description", "default", "min", "max"):
                if k in kw and isinstance(kw[k], (int, float, str, bool, type(None))):
                    info[k] = kw[k]
            items = kw.get("items")
            if callable(items):
                try:
                    info["enum_items"] = [t[0] for t in items(None, None)]
                except Exception:
                    pass
            elif isinstance(items, (list, tuple)):
                info["enum_items"] = [t[0] for t in items if t]
        out.append(info)
    return out


def _introspect_sockets():
    """Build a transient tree, add one of each node, read socket lists."""
    tree = bpy.data.node_groups.new(
        name=f"__mcp_introspect_{uuid.uuid4().hex[:8]}", type=TREE_IDNAME
    )
    try:
        out = {}
        for idname in _NODE_TYPES:
            try:
                n = tree.nodes.new(type=idname)
            except Exception:
                continue
            out[idname] = {
                "inputs": [
                    {"name": s.name, "id": s.identifier, "type": s.bl_idname,
                     "multi": getattr(s, "is_multi_input", False)}
                    for s in n.inputs
                ],
                "outputs": [
                    {"name": s.name, "id": s.identifier, "type": s.bl_idname}
                    for s in n.outputs
                ],
            }
        return out
    finally:
        try:
            bpy.data.node_groups.remove(tree)
        except (ReferenceError, RuntimeError):
            pass


def cmd_describe_pipeline_schema(params: dict) -> dict:
    def _read():
        sockets = _introspect_sockets()
        out = []
        for idname in _NODE_TYPES:
            cls = getattr(bpy.types, idname, None)
            if cls is None:
                continue
            out.append({
                "type": idname,
                "label": getattr(cls, "bl_label", idname),
                "runs_async": bool(getattr(cls, "runs_async", False)),
                "terminal": idname in _TERMINAL_IDNAMES,
                "props": _introspect_props(cls),
                "sockets": sockets.get(idname, {"inputs": [], "outputs": []}),
            })
        return out
    return {"nodes": main_thread.run_on_main(_read)}


PIPELINE_COMMANDS = {
    "run_pipeline": cmd_run_pipeline,
    "list_pipelines": cmd_list_pipelines,
    "describe_pipeline_schema": cmd_describe_pipeline_schema,
}
