"""Public API for adding texture-generation providers.

To add a new provider:

  1. Subclass `Provider`, set `id` and `label`.
  2. Override `capabilities()` to declare what this provider supports.
  3. Override `preference_fields()` to declare config fields the user will set
     in addon preferences (API key, model variant, etc.). The addon merges
     these into the global preferences UI automatically.
  4. Implement the feature methods you actually support
     (`text2img`, `img2img`, `inpaint`, `depth`, `normal`). The default
     `generate(request)` dispatches to them based on what's set on the
     request.
  5. Register the class with `@register_provider`.

See `providers/stability.py` and `providers/fal.py` for working examples,
and `providers/README.md` for a step-by-step guide.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# Capability tags
# ---------------------------------------------------------------------------
# Capabilities are strings rather than an enum so plugin authors can introduce
# their own without modifying core code.

CAP_TEXT2IMG = "text2img"           # plain prompt -> image
CAP_IMG2IMG = "img2img"             # init image + prompt -> image
CAP_INPAINT = "inpaint"             # init + mask + prompt -> image (true mask)
CAP_REFERENCE_IMAGES = "reference_images"   # extra style reference images
CAP_DEPTH_CONTROL = "depth_control"  # provider can accept a depth ControlNet map


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

@dataclass
class GenerateRequest:
    """A single inference request.

    All image fields are PNG bytes. When multiple are set, the provider's
    `generate()` dispatches in this priority:

        inpaint   (init_image + mask_image)
        img2img   (init_image)
        text2img  (none)
    """

    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    init_image: bytes | None = None
    mask_image: bytes | None = None
    reference_images: list[bytes] = field(default_factory=list)
    # PNG depth map (white = close, black = far). Providers that advertise
    # `CAP_DEPTH_CONTROL` will pass this through a depth ControlNet so generated
    # content respects the mesh's 3D structure instead of just the silhouette.
    depth_image: bytes | None = None
    depth_scale: float = 0.6
    strength: float = 0.75
    seed: int | None = None

    # ---------- convenience predicates ----------

    @property
    def is_inpaint(self) -> bool:
        return self.init_image is not None and self.mask_image is not None

    @property
    def is_img2img(self) -> bool:
        return self.init_image is not None and self.mask_image is None

    @property
    def is_text2img(self) -> bool:
        return self.init_image is None and self.mask_image is None


@dataclass
class GenerateResult:
    image_bytes: bytes
    seed: int = 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base error for provider failures."""


class AuthenticationError(ProviderError):
    """Invalid or missing API key."""


class RateLimitError(ProviderError):
    """API rate limit exceeded."""


class ContentFilterError(ProviderError):
    """Content was filtered by the API's safety system."""


class UnsupportedRequestError(ProviderError):
    """Provider can't fulfill this request (capability mismatch)."""


# ---------------------------------------------------------------------------
# Preference declarations
# ---------------------------------------------------------------------------

@dataclass
class PreferenceField:
    """Declarative description of a preference field a provider needs.

    The addon dynamically merges these into the AddonPreferences class. The
    runtime attribute name is namespaced as `<provider_id>__<name>` to avoid
    collisions between providers.

    `kind` is one of:
        "string"     plain text
        "password"   hidden text (e.g. API key)
        "enum"       items must be a list of (id, label, description) tuples
        "int"        integer
        "float"      float
        "bool"       checkbox
    """

    name: str
    label: str
    description: str = ""
    kind: str = "string"
    default: object = None
    items: list | None = None  # for enum


# ---------------------------------------------------------------------------
# Provider base class
# ---------------------------------------------------------------------------

class Provider(ABC):
    """Base class for all texture-generation providers.

    Subclasses must set `id` (unique slug) and `label` (UI name) and override
    `capabilities()` and at least one feature method.
    """

    id: str = ""
    label: str = ""

    # ---- declaration -------------------------------------------------------

    @classmethod
    def capabilities(cls) -> set[str]:
        """Return the set of CAP_* this provider supports."""
        return set()

    @classmethod
    def preference_fields(cls) -> list[PreferenceField]:
        """Declare the preference fields this provider exposes in the UI.

        Most providers will return at least an API-key field. The values are
        passed to `__init__` via the `settings` dict.
        """
        return []

    # ---- lifecycle ---------------------------------------------------------

    def __init__(self, settings: dict | None = None):
        """`settings` is a flat dict of {field_name: value} loaded from prefs."""
        self.settings = settings or {}

    # ---- per-feature methods (override what you support) ------------------

    def text2img(self, request: GenerateRequest) -> GenerateResult:
        raise UnsupportedRequestError(
            f"{self.label or self.id} does not support text-to-image"
        )

    def img2img(self, request: GenerateRequest) -> GenerateResult:
        raise UnsupportedRequestError(
            f"{self.label or self.id} does not support image-to-image"
        )

    def inpaint(self, request: GenerateRequest) -> GenerateResult:
        raise UnsupportedRequestError(
            f"{self.label or self.id} does not support inpainting"
        )

    # ---- top-level entry point --------------------------------------------

    def generate(self, request: GenerateRequest) -> GenerateResult:
        """Dispatch to the appropriate feature method.

        Override this only if you need cross-feature logic (e.g. dynamic
        capability switching based on request shape).
        """
        if request.is_inpaint:
            if CAP_INPAINT in self.capabilities():
                return self.inpaint(request)
            # Fall back to img2img — caller is responsible for any
            # client-side mask compositing.
            return self.img2img(request)
        if request.is_img2img:
            return self.img2img(request)
        return self.text2img(request)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, type[Provider]] = {}


def register_provider(cls: type[Provider]) -> type[Provider]:
    """Register a Provider subclass.

    Use as a decorator on the class. Validates that `id` is set.
    """
    if not getattr(cls, "id", ""):
        raise ValueError(f"{cls.__name__}: Provider.id must be set")
    PROVIDERS[cls.id] = cls
    return cls


def get_provider(provider_id: str, settings: dict | None = None) -> Provider:
    """Instantiate a registered provider by id."""
    if provider_id not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{provider_id}'")
    return PROVIDERS[provider_id](settings or {})
