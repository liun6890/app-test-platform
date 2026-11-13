"""Small pure helpers shared by the console server modules."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional


def safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(value).name).strip("._")
    return cleaned[:160] or fallback


def _ui_bool(value: Any, default: bool) -> bool:
    """Coerce legacy JSON boolean spellings without treating ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resume_mode_from_source(value: Any) -> Optional[str]:
    """Read a resume mode from a config-like mapping.

    Reports and persisted jobs do not all carry the same shape: a live job
    normally points at ``configPath``, while a report embeds a reportable
    ``config.resume`` mapping.  Keep this parser side-effect free so every UI
    surface applies the same policy.
    """
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized if normalized in {"auto", "manual"} else None
    if not isinstance(value, Mapping):
        return None
    raw = value.get("mode")
    if str(raw or "").strip().lower() in {"auto", "manual"}:
        return str(raw).strip().lower()
    nested = value.get("resume")
    if isinstance(nested, Mapping):
        raw = nested.get("mode")
        if str(raw or "").strip().lower() in {"auto", "manual"}:
            return str(raw).strip().lower()
    return None


def _path_is_within(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` is inside ``root`` without string tricks."""
    try:
        candidate.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _draft_integer(value: Any, minimum: int, maximum: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None
