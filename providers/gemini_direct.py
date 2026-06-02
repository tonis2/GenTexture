"""Direct Google Gemini provider (Nano Banana / Nano Banana 2).

Calls https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
with a Google AI Studio API key. Avoids fal.ai entirely so the user can pick
their model (gemini-3.5-flash-image, gemini-2.5-flash-image, or
gemini-3-pro-image-preview) directly.

Capabilities: text2img, img2img (init image becomes another input image),
and reference_images (sent as additional inline_data parts). Gemini doesn't
have a separate mask channel — the addon does client-side masking elsewhere.
"""

from __future__ import annotations

import os
import tempfile

from .api import (
    Provider, GenerateRequest, GenerateResult, PreferenceField,
    AuthenticationError,
    register_provider,
    CAP_TEXT2IMG, CAP_IMG2IMG, CAP_REFERENCE_IMAGES,
)
from ._http import run_subprocess


_STATUS_FILE = os.path.join(tempfile.gettempdir(), "gentex_gemini_status")


# Selectable Gemini image models. Single source of truth, shared by the
# Generate-node Model dropdown (via models()) and the "Default Model"
# preference (via preference_fields()). First entry is the default.
_MODELS = [
    ("gemini-3-pro-image-preview", "Gemini 3 Pro Image Preview (Nano Banana 2)", ""),
    ("gemini-2.5-flash-image", "Gemini 2.5 Flash Image (Nano Banana)", ""),
]


# Gemini's image API takes an aspect ratio + resolution tier, not arbitrary
# pixel dimensions. We map the Generate node's width/height onto the nearest
# supported aspect ratio so a square (1024x1024) request yields a square image
# instead of the model's ~16:9 landscape default.
_ASPECT_RATIOS = {
    "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2, "3:4": 3 / 4, "4:3": 4 / 3,
    "4:5": 4 / 5, "5:4": 5 / 4, "9:16": 9 / 16, "16:9": 16 / 9, "21:9": 21 / 9,
}


def _aspect_ratio(width: int, height: int) -> str:
    """Nearest supported Gemini aspect-ratio token for a pixel w x h request."""
    import math
    w, h = int(width or 0), int(height or 0)
    if w <= 0 or h <= 0:
        return "1:1"
    r = w / h
    # Compare in log space so e.g. 2:3 and 3:2 are treated symmetrically.
    return min(_ASPECT_RATIOS, key=lambda k: abs(math.log(_ASPECT_RATIOS[k] / r)))


def _image_size(width: int, height: int):
    """Resolution tier from the longer requested edge.

    Returns "2K"/"4K", or None to leave Gemini at its 1K default (so we don't
    send an upscale request for ordinary ~1024px sizes).
    """
    longest = max(int(width or 0), int(height or 0))
    if longest > 2048:
        return "4K"
    if longest > 1024:
        return "2K"
    return None


