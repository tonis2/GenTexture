"""Stability AI provider.

Uses the v2beta Stable Image API:

  * generate/sd3                  - text2img + img2img (`mode=image-to-image`)
  * control/structure             - depth/normal-style structural conditioning
  * edit/inpaint                  - mask-based inpainting
"""

from __future__ import annotations

import base64

from .api import (
    Provider, GenerateRequest, GenerateResult, PreferenceField,
    register_provider,
    CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT, CAP_DEPTH, CAP_NORMAL,
)
from ._http import run_subprocess


API_BASE = "https://api.stability.ai"


# Worker process: rebuilds a multipart/form-data request and POSTs.
_WORKER_SCRIPT = r'''
import json, sys, random, urllib.request, urllib.error, base64

config = json.loads(sys.stdin.read())
url = config["url"]
api_key = config["api_key"]
fields = config["fields"]
output_path = config["output_path"]

boundary = f"----GenTexBoundary{random.randint(100000, 999999)}"
body = bytearray()
for key, value in fields.items():
    body.extend(f"--{boundary}\r\n".encode())
    if isinstance(value, dict) and "filename" in value:
        file_data = base64.b64decode(value["data_b64"])
        body.extend(
            f'Content-Disposition: form-data; name="{key}"; filename="{value["filename"]}"\r\n'
            f'Content-Type: {value["content_type"]}\r\n\r\n'.encode()
        )
        body.extend(file_data)
    else:
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}'.encode()
        )
    body.extend(b"\r\n")
body.extend(f"--{boundary}--\r\n".encode())

req = urllib.request.Request(
    url, data=bytes(body),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "image/*",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        image_bytes = resp.read()
        seed_header = resp.headers.get("seed")
        seed = int(seed_header) if seed_header else 0
        finish_reason = resp.headers.get("finish-reason", "")
        if finish_reason == "CONTENT_FILTERED":
            print(json.dumps({"error": "Content filtered by Stability AI safety system"}))
            sys.exit(0)
        with open(output_path, "wb") as f:
            f.write(image_bytes)
        print(json.dumps({"seed": seed, "size": len(image_bytes)}))
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(json.dumps({"error": f"HTTP {e.code}: {err}"}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
'''


@register_provider
class StabilityProvider(Provider):
    id = "stability"
    label = "Stability AI"

    @classmethod
    def capabilities(cls) -> set[str]:
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT, CAP_DEPTH, CAP_NORMAL}

    @classmethod
    def preference_fields(cls) -> list[PreferenceField]:
        return [
            PreferenceField(
                name="api_key",
                label="API Key",
                description="API key from platform.stability.ai",
                kind="password",
            ),
        ]

    # ---------- feature methods ---------------------------------------------

    def text2img(self, request: GenerateRequest) -> GenerateResult:
        fields = {
            "prompt": request.prompt,
            "output_format": "png",
        }
        if request.negative_prompt:
            fields["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            fields["seed"] = str(request.seed)
        if request.width and request.height:
            fields["aspect_ratio"] = _closest_aspect_ratio(request.width, request.height)
        return self._post(f"{API_BASE}/v2beta/stable-image/generate/sd3", fields)

    def img2img(self, request: GenerateRequest) -> GenerateResult:
        fields = {
            "prompt": request.prompt,
            "mode": "image-to-image",
            "image": _file_field("image.png", request.init_image, "image/png"),
            "strength": str(request.strength),
            "output_format": "png",
        }
        if request.negative_prompt:
            fields["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            fields["seed"] = str(request.seed)
        return self._post(f"{API_BASE}/v2beta/stable-image/generate/sd3", fields)

    def inpaint(self, request: GenerateRequest) -> GenerateResult:
        fields = {
            "prompt": request.prompt,
            "image": _file_field("image.png", request.init_image, "image/png"),
            "mask": _file_field("mask.png", request.mask_image, "image/png"),
            "output_format": "png",
        }
        if request.negative_prompt:
            fields["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            fields["seed"] = str(request.seed)
        return self._post(f"{API_BASE}/v2beta/stable-image/edit/inpaint", fields)

    def depth(self, request: GenerateRequest) -> GenerateResult:
        return self._control_structure(request, request.depth_image)

    def normal(self, request: GenerateRequest) -> GenerateResult:
        # Stability has no dedicated normal endpoint; reuse structure control.
        return self._control_structure(request, request.normal_image)

    # ---------- internals ---------------------------------------------------

    def _control_structure(self, request: GenerateRequest, control_png: bytes) -> GenerateResult:
        fields = {
            "prompt": request.prompt,
            "image": _file_field("control.png", control_png, "image/png"),
            "control_strength": str(request.strength),
            "output_format": "png",
        }
        if request.negative_prompt:
            fields["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            fields["seed"] = str(request.seed)
        return self._post(f"{API_BASE}/v2beta/stable-image/control/structure", fields)

    def _post(self, url: str, fields: dict) -> GenerateResult:
        api_key = self.settings.get("api_key", "")
        result = run_subprocess(
            _WORKER_SCRIPT,
            {"url": url, "api_key": api_key, "fields": fields},
            timeout=360,
        )
        return GenerateResult(image_bytes=result["image_bytes"], seed=result.get("seed", 0))


# ---------- helpers ---------------------------------------------------------

def _file_field(filename: str, data: bytes, content_type: str) -> dict:
    return {
        "filename": filename,
        "data_b64": base64.b64encode(data).decode("ascii"),
        "content_type": content_type,
    }


def _closest_aspect_ratio(width: int, height: int) -> str:
    ratio = width / height
    ratios = {
        "1:1": 1.0,
        "16:9": 16 / 9, "9:16": 9 / 16,
        "21:9": 21 / 9, "9:21": 9 / 21,
        "4:5": 4 / 5,   "5:4": 5 / 4,
        "2:3": 2 / 3,   "3:2": 3 / 2,
    }
    return min(ratios, key=lambda k: abs(ratios[k] - ratio))
