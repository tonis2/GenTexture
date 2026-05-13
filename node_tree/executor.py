"""Pipeline executor — topo-sort terminal nodes, run each one sequentially.

Run flow:
  1. Resolve the viewport context once (3D area/region/space + edit-mode meshes).
  2. Walk every terminal node (Output Image, Project Layer) backward through
     input links to build a deduplicated execution order.
  3. For each node in order:
       - if `node.runs_async` is True (Generate), dispatch its evaluate() to
         a worker thread via `utils.threading.run_async`;
       - otherwise run it inline on the main thread, deferred by a brief
         timer so the just-set status message gets a chance to paint.
     The next node only starts after the current one returns/completes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import bpy

from ..utils.threading import AsyncTask, run_async


TERMINAL_IDNAMES = {"GenTexNodeOutputImage", "GenTexNodeProjectLayer"}


@dataclass
class RunContext:
    tree: bpy.types.NodeTree
    cache: dict = field(default_factory=dict)
    # Populated by Viewport Capture:
    captured_per_obj: list = field(default_factory=list)
    captured_visible: object = None
    captured_mask: object = None
    captured_depth: object = None
    captured_size: tuple = (0, 0)
    last_result: object = None
    # Viewport context resolved at run start:
    area: object = None
    region: object = None
    space_3d: object = None
    edit_objs: list = field(default_factory=list)
    info_setter: Callable[[str], None] | None = None


def _get_view3d(window_manager):
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
                        return area, region, space
    return None, None, None


def topo_order(tree) -> list[bpy.types.Node]:
    """Return all nodes upstream of any terminal node, in execution order."""
    terminals = [n for n in tree.nodes if n.bl_idname in TERMINAL_IDNAMES]
    visited: set[str] = set()
    order: list[bpy.types.Node] = []

    def visit(node):
        if node.name in visited:
            return
        for inp in node.inputs:
            for link in inp.links:
                visit(link.from_node)
        visited.add(node.name)
        order.append(node)

    for t in terminals:
        visit(t)
    return order


def make_context(window_manager) -> RunContext:
    ctx = RunContext(tree=None)
    edit_objs = []
    if window_manager:
        # Use the actual context to gather edit-mode meshes.
        try:
            for o in bpy.context.objects_in_mode:
                if o.type == 'MESH' and o.data.is_editmode:
                    edit_objs.append(o)
        except Exception:
            pass
    area, region, space = _get_view3d(window_manager)
    ctx.area = area
    ctx.region = region
    ctx.space_3d = space
    ctx.edit_objs = edit_objs
    return ctx


# Module-level handle so the cancel operator can flip the flag on the active run.
_active_task: AsyncTask | None = None


def is_running() -> bool:
    return _active_task is not None


def cancel():
    global _active_task
    if _active_task is not None:
        _active_task.cancel()


def run(tree, info_setter: Callable[[str], None], on_finish: Callable[[], None]):
    """Kick off sequential execution of the tree. Returns immediately."""
    global _active_task

    ctx = make_context(bpy.context.window_manager)
    ctx.tree = tree
    ctx.info_setter = info_setter

    order = topo_order(tree)
    if not order:
        info_setter("Error: no nodes to run (need at least one Output Image or Project Layer)")
        on_finish()
        return

    total = len(order)

    def step(i: int):
        global _active_task
        if i >= total:
            info_setter("")
            _active_task = None
            on_finish()
            return

        node = order[i]
        info_setter(f"[{i + 1}/{total}] {node.bl_label}: {node.name}...")

        if not getattr(node, "runs_async", False):
            # Defer the evaluate via a timer so the status message just set
            # by info_setter() actually paints before the (potentially heavy)
            # main-thread work begins — otherwise the UI looks frozen showing
            # the previous node's status until evaluate() returns.
            def _run_sync():
                global _active_task
                try:
                    node.evaluate(ctx)
                except Exception as err:
                    info_setter(f"Error in {node.name}: {err}")
                    import traceback
                    traceback.print_exc()
                    _active_task = None
                    on_finish()
                    return None
                if _active_task and _active_task.is_cancelled:
                    info_setter("Cancelled")
                    _active_task = None
                    on_finish()
                    return None
                step(i + 1)
                return None
            # Tag a redraw so the new status message paints right now.
            for w in bpy.context.window_manager.windows:
                for area in w.screen.areas:
                    area.tag_redraw()
            bpy.app.timers.register(_run_sync, first_interval=0.05)
            return

        def task():
            node.evaluate(ctx)
            return None

        def on_complete(_result):
            if _active_task and _active_task.is_cancelled:
                info_setter("Cancelled")
                on_finish()
                return
            step(i + 1)

        def on_error(err):
            global _active_task
            info_setter(f"Error in {node.name}: {err}")
            import traceback
            traceback.print_exc()
            _active_task = None
            on_finish()

        _active_task = run_async(task, on_complete, on_error)

    step(0)
