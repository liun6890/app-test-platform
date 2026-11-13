"""Small runtime helpers shared by the pressure-test entry point."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from core.configuration import resolve_project_path


_REPORT_LOCKS_GUARD = threading.Lock()
_REPORT_LOCKS: Dict[str, threading.RLock] = {}
_SERIAL_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def utc_timestamp() -> str:
    """Return a sortable UTC timestamp with microsecond uniqueness."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def safe_serial(serial: str) -> str:
    """Convert an ADB serial into a filesystem-safe report component."""
    return _SERIAL_SAFE.sub("_", str(serial))[:120] or "unknown-device"


def _report_write_lock(report_dir: Path) -> threading.RLock:
    key = str(Path(report_dir).resolve(strict=False))
    with _REPORT_LOCKS_GUARD:
        return _REPORT_LOCKS.setdefault(key, threading.RLock())


def report_directory(config: Mapping[str, Any], run_id: str, serial: str) -> Path:
    """Build the per-device report directory without accepting a device path."""
    paths = config.get("paths", {})
    reports_root = (
        str(paths.get("reports_dir", "./reports"))
        if isinstance(paths, Mapping)
        else "./reports"
    )
    directory = resolve_project_path(reports_root) / (
        "%s_%s" % (run_id, safe_serial(serial))
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _check_cancel(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("test run cancelled")


_LOG_SEGMENT_LIMIT = 512 * 1024
# Live polling intentionally stays bounded to keep ADB/logcat probes cheap.
# Phase finalization uses a chunked scan instead, so a crash in the middle of a
# long log cannot be hidden by the tail window.  The overlap keeps a multiline
# Android crash/process block intact when it crosses a chunk boundary.
_LOG_SCAN_CHUNK_SIZE = 256 * 1024
_LOG_SCAN_OVERLAP = 128 * 1024


def _log_segment_start(path: Path, offset: int, limit: int = _LOG_SEGMENT_LIMIT) -> int:
    """Return a valid incremental-log start, including after log rotation.

    Logcat collectors commonly truncate and recreate their output file between
    phases.  Reusing an old offset in that situation seeks past EOF and silently
    drops the only crash block we have.  Treat a shorter file as a new segment.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    try:
        previous = max(0, int(offset))
    except (TypeError, ValueError, OverflowError):
        previous = 0
    if previous > size:
        previous = 0
    return max(previous, max(0, size - max(1, int(limit))))


def _read_log_segment(path: Path, offset: int) -> str:
    """Read only the bounded bytes written for the current cycle."""
    try:
        with path.open("rb") as handle:
            handle.seek(_log_segment_start(path, offset))
            return handle.read(_LOG_SEGMENT_LIMIT).decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""
