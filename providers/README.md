# Adding a Provider

A "provider" is anything that turns a `GenerateRequest` into image bytes:
a public API (Stability, fal, Replicate, OpenAI, Google), a self-hosted
ComfyUI server, or a local model run via subprocess. The addon doesn't care
how — it just calls `provider.generate(request)`.

This document walks through adding one. The whole API lives in
[`api.py`](./api.py).

## TL;DR

```python
# providers/myprovider.py
from .api import (
    Provider, GenerateRequest, GenerateResult, PreferenceField,
    register_provider,
    CAP_TEXT2IMG, CAP_IMG2IMG,
)
from ._http import run_subprocess


@register_provider
class MyProvider(Provider):
    id = "myprovider"
    label = "My Provider"

    @classmethod
    def capabilities(cls):
        return {CAP_TEXT2IMG, CAP_IMG2IMG}

    @classmethod
    def preference_fields(cls):
        return [
            PreferenceField(name="api_key", label="API Key", kind="password"),
        ]

    def text2img(self, req):
        # ... call your API, return GenerateResult(image_bytes=..., seed=...)
        ...

    def img2img(self, req):
        ...
```

Then add the import to `__init__.py` so the module's `@register_provider`
runs during addon load:

```python
from .providers import myprovider as _myprov
```

(and to the reload block).

That's it. The addon will:

- Show **"My Provider"** in the provider dropdown
- Show an **API Key** field in your provider's section in addon preferences
- Pass `{api_key: "..."}` as `settings` when instantiating
- Route text-to-image requests to `text2img()` and image-to-image to `img2img()`
- Skip request fields you can't handle: depth/references sockets on the
  Generate node are always present, but if your provider doesn't declare
  the matching capability they're ignored at dispatch time rather than
  surfaced as errors

## API Reference

### Capabilities

Capabilities are plain strings so plugins can introduce custom ones:

| Constant | When it fires |
|---|---|
| `CAP_TEXT2IMG` | Plain prompt only |
| `CAP_IMG2IMG` | Init image + prompt (no mask) |
| `CAP_INPAINT` | Init image + mask + prompt. Without this, the addon's `generate()` dispatcher falls back to `img2img` and the Project Layer node composites the result against the captured mask client-side. |
| `CAP_DEPTH_CONTROL` | Provider can take a depth-map as ControlNet conditioning. |
| `CAP_REFERENCE_IMAGES` | Provider takes additional reference images to condition style/theme (e.g. Nano Banana's multi-image input, FLUX's IP-Adapter). Without this, references are ignored. |

### `GenerateRequest`

Dataclass passed to your feature methods. Fields:

```python
prompt: str
negative_prompt: str = ""
width: int = 1024
height: int = 1024
init_image: bytes | None
mask_image: bytes | None
reference_images: list[bytes]
depth_image: bytes | None    # white = close, black = far
depth_scale: float = 0.6     # ControlNet conditioning strength
strength: float = 0.75       # 0..1, "denoising strength" for img2img/inpaint
seed: int | None = None      # random if None
```

Convenience predicates: `req.is_text2img`, `req.is_img2img`, `req.is_inpaint`.

The depth map is supplied to providers that declare `CAP_DEPTH_CONTROL` and
should be passed through a depth ControlNet so the generation respects the
mesh's 3D structure, not just the silhouette.

### `GenerateResult`

```python
@dataclass
class GenerateResult:
    image_bytes: bytes   # PNG / JPEG / WebP — the addon decodes by magic bytes
    seed: int = 0
```

Don't decode the image yourself; the addon does it on the main thread for
thread-safe `bpy.data.images` access.

### `PreferenceField`

Each field becomes a Blender property in addon preferences, namespaced as
`<provider_id>__<field_name>`:

