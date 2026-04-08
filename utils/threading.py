import threading
from typing import Callable, Any

import bpy


class AsyncTask:
    """Runs a function in a background thread with main-thread callbacks."""

    def __init__(self):
        self._cancelled = False
        self._thread: threading.Thread | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self):
        self._cancelled = True


def _tag_redraw():
    """Force UI panels to redraw so progress is visible."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('VIEW_3D', 'IMAGE_EDITOR'):
                    for region in area.regions:
                        if region.type == 'UI':
                            region.tag_redraw()
    except Exception:
        pass


def run_async(
    task_fn: Callable[[], Any],
    on_complete: Callable[[Any], None],
    on_error: Callable[[Exception], None],
    task: AsyncTask | None = None,
) -> AsyncTask:
    """Run task_fn in a background thread, dispatch callbacks on the main thread.

    Returns an AsyncTask that can be used to cancel the operation.
    """
    if task is None:
        task = AsyncTask()

    result_holder: dict = {}

    def worker():
        try:
            result = task_fn()
            result_holder["result"] = result
        except Exception as e:
            result_holder["error"] = e

    thread = threading.Thread(target=worker, daemon=True)
    task._thread = thread
    thread.start()

    def poll():
        # Update UI with provider status if available
        try:
            from ..providers.fal import get_status
            status = get_status()
            if status:
                bpy.context.scene.gentex_info = status
        except Exception:
            pass

        _tag_redraw()

        if thread.is_alive():
            return 0.5  # check again in 500ms
        if task.is_cancelled:
            return None
        if "error" in result_holder:
            on_error(result_holder["error"])
        elif "result" in result_holder:
            on_complete(result_holder["result"])
        return None  # stop timer

    bpy.app.timers.register(poll, first_interval=0.1)
    return task
