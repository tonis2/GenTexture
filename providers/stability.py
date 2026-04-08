import base64
import json
import os
import random
import subprocess
import sys
import tempfile

from . import (
    Provider, GenerateRequest, GenerateResult,
    ProviderError, AuthenticationError, RateLimitError, ContentFilterError,
    register_provider,
)


API_BASE = "https://api.stability.ai"


# Python script executed in a subprocess to avoid GIL-blocking SSL.
_WORKER_SCRIPT = r'''
import json, sys, random, urllib.request, urllib.error

config = json.loads(sys.stdin.read())
url = config["url"]
api_key = config["api_key"]
fields = config["fields"]
output_path = config["output_path"]

# Rebuild multipart body
boundary = f"----GenTexBoundary{random.randint(100000, 999999)}"
body = bytearray()
for key, value in fields.items():
    body.extend(f"--{boundary}\r\n".encode())
    if isinstance(value, dict) and "filename" in value:
        import base64
        file_data = base64.b64decode(value["data_b64"])
        body.extend(
            f'Content-Disposition: form-data; name="{key}"; filename="{value["filename"]}"\r\n'
            f'Content-Type: {value["content_type"]}\r\n\r\n'.encode()
        )
        body.extend(file_data)
    else:
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f'{value}'.encode()
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
        seed = 0
        seed_header = resp.headers.get("seed")
        if seed_header:
            seed = int(seed_header)
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
    name = "stability"
    supports_depth = True
    supports_img2img = True

    def generate(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        if request.depth_image is not None:
            return self._generate_structure(request, api_key)
        elif request.init_image is not None:
            return self._generate_img2img(request, api_key)
        else:
            return self._generate_text2img(request, api_key)

    def _generate_text2img(self, request: GenerateRequest, api_key: str) -> GenerateResult:
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

        return self._call_api(
            f"{API_BASE}/v2beta/stable-image/generate/sd3",
            fields, api_key,
        )

    def _generate_img2img(self, request: GenerateRequest, api_key: str) -> GenerateResult:
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

        return self._call_api(
            f"{API_BASE}/v2beta/stable-image/generate/sd3",
            fields, api_key,
        )

    def _generate_structure(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        fields = {
            "prompt": request.prompt,
            "image": _file_field("depth.png", request.depth_image, "image/png"),
            "control_strength": str(request.strength),
            "output_format": "png",
        }
        if request.negative_prompt:
            fields["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            fields["seed"] = str(request.seed)

        return self._call_api(
            f"{API_BASE}/v2beta/stable-image/control/structure",
            fields, api_key,
        )

    def _call_api(self, url: str, fields: dict, api_key: str) -> GenerateResult:
        """Run the API call in a subprocess to avoid GIL-blocking SSL."""
        out_fd, out_path = tempfile.mkstemp(suffix=".png")
        os.close(out_fd)

        config = json.dumps({
            "url": url,
            "api_key": api_key,
            "fields": fields,
            "output_path": out_path,
        })

        try:
            proc = subprocess.run(
                [sys.executable, "-c", _WORKER_SCRIPT],
                input=config,
                capture_output=True,
                text=True,
                timeout=360,
            )
        except subprocess.TimeoutExpired:
            _cleanup(out_path)
            raise ProviderError("Generation timed out")
        except Exception as e:
            _cleanup(out_path)
            raise ProviderError(f"Subprocess error: {e}")

        if proc.returncode != 0:
            _cleanup(out_path)
            raise ProviderError(f"Worker error: {proc.stderr}")

        stdout = proc.stdout.strip()
        if not stdout:
            _cleanup(out_path)
            raise ProviderError(f"No output from worker. stderr: {proc.stderr}")

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            _cleanup(out_path)
            raise ProviderError(f"Invalid worker output: {stdout}")

        if "error" in result:
            _cleanup(out_path)
            error_msg = result["error"]
            if "401" in error_msg or "403" in error_msg:
                raise AuthenticationError(error_msg)
            elif "429" in error_msg:
                raise RateLimitError(error_msg)
            raise ProviderError(error_msg)

        try:
            with open(out_path, "rb") as f:
                image_bytes = f.read()
        finally:
            _cleanup(out_path)

        return GenerateResult(image_bytes=image_bytes, seed=result.get("seed", 0))


def _file_field(filename: str, data: bytes, content_type: str) -> dict:
    """Serialize a file field for JSON transport to the subprocess."""
    return {
        "filename": filename,
        "data_b64": base64.b64encode(data).decode("ascii"),
        "content_type": content_type,
    }


def _cleanup(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


def _closest_aspect_ratio(width: int, height: int) -> str:
    ratio = width / height
    ratios = {
        "1:1": 1.0,
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "21:9": 21 / 9,
        "9:21": 9 / 21,
        "4:5": 4 / 5,
        "5:4": 5 / 4,
        "2:3": 2 / 3,
        "3:2": 3 / 2,
    }
    return min(ratios, key=lambda k: abs(ratios[k] - ratio))
