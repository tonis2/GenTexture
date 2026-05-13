"""TCP command handlers — the addon-side API surface.

Each handler takes a `params` dict and returns a `result` dict. The wire
protocol is one JSON request per connection:

    request:  {"type": <command>, "params": {...}}
    response: {"status": "ok", "result": {...}}
              {"status": "error", "message": "..."}

bpy access is wrapped in `run_on_main` so it executes on Blender's main
thread. Provider `.generate()` is called directly on the worker thread —
it only hits subprocess HTTP, no bpy.
"""

from __future__ import annotations

import base64
import re
import threading

import bpy

from ..providers import (
    PROVIDERS,
    GenerateRequest,
    ProviderError,
)
from ..utils.image import (
    bpy_to_np,
    load_image_bytes,
    np_to_bpy,
    np_to_png_bytes,
)
from . import main_thread


# Per-provider generation lock. The fal/local_server providers write a
# status JSON to a fixed tempfile path during a job; two concurrent calls
# to the same provider would race on that file. Different providers run
# in parallel.
_provider_locks: dict[str, threading.Lock] = {}
_provider_locks_lock = threading.Lock()


def _lock_for(provider_id: str) -> threading.Lock:
    with _provider_locks_lock:
        lock = _provider_locks.get(provider_id)
        if lock is None:
            lock = threading.Lock()
            _provider_locks[provider_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Helpers (always run on main thread)
# ---------------------------------------------------------------------------

# `__package__` for this module is e.g. "GenTexture.mcp_server"; the addon
# root is one level up.
ADDON_PKG = __package__.rsplit(".", 1)[0]


def _get_settings(provider_id: str) -> dict:
    prefs = bpy.context.preferences.addons[ADDON_PKG].preferences
    return prefs.get_provider_settings(provider_id)


def _store_image(png_bytes: bytes, name: str, pack: bool = True) -> str:
    """Decode PNG bytes into bpy.data.images[name]; return the actual name."""
    arr = load_image_bytes(png_bytes, name=name)
    existing = bpy.data.images.get(name)
    img = np_to_bpy(arr, name, existing=existing, pack=pack)
    return img.name


def _resolve_image_arg(arg) -> bytes | None:
    """Image argument may be a bpy.data.images name OR base64-encoded PNG.

    Returns PNG bytes, or None if `arg` is falsy.
    """
    if not arg:
        return None
    if isinstance(arg, (bytes, bytearray)):
        return bytes(arg)
    if not isinstance(arg, str):
        raise ValueError(f"Image argument must be string, got {type(arg).__name__}")
    # Heuristic: if it parses as a valid bpy image name, use that; otherwise
    # assume base64. Names containing characters outside base64's alphabet
    # are an unambiguous signal it's a name.
    if arg in bpy.data.images:
        return _image_to_png(bpy.data.images[arg])
    # No such datablock — try base64.
    try:
        return base64.b64decode(arg, validate=True)
    except Exception as e:
        raise ValueError(
            f"Image '{arg[:40]}...' is neither a bpy.data.images name nor valid base64 PNG"
        ) from e


def _image_to_png(img: bpy.types.Image, max_size: int | None = None) -> bytes:
    arr = bpy_to_np(img)
    if max_size is not None and max_size > 0:
        h, w = arr.shape[:2]
        scale = max_size / max(w, h)
        if scale < 1.0:
            # Cheap nearest-ish via array striding to avoid pulling in scipy.
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            xs = (
                (i * w // new_w) for i in range(new_w)
            )
            import numpy as np
            xs_arr = np.fromiter(xs, dtype=np.int32, count=new_w)
            ys_arr = np.fromiter(((i * h // new_h) for i in range(new_h)),
                                 dtype=np.int32, count=new_h)
            arr = arr[ys_arr[:, None], xs_arr[None, :]]
    return np_to_png_bytes(arr)


_NAME_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _auto_name(provider_id: str, prompt: str) -> str:
    slug = _NAME_SLUG_RE.sub("_", prompt.strip())[:48].strip("_") or "image"
    return f"{provider_id}_{slug}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(params: dict) -> dict:
    """Cheap health check; doesn't touch bpy."""
    return {"ok": True, "providers": sorted(PROVIDERS.keys())}


def cmd_list_providers(params: dict) -> dict:
    def _read():
        out = []
        for pid, pcls in PROVIDERS.items():
            settings = _get_settings(pid)
            api_key_present = bool(settings.get("api_key"))
            out.append({
                "id": pid,
                "label": pcls.label or pid,
                "capabilities": sorted(pcls.capabilities()),
                "api_key_configured": api_key_present,
            })
        return out
    providers = main_thread.run_on_main(_read)
    return {"providers": providers}


def _build_request(params: dict) -> GenerateRequest:
    return GenerateRequest(
        prompt=params.get("prompt", ""),
        negative_prompt=params.get("negative_prompt", ""),
        width=int(params.get("width", 1024)),
        height=int(params.get("height", 1024)),
        init_image=_resolve_image_arg(params.get("init_image")),
        mask_image=_resolve_image_arg(params.get("mask_image")),
        depth_image=_resolve_image_arg(params.get("depth_image")),
        reference_images=[
            _resolve_image_arg(r)
            for r in (params.get("reference_images") or [])
            if r
        ],
        strength=float(params.get("strength", 0.75)),
        depth_scale=float(params.get("depth_scale", 0.6)),
        seed=int(params["seed"]) if params.get("seed") is not None else None,
    )


def _run_generation(params: dict, default_op: str) -> dict:
    provider_id = params.get("provider")
    if not provider_id:
        raise ValueError("'provider' is required")
    if provider_id not in PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider_id}'. Available: {sorted(PROVIDERS.keys())}"
        )

    # bpy work (read settings, resolve images by name) on the main thread
    settings, req = main_thread.run_on_main(
        lambda: (_get_settings(provider_id), _build_request(params))
    )
    inst = PROVIDERS[provider_id](settings)

    # Provider call: blocking, runs subprocess HTTP. Serialize per-provider
    # because some providers write a fixed tempfile for status polling.
    with _lock_for(provider_id):
        try:
            result = inst.generate(req)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"{provider_id}: {e}") from e

    # Store result in bpy.data.images (main thread again)
    requested_name = params.get("save_as") or _auto_name(provider_id, req.prompt)
    stored_name = main_thread.run_on_main(_store_image, result.image_bytes, requested_name)

    return {
        "image_name": stored_name,
        "seed": result.seed,
        "image_base64": base64.b64encode(result.image_bytes).decode("ascii"),
        "operation": default_op,
    }


def cmd_text2img(params: dict) -> dict:
    # Clear init/mask so request dispatches to text2img even if caller sent
    # spurious empty strings.
    params = {**params, "init_image": None, "mask_image": None}
    return _run_generation(params, "text2img")


def cmd_img2img(params: dict) -> dict:
    if not params.get("init_image"):
        raise ValueError("img2img requires 'init_image'")
    params = {**params, "mask_image": None}
    return _run_generation(params, "img2img")


def cmd_inpaint(params: dict) -> dict:
    if not params.get("init_image") or not params.get("mask_image"):
        raise ValueError("inpaint requires both 'init_image' and 'mask_image'")
    return _run_generation(params, "inpaint")


def cmd_list_images(params: dict) -> dict:
    name_filter = (params.get("filter") or "").lower()

    def _read():
        out = []
        for img in bpy.data.images:
            if name_filter and name_filter not in img.name.lower():
                continue
            w, h = (img.size[0], img.size[1]) if img.size else (0, 0)
            out.append({
                "name": img.name,
                "width": w,
                "height": h,
                "packed": bool(img.packed_file),
                "filepath": img.filepath,
            })
        return out
    return {"images": main_thread.run_on_main(_read)}


def cmd_get_image(params: dict) -> dict:
    name = params.get("name")
    if not name:
        raise ValueError("'name' is required")
    max_size = params.get("max_size")
    max_size = int(max_size) if max_size else None

    def _read():
        img = bpy.data.images.get(name)
        if img is None:
            raise ValueError(f"No bpy.data.images['{name}']")
        png = _image_to_png(img, max_size=max_size)
        return {
            "image_base64": base64.b64encode(png).decode("ascii"),
            "width": img.size[0],
            "height": img.size[1],
        }
    return main_thread.run_on_main(_read)


def cmd_save_image_to_file(params: dict) -> dict:
    name = params.get("name")
    path = params.get("path")
    if not name or not path:
        raise ValueError("'name' and 'path' are required")

    def _save():
        img = bpy.data.images.get(name)
        if img is None:
            raise ValueError(f"No bpy.data.images['{name}']")
        png = _image_to_png(img)
        with open(path, "wb") as f:
            n = f.write(png)
        return {"path": path, "bytes_written": n}
    return main_thread.run_on_main(_save)


def cmd_import_image_file(params: dict) -> dict:
    path = params.get("path")
    save_as = params.get("save_as")
    if not path:
        raise ValueError("'path' is required")

    def _load():
        img = bpy.data.images.load(path, check_existing=False)
        if save_as:
            img.name = save_as
        try:
            img.pack()
        except RuntimeError:
            # Some formats / states can't pack; that's fine.
            pass
        return {"image_name": img.name, "width": img.size[0], "height": img.size[1]}
    return main_thread.run_on_main(_load)


def cmd_bake_layers(params: dict) -> dict:
    object_name = params.get("object_name")
    width = int(params.get("width", 2048))
    height = int(params.get("height", 2048))

    def _bake():
        if object_name:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise ValueError(f"No object '{object_name}'")
            # Make it active so the operator's poll picks it up.
            for vl in bpy.context.scene.view_layers:
                if obj.name in vl.objects:
                    vl.objects.active = obj
                    break
        ret = bpy.ops.gentex.bake_layers('EXEC_DEFAULT', width=width, height=height)
        if 'FINISHED' not in ret:
            raise RuntimeError(f"bake_layers operator returned {ret}")
        active = bpy.context.active_object
        baked = getattr(active, "gentex_baked_image", None) if active else None
        result = {"object_name": active.name if active else None}
        if baked is not None:
            result["baked_image_name"] = baked.name
            png = _image_to_png(baked)
            result["image_base64"] = base64.b64encode(png).decode("ascii")
        return result
    return main_thread.run_on_main(_bake, timeout=300.0)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

COMMANDS = {
    "status": cmd_status,
    "list_providers": cmd_list_providers,
    "text2img": cmd_text2img,
    "img2img": cmd_img2img,
    "inpaint": cmd_inpaint,
    "list_images": cmd_list_images,
    "get_image": cmd_get_image,
    "save_image_to_file": cmd_save_image_to_file,
    "import_image_file": cmd_import_image_file,
    "bake_layers": cmd_bake_layers,
}

# Pipeline commands live in their own module to keep this file focused on
# single-shot generation; merge them into the registry here.
from .pipeline import PIPELINE_COMMANDS  # noqa: E402
COMMANDS.update(PIPELINE_COMMANDS)
