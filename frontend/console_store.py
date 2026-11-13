"""JSON read/write helpers with an mtime-keyed parse cache."""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Tuple

from core.file_io import atomic_write_text


def read_json_file(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Polling caches
#
# The console is polled every few seconds while a run may hold hundreds of
# report/checkpoint files.  Parsing each of them on every request made
# ``/api/runs`` take ~17s and ``/api/devices`` ~20s, which left the UI a full
# poll behind.  These caches are keyed on the file identity (path, mtime, size)
# so an unchanged file is parsed at most once; a running report is re-read as
# soon as its worker writes it.
# ---------------------------------------------------------------------------
_JSON_CACHE: "OrderedDict[Tuple[str, int, int], Any]" = OrderedDict()
_JSON_CACHE_LOCK = threading.Lock()
_JSON_CACHE_MAX_ENTRIES = 512

_CHECKPOINT_CACHE: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
_CHECKPOINT_CACHE_LOCK = threading.Lock()
_CHECKPOINT_CACHE_MAX_ENTRIES = 256

_DEVICE_PROBE_CACHE: Dict[str, Tuple[float, Any]] = {}
_DEVICE_PROBE_LOCK = threading.Lock()
_DEVICE_PROBE_TTL_SECONDS = 10.0
_CHECKPOINT_FILE_NAME = "checkpoint.json"

_REPORT_PATHS_CACHE: Dict[str, Tuple[Tuple[int, int], List[Path]]] = {}
_REPORT_PATHS_LOCK = threading.Lock()

_JOB_EVIDENCE_CACHE: Dict[str, Tuple[Tuple[Any, ...], Dict[str, Any]]] = {}
_JOB_EVIDENCE_LOCK = threading.Lock()

_REPORT_SUMMARY_CACHE: Dict[str, Tuple[Tuple[Any, ...], Any]] = {}
_REPORT_SUMMARY_LOCK = threading.Lock()


def _cached_json_document(path: Path, default: Any = None) -> Any:
    """Return a parsed JSON document, reusing the parse while it is unchanged."""
    try:
        stat = path.stat()
    except OSError:
        return default
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    with _JSON_CACHE_LOCK:
        if key in _JSON_CACHE:
            _JSON_CACHE.move_to_end(key)
            return _JSON_CACHE[key]
    value = read_json_file(path, default)
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[key] = value
        _JSON_CACHE.move_to_end(key)
        while len(_JSON_CACHE) > _JSON_CACHE_MAX_ENTRIES:
            _JSON_CACHE.popitem(last=False)
    return value


def write_json_file(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
    )
