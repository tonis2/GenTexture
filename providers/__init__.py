from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import numpy as np


@dataclass
class GenerateRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    init_image: bytes | None = None      # PNG bytes for img2img
    mask_image: bytes | None = None      # PNG bytes; white=regenerate, black=keep init
    depth_image: bytes | None = None     # PNG bytes for depth conditioning
    normal_image: bytes | None = None    # PNG bytes for normal map conditioning
    strength: float = 0.75              # Denoising strength for img2img/depth
    seed: int | None = None             # None = random


@dataclass
class GenerateResult:
    image_bytes: bytes  # Raw PNG/JPEG bytes (decoded on main thread for thread safety)
    seed: int


class ProviderError(Exception):
    """Base error for provider failures."""
    pass


class AuthenticationError(ProviderError):
    """Invalid or missing API key."""
    pass


class RateLimitError(ProviderError):
    """API rate limit exceeded."""
    pass


class ContentFilterError(ProviderError):
    """Content was filtered by the API's safety system."""
    pass


class Provider(ABC):
    name: str = ""
    supports_depth: bool = False
    supports_img2img: bool = False
    supports_inpaint: bool = False

    @abstractmethod
    def generate(self, request: GenerateRequest, api_key: str) -> GenerateResult:
        ...


PROVIDERS: dict[str, type[Provider]] = {}


def register_provider(cls: type[Provider]) -> type[Provider]:
    PROVIDERS[cls.name] = cls
    return cls
