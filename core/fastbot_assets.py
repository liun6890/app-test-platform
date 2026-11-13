"""Shared Fastbot runtime asset discovery and device ABI helpers."""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Iterable, MutableMapping, Optional


FASTBOT_NATIVE_NAME = "libfastbot_native.so"
FASTBOT_RUNTIME_NAME = "fastbot-runtime.apk"
# AiClient's Android 6+ fallback loader is hard-coded to this APK path.  The
# APK contains classes.dex and the ABI-specific native library, so it is the
# only Fastbot artifact that needs to be pushed to a supported device.
FASTBOT_RUNTIME_REMOTE_PATH = "/data/local/tmp/monkey.apk"
LEGACY_RUNTIME_MD5S = {
    # Runtime before control text input and app-window ownership guards.
    "95d647f3daf8fa138d06b14ec10a8dd3",
    # Split safe-input runtime and the first merged Java-only bundle.
    "f5fc08ba81637c75ecd86d5b393722d7",
    "ccfe577054a3349c97940ee45049aed1",
    # Earlier merged APK before the unrestricted-input patch.
    "1963730138c4e61f908f14e436bbdaef",
    # Runtime built before the Fastbot page-region shield was removed.
    "728b711ad5a72fd5892c07d46a7bc4e6",
    # Unrestricted-input runtime with permission-dialog handoff protection.
    "14c968755e33340f957e81b087d3722c",
    # Runtime with the first status-bar and SystemUI guards.
    "94287293adead33cdbcc92e35818a8be",
    # Runtime with vendor inset and interactive-window detection.
    "ca3e84ae4321f091e047f98bb6ebf7cc",
    # Runtime before the status-bar service guard covered Google Monkey mode.
    "c5f5aa76d27ce6f114c8dac0f526aca6",
    # Runtime before bottom/side system-gesture edges were guarded.
    "ea2d4d7461c3137590898765470ce7ab",
    # First edge-guard runtime; its 24dp bottom fallback was too narrow on vivo.
    "e67cc9eab9c5300cc9caf00d96c6c9d6",
    # Runtime before strict-mode BACK suppression at the input boundary.
    "1cd1722e4abade89fecdefea244fc588",
}
LEGACY_RUNTIME_REMOTE_PATHS = (
    "/sdcard/monkeyq.jar",
    "/sdcard/framework.jar",
    "/sdcard/fastbot-thirdpart.jar",
)
LEGACY_RUNTIME_CLASSPATHS = {
    "/sdcard/monkeyq.jar",
    "/sdcard/monkeyq.jar:/sdcard/framework.jar",
    "/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar",
}
SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ASSET_ROOT = _PROJECT_ROOT / "tools" / "fastbot_android15_patch"


def runtime_asset_path() -> Path:
    """Return the single bundled APK runtime used by device-side Fastbot.

    The bundle contains the safe-input Monkey dex and ABI-specific native
    libraries that older releases required as separate device files.
    """
    return _ASSET_ROOT / FASTBOT_RUNTIME_NAME


def runtime_remote_path() -> str:
    """Return the stable device path for the bundled Java runtime."""
    return FASTBOT_RUNTIME_REMOTE_PATH


def normalize_runtime_classpath(value: str) -> str:
    """Collapse classpaths from pre-bundle configs to the canonical runtime."""
    classpath = str(value or "").strip()
    if classpath in LEGACY_RUNTIME_CLASSPATHS:
        return FASTBOT_RUNTIME_REMOTE_PATH
    return classpath


def normalize_runtime_options(options: MutableMapping[str, object]) -> None:
    """Migrate a pre-bundle Fastbot options mapping in place.

    Existing project JSON files referenced the split Kotlin/Gson dex files and
    pinned the old safe-input jar checksum. Keep those files usable while the
    device now receives only the merged runtime bundle.
    """
    original = str(options.get("classpath") or "").strip()
    normalized = normalize_runtime_classpath(original)
    expected = str(options.get("expected_monkeyq_md5") or "").strip().lower()
    if normalized != original:
        options["classpath"] = normalized
    if normalized == FASTBOT_RUNTIME_REMOTE_PATH and expected in LEGACY_RUNTIME_MD5S:
        bundled = runtime_asset_md5()
        if bundled:
            options["expected_monkeyq_md5"] = bundled


def normalize_abi(value: str) -> Optional[str]:
    """Return the first ABI understood by the bundled Fastbot native assets."""
    candidates = re.split(r"[\s,]+", str(value or "").strip())
    for candidate in candidates:
        if candidate in SUPPORTED_ABIS:
            return candidate
    return None


def native_asset_path(abi: str) -> Path:
    """Return the checked-in native asset path for an ABI.

    The arm64 asset historically lived directly under the patch directory.
    Keep that location as a fallback so an existing checkout can be upgraded
    without requiring a manual rebuild.
    """
    normalized = normalize_abi(abi)
    if not normalized:
        raise ValueError("unsupported Android ABI: %s" % abi)
    path = _ASSET_ROOT / "native" / normalized / FASTBOT_NATIVE_NAME
    if path.is_file():
        return path
    if normalized == "arm64-v8a":
        legacy = _ASSET_ROOT / FASTBOT_NATIVE_NAME
        if legacy.is_file():
            return legacy
    return path


def native_remote_path(abi: str) -> str:
    """Return the device path Fastbot's AiClient resolves for an ABI."""
    normalized = normalize_abi(abi)
    if not normalized:
        raise ValueError("unsupported Android ABI: %s" % abi)
    return "/data/local/tmp/%s/%s" % (normalized, FASTBOT_NATIVE_NAME)


def bundled_native_remote_path(abi: str) -> str:
    """Return AiClient's native-library path inside the one-file runtime."""
    normalized = normalize_abi(abi)
    if not normalized:
        raise ValueError("unsupported Android ABI: %s" % abi)
    return "%s!/lib/%s/%s" % (
        FASTBOT_RUNTIME_REMOTE_PATH,
        normalized,
        FASTBOT_NATIVE_NAME,
    )


def md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def runtime_asset_md5() -> str:
    """Return the checksum of the bundled Java runtime, or an empty string."""
    path = runtime_asset_path()
    return md5(path) if path.is_file() else ""


def runtime_bundle_has_native(abi: str, path: Optional[Path] = None) -> bool:
    """Return whether the bundled APK contains native code for ``abi``."""
    normalized = normalize_abi(abi)
    if not normalized:
        return False
    bundle = path or runtime_asset_path()
    if not bundle.is_file():
        return False
    entry = "lib/%s/%s" % (normalized, FASTBOT_NATIVE_NAME)
    try:
        with zipfile.ZipFile(bundle) as archive:
            info = archive.getinfo(entry)
            return info.file_size > 0 and info.compress_type == zipfile.ZIP_STORED
    except (KeyError, OSError, zipfile.BadZipFile):
        return False


def first_supported_abi(values: Iterable[str]) -> Optional[str]:
    """Select an ABI from one or more getprop values."""
    for value in values:
        result = normalize_abi(value)
        if result:
            return result
    return None
