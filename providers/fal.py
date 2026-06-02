"""fal.ai provider.

A single provider (`id = "fal"`) that exposes fal's image models through one
model dropdown on the Generate node. Picking a model selects both its
capabilities and how the request is built/routed:

  * nano_banana_2  - Google Nano Banana 2 (Gemini 3.1 Flash Image).
                     Reasoning-guided edits; no native mask channel.
  * flux           - Black Forest Labs FLUX (schnell / dev / pro-fill).
                     text2img, img2img, inpaint via separate endpoints.
  * flux_general   - fal's `flux-general` pipeline. Inpaint + IP-Adapter
                     style references + depth ControlNet in one call.

All endpoints follow the same async-queue protocol:
  POST /<model>            -> { request_id, status_url, response_url }
  GET  status_url          -> poll until status == "COMPLETED"
  GET  response_url        -> { images: [{url}], seed }
  GET  images[0].url       -> PNG bytes
"""

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass
from typing import Callable

from .api import (
    Provider, GenerateRequest, GenerateResult, PreferenceField,
    AuthenticationError, ProviderError,
    register_provider,
    CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT,
    CAP_REFERENCE_IMAGES, CAP_DEPTH_CONTROL,
)
from ._http import run_subprocess


# Path to a temp file where the subprocess writes status updates,
# polled by the UI timer for progress display.
_STATUS_FILE = os.path.join(tempfile.gettempdir(), "gentex_fal_status")


_WORKER_SCRIPT = r'''
import json, sys, time, urllib.request, urllib.error, http.client, ssl
from urllib.parse import urlparse

config = json.loads(sys.stdin.read())
status_path = config["status_path"]

def status(msg):
    try:
        with open(status_path, "w") as f:
            f.write(msg)
    except: pass

api_key = config["api_key"]
model = config["model"]
body = config["body"]
headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}

status("Submitting...")
data = json.dumps(body).encode()
req = urllib.request.Request(f"https://queue.fal.run/{model}", data=data, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        qr = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    print(json.dumps({"error": f"HTTP {e.code}: {e.read().decode()}"}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(0)

request_id = qr.get("request_id")
if not request_id:
    print(json.dumps({"error": "No request_id returned"}))
    sys.exit(0)

status_url = qr.get("status_url", f"https://queue.fal.run/{model}/requests/{request_id}/status")
response_url = qr.get("response_url", f"https://queue.fal.run/{model}/requests/{request_id}")
status("Queued...")

parsed = urlparse(status_url)
ctx = ssl.create_default_context()
conn = http.client.HTTPSConnection(parsed.hostname, port=parsed.port or 443, context=ctx, timeout=15)

interval = 2.0
elapsed = 0
max_wait = 600

while elapsed < max_wait:
    time.sleep(interval)
    elapsed += interval
    try:
        conn.request("GET", parsed.path, headers={"Authorization": f"Key {api_key}"})
        r = conn.getresponse()
        sd = json.loads(r.read().decode())
    except Exception:
        try: conn.close()
        except: pass
        conn = http.client.HTTPSConnection(parsed.hostname, port=parsed.port or 443, context=ctx, timeout=15)
        status(f"Reconnecting... ({int(elapsed)}s)")
        continue

    st = sd.get("status", "")
    status(f"{st} ({int(elapsed)}s)")
    if st == "COMPLETED":
        break
    elif st in ("FAILED", "CANCELLED"):
        conn.close()
        print(json.dumps({"error": f"Generation {st.lower()}: {sd.get('error', 'unknown')}"}))
        sys.exit(0)
    if interval < 5.0:
        interval += 0.5
else:
    conn.close()
    print(json.dumps({"error": "Timed out after 5 minutes"}))
    sys.exit(0)

conn.close()

status("Fetching result...")
rreq = urllib.request.Request(response_url, headers={"Authorization": f"Key {api_key}", "Accept": "application/json"})
try:
    with urllib.request.urlopen(rreq, timeout=30) as resp:
        result = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    body = ""
    try: body = e.read().decode()[:500]
    except Exception: pass
    print(json.dumps({"error": f"Fetch result HTTP {e.code}: {body}"}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({"error": f"Fetch result failed: {e}"}))
    sys.exit(0)

images = result.get("images", [])
if not images:
    print(json.dumps({"error": "No images returned"}))
    sys.exit(0)

image_url = images[0]["url"]
seed = result.get("seed", 0)

status("Downloading...")
ireq = urllib.request.Request(image_url)
try:
    with urllib.request.urlopen(ireq, timeout=60) as resp:
        img_data = resp.read()
except Exception as e:
    print(json.dumps({"error": f"Image download failed: {e}"}))
    sys.exit(0)

with open(config["output_path"], "wb") as f:
    f.write(img_data)

status("")
print(json.dumps({"seed": seed, "size": len(img_data)}))
'''


# ---------------------------------------------------------------------------
# IP-Adapter / depth ControlNet defaults (flux_general)
# ---------------------------------------------------------------------------

