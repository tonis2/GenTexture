"""Main-thread bridge for bpy calls made from server worker threads.

Blender's data API is not thread-safe; everything touching `bpy.*` must
run on the main thread. The server runs each TCP connection on a worker
thread, so we marshal bpy work back to the main thread via a queue that
`bpy.app.timers` drains.
"""

import queue
import threading

import bpy


_q: "queue.Queue[tuple]" = queue.Queue()
_drain_registered = False
_drain_lock = threading.Lock()


def _drain():
    while True:
        try:
            fn, args, kwargs, box, done = _q.get_nowait()
        except queue.Empty:
            return 0.05  # 50 ms — next poll
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:
            box["error"] = e
        finally:
            done.set()


def _ensure_drain():
    global _drain_registered
    with _drain_lock:
        if _drain_registered:
            return
        bpy.app.timers.register(_drain, first_interval=0.0)
        _drain_registered = True


def run_on_main(fn, *args, timeout: float = 120.0, **kwargs):
    """Call `fn(*args, **kwargs)` on Blender's main thread; block until done.

    Use for any operation that touches bpy. Provider `.generate()` calls
    do NOT need this — they only hit subprocess HTTP and the filesystem.
    """
    _ensure_drain()
    box: dict = {}
    done = threading.Event()
    _q.put((fn, args, kwargs, box, done))
    if not done.wait(timeout):
        raise TimeoutError(f"main-thread call exceeded {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]
