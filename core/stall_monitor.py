"""Passive page fingerprinting for pressure-test stall observation."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image, UnidentifiedImageError


DEFAULT_HASH_SIZE = 16


@dataclass(frozen=True)
class ImageFingerprint:
    """Fixed-width difference hash for one normalized screenshot."""

    value: int
    bit_count: int

    def distance(self, other: "ImageFingerprint") -> int:
        if self.bit_count != other.bit_count:
            raise ValueError("fingerprints must use the same bit width")
        return bin(self.value ^ other.value).count("1")

    def as_hex(self) -> str:
        width = (self.bit_count + 3) // 4
        return format(self.value, "0%dx" % width)


@dataclass(frozen=True)
class StallObservation:
    """Result of adding one component and screenshot sample."""

    component: str
    fingerprint: ImageFingerprint
    hash_distance: int
    unchanged_seconds: float
    state_changed: bool
    detected: bool


def fingerprint_png(
    content: bytes,
    *,
    hash_size: int = DEFAULT_HASH_SIZE,
    crop_top_ratio: float = 0.06,
    crop_bottom_ratio: float = 0.08,
) -> ImageFingerprint:
    """Return a dHash after excluding common Android system-bar regions."""
    if not content:
        raise ValueError("screenshot content is empty")
    if hash_size < 4 or hash_size > 32:
        raise ValueError("hash_size must be between 4 and 32")
    if not 0.0 <= crop_top_ratio < 0.5 or not 0.0 <= crop_bottom_ratio < 0.5:
        raise ValueError("crop ratios must be between 0 and 0.5")

    try:
        with Image.open(BytesIO(content)) as source:
            image = source.convert("L")
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("screenshot is not a readable image") from exc

    width, height = image.size
    if width < 2 or height < 2:
        raise ValueError("screenshot dimensions are too small")
    top = min(height - 1, max(0, int(height * crop_top_ratio)))
    bottom = max(top + 1, min(height, height - int(height * crop_bottom_ratio)))
    image = image.crop((0, top, width, bottom))

    resampling = getattr(Image, "Resampling", Image)
    image = image.resize((hash_size + 1, hash_size), resampling.LANCZOS)
    pixels = list(image.getdata())
    value = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for column in range(hash_size):
            value <<= 1
            if pixels[offset + column] > pixels[offset + column + 1]:
                value |= 1
    return ImageFingerprint(value=value, bit_count=hash_size * hash_size)


class StallDetector:
    """Detect one prolonged static episode without performing recovery."""

    def __init__(self, unchanged_seconds: float, max_hash_distance: int):
        if unchanged_seconds <= 0:
            raise ValueError("unchanged_seconds must be positive")
        if max_hash_distance < 0:
            raise ValueError("max_hash_distance must be non-negative")
        self.unchanged_seconds = float(unchanged_seconds)
        self.max_hash_distance = int(max_hash_distance)
        self._component: Optional[str] = None
        self._anchor: Optional[ImageFingerprint] = None
        self._static_since: Optional[float] = None
        self._reported = False

    def reset(self) -> None:
        self._component = None
        self._anchor = None
        self._static_since = None
        self._reported = False

    def observe(
        self,
        component: str,
        fingerprint: ImageFingerprint,
        now: float,
    ) -> StallObservation:
        component = str(component).strip()
        if not component:
            raise ValueError("component must not be empty")
        timestamp = float(now)

        state_changed = self._component != component or self._anchor is None
        distance = 0
        if not state_changed and self._anchor is not None:
            distance = self._anchor.distance(fingerprint)
            state_changed = distance > self.max_hash_distance

        if state_changed:
            self._component = component
            self._anchor = fingerprint
            self._static_since = timestamp
            self._reported = False
            distance = 0

        static_since = timestamp if self._static_since is None else self._static_since
        unchanged = max(0.0, timestamp - static_since)
        detected = unchanged >= self.unchanged_seconds and not self._reported
        if detected:
            self._reported = True

        return StallObservation(
            component=component,
            fingerprint=fingerprint,
            hash_distance=distance,
            unchanged_seconds=unchanged,
            state_changed=state_changed,
            detected=detected,
        )