_WORKER_SCRIPT = r'''
import base64, json, sys, urllib.request, urllib.error

config = json.loads(sys.stdin.read())
status_path = config["status_path"]

def status(msg):
    try:
        with open(status_path, "w") as f:
            f.write(msg)
    except: pass

api_key = config["api_key"]
model = config["model"]
prompt = config["prompt"]
images_b64 = config.get("images_b64", [])
output_path = config["output_path"]

parts = []
# Gemini image editing is order-sensitive: the image(s) must come BEFORE the
# text instruction. Sending text first makes Gemini treat the prompt as a loose
# caption and pass the input through recolored (the "not an edit" failure mode);
# image-first makes it actually edit the supplied image.
for b64 in images_b64:
    parts.append({"inline_data": {"mime_type": "image/png", "data": b64}})
if prompt:
    parts.append({"text": prompt})

# Gemini doesn't take arbitrary pixel dimensions — it takes an aspect ratio and
# (on the Pro model) a resolution tier via generationConfig.imageConfig. Without
# this, the model ignores the requested size and falls back to its ~16:9
# landscape default. aspect_ratio/image_size are mapped from the node's
# width/height by the provider before the worker runs.
gen_cfg = {"responseModalities": ["IMAGE", "TEXT"]}
image_config = {}
if config.get("aspect_ratio"):
    image_config["aspectRatio"] = config["aspect_ratio"]
if config.get("image_size"):
    image_config["imageSize"] = config["image_size"]
if image_config:
    gen_cfg["imageConfig"] = image_config

body = {
    "contents": [{"parts": parts}],
    "generationConfig": gen_cfg,
}

url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
req = urllib.request.Request(
    url,
    data=json.dumps(body).encode(),
    headers={
        "Content-Type": "application/json",
        "X-goog-api-key": api_key,
    },
    method="POST",
)

status("Generating...")
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    err_body = ""
    try: err_body = e.read().decode()[:1000]
    except Exception: pass
    print(json.dumps({"error": f"HTTP {e.code}: {err_body}"}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(0)

candidates = result.get("candidates", [])
if not candidates:
    pf = result.get("promptFeedback", {})
    block_reason = pf.get("blockReason") or "no candidates"
    print(json.dumps({"error": f"Gemini refused: {block_reason}"}))
    sys.exit(0)

image_b64 = None
refusal_text = []
for part in candidates[0].get("content", {}).get("parts", []):
    if "inlineData" in part or "inline_data" in part:
        data = part.get("inlineData") or part.get("inline_data")
        image_b64 = data.get("data")
        break
    if "text" in part and part["text"]:
        refusal_text.append(part["text"])

if image_b64 is None:
    msg = " ".join(refusal_text) if refusal_text else "no image in response"
    print(json.dumps({"error": f"Gemini refused/filtered: {msg[:500]}"}))
    sys.exit(0)

img_bytes = base64.b64decode(image_b64)
with open(output_path, "wb") as f:
    f.write(img_bytes)

status("")
print(json.dumps({"seed": 0, "size": len(img_bytes)}))
'''


@register_provider
class GeminiDirectProvider(Provider):
    id = "gemini_direct"
    label = "Google · Gemini (direct)"

    @classmethod
    def capabilities(cls) -> set[str]:
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_REFERENCE_IMAGES}

    @classmethod
    def models(cls) -> list[tuple]:
        return list(_MODELS)

    @classmethod
    def preference_fields(cls) -> list[PreferenceField]:
        return [
            PreferenceField(
                name="api_key",
                label="API Key",
                description=(
                    "Google AI Studio API key from aistudio.google.com/apikey. "
                    "Calls generativelanguage.googleapis.com directly."
                ),
                kind="password",
            ),
            PreferenceField(
                name="default_model",
                label="Default Model",
                description="Model ID used when the Generate node doesn't override it",
                kind="enum",
                default=_MODELS[0][0],
                items=list(_MODELS),
            ),
        ]

    def _run(self, request: GenerateRequest, images_b64: list[str]) -> GenerateResult:
        api_key = self.settings.get("api_key", "").strip()
        if not api_key:
            raise AuthenticationError(
                "No Google Gemini API key configured. Get one at "
                "aistudio.google.com/apikey and set it in addon preferences."
            )

        model = (request.__dict__.get("_model_override") or "").strip()
        if not model:
            model = self.settings.get("default_model") or "gemini-3-pro-image-preview"

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
                "prompt": request.prompt,
                "images_b64": images_b64,
                "aspect_ratio": _aspect_ratio(request.width, request.height),
                "image_size": _image_size(request.width, request.height),
                "status_path": _STATUS_FILE,
            },
            timeout=360,
        )
        return GenerateResult(image_bytes=result["image_bytes"], seed=0)

    def text2img(self, request: GenerateRequest) -> GenerateResult:
        images_b64 = [_b64(ref) for ref in request.reference_images]
        return self._run(request, images_b64)

    def img2img(self, request: GenerateRequest) -> GenerateResult:
        images_b64 = [_b64(request.init_image)]
        images_b64.extend(_b64(ref) for ref in request.reference_images)
        return self._run(request, images_b64)


def get_status() -> str:
    """Read current status from the status file (polled by the UI timer)."""
    try:
        with open(_STATUS_FILE, "r") as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


def _b64(png_bytes: bytes) -> str:
    import base64
    return base64.b64encode(png_bytes).decode("ascii")