# Default IP-Adapter checkpoints used when `reference_images` are supplied.
# These ship with fal's flux-general endpoint and don't require extra config.
_FLUX_IP_ADAPTER_PATH = "XLabs-AI/flux-ip-adapter"
# Without `weight_name`, fal's loader can't pick a file out of the repo and
# 422s with "Failed to download ip-adapter weights: 'NoneType' object has no
# attribute 'split'" — it's literally splitting a None URL.
_FLUX_IP_ADAPTER_WEIGHT = "ip_adapter.safetensors"
_FLUX_IP_ADAPTER_ENCODER = "openai/clip-vit-large-patch14"

# Depth ControlNet checkpoint used when `depth_image` is supplied. Shakker-Labs'
# FLUX.1-dev depth ControlNet is the de-facto choice — it ships with fal and
# expects depth-anything-v2-style maps (white = near, black = far), which is
# the convention our `render_depth_map` produces.
_FLUX_DEPTH_CONTROLNET_PATH = "Shakker-Labs/FLUX.1-dev-ControlNet-Depth"


# ---------------------------------------------------------------------------
# Nano Banana 2 generation params
# ---------------------------------------------------------------------------
# Nano Banana 2 is a generational leap over the original Nano Banana (Gemini
# 2.5 Flash Image): it reasons about the request before rendering. With
# `thinking_level` enabled it actually follows data-conversion instructions
# like "produce a tangent-space normal map" instead of just hue-shifting.
_NANO_BANANA_RESOLUTION = "1K"        # "0.5K" / "1K" / "2K" / "4K"
_NANO_BANANA_THINKING = "high"        # "minimal" / "high" / None
_NANO_BANANA_ASPECT = "auto"


def _nano_banana_body(prompt: str) -> dict:
    body = {
        "prompt": prompt,
        "num_images": 1,
        "aspect_ratio": _NANO_BANANA_ASPECT,
        "output_format": "png",
        "resolution": _NANO_BANANA_RESOLUTION,
    }
    if _NANO_BANANA_THINKING:
        body["thinking_level"] = _NANO_BANANA_THINKING
    return body


# ---------------------------------------------------------------------------
# Per-model request builders. Each returns (endpoint_path, request_body) and
# branches internally on the request shape (text2img / img2img / inpaint).
# ---------------------------------------------------------------------------

def _flux_build(req: GenerateRequest) -> tuple[str, dict]:
    if req.is_inpaint:
        body = {
            "prompt": req.prompt,
            "image_url": _to_data_uri(req.init_image),
            "mask_url": _to_data_uri(req.mask_image),
            "num_images": 1,
            "output_format": "png",
        }
        if req.seed is not None:
            body["seed"] = req.seed
        return "fal-ai/flux-pro/v1/fill", body
    if req.is_img2img:
        body = {
            "prompt": req.prompt,
            "image_url": _to_data_uri(req.init_image),
            "strength": req.strength,
            "num_images": 1,
            "output_format": "png",
        }
        if req.seed is not None:
            body["seed"] = req.seed
        return "fal-ai/flux/dev/image-to-image", body
    body = {
        "prompt": req.prompt,
        "image_size": {"width": req.width, "height": req.height},
        "num_images": 1,
        "output_format": "png",
    }
    if req.seed is not None:
        body["seed"] = req.seed
    return "fal-ai/flux/schnell", body


def _flux_general_build(req: GenerateRequest) -> tuple[str, dict]:
    # All capabilities map to the same endpoint — only the body fields change.
    body = {
        "prompt": req.prompt,
        "num_images": 1,
        "output_format": "png",
        "image_size": {"width": req.width, "height": req.height},
    }
    if req.seed is not None:
        body["seed"] = req.seed
    if req.negative_prompt:
        body["negative_prompt"] = req.negative_prompt

    if req.init_image is not None:
        body["image_url"] = _to_data_uri(req.init_image)
        body["strength"] = req.strength
    if req.mask_image is not None:
        body["mask_url"] = _to_data_uri(req.mask_image)

    if req.reference_images:
        # Spread weight evenly across references and cap at 0.6 per ref. Higher
        # scales (>=0.8) overpower the depth ControlNet and FLUX starts
        # duplicating reference features. 0.5-0.6 is the sweet spot with depth.
        per_ref = min(0.6, 1.0 / len(req.reference_images))
        body["ip_adapters"] = [{
            "path": _FLUX_IP_ADAPTER_PATH,
            "weight_name": _FLUX_IP_ADAPTER_WEIGHT,
            "image_encoder_path": _FLUX_IP_ADAPTER_ENCODER,
            "image_url": _to_data_uri(ref),
            "scale": per_ref,
        } for ref in req.reference_images]

    if req.depth_image is not None:
        # Depth ControlNet — makes the generated content wrap the mesh's 3D
        # structure instead of being flat inside the silhouette. Release before
        # the final steps so prompt-driven high-frequency detail isn't clipped.
        body["controlnets"] = [{
            "path": _FLUX_DEPTH_CONTROLNET_PATH,
            "control_image_url": _to_data_uri(req.depth_image),
            "conditioning_scale": req.depth_scale,
            "start_percentage": 0.0,
            "end_percentage": 0.8,
        }]

    return "fal-ai/flux-general", body


