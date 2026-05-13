"""Self-hosted FLUX.1-dev server provider.

Points at a long-running `gpu_rig/serve.py` instance (see fal_test/gpu_rig/)
that keeps FLUX.1-dev + depth ControlNet + IP-Adapter resident in VRAM and
exposes a single POST /generate endpoint. Use this when fal's hosted
flux-general caps (IP-Adapter scale capped low, ControlNet timestep range
clipped) are limiting reference-image fidelity.

Architecture mirrors the fal providers: a stdin-driven worker subprocess
does the HTTP I/O so Blender's main thread never blocks on a socket. The
server's response is base64-decoded into the worker's output file.

Typical setup: SSH-tunnel the GPU box so the server URL is
http://localhost:8000 — keeps FLUX off the public internet.
"""

from __future__ import annotations

import os
import tempfile

from .api import (
    Provider, GenerateRequest, GenerateResult, PreferenceField,
    AuthenticationError, ProviderError,
    register_provider,
    CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT,
    CAP_REFERENCE_IMAGES, CAP_DEPTH_CONTROL,
)
from ._http import run_subprocess


_STATUS_FILE = os.path.join(tempfile.gettempdir(), "gentex_local_server_status")


# The worker reads JSON from stdin (config dict the provider builds), posts
# to {server_url}/generate, decodes the returned base64 PNG into
# config["output_path"], and prints a JSON status line on stdout.
_WORKER_SCRIPT = r'''
import base64, json, sys, urllib.request, urllib.error

config = json.loads(sys.stdin.read())
status_path = config.get("status_path", "")

def status(msg):
    if not status_path: return
    try:
        with open(status_path, "w") as f: f.write(msg)
    except: pass

url = config["server_url"].rstrip("/") + "/generate"
token = config.get("token", "")
body = config["body"]
timeout = config.get("timeout", 180)

headers = {"Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"

status("Submitting...")
data = json.dumps(body).encode()
req = urllib.request.Request(url, data=data, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    detail = ""
    try: detail = e.read().decode()[:500]
    except Exception: pass
    print(json.dumps({"error": f"HTTP {e.code}: {detail}"}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({"error": f"Request failed: {e}"}))
    sys.exit(0)

img_b64 = result.get("image")
if not img_b64:
    print(json.dumps({"error": f"No image in response: {str(result)[:300]}"}))
    sys.exit(0)

try:
    img_data = base64.b64decode(img_b64)
except Exception as e:
    print(json.dumps({"error": f"Decode failed: {e}"}))
    sys.exit(0)

with open(config["output_path"], "wb") as f:
    f.write(img_data)

status("")
print(json.dumps({"elapsed": result.get("elapsed", 0)}))
'''


@register_provider
class LocalServerProvider(Provider):
    """Self-hosted FLUX.1-dev with depth ControlNet + IP-Adapter."""

    id = "local_server"
    label = "Local · FLUX.1-dev server (depth + IP-Adapter)"

    @classmethod
    def capabilities(cls) -> set[str]:
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT,
                CAP_REFERENCE_IMAGES, CAP_DEPTH_CONTROL}

    @classmethod
    def preference_fields(cls) -> list[PreferenceField]:
        return [
            PreferenceField(
                name="server_url",
                label="Server URL",
                description=(
                    "URL of the FLUX.1-dev serve.py instance "
                    "(e.g. http://localhost:8000 when SSH-tunneled)."
                ),
                kind="string",
                default="http://localhost:8000",
            ),
            PreferenceField(
                name="token",
                label="Auth Token",
                description=(
                    "Bearer token matching GENTEX_TOKEN on the server. "
                    "Leave empty if the server runs unauthenticated."
                ),
                kind="password",
            ),
            PreferenceField(
                name="ip_scale",
                label="IP-Adapter Scale",
                description=(
                    "Reference-image strength. Self-hosted is uncapped — "
                    "1.0 is a good start, push to 1.2-1.5 for tighter style "
                    "match. Above ~1.5 starts duplicating reference features."
                ),
                kind="float",
                default=1.0,
            ),
        ]

    def generate(self, request: GenerateRequest) -> GenerateResult:
        server_url = (self.settings.get("server_url") or "").strip()
        if not server_url:
            raise AuthenticationError(
                "No server URL configured. Set it in the addon preferences."
            )

        # The server's pipeline always runs the full FLUX.1-dev + depth +
        # IP-Adapter recipe, so it needs an init image, mask, depth, and one
        # reference. Refuse early if any are missing rather than letting the
        # subprocess return a 400 from the server.
        missing = [
            n for n, v in (
                ("init image", request.init_image),
                ("mask image", request.mask_image),
                ("depth image", request.depth_image),
            ) if v is None
        ]
        if missing:
            raise ProviderError(
                "Local FLUX server requires "
                + ", ".join(missing)
                + ". Run a Preview Capture first."
            )
        if not request.reference_images:
            raise ProviderError(
                "Local FLUX server requires a reference image. Add one in "
                "the References panel."
            )
        if len(request.reference_images) > 1:
            # XLabs IP-Adapter takes a single reference. Picking the first is
            # the same behaviour as fal-general would land at if we capped
            # ip_scale to keep all of them under 0.6 — except here we want
            # full weight on one reference. Document instead of averaging.
            print("[gentex] local server: multiple references given, "
                  "using the first only.")

        ip_scale = float(self.settings.get("ip_scale", 1.0))

        body = {
            "init": _b64(request.init_image),
            "mask": _b64(request.mask_image),
            "depth": _b64(request.depth_image),
            "reference": _b64(request.reference_images[0]),
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt or "",
            "strength": request.strength,
            "depth_scale": request.depth_scale,
            "depth_end": 0.8,
            "ip_scale": ip_scale,
        }
        if request.seed is not None:
            body["seed"] = request.seed

        try:
            with open(_STATUS_FILE, "w") as f:
                f.write("Sending to GPU server...")
        except OSError:
            pass

        # 180s covers FLUX.1-dev's typical 30-45s generation + offload
        # warmup spikes (T5 swap-in on first request). run_subprocess wraps
        # this with its own timeout for the worker process itself.
        result = run_subprocess(
            _WORKER_SCRIPT,
            {
                "server_url": server_url,
                "token": (self.settings.get("token") or "").strip(),
                "body": body,
                "timeout": 180,
                "status_path": _STATUS_FILE,
            },
            timeout=240,
        )
        return GenerateResult(image_bytes=result["image_bytes"], seed=request.seed or 0)


def get_status() -> str:
    try:
        with open(_STATUS_FILE, "r") as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


def _b64(png_bytes: bytes) -> str:
    import base64
    return base64.b64encode(png_bytes).decode("ascii")
