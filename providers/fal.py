import base64
import json
import subprocess
import sys
import tempfile
import os

from . import (
    Provider, GenerateRequest, GenerateResult,
    ProviderError, AuthenticationError, RateLimitError,
    register_provider,
)


# Path to a temp file where the subprocess writes status updates.
# The UI timer reads this file to show progress.
_status_file = os.path.join(tempfile.gettempdir(), "gentex_fal_status")


# Python script executed in a subprocess to avoid GIL-blocking SSL handshakes.
# Writes status to a file, final JSON result to stdout.
_WORKER_SCRIPT = r'''
import json, sys, time, urllib.request, urllib.error, http.client, ssl
from urllib.parse import urlparse

config = json.loads(sys.stdin.read())
status_path = config["status_path"]

def status(msg):
    try:
        with open(status_path, "w") as f:
            f.write(msg)
    except:
        pass
api_key = config["api_key"]
model = config["model"]
body = config["body"]

headers = {
    "Authorization": f"Key {api_key}",
    "Content-Type": "application/json",
}

# Step 1: Submit to queue
status("Submitting...")
data = json.dumps(body).encode()
req = urllib.request.Request(
    f"https://queue.fal.run/{model}",
    data=data, headers=headers, method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        qr = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(json.dumps({"error": f"HTTP {e.code}: {err}"}))
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

# Step 2: Poll with persistent connection
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

# Step 3: Fetch result
status("Fetching result...")
rreq = urllib.request.Request(response_url, headers={"Authorization": f"Key {api_key}", "Accept": "application/json"})
with urllib.request.urlopen(rreq, timeout=30) as resp:
    result = json.loads(resp.read().decode())

images = result.get("images", [])
if not images:
    print(json.dumps({"error": "No images returned"}))
    sys.exit(0)

image_url = images[0]["url"]
seed = result.get("seed", 0)

# Step 4: Download image
status("Downloading...")
ireq = urllib.request.Request(image_url)
with urllib.request.urlopen(ireq, timeout=60) as resp:
    img_data = resp.read()

out_path = config["output_path"]
with open(out_path, "wb") as f:
    f.write(img_data)

status("")
print(json.dumps({"seed": seed, "output_path": out_path, "size": len(img_data)}))
'''


@register_provider
class FalProvider(Provider):
    name = "fal"
    supports_depth = True
    supports_img2img = True

    def generate(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        if request.normal_image is not None:
            return self._generate_normal(request, api_key)
        elif request.depth_image is not None:
            return self._generate_depth(request, api_key)
        elif request.init_image is not None:
            return self._generate_img2img(request, api_key)
        else:
            return self._generate_text2img(request, api_key)

    def _generate_text2img(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        body = {
            "prompt": request.prompt,
            "image_size": {"width": request.width, "height": request.height},
            "num_images": 1,
            "output_format": "png",
        }
        if request.seed is not None:
            body["seed"] = request.seed
        return self._run_worker("fal-ai/flux/schnell", body, api_key)

    def _generate_img2img(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        body = {
            "prompt": request.prompt,
            "image_url": _to_data_uri(request.init_image),
            "strength": request.strength,
            "num_images": 1,
            "output_format": "png",
        }
        if request.seed is not None:
            body["seed"] = request.seed
        return self._run_worker("fal-ai/flux/dev/image-to-image", body, api_key)

    def _generate_depth(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        """Depth-conditioned generation using flux-general with easycontrols."""
        body = {
            "prompt": request.prompt,
            "easycontrols": [{
                "control_method_url": "depth",
                "image_url": _to_data_uri(request.depth_image),
                "image_control_type": "spatial",
                "scale": request.strength,
            }],
            "num_images": 1,
            "output_format": "png",
            "image_size": {"width": request.width, "height": request.height},
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.init_image is not None:
            body["image_url"] = _to_data_uri(request.init_image)
        return self._run_worker("fal-ai/flux-general", body, api_key)

    def _generate_normal(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        """Normal-map-conditioned generation using flux-general with easycontrols."""
        body = {
            "prompt": request.prompt,
            "easycontrols": [{
                "control_method_url": "depth",
                "image_url": _to_data_uri(request.normal_image),
                "image_control_type": "spatial",
                "scale": request.strength,
            }],
            "num_images": 1,
            "output_format": "png",
            "image_size": {"width": request.width, "height": request.height},
        }
        if request.seed is not None:
            body["seed"] = request.seed
        return self._run_worker("fal-ai/flux-general", body, api_key)

    def _run_worker(self, model: str, body: dict, api_key: str) -> GenerateResult:
        """Run the API call in a subprocess to avoid GIL-blocking SSL."""
        out_fd, out_path = tempfile.mkstemp(suffix=".png")
        os.close(out_fd)

        # Clear status file
        try:
            with open(_status_file, "w") as f:
                f.write("Starting...")
        except OSError:
            pass

        config = json.dumps({
            "api_key": api_key,
            "model": model,
            "body": body,
            "output_path": out_path,
            "status_path": _status_file,
        })

        try:
            proc = subprocess.run(
                [sys.executable, "-c", _WORKER_SCRIPT],
                input=config,
                capture_output=True,
                text=True,
                timeout=660,
            )
        except subprocess.TimeoutExpired:
            self._cleanup(out_path)
            raise ProviderError("Generation timed out")
        except Exception as e:
            self._cleanup(out_path)
            raise ProviderError(f"Subprocess error: {e}")

        if proc.returncode != 0:
            self._cleanup(out_path)
            raise ProviderError(f"Worker error (exit {proc.returncode}): {proc.stderr[:200]}")

        stdout = proc.stdout.strip()
        if not stdout:
            self._cleanup(out_path)
            raise ProviderError(f"No output from worker. stderr: {proc.stderr[:200]}")

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            self._cleanup(out_path)
            raise ProviderError(f"Invalid worker output: {stdout[:200]}")

        if "error" in result:
            self._cleanup(out_path)
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
            self._cleanup(out_path)

        return GenerateResult(image_bytes=image_bytes, seed=result.get("seed", 0))

    def _cleanup(self, path: str):
        try:
            os.unlink(path)
        except OSError:
            pass


def get_status() -> str:
    """Read current status from the status file."""
    try:
        with open(_status_file, "r") as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


def _to_data_uri(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"