def _nano_banana_build(req: GenerateRequest) -> tuple[str, dict]:
    body = _nano_banana_body(req.prompt)
    # Nano Banana has no mask channel; an inpaint request (init+mask) is treated
    # as an edit of the init image — the addon composites the mask client-side.
    if req.init_image is not None:
        urls = [_to_data_uri(req.init_image)]
        urls.extend(_to_data_uri(b) for b in req.reference_images)
        body["image_urls"] = urls
        return "fal-ai/nano-banana-2/edit", body
    if req.reference_images:
        body["image_urls"] = [_to_data_uri(b) for b in req.reference_images]
        return "fal-ai/nano-banana-2/edit", body
    return "fal-ai/nano-banana-2", body


@dataclass(frozen=True)
class _FalModel:
    id: str
    label: str
    caps: frozenset
    build: Callable[[GenerateRequest], tuple]


# Single source of truth for the model dropdown + routing. First entry is the
# default used when the Generate node leaves the model on "Default".
_FAL_MODELS = [
    _FalModel(
        "nano_banana_2", "Nano Banana 2 (Gemini 3.1 Flash Image)",
        frozenset({CAP_TEXT2IMG, CAP_IMG2IMG, CAP_REFERENCE_IMAGES}),
        _nano_banana_build,
    ),
    _FalModel(
        "flux", "FLUX (schnell / dev / pro-fill)",
        frozenset({CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT}),
        _flux_build,
    ),
    _FalModel(
        "flux_general", "FLUX General (Inpaint + IP-Adapter + Depth)",
        frozenset({CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT,
                   CAP_REFERENCE_IMAGES, CAP_DEPTH_CONTROL}),
        _flux_general_build,
    ),
]
_FAL_BY_ID = {m.id: m for m in _FAL_MODELS}


@register_provider
class FalProvider(Provider):
    id = "fal"
    label = "fal.ai"

    @classmethod
    def capabilities(cls) -> set[str]:
        # Union across models so the Generate node never blocks an input that
        # some model needs; per-model routing handles what each actually uses.
        caps: set[str] = set()
        for m in _FAL_MODELS:
            caps |= m.caps
        return caps

    @classmethod
    def models(cls) -> list[tuple]:
        return [(m.id, m.label, "") for m in _FAL_MODELS]

    @classmethod
    def preference_fields(cls) -> list[PreferenceField]:
        return [
            PreferenceField(
                name="api_key",
                label="API Key",
                description=(
                    "fal.ai key in 'key_id:key_secret' format from "
                    "fal.ai/dashboard/keys"
                ),
                kind="password",
            ),
            PreferenceField(
                name="default_model",
                label="Default Model",
                description="Model used when the Generate node doesn't override it",
                kind="enum",
                default=_FAL_MODELS[0].id,
                items=[(m.id, m.label, "") for m in _FAL_MODELS],
            ),
        ]

    # ---- dispatch ----------------------------------------------------------

    def _resolve_model(self, request: GenerateRequest) -> _FalModel:
        mid = (request.__dict__.get("_model_override") or "").strip()
        if not mid:
            mid = self.settings.get("default_model") or _FAL_MODELS[0].id
        spec = _FAL_BY_ID.get(mid)
        if spec is None:
            raise ProviderError(
                f"Unknown fal model '{mid}'. Available: {sorted(_FAL_BY_ID)}"
            )
        return spec

    def generate(self, request: GenerateRequest) -> GenerateResult:
        spec = self._resolve_model(request)
        endpoint, body = spec.build(request)
        return self._run(endpoint, body)

    # ---- queue protocol ----------------------------------------------------

    def _run(self, model: str, body: dict, *, timeout: int = 660) -> GenerateResult:
        # Strip whitespace defensively — pastes often include a trailing
        # newline, which makes fal return "No user found for Key ID and Secret"
        # because the secret half ends up containing the newline character.
        api_key = self.settings.get("api_key", "").strip()
        if not api_key:
            raise AuthenticationError(
                "No fal.ai API key configured. Set it in the addon preferences."
            )
        if ":" not in api_key:
            raise AuthenticationError(
                "fal.ai key must be in 'key_id:key_secret' format. Copy the "
                "full key from fal.ai/dashboard/keys."
            )

        try:
            with open(_STATUS_FILE, "w") as f:
                f.write("Starting...")
        except OSError:
            pass

        result = run_subprocess(
            _WORKER_SCRIPT,
            {
                "api_key": api_key,
                "model": model,
                "body": body,
                "status_path": _STATUS_FILE,
            },
            timeout=timeout,
        )
        return GenerateResult(image_bytes=result["image_bytes"], seed=result.get("seed", 0))


# ---------- helpers ---------------------------------------------------------

def get_status() -> str:
    """Read current status from the status file (used by the UI timer)."""
    try:
        with open(_STATUS_FILE, "r") as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


def _to_data_uri(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"