```python
PreferenceField(
    name="api_key",         # short name (looks up settings.get("api_key"))
    label="API Key",        # what the user sees
    description="...",      # tooltip
    kind="password",        # "string" | "password" | "enum" | "int" | "float" | "bool"
    default=None,
    items=None,             # for enum: [(id, label, description), ...]
)
```

The values are passed to `__init__` via the `settings` dict:

```python
def __init__(self, settings):
    super().__init__(settings)
    self.api_key = settings.get("api_key", "")
    self.model = settings.get("model_variant", "default")
```

### Feature methods

Override only what you actually support; the default raises `UnsupportedRequestError`.

```python
def text2img(self, req: GenerateRequest) -> GenerateResult: ...
def img2img(self, req: GenerateRequest) -> GenerateResult: ...
def inpaint(self, req: GenerateRequest) -> GenerateResult: ...
```

The default `generate()` dispatches based on what's set on the request
(inpaint > img2img > text2img). Depth, normal, and reference images are not
separate dispatch targets — they're additional fields the provider's own
`text2img`/`img2img`/`inpaint` implementation reads from the request. Override
`generate()` only if you need cross-feature logic.

### Subprocess HTTP helper

Blender's main-thread SSL stalls the UI. Use [`_http.run_subprocess`](./_http.py)
to spawn a worker. Pattern:

```python
_WORKER = r'''
import json, sys, urllib.request
config = json.loads(sys.stdin.read())
# do the HTTP call, write image to config["output_path"], print JSON status
'''

result = run_subprocess(
    _WORKER,
    {"api_key": ..., "url": ..., "body": ...},
    timeout=360,
)
# result["image_bytes"] is the file you wrote to output_path
return GenerateResult(image_bytes=result["image_bytes"], seed=result.get("seed", 0))
```

The worker reads JSON config from stdin, writes binary output to
`config["output_path"]`, and prints a JSON status line. On worker error,
print `{"error": "..."}`. The helper maps HTTP 401/403 → `AuthenticationError`,
429 → `RateLimitError`, etc.

You don't have to use the subprocess pattern — if your provider is local
(e.g. calling a self-hosted ComfyUI on localhost), you can just `urllib`
directly from the `generate()` method, since localhost SSL isn't an issue.

### Errors

Raise the most specific class:

- `AuthenticationError` — bad/missing key
- `RateLimitError` — 429 / over quota
- `ContentFilterError` — safety system rejected
- `UnsupportedRequestError` — capability mismatch
- `ProviderError` — anything else

The addon surfaces these in the Node Editor header (and `scene.gentex_info`)
as `Error in <node>: ...`, and halts the pipeline.

## Multi-model providers

Two patterns — pick whichever reads cleaner.

**Multiple classes** (current fal.py): `FalFluxProvider`,
`FalFluxGeneralProvider`, and `FalNanoBananaProvider` are siblings sharing a
`_FalBase` mixin. Each registers separately, has its own capabilities, and
shows up in the provider dropdown. Best when the models have meaningfully
different capabilities or endpoint shapes.

**One class with an enum field**: declare `model` as an `enum` preference
field, branch on `self.settings["model"]` in `generate()`. Best when the
models differ only in endpoint URL.

## Testing locally

A throwaway provider for development:

```python
@register_provider
class DummyProvider(Provider):
    id = "dummy"
    label = "Dummy (returns red square)"

    @classmethod
    def capabilities(cls):
        return {CAP_TEXT2IMG, CAP_IMG2IMG, CAP_INPAINT}

    def text2img(self, req):
        from ..utils.image import np_to_png_bytes
        import numpy as np
        red = np.zeros((req.height, req.width, 4), dtype=np.float32)
        red[..., 0] = 1.0
        red[..., 3] = 1.0
        return GenerateResult(image_bytes=np_to_png_bytes(red), seed=0)

    img2img = text2img
    inpaint = text2img
```

Drop it in `providers/dummy.py`, add the import in `__init__.py`, reload
scripts. It'll appear in the dropdown.
