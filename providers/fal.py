"""fal.ai providers.

Two provider classes share fal's queue-based worker protocol via `_FalBase`:

  * `FalFluxProvider`         - Black Forest Labs FLUX. Full caps incl. inpaint.
  * `FalNanoBananaProvider`   - Google Gemini 2.5 Flash Image. Multi-image
                                consistency; no native mask channel.

Both endpoints follow the same async-queue pattern:
  POST /<model>            -> { request_id, status_url, response_url }
  GET  status_url          -> poll until status == "COMPLETED"
  GET  response_url        -> { images: [{url}], seed }
  GET  images[0].url       -> PNG bytes
"""

from __future__ import annotations

import base64
import os
import tempfile

from .api import (
    Provider, GenerateRequest, GenerateResult, PreferenceField,
    AuthenticationError,
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
# Shared base
# ---------------------------------------------------------------------------

class _FalBase(Provider):
    """Shared queue-protocol logic + API-key field."""

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
        ]

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


# ---------------------------------------------------------------------------
# FLUX
# ---------------------------------------------------------------------------

@register_provider
class FalFluxProvider(_FalBase):
    id = "fal_flux"
    label = "fal · FLUX"

    @classmethod
    def capabilities(cls) -> set[str]:
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT}

    def text2img(self, request: GenerateRequest) -> GenerateResult:
        body = {
            "prompt": request.prompt,
            "image_size": {"width": request.width, "height": request.height},
            "num_images": 1,
            "output_format": "png",
        }
        if request.seed is not None:
            body["seed"] = request.seed
        return self._run("fal-ai/flux/schnell", body)

    def img2img(self, request: GenerateRequest) -> GenerateResult:
        body = {
            "prompt": request.prompt,
            "image_url": _to_data_uri(request.init_image),
            "strength": request.strength,
            "num_images": 1,
            "output_format": "png",
        }
        if request.seed is not None:
            body["seed"] = request.seed
        return self._run("fal-ai/flux/dev/image-to-image", body)

    def inpaint(self, request: GenerateRequest) -> GenerateResult:
        body = {
            "prompt": request.prompt,
            "image_url": _to_data_uri(request.init_image),
            "mask_url": _to_data_uri(request.mask_image),
            "num_images": 1,
            "output_format": "png",
        }
        if request.seed is not None:
            body["seed"] = request.seed
        return self._run("fal-ai/flux-pro/v1/fill", body)


# ---------------------------------------------------------------------------
# FLUX General — inpaint + IP-Adapter (style references)
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


@register_provider
class FalFluxGeneralProvider(_FalBase):
    """fal's `flux-general` endpoint — the multi-modal FLUX pipeline.

    Accepts init image, mask, and a list of IP-Adapter references for style.
    This is the one provider that gives you both the inpaint pixel-preservation
    contract (so screen-space projection lines up) AND multi-image style
    references in a single call.
    """

    id = "fal_flux_general"
    label = "fal · FLUX General (Inpaint + IP-Adapter)"

    @classmethod
    def capabilities(cls) -> set[str]:
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT,
                CAP_REFERENCE_IMAGES, CAP_DEPTH_CONTROL}

    def generate(self, request: GenerateRequest) -> GenerateResult:
        # All capabilities map to the same endpoint — it's the fields on the
        # body that change. Override `generate()` instead of routing through
        # text2img/img2img/inpaint because the dispatch in api.py would split
        # apart cases (e.g. inpaint + references) that this endpoint handles
        # in a single call.
        body = {
            "prompt": request.prompt,
            "num_images": 1,
            "output_format": "png",
            "image_size": {"width": request.width, "height": request.height},
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt

        if request.init_image is not None:
            body["image_url"] = _to_data_uri(request.init_image)
            body["strength"] = request.strength
        if request.mask_image is not None:
            body["mask_url"] = _to_data_uri(request.mask_image)

        if request.reference_images:
            # Spread weight evenly across references and cap at 0.6 per ref.
            # Higher scales (≥0.8) overpower the depth ControlNet and FLUX
            # starts duplicating reference features — e.g. two heads/poms on
            # a single mesh. The fal_test/ bench (cat_ref_scale_* configs)
            # confirmed 0.5–0.6 is the sweet spot when depth is also on.
            per_ref = min(0.6, 1.0 / len(request.reference_images))
            body["ip_adapters"] = [{
                "path": _FLUX_IP_ADAPTER_PATH,
                "weight_name": _FLUX_IP_ADAPTER_WEIGHT,
                "image_encoder_path": _FLUX_IP_ADAPTER_ENCODER,
                "image_url": _to_data_uri(ref),
                "scale": per_ref,
            } for ref in request.reference_images]

        if request.depth_image is not None:
            # Depth ControlNet — this is what makes the generated content
            # actually wrap the mesh's 3D structure instead of being a flat
            # image inside the silhouette. Apply from the start of denoising
            # but release before the final steps so high-frequency surface
            # detail (specified by the prompt) isn't clipped by the depth.
            body["controlnets"] = [{
                "path": _FLUX_DEPTH_CONTROLNET_PATH,
                "control_image_url": _to_data_uri(request.depth_image),
                "conditioning_scale": request.depth_scale,
                "start_percentage": 0.0,
                "end_percentage": 0.8,
            }]

        return self._run("fal-ai/flux-general", body)


# ---------------------------------------------------------------------------
# Nano Banana (Gemini 2.5 Flash Image)
# ---------------------------------------------------------------------------

@register_provider
class FalNanoBananaProvider(_FalBase):
    id = "fal_nano_banana"
    label = "fal · Nano Banana (Gemini 2.5 Flash Image)"

    @classmethod
    def capabilities(cls) -> set[str]:
        # No CAP_INPAINT: Nano Banana has no mask channel. The addon's
        # client-side composite handles per-pixel masking instead.
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_REFERENCE_IMAGES}

    def text2img(self, request: GenerateRequest) -> GenerateResult:
        body = {
            "prompt": request.prompt,
            "num_images": 1,
            "output_format": "png",
        }
        if request.reference_images:
            # When refs are present, route through the edit endpoint.
            body["image_urls"] = [_to_data_uri(b) for b in request.reference_images]
            return self._run("fal-ai/gemini-25-flash-image/edit", body)
        return self._run("fal-ai/gemini-25-flash-image", body)

    def img2img(self, request: GenerateRequest) -> GenerateResult:
        urls = [_to_data_uri(request.init_image)]
        urls.extend(_to_data_uri(b) for b in request.reference_images)
        body = {
            "prompt": request.prompt,
            "image_urls": urls,
            "num_images": 1,
            "output_format": "png",
        }
        return self._run("fal-ai/gemini-25-flash-image/edit", body)


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
