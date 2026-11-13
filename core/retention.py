"""Disk retention for run artifacts and uploaded APKs.

Long-running pressure frameworks accumulate hundreds of gigabytes: every run
keeps per-device reports, screenshots, logcat captures and Fastbot logs.  Left
unmanaged the run directory both fills the disk and slows down every console
poll that has to stat and read it.

This module is deliberately pure: it decides *what* to delete and never touches
a device.  Callers pass the run root, the ids that must survive, and the policy;
the result is a list of directories to remove.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_MAX_TOTAL_GB = 60.0
DEFAULT_UPLOAD_MAX_AGE_DAYS = 30
_ACTIVE_DIR_MARKERS = (
    "checkpoint.json",
    "resume.claim.json",
)


def _directory_size(path: Path) -> int:
    """Sum file sizes with ``os.scandir``; much faster than ``rglob`` here."""
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _job_has_crash(run_dir: Path) -> bool:
    """Return whether one run recorded an application crash.

    ``runtime_failures`` alone is not enough: stalls and permission failures
    create it too, so treating it as crash evidence kept almost every run alive.
    A captured crash page or a non-empty ``crashes`` list in a device report are
    the authoritative markers.
    """
    reports = run_dir / "reports"
    if not reports.is_dir():
        return False
    try:
        if next(reports.rglob("crash_pages"), None) is not None:
            return True
    except OSError:
        return False
    try:
        candidates = list(reports.glob("*/report.json"))
    except OSError:
        return False
    for report_path in candidates:
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("crashes"):
            return True
    return False


def list_run_directories(runs_root: Path) -> List[Path]:
    root = Path(runs_root)
    if not root.is_dir():
        return []
    try:
        return sorted(
            (item for item in root.iterdir() if item.is_dir()),
            key=lambda item: _safe_mtime(item),
        )
    except OSError:
        return []


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def plan_run_cleanup(
    runs_root: Path,
    *,
    protected_ids: Iterable[str] = (),
    keep_crash_runs: bool = True,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_total_gb: float = DEFAULT_MAX_TOTAL_GB,
    now: Optional[float] = None,
) -> List[Path]:
    """Return the run directories that should be deleted, oldest first.

    ``protected_ids`` covers everything that must survive regardless of age:
    the job the console is currently tracking and any run with a live worker
    claim.  The size cap is applied after the age rule, deleting oldest first
    until the remaining tree fits.  Sizes of protected directories still count
    towards the total, otherwise a tree made mostly of crash runs could never
    trigger the cap.
    """
    protected: Set[str] = {str(value) for value in protected_ids if str(value)}
    current = time.time() if now is None else float(now)
    cutoff = current - max(0, int(max_age_days)) * 86400.0
    root = Path(runs_root)
    if not root.is_dir():
        return []

    entries: List[Dict[str, Any]] = []
    for directory in list_run_directories(root):
        try:
            has_checkpoint = any(
                (directory / marker).exists() for marker in _ACTIVE_DIR_MARKERS
            )
        except OSError:
            has_checkpoint = False
        keep = str(directory.name) in protected
        if not keep and keep_crash_runs:
            keep = _job_has_crash(directory)
        entries.append(
            {
                "path": directory,
                "mtime": _safe_mtime(directory),
                "size": _directory_size(directory),
                "protected": keep,
                # A run directory with a live checkpoint is never deleted; the
                # job snapshot may already be gone after a crash of the console.
                "live_claim": has_checkpoint,
            }
        )

    limit_bytes = max(0.0, float(max_total_gb)) * (1024 ** 3)
    total = sum(entry["size"] for entry in entries)
    selected: List[Dict[str, Any]] = []

    def _deletable(entry: Dict[str, Any]) -> bool:
        return not entry["protected"] and not entry["live_claim"]

    # Age rule first: anything older than the cutoff goes, regardless of size.
    for entry in sorted(entries, key=lambda item: item["mtime"]):
        if not _deletable(entry):
            continue
        if entry["mtime"] and entry["mtime"] < cutoff:
            selected.append(entry)
            total -= entry["size"]

    # Then the size cap, oldest first.
    for entry in sorted(entries, key=lambda item: item["mtime"]):
        if total <= limit_bytes:
            break
        if not _deletable(entry) or entry in selected:
            continue
        selected.append(entry)
        total -= entry["size"]

    selected.sort(key=lambda item: item["mtime"])
    return [entry["path"] for entry in selected]


def plan_upload_cleanup(
    upload_root: Path,
    *,
    keep_paths: Iterable[str] = (),
    max_age_days: int = DEFAULT_UPLOAD_MAX_AGE_DAYS,
    now: Optional[float] = None,
) -> List[Path]:
    """Return uploaded APKs that are old enough and not referenced by a run."""
    root = Path(upload_root)
    if not root.is_dir():
        return []
    keep = {str(Path(value).resolve(strict=False)) for value in keep_paths if value}
    current = time.time() if now is None else float(now)
    cutoff = current - max(0, int(max_age_days)) * 86400.0
    stale: List[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for item in entries:
        try:
            if not item.is_file():
                continue
            resolved = str(item.resolve(strict=False))
            if resolved in keep:
                continue
            stat = item.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            stale.append(item)
    return sorted(stale, key=_safe_mtime)


def apply_cleanup(paths: Sequence[Path]) -> int:
    """Delete the given directories/files and return how many were removed."""
    removed = 0
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
            else:
                continue
            removed += 1
        except OSError:
            continue
    return removed


def describe_plan(
    run_dirs: Sequence[Path], upload_files: Sequence[Path]
) -> Dict[str, Any]:
    """Small summary for logs and the CLI's dry-run output."""
    return {
        "runs": [str(path) for path in run_dirs],
        "uploads": [str(path) for path in upload_files],
        "run_count": len(run_dirs),
        "upload_count": len(upload_files),
    }
