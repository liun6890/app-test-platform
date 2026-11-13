"""Durable per-device checkpoints and bounded recovery bookkeeping."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .file_io import atomic_write_text


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_NAME = "checkpoint.json"
RECOVERY_REQUEST_NAME = "resume.request.json"
RESUME_CLAIM_NAME = "resume.claim.json"
MANUALLY_ENDED_STATE = "manually_ended"
RESUMABLE_STATES = frozenset(
    {"recovering", "waiting_for_device", "resuming", "paused", "needs_manual_resume"}
)
AUTOMATIC_RECOVERY_STATES = frozenset({"recovering", "waiting_for_device"})
MANUAL_RECOVERY_STATES = frozenset({"paused", "needs_manual_resume"})
# Reports written by older runners used ``status: completed`` (and some used
# ``completed_with_crashes``) while newer runners put the terminal marker in
# ``resume_state``.  Keep the complete set in one place so a stale heartbeat
# can never turn a terminal report back into an in-flight state.
TERMINAL_RESUME_STATES = frozenset(
    {
        "completed",
        "completed_with_crashes",
        "completed_with_errors",
        "failed",
        "passed",
        # Explicit per-device operator termination.  This is deliberately
        # distinct from the legacy ``stopped`` marker, which still means a
        # resumable/manual pause in older reports.
        "manually_ended",
    }
)
# A report can legitimately carry a generic ``resume_state: completed`` while
# its legacy ``status`` still records the more useful outcome.  Keep the
# stronger marker instead of letting the generic field make a failed run look
# green in the web console.
_TERMINAL_STATE_PRIORITY = {
    "passed": 10,
    "completed": 20,
    "completed_with_crashes": 30,
    "completed_with_errors": 40,
    "failed": 50,
    # Once an operator explicitly ends a device, a late worker heartbeat must
    # never reopen it or replace the stop with a stale success/failure state.
    "manually_ended": 60,
}
_MANUAL_STATE_PRIORITY = {"paused": 10, "needs_manual_resume": 20}
COMPLETED_CYCLE_STATUSES = frozenset(
    {"passed", "completed", "crash", "failed", "error"}
)


def terminal_resume_state(value: Any) -> Optional[str]:
    """Return a normalized terminal state from a report-like mapping.

    A report may contain a stale non-terminal ``resume_state`` alongside a
    terminal ``status``.  Inspect both fields instead of using ``or`` so the
    terminal marker wins during an atomic report/checkpoint replacement.
    """
    if not isinstance(value, Mapping):
        return None
    candidates = [
        str(value.get(key) or "").strip().lower()
        for key in ("resume_state", "status")
        if str(value.get(key) or "").strip().lower() in TERMINAL_RESUME_STATES
    ]
    # A few integrations persist the public ``status: stopped`` together with
    # a boolean marker instead of writing the internal state field.  Normalize
    # that shape to the same immutable terminal state.
    if bool(value.get("manual_end")) and str(value.get("status") or "").strip().lower() == "stopped":
        candidates.append("manually_ended")
    if not candidates:
        return None
    return max(candidates, key=lambda state: _TERMINAL_STATE_PRIORITY.get(state, 0))


def _resume_state_timestamp(value: Any) -> float:
    """Return the newest durable timestamp found on a report-like object."""
    if not isinstance(value, Mapping):
        return 0.0
    latest = 0.0
    for key in (
        "last_heartbeat",
        "saved_at",
        "updated_at",
        "scheduler_updated_at",
        "finished_at",
    ):
        raw = value.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            latest = max(latest, parsed.timestamp())
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return latest


def effective_resume_state(
    report: Any = None,
    checkpoint: Any = None,
    default: str = "running",
) -> str:
    """Resolve the durable state of one device across two atomic files.

    ``report.json`` and ``checkpoint.json`` are replaced independently.  A
    terminal marker therefore always wins over an in-flight heartbeat, manual
    gates win over automatic states, and otherwise the newest timestamp wins.
    The helper is shared by the CLI and web service so they cannot disagree
    about whether a device may be launched again.
    """
    sources = [
        (report if isinstance(report, Mapping) else None, 0),
        (checkpoint if isinstance(checkpoint, Mapping) else None, 1),
    ]

    terminals = [
        (terminal_resume_state(value), _resume_state_timestamp(value), rank)
        for value, rank in sources
        if terminal_resume_state(value)
    ]
    if terminals:
        # A stronger terminal outcome is never downgraded by a stale file.  If
        # both outcomes have the same strength, prefer the newest snapshot and
        # then the full report over the checkpoint.
        state, _timestamp, _rank = max(
            terminals,
            key=lambda item: (
                _TERMINAL_STATE_PRIORITY.get(str(item[0]), 0),
                item[1],
                -item[2],
            ),
        )
        return str(state)

    manual = []
    for value, rank in sources:
        if not isinstance(value, Mapping):
            continue
        for key in ("resume_state", "status"):
            state = str(value.get(key) or "").strip().lower()
            if state in _MANUAL_STATE_PRIORITY:
                manual.append((state, _resume_state_timestamp(value), rank))
    if manual:
        state, _timestamp, _rank = max(
            manual,
            key=lambda item: (
                _MANUAL_STATE_PRIORITY.get(str(item[0]), 0),
                item[1],
                -item[2],
            ),
        )
        return str(state)

    candidates = []
    for value, rank in sources:
        if not isinstance(value, Mapping):
            continue
        state = str(value.get("resume_state") or value.get("status") or "").strip()
        if state:
            candidates.append((state, _resume_state_timestamp(value), rank))
    if candidates:
        state, _timestamp, _rank = max(
            candidates, key=lambda item: (item[1], item[2])
        )
        return str(state)
    return str(default or "running")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    """Return a stable fingerprint without loading a potentially large APK."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        Path(path),
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
    )


def load_checkpoint(
    report_dir: Path,
    *,
    run_id: Optional[str] = None,
    device_serial: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read a checkpoint only when it belongs to the requested device run."""
    value = read_json(Path(report_dir) / CHECKPOINT_NAME)
    if not isinstance(value, Mapping):
        return None
    checkpoint = dict(value)
    if run_id and str(checkpoint.get("job_id") or "") not in {"", str(run_id)}:
        return None
    if device_serial and str(checkpoint.get("device_serial") or "") not in {
        "",
        str(device_serial),
    }:
        return None
    try:
        cycle = int(checkpoint.get("cycle", 0) or 0)
        elapsed = float(checkpoint.get("elapsed_seconds", 0) or 0)
        operations = int(checkpoint.get("operation_count", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if cycle < 0 or elapsed < 0 or operations < 0 or not math.isfinite(elapsed):
        return None
    checkpoint["cycle"] = cycle
    checkpoint["elapsed_seconds"] = elapsed
    checkpoint["operation_count"] = operations
    return checkpoint


def is_completed_cycle(value: Mapping[str, Any]) -> bool:
    if value.get("completed") is True:
        return True
    status = str(value.get("status") or "").strip().lower()
    return status in COMPLETED_CYCLE_STATUSES and not bool(value.get("recoverable"))


def _cycle_number(value: Mapping[str, Any]) -> Optional[int]:
    """Parse a cycle number, returning ``None`` for legacy/unkeyed rows."""
    try:
        number = int(value.get("cycle") or value.get("cycle_number") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _cycle_progress(value: Mapping[str, Any]) -> tuple[float, int, float]:
    """Return a monotonic progress key for duplicate in-flight rows."""
    try:
        elapsed = max(
            0.0,
            float(
                value.get("elapsed_seconds")
                if value.get("elapsed_seconds") is not None
                else value.get("pressure_elapsed_seconds", 0)
            ),
        )
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    try:
        operations = max(0, int(value.get("operation_count", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        operations = 0
    timestamp = _resume_state_timestamp(value)
    return elapsed, operations, timestamp


def unique_cycle_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return cycle rows once per cycle without mutating the report.

    A crash, host restart, or concurrent report replacement can leave the same
    completed cycle in ``cycles`` more than once.  Completed rows are immutable:
    the first completed copy wins.  If an earlier copy is partial, a later
    completed copy replaces it; otherwise the most advanced partial snapshot is
    retained.  Legacy rows without a cycle number remain one-for-one.
    """
    raw_cycles = report.get("cycles", []) if isinstance(report, Mapping) else []
    if not isinstance(raw_cycles, list):
        return []
    rows: list[Mapping[str, Any]] = []
    positions: dict[int, int] = {}
    for item in raw_cycles:
        if not isinstance(item, Mapping):
            continue
        number = _cycle_number(item)
        if number is None:
            rows.append(item)
            continue
        previous_index = positions.get(number)
        if previous_index is None:
            positions[number] = len(rows)
            rows.append(item)
            continue
        previous = rows[previous_index]
        # Preserve the earliest durable terminal row exactly as written.
        if is_completed_cycle(previous):
            continue
        if is_completed_cycle(item) or _cycle_progress(item) > _cycle_progress(previous):
            rows[previous_index] = item
    return rows


# Descriptive alias for callers that prefer the terminology used in reports.
deduplicated_cycle_rows = unique_cycle_rows


def completed_cycle_numbers(report: Mapping[str, Any]) -> set[int]:
    numbers: set[int] = set()
    for value in unique_cycle_rows(report):
        if not isinstance(value, Mapping) or not is_completed_cycle(value):
            continue
        number = _cycle_number(value)
        if number is not None:
            numbers.add(number)
    return numbers


def upsert_completed_cycle(report: Dict[str, Any], cycle: Mapping[str, Any]) -> bool:
    """Insert one terminal cycle while refusing to mutate prior completed data."""
    value = copy.deepcopy(dict(cycle))
    number = int(value.get("cycle", 0) or 0)
    if number <= 0:
        raise ValueError("cycle number must be positive")
    values = report.setdefault("cycles", [])
    if not isinstance(values, list):
        values = []
        report["cycles"] = values
    for index, existing in enumerate(values):
        if not isinstance(existing, Mapping):
            continue
        try:
            existing_number = int(existing.get("cycle", 0) or 0)
        except (TypeError, ValueError):
            continue
        if existing_number != number:
            continue
        if is_completed_cycle(existing):
            return False
        value["completed"] = True
        values[index] = value
        return True
    value["completed"] = True
    values.append(value)
    values.sort(
        key=lambda item: int(item.get("cycle", 0) or 0)
        if isinstance(item, Mapping)
        else 0
    )
    return True


def _flatten_error_text(value: Any, limit: int = 24000) -> str:
    parts: list[str] = []

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 5 or sum(len(part) for part in parts) >= limit:
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if str(key).lower() in {
                    "artifacts",
                    "events",
                    "config",
                    "device_info",
                    "replay_attempts",
                }:
                    continue
                visit(nested, depth + 1)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested, depth + 1)
        elif item is not None:
            text = str(item).strip()
            if text:
                parts.append(text)

    visit(value)
    return "\n".join(parts)[:limit].lower()


def classify_recovery(value: Any, *, manual_stop: bool = False) -> Dict[str, Any]:
    """Classify a worker interruption into one bounded scheduler action."""
    text = _flatten_error_text(value)
    if isinstance(value, Mapping):
        declared = str(
            value.get("resume_state") or value.get("state") or ""
        ).strip().lower()
        if declared == "manually_ended" or (
            bool(value.get("manual_end"))
            and str(value.get("status") or "").strip().lower() == "stopped"
        ):
            return {
                "state": "manually_ended",
                "recoverable": False,
                "automatic": False,
                "reason": "device test was ended manually",
                "category": "manual_end",
            }
    if manual_stop or any(
        marker in text
        for marker in (
            "manual_stop",
            "manual stop",
            "cancelled by user",
            "test run cancelled",
            "pressure stopped by web console",
        )
    ):
        return {
            "state": "paused",
            "recoverable": False,
            "automatic": False,
            "reason": "user requested stop",
            "category": "manual_stop",
        }

    # Error categories that are unsafe to automate always outrank an
    # optimistic ``resume_state`` left by an interrupted worker.  This keeps a
    # stale ``recovering`` marker from turning permission/APK/wait failures
    # into another automatic retry.
    if any(
        marker in text
        for marker in (
            "permission_recovery_failed",
            "permission recovery failed",
            "permission dialog reappeared repeatedly",
            "permission dialog could not be handled",
            "runtime permission recovery failed",
            "permission_recovery_error",
            "permission recovery exhausted",
            "startup gate reappeared repeatedly",
            "startup audit gate could not be completed",
            "audit-gate recovery failed",
        )
    ):
        return {
            "state": "needs_manual_resume",
            "recoverable": False,
            "automatic": False,
            "reason": "permission or startup gate requires operator review",
            "category": "permission_or_gate",
        }
    if any(
        marker in text
        for marker in (
            "apk fingerprint mismatch",
            "apk mismatch",
            "package mismatch",
            "package name mismatch",
            "local apk file does not exist",
            "apk does not exist",
        )
    ):
        return {
            "state": "needs_manual_resume",
            "recoverable": False,
            "automatic": False,
            "reason": "APK identity changed during the job",
            "category": "apk_mismatch",
        }
    if any(
        marker in text
        for marker in (
            "wait_for_device_timeout",
            "device wait timed out",
            "device was not reconnected within",
            "device reconnect timeout",
            "device long-term offline",
        )
    ):
        return {
            "state": "needs_manual_resume",
            "recoverable": False,
            "automatic": False,
            "reason": "device did not reconnect within the configured wait window",
            "category": "device_wait_timeout",
        }

    # Prefer an explicit state/category emitted by a newer worker.  This is
    # useful when the human-readable error is intentionally kept short (for
    # example, a device disappeared before ADB could return stderr).
    declared_state = ""
    declared_category = ""
    if isinstance(value, Mapping):
        declared_state = str(
            value.get("resume_state") or value.get("state") or ""
        ).strip().lower()
        declared_category = str(
            value.get("recovery_category") or value.get("category") or ""
        ).strip().lower()
    if declared_category in {"device_transport", "adb", "adb_timeout", "device_offline"}:
        return {
            "state": "waiting_for_device",
            "recoverable": True,
            "automatic": True,
            "reason": "ADB device transport is temporarily unavailable",
            "category": "device_transport",
        }
    if declared_category in {"foreground", "foreground_recovery"}:
        return {
            "state": "recovering",
            "recoverable": True,
            "automatic": True,
            "reason": "target application foreground recovery timed out",
            "category": "foreground",
        }
    if declared_category in {"pressure_engine", "fastbot", "pressure_engine_exit"}:
        return {
            "state": "recovering",
            "recoverable": True,
            "automatic": True,
            "reason": "Fastbot stopped before the pressure budget completed",
            "category": "pressure_engine",
        }
    if declared_category in {"uiautomator2", "uiautomation", "jsonrpc"}:
        return {
            "state": "recovering",
            "recoverable": True,
            "automatic": True,
            "reason": "uiautomator2 control was temporarily unavailable",
            "category": "uiautomator2",
        }
    if declared_state in AUTOMATIC_RECOVERY_STATES:
        category = "device_transport" if declared_state == "waiting_for_device" else "recovery_state"
        return {
            "state": declared_state,
            "recoverable": True,
            "automatic": True,
            "reason": "worker reported a recoverable interruption",
            "category": category,
        }
    if declared_state in MANUAL_RECOVERY_STATES:
        return {
            "state": declared_state,
            "recoverable": False,
            "automatic": False,
            "reason": "operator action is required before resuming",
            "category": "manual_gate",
        }

    if any(
        marker in text
        for marker in (
            "permission_recovery_failed",
            "permission dialog reappeared repeatedly",
            "permission dialog could not be handled",
            "runtime permission recovery failed",
            "permission_recovery_error",
            "permission recovery exhausted",
            "startup gate reappeared repeatedly",
            "startup audit gate could not be completed",
            "audit-gate recovery failed",
        )
    ):
        return {
            "state": "needs_manual_resume",
            "recoverable": False,
            "automatic": False,
            "reason": "permission or startup gate requires operator review",
            "category": "permission_or_gate",
        }

    if any(
        marker in text
        for marker in (
            "apk fingerprint mismatch",
            "apk mismatch",
            "package mismatch",
            "package name mismatch",
            "local apk file does not exist",
            "apk does not exist",
            "apk fingerprint mismatch after reinstall",
        )
    ):
        return {
            "state": "needs_manual_resume",
            "recoverable": False,
            "automatic": False,
            "reason": "APK identity changed during the job",
            "category": "apk_mismatch",
        }

    if any(
        marker in text
        for marker in (
            "wait_for_device_timeout",
            "device wait timed out",
            "device was not reconnected within",
            "device reconnect timeout",
            "device long-term offline",
        )
    ):
        return {
            "state": "needs_manual_resume",
            "recoverable": False,
            "automatic": False,
            "reason": "device did not reconnect within the configured wait window",
            "category": "device_wait_timeout",
        }

    if any(
        marker in text
        for marker in (
            "device_disconnected",
            "device disconnected",
            "device is offline",
            "device offline",
            "device not found",
            "not listed by the local adb",
            "device is not ready or authorized",
            "device is not ready",
            "no devices/emulators found",
            "transport error",
            "adb transport",
            "adb command failed after",
            "adb timed out",
            "adb timeout",
            "adb command timed out",
            "shell command timed out",
            "timeout waiting for adb",
            "device transport timeout",
            "connection reset by peer",
            "usb transport",
            "adberror",
            "read timed out",
        )
    ):
        return {
            "state": "waiting_for_device",
            "recoverable": True,
            "automatic": True,
            "reason": "ADB device transport is temporarily unavailable",
            "category": "device_transport",
        }

    if any(
        marker in text
        for marker in (
            "foreground_recovery_failed",
            "foreground recovery failed",
            "target app did not return to foreground",
            "target package did not reach the foreground",
            "target application launch failed",
            "launcher",
            "am start",
            "activitymanager",
            "activity manager",
        )
    ):
        return {
            "state": "recovering",
            "recoverable": True,
            "automatic": True,
            "reason": "target application foreground recovery timed out",
            "category": "foreground",
        }

    if any(
        marker in text
        for marker in (
            "pressure_engine_recovery_failed",
            "pressure engine recovery failed",
            "pressure engine failed",
            "pressure_engine_error",
            "pressure_engine_exit",
            "fastbot exited",
            "fastbot exited with return code",
            "fastbot exited with an error",
            "fastbot was stopped",
            "fastbot process ended",
            "monkey process exited",
            "system monkey exited",
            "monkeyq process",
            "pressure engine could not be recovered",
        )
    ):
        return {
            "state": "recovering",
            "recoverable": True,
            "automatic": True,
            "reason": "Fastbot stopped before the pressure budget completed",
            "category": "pressure_engine",
        }

    if any(
        marker in text
        for marker in (
            "uiautomator2",
            "uiautomator2 control unavailable",
            "uiautomator2 could not acquire",
            "uiautomation",
            "jsonrpc",
            "jsonrpcserver",
            "rpcserver",
        )
    ):
        return {
            "state": "recovering",
            "recoverable": True,
            "automatic": True,
            "reason": "uiautomator2 control was temporarily unavailable",
            "category": "uiautomator2",
        }

    return {
        "state": "failed",
        "recoverable": False,
        "automatic": False,
        "reason": "unclassified execution failure",
        "category": "fatal",
    }


class CheckpointHeartbeat:
    """Atomically publish current device progress at a fixed interval."""

    def __init__(
        self,
        report_dir: Path,
        report: Dict[str, Any],
        *,
        interval_seconds: float,
        cycle_seconds: float,
        total_seconds: float,
        apk_fingerprint: str,
        report_lock: Optional[threading.RLock] = None,
        foreground_recovery_strategy: str = "standard_foreground_recovery",
    ) -> None:
        self.report_dir = Path(report_dir)
        self.path = self.report_dir / CHECKPOINT_NAME
        self.report = report
        self.interval_seconds = min(10.0, max(5.0, float(interval_seconds)))
        self.cycle_seconds = max(0.0, float(cycle_seconds))
        self.total_seconds = max(0.0, float(total_seconds))
        self.apk_fingerprint = str(apk_fingerprint)
        self.foreground_recovery_strategy = str(
            foreground_recovery_strategy or "standard_foreground_recovery"
        )
        self.report_lock = report_lock or threading.RLock()
        self._lock = threading.RLock()
        # ``flush`` may be called by the heartbeat thread and by a transition
        # in the foreground worker at the same time.  The report lock
        # serializes the final writes, but without a dedicated flush lock an
        # older snapshot could be prepared first and replace a newer one
        # afterwards.  Keep the whole probe/snapshot/write transaction ordered.
        self._flush_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._provider: Optional[Callable[[], Mapping[str, Any]]] = None
        self._running_since: Optional[float] = None
        self._state: Dict[str, Any] = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "job_id": str(report.get("run_id") or ""),
            "device_serial": str(report.get("device_serial") or ""),
            "package_name": str(report.get("package_name") or ""),
            "cycle": 0,
            "phase": 0,
            "phase_name": "initializing",
            "elapsed_seconds": 0.0,
            "operation_count": 0,
            "fastbot_pid": None,
            "fastbot_alive": None,
            "fastbot_exit_code": None,
            "fastbot_started_at": None,
            "last_operation_at": None,
            "remote_log_path": None,
            "remote_log_offset": 0,
            "logcat_path": str(self.report_dir / "logcat.log"),
            "logcat_offset": 0,
            "foreground_component": None,
            "last_error": None,
            "apk_fingerprint": self.apk_fingerprint,
            "foreground_recovery_strategy": self.foreground_recovery_strategy,
            "resume_state": str(report.get("resume_state") or "running"),
            "resume_attempts": int(report.get("resume_attempts", 0) or 0),
            "worker_pid": os.getpid(),
        }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="checkpoint-%s" % (self._state.get("device_serial") or "device"),
                daemon=True,
            )
            self._thread.start()
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.flush()
            except Exception:
                # The execution path must remain authoritative. A later
                # transition calls flush again and surfaces the write failure.
                continue

    def stop(self, resume_state: Optional[str] = None) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(2.0, self.interval_seconds))
        if resume_state:
            self.update(resume_state=resume_state, running=False, write=False)
        self.flush()

    def update(self, *, write: bool = True, **values: Any) -> None:
        with self._lock:
            # An explicit terminal liveness update must win over the next
            # provider probe. Callers use this transition when Fastbot has
            # exited, so retaining the provider would immediately publish
            # ``fastbot_alive=True`` again during the automatic flush below.
            if values.get("fastbot_alive") is False:
                self._provider = None
            current_cycle = int(self._state.get("cycle", 0) or 0)
            incoming_cycle = current_cycle
            has_cycle = "cycle" in values
            if has_cycle:
                try:
                    incoming_cycle = max(0, int(values.get("cycle", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    values.pop("cycle", None)
                    has_cycle = False
                    incoming_cycle = current_cycle
            stale_cycle = bool(
                has_cycle
                and current_cycle > 0
                and incoming_cycle > 0
                and incoming_cycle < current_cycle
            )
            cycle_changed = bool(has_cycle and incoming_cycle != current_cycle)
            running = values.pop("running", None)
            if cycle_changed:
                # Progress counters are cycle-local. A deliberate transition
                # to cycle zero or to the next cycle may reset them; a late
                # update for an older positive cycle is ignored below.
                self._running_since = None
                # ``pressure_phases`` is also cycle-local.  A caller that is
                # deliberately restoring the same cycle can provide its
                # durable phase list below; otherwise a cycle transition must
                # never leak the previous round's phase history.
                values.setdefault("pressure_phases", [])
            if running is True and self._running_since is None and not stale_cycle:
                self._running_since = time.monotonic()
            elif running is False and not cycle_changed:
                self._state["elapsed_seconds"] = self._elapsed_locked()
                self._running_since = None
            for key, value in values.items():
                if stale_cycle and key in {
                    "cycle",
                    "phase",
                    "phase_name",
                    "elapsed_seconds",
                    "remaining_seconds",
                    "operation_count",
                    "fastbot_pid",
                    "fastbot_alive",
                    "fastbot_exit_code",
                    "fastbot_started_at",
                    "last_operation_at",
                    "remote_log_path",
                    "remote_log_offset",
                    "logcat_offset",
                    "cycle_started_at",
                    "pressure_phases",
                }:
                    continue
                if key in {"elapsed_seconds", "remaining_seconds"}:
                    try:
                        value = max(0.0, float(value))
                    except (TypeError, ValueError):
                        continue
                elif key in {
                    "cycle",
                    "phase",
                    "operation_count",
                    "remote_log_offset",
                    "logcat_offset",
                    "resume_attempts",
                }:
                    try:
                        value = max(0, int(value))
                    except (TypeError, ValueError):
                        continue
                elif key == "pressure_phases":
                    if not isinstance(value, list):
                        continue
                    value = copy.deepcopy(value)
                if not cycle_changed and key in {
                    "elapsed_seconds",
                    "operation_count",
                    "phase",
                }:
                    try:
                        value = max(value, self._state.get(key, 0) or 0)
                    except TypeError:
                        continue
                elif key == "resume_attempts":
                    value = max(value, int(self._state.get(key, 0) or 0))
                elif key == "remaining_seconds" and not cycle_changed:
                    try:
                        current = max(
                            0.0, float(self._state.get(key, value) or 0)
                        )
                        # Remaining time is the inverse of progress. Zero is
                        # authoritative only when it was explicitly observed.
                        value = min(current, value) if key in self._state else value
                    except (TypeError, ValueError, OverflowError):
                        pass
                elif key == "resume_state":
                    current_state = str(self._state.get(key) or "").strip().lower()
                    incoming_state = str(value or "").strip().lower()
                    current_terminal = terminal_resume_state(
                        {"resume_state": current_state}
                    )
                    incoming_terminal = terminal_resume_state(
                        {"resume_state": incoming_state}
                    )
                    if current_terminal:
                        value = effective_resume_state(
                            {"resume_state": current_terminal},
                            {"resume_state": incoming_terminal}
                            if incoming_terminal
                            else None,
                            current_terminal,
                        )
                    elif (
                        current_state in MANUAL_RECOVERY_STATES
                        and incoming_state != "resuming"
                    ):
                        # Only an explicitly claimed resume worker transitions
                        # an operator gate back into the running state.
                        value = current_state
                self._state[key] = value
        if write:
            self.flush()

    def begin_cycle(
        self,
        cycle: int,
        *,
        elapsed_seconds: float = 0.0,
        operation_count: int = 0,
        phase: int = 0,
        started_at: Optional[str] = None,
        resuming: Optional[bool] = None,
        pressure_phases: Optional[Any] = None,
    ) -> None:
        # ``elapsed_seconds`` can legitimately be zero when a worker was
        # interrupted between the durable cycle marker and its first Fastbot
        # heartbeat.  Let the caller explicitly preserve that distinction so
        # the next phase is still recorded as a continuation.
        resumed = (
            bool(resuming)
            if resuming is not None
            else bool(elapsed_seconds or operation_count or phase)
        )
        phases = (
            copy.deepcopy(list(pressure_phases))
            if isinstance(pressure_phases, list)
            else []
        )
        self.update(
            cycle=cycle,
            phase=phase,
            phase_name="resuming" if resumed else "preparing",
            elapsed_seconds=elapsed_seconds,
            operation_count=operation_count,
            pressure_phases=phases,
            cycle_started_at=started_at or utc_now(),
            fastbot_pid=None,
            fastbot_alive=None,
            fastbot_exit_code=None,
            fastbot_started_at=None,
            last_operation_at=None,
            remote_log_path=None,
            remote_log_offset=0,
            waiting_for_device=False,
            wait_started_at=None,
            wait_deadline_at=None,
            last_error=None,
            running=False,
        )

    def begin_phase(
        self,
        phase: int,
        *,
        elapsed_seconds: float,
        operation_count: int,
        logcat_offset: int = 0,
        remote_log_offset: int = 0,
    ) -> None:
        self._provider = None
        self.update(
            phase=phase,
            phase_name="starting_fastbot",
            elapsed_seconds=elapsed_seconds,
            operation_count=operation_count,
            logcat_offset=logcat_offset,
            remote_log_offset=remote_log_offset,
            fastbot_pid=None,
            fastbot_alive=None,
            fastbot_exit_code=None,
            fastbot_started_at=None,
            last_operation_at=None,
            running=False,
        )

    def attach_fastbot(
        self,
        *,
        pid: Any,
        remote_log_path: Optional[str],
        provider: Optional[Callable[[], Mapping[str, Any]]] = None,
    ) -> None:
        with self._lock:
            self._provider = provider
            self._running_since = time.monotonic()
            self._state["fastbot_pid"] = str(pid) if pid not in (None, "") else None
            self._state["fastbot_alive"] = True
            self._state["fastbot_exit_code"] = None
            self._state["fastbot_started_at"] = utc_now()
            self._state["last_operation_at"] = None
            self._state["remote_log_path"] = remote_log_path
            self._state["phase_name"] = "fastbot"
        self.flush()

    def finish_phase(
        self,
        *,
        elapsed_seconds: float,
        operation_count: int,
        remote_log_offset: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> None:
        values: Dict[str, Any] = {
            "elapsed_seconds": elapsed_seconds,
            "operation_count": operation_count,
            "phase_name": "phase_complete",
            "fastbot_pid": None,
            "fastbot_alive": False,
            "running": False,
        }
        if remote_log_offset is not None:
            values["remote_log_offset"] = remote_log_offset
        # ``None`` is meaningful here: a later phase may have recovered an
        # earlier engine failure, so retaining the old value would make the
        # current checkpoint look failed even after a clean handoff.
        values["last_error"] = str(last_error) if last_error else None
        with self._lock:
            self._provider = None
        self.update(**values)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            value = copy.deepcopy(self._state)
            value["elapsed_seconds"] = round(self._elapsed_locked(), 3)
            # ``snapshot`` is also used when an interruption is captured
            # between periodic flushes.  Keep it shape-compatible with the
            # durable checkpoint so recovery evidence never loses the
            # remaining-time field.
            value["remaining_seconds"] = round(
                max(0.0, self.cycle_seconds - value["elapsed_seconds"]), 3
            )
        return value

    def _elapsed_locked(self) -> float:
        elapsed = max(0.0, float(self._state.get("elapsed_seconds", 0) or 0))
        if self._running_since is not None:
            elapsed += max(0.0, time.monotonic() - self._running_since)
        return elapsed

    def flush(self) -> Dict[str, Any]:
        with self._flush_lock:
            provider = self._provider
            provider_values: Mapping[str, Any] = {}
            if provider is not None:
                try:
                    value = provider()
                    if isinstance(value, Mapping):
                        provider_values = value
                except Exception as exc:
                    provider_values = {"heartbeat_probe_error": str(exc)}

            if provider_values:
                # ``operation_count`` is monotonic in ``update``. Record the
                # wall-clock time only when a new operation was observed so a
                # live-but-stuck engine can be distinguished from a healthy
                # running one by the API.
                try:
                    observed_operations = int(
                        provider_values.get("operation_count", 0) or 0
                    )
                    with self._lock:
                        current_operations = int(
                            self._state.get("operation_count", 0) or 0
                        )
                    if observed_operations > current_operations:
                        provider_values = dict(provider_values)
                        provider_values["last_operation_at"] = utc_now()
                except (TypeError, ValueError, OverflowError):
                    pass
                self.update(
                    write=False,
                    **{
                        key: value
                        for key, value in provider_values.items()
                        if value is not None
                    },
                )

            with self._lock:
                checkpoint = copy.deepcopy(self._state)
                checkpoint["elapsed_seconds"] = round(self._elapsed_locked(), 3)
                checkpoint["remaining_seconds"] = round(
                    max(0.0, self.cycle_seconds - checkpoint["elapsed_seconds"]), 3
                )
                checkpoint["saved_at"] = utc_now()
                checkpoint["last_heartbeat"] = checkpoint["saved_at"]

            with self.report_lock:
                # The report and checkpoint are replaced independently.  A
                # worker heartbeat can therefore race the final report write;
                # inspect both durable files before publishing a new snapshot
                # and make terminal state monotonic.
                durable_report = read_json(self.report_dir / "report.json")
                durable_checkpoint = read_json(self.path)

                # A web stop can arrive while this worker is blocked in a
                # reconnect wait.  The service writes a durable manual gate,
                # but this heartbeat thread has its own in-memory state and
                # would otherwise publish ``running`` again on its next tick.
                # Manual gates are monotonic until an explicitly resumed
                # worker changes its in-memory state to ``resuming``.
                manual_candidates = []
                for rank, source in enumerate((durable_report, durable_checkpoint)):
                    if not isinstance(source, Mapping):
                        continue
                    # An explicit terminal marker is handled by the terminal
                    # merge below and must not be downgraded to a resumable
                    # manual pause merely because it also carries
                    # ``manual_stop: true`` for compatibility.
                    if terminal_resume_state(source):
                        continue
                    source_state = str(
                        source.get("resume_state") or source.get("status") or ""
                    ).strip().lower()
                    if not (
                        bool(source.get("manual_stop"))
                        or source_state in MANUAL_RECOVERY_STATES
                    ):
                        continue
                    if source_state not in MANUAL_RECOVERY_STATES:
                        source_state = "paused"
                    manual_candidates.append(
                        (
                            source_state,
                            _resume_state_timestamp(source),
                            rank,
                            source,
                        )
                    )
                durable_manual = (
                    max(
                        manual_candidates,
                        key=lambda item: (
                            _MANUAL_STATE_PRIORITY.get(item[0], 0),
                            item[1],
                            -item[2],
                        ),
                    )
                    if manual_candidates
                    else None
                )
                memory_state = str(
                    self._state.get("resume_state")
                    or self.report.get("resume_state")
                    or ""
                ).strip().lower()
                preserve_manual_gate = bool(
                    durable_manual is not None and memory_state != "resuming"
                )
                if preserve_manual_gate:
                    manual_state, _timestamp, _rank, manual_source = durable_manual
                    manual_error = str(
                        manual_source.get("last_error")
                        or manual_source.get("recovery_reason")
                        or "pressure stopped by web console"
                    )
                    manual_category = str(
                        manual_source.get("recovery_category") or "manual_stop"
                    )
                    checkpoint_manual_fields = {
                        "resume_state": manual_state,
                        "manual_stop": True,
                        "recovery_category": manual_category,
                        "recovery_reason": manual_error,
                        "last_error": manual_error,
                    }
                    checkpoint.update(checkpoint_manual_fields)
                    with self._lock:
                        self._state.update(checkpoint_manual_fields)
                    self.report.update(
                        {
                            "resume_state": manual_state,
                            "status": manual_state,
                            "manual_stop": True,
                            "recovery_category": manual_category,
                            "recovery_reason": manual_error,
                            "last_error": manual_error,
                            "finished_at": None,
                        }
                    )

                # A final report write can race a heartbeat prepared just
                # before the cycle was committed. Import every completed row
                # from disk before publishing the report snapshot so that a
                # stale heartbeat can never delete or rewrite completed work.
                durable_cycles = (
                    durable_report.get("cycles", [])
                    if isinstance(durable_report, Mapping)
                    else []
                )
                memory_cycles = self.report.get("cycles", [])
                if not isinstance(memory_cycles, list):
                    memory_cycles = []
                    self.report["cycles"] = memory_cycles
                if isinstance(durable_cycles, list):
                    for durable_cycle in durable_cycles:
                        if not isinstance(durable_cycle, Mapping) or not is_completed_cycle(
                            durable_cycle
                        ):
                            continue
                        try:
                            number = int(durable_cycle.get("cycle", 0) or 0)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if number <= 0:
                            continue
                        replacement = copy.deepcopy(dict(durable_cycle))
                        replaced = False
                        for index, memory_cycle in enumerate(memory_cycles):
                            if not isinstance(memory_cycle, Mapping):
                                continue
                            try:
                                memory_number = int(memory_cycle.get("cycle", 0) or 0)
                            except (TypeError, ValueError, OverflowError):
                                continue
                            if memory_number == number:
                                # A completed row is immutable.  Keep the
                                # first in-memory terminal copy instead of
                                # replacing richer evidence with an older
                                # atomic snapshot from disk.
                                if is_completed_cycle(memory_cycle):
                                    replaced = True
                                    break
                                memory_cycles[index] = replacement
                                replaced = True
                                break
                        if not replaced:
                            memory_cycles.append(replacement)
                    memory_cycles.sort(
                        key=lambda item: int(item.get("cycle", 0) or 0)
                        if isinstance(item, Mapping)
                        else 0
                    )
                # Canonicalize the shared list in place so the report snapshot
                # and the heartbeat's statistics see the same one-row-per-cycle
                # view without replacing the list object held by callers.
                memory_cycles[:] = list(unique_cycle_rows(self.report))

                # The previous durable heartbeat may have observed slightly
                # more progress than this in-memory snapshot. Merge only when
                # both files describe the same unfinished cycle.
                if isinstance(durable_checkpoint, Mapping):
                    try:
                        durable_cycle_number = int(
                            durable_checkpoint.get("cycle", 0) or 0
                        )
                        checkpoint_cycle_number = int(
                            checkpoint.get("cycle", 0) or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        durable_cycle_number = -1
                        checkpoint_cycle_number = -2
                    if (
                        durable_cycle_number > 0
                        and durable_cycle_number == checkpoint_cycle_number
                    ):
                        for key, caster in (
                            ("phase", int),
                            ("operation_count", int),
                            ("elapsed_seconds", float),
                        ):
                            try:
                                checkpoint[key] = max(
                                    caster(checkpoint.get(key, 0) or 0),
                                    caster(durable_checkpoint.get(key, 0) or 0),
                                )
                            except (TypeError, ValueError, OverflowError):
                                continue
                memory_terminal = terminal_resume_state(self.report)
                report_terminal = terminal_resume_state(durable_report)
                checkpoint_terminal = terminal_resume_state(durable_checkpoint)
                terminal_state = None
                for candidate in (
                    memory_terminal,
                    report_terminal,
                    checkpoint_terminal,
                ):
                    if candidate:
                        terminal_state = effective_resume_state(
                            {"resume_state": terminal_state}
                            if terminal_state
                            else None,
                            {"resume_state": candidate},
                            candidate,
                        )
                preserve_external_report = bool(
                    report_terminal and not memory_terminal
                )
                if terminal_state:
                    checkpoint["resume_state"] = terminal_state
                    checkpoint["remaining_seconds"] = 0.0
                    with self._lock:
                        self._state["resume_state"] = terminal_state

                completed_elapsed = 0.0
                cycles = unique_cycle_rows(self.report)
                if cycles:
                    for cycle in cycles:
                        if not is_completed_cycle(cycle):
                            continue
                        try:
                            completed_elapsed += max(
                                0.0, float(cycle.get("pressure_elapsed_seconds", 0) or 0)
                            )
                        except (TypeError, ValueError):
                            continue
                completed_numbers = completed_cycle_numbers(self.report)
                checkpoint_cycle_number = int(checkpoint.get("cycle", 0) or 0)
                if checkpoint_cycle_number in completed_numbers:
                    checkpoint.update(
                        {
                            "cycle": 0,
                            "phase": 0,
                            "phase_name": "between_cycles",
                            "elapsed_seconds": 0.0,
                            "operation_count": 0,
                            "fastbot_pid": None,
                            "fastbot_alive": None,
                            "fastbot_exit_code": None,
                            "fastbot_started_at": None,
                            "last_operation_at": None,
                            "remote_log_path": None,
                            "remote_log_offset": 0,
                        }
                    )
                    with self._lock:
                        if int(self._state.get("cycle", 0) or 0) == checkpoint_cycle_number:
                            self._state.update(
                                {
                                    "cycle": 0,
                                    "phase": 0,
                                    "phase_name": "between_cycles",
                                    "elapsed_seconds": 0.0,
                                    "operation_count": 0,
                                    "fastbot_pid": None,
                                    "fastbot_alive": None,
                                    "fastbot_exit_code": None,
                                    "fastbot_started_at": None,
                                    "last_operation_at": None,
                                    "remote_log_path": None,
                                    "remote_log_offset": 0,
                                }
                            )
                            self._running_since = None
                    checkpoint_cycle_number = 0
                    current_cycle = self.report.get("current_cycle")
                    if isinstance(current_cycle, Mapping):
                        try:
                            current_cycle_number = int(
                                current_cycle.get("cycle", 0) or 0
                            )
                        except (TypeError, ValueError, OverflowError):
                            current_cycle_number = 0
                        if current_cycle_number in completed_numbers:
                            self.report.pop("current_cycle", None)
                current_elapsed = (
                    checkpoint["elapsed_seconds"]
                    if checkpoint_cycle_number > 0
                    and checkpoint_cycle_number not in completed_numbers
                    else 0.0
                )
                if terminal_state:
                    prior_report_terminal = terminal_resume_state(self.report)
                    self.report["resume_state"] = terminal_state
                    # Keep a more specific legacy run status (for example
                    # ``completed_with_crashes``) when it is already present;
                    # otherwise mirror the terminal resume marker.
                    if prior_report_terminal is None:
                        self.report["status"] = terminal_state
                    self.report["finished_at"] = (
                        durable_report.get("finished_at")
                        if isinstance(durable_report, Mapping)
                        else self.report.get("finished_at")
                    ) or self.report.get("finished_at") or utc_now()
                else:
                    self.report["resume_state"] = str(
                        checkpoint.get("resume_state")
                        or self.report.get("resume_state")
                        or "running"
                    )
                # Resume attempts are also monotonic across a report loaded
                # during a host restart.  Never let a stale heartbeat reduce
                # the durable count.
                try:
                    self.report["resume_attempts"] = max(
                        int(self.report.get("resume_attempts", 0) or 0),
                        int(checkpoint.get("resume_attempts", 0) or 0),
                    )
                except (TypeError, ValueError, OverflowError):
                    self.report["resume_attempts"] = int(
                        checkpoint.get("resume_attempts", 0) or 0
                    )
                self.report["remaining_seconds"] = 0.0 if terminal_state else round(
                    max(0.0, self.total_seconds - completed_elapsed - current_elapsed),
                    3,
                )
                self.report["last_heartbeat"] = checkpoint["last_heartbeat"]
                self.report["apk_fingerprint"] = self.apk_fingerprint
                self.report["foreground_recovery_strategy"] = str(
                    checkpoint.get("foreground_recovery_strategy")
                    or self.foreground_recovery_strategy
                )
                if (
                    int(checkpoint.get("cycle", 0) or 0) > 0
                    and terminal_resume_state(self.report) is None
                ):
                    self.report["current_cycle"] = {
                        "cycle": int(checkpoint["cycle"]),
                        "phase": int(checkpoint.get("phase", 0) or 0),
                        "started_at": checkpoint.get("cycle_started_at"),
                        "status": self.report["resume_state"],
                        "pressure_elapsed_seconds": checkpoint["elapsed_seconds"],
                        "remaining_seconds": checkpoint["remaining_seconds"],
                        "operation_count": int(checkpoint.get("operation_count", 0) or 0),
                        "last_error": checkpoint.get("last_error"),
                        "foreground_recovery_strategy": checkpoint.get(
                            "foreground_recovery_strategy"
                        ),
                    }
                report_snapshot = (
                    None if preserve_external_report else copy.deepcopy(self.report)
                )
                write_json(self.path, checkpoint)
                if report_snapshot is not None:
                    write_json(self.report_dir / "report.json", report_snapshot)
            return checkpoint


def create_recovery_attempt(
    report_dir: Path,
    attempt: int,
    *,
    state: str,
    error: Optional[str],
    checkpoint: Optional[Mapping[str, Any]],
    adb_error: Optional[str] = None,
) -> Path:
    """Create a unique evidence directory that later retries cannot replace."""
    root = Path(report_dir) / "recovery_attempts"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = "attempt_%03d_%s" % (max(0, int(attempt)), stamp)
    directory = root / base
    suffix = 1
    while True:
        try:
            directory.mkdir(parents=False, exist_ok=False)
            break
        except FileExistsError:
            directory = root / ("%s_%02d" % (base, suffix))
            suffix += 1
    checkpoint_value = dict(checkpoint or {})
    recovery_started_at = utc_now()
    manifest = {
        "schema_version": 1,
        "attempt": max(0, int(attempt)),
        "state": str(state),
        "started_at": recovery_started_at,
        "fastbot_pid": checkpoint_value.get("fastbot_pid"),
        "worker_pid": checkpoint_value.get("worker_pid") or os.getpid(),
        "remote_log_path": checkpoint_value.get("remote_log_path"),
        "remote_log_offset": checkpoint_value.get("remote_log_offset", 0),
        "logcat_path": checkpoint_value.get("logcat_path"),
        "logcat_offset": checkpoint_value.get("logcat_offset", 0),
        "cleanup_before_pid_snapshot": {},
        "cleanup_after_pid_snapshot": {},
        "recovery_started_at": recovery_started_at,
        "recovery_finished_at": None,
        "foreground_component": checkpoint_value.get("foreground_component"),
        "error": str(error or ""),
        # Keep the transport failure explicit even when the higher-level
        # decision was a foreground, permission, or engine recovery.  The
        # companion text file is filled by the evidence collector.
        "adb_error": str(adb_error or checkpoint_value.get("adb_error") or ""),
        "adb_error_path": None,
        "checkpoint_before": copy.deepcopy(checkpoint_value),
        "timeline": [
            {
                "at": recovery_started_at,
                "event": "recovery_attempt_created",
                "state": str(state),
            }
        ],
        "artifacts": {},
        "errors": [],
    }
    write_json(directory / "recovery.json", manifest)
    return directory


def update_recovery_attempt(directory: Path, **values: Any) -> Dict[str, Any]:
    path = Path(directory) / "recovery.json"
    current = read_json(path, {})
    manifest = dict(current) if isinstance(current, Mapping) else {}
    timeline = manifest.setdefault("timeline", [])
    event = values.pop("timeline_event", None)
    if event:
        if not isinstance(timeline, list):
            timeline = []
            manifest["timeline"] = timeline
        timeline.append({"at": utc_now(), **dict(event)})
    for key, value in values.items():
        if key == "errors" and isinstance(value, Mapping):
            existing_errors = manifest.setdefault("errors", [])
            if isinstance(existing_errors, list):
                existing_errors.extend(
                    {"source": str(error_key), "error": str(error_value)}
                    for error_key, error_value in value.items()
                )
                continue
            if isinstance(existing_errors, dict):
                existing_errors.update(copy.deepcopy(dict(value)))
                continue
        if key == "artifacts" and isinstance(value, Mapping):
            existing = manifest.setdefault(key, {})
            if isinstance(existing, dict):
                existing.update(copy.deepcopy(dict(value)))
                continue
        manifest[key] = copy.deepcopy(value)
    manifest["updated_at"] = utc_now()
    write_json(path, manifest)
    return manifest


def copy_tail(source: Path, destination: Path, max_bytes: int = 512 * 1024) -> bool:
    try:
        with Path(source).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max(1, int(max_bytes))))
            content = stream.read()
        Path(destination).write_bytes(content)
        return True
    except OSError:
        return False


def recovery_request_path(report_dir: Path) -> Path:
    return Path(report_dir) / RECOVERY_REQUEST_NAME


def request_manual_resume(report_dir: Path, requested_by: str = "web") -> Dict[str, Any]:
    request = {
        "request_id": hashlib.sha256(
            (str(report_dir) + utc_now()).encode("utf-8")
        ).hexdigest()[:20],
        "requested_at": utc_now(),
        "requested_by": str(requested_by),
    }
    write_json(recovery_request_path(report_dir), request)
    return request


def consume_manual_resume_request(report_dir: Path) -> Optional[Dict[str, Any]]:
    path = recovery_request_path(report_dir)
    value = read_json(path)
    if not isinstance(value, Mapping):
        return None
    consumed = dict(value)
    try:
        path.unlink()
    except OSError:
        return None
    return consumed


def resume_claim_path(report_dir: Path) -> Path:
    """Return the durable per-device worker claim path."""
    return Path(report_dir) / RESUME_CLAIM_NAME


def _process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if value <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) can terminate the queried process on Windows. A
        # resume claim can name this web service itself before Popen, so use
        # a read-only process handle instead of sending any signal.
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            get_exit_code = kernel32.GetExitCodeProcess
            close_handle = kernel32.CloseHandle
            open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            open_process.restype = ctypes.c_void_p
            get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            get_exit_code.restype = ctypes.c_int
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            handle = open_process(0x1000, 0, value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                # Access denied still establishes a live owner; do not let a
                # second controller steal its claim under a different token.
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_uint32()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                close_handle(handle)
        except (AttributeError, OSError, SystemError, TypeError, ValueError, OverflowError):
            return False
    try:
        os.kill(value, 0)
    except PermissionError:
        # The process exists but is owned by a different token. Treat that as
        # live; replacing its claim could start two device controllers.
        return True
    except (OSError, ProcessLookupError, SystemError, ValueError, OverflowError):
        # Windows may report stale/invalid PIDs as WinError 87 (and some
        # Python builds wrap that result in ``SystemError``).  Claims are
        # advisory recovery guards, so treat such a PID as gone rather than
        # crashing startup or the scheduler.
        return False
    return True


def resume_claim_active(
    report_dir: Path,
    *,
    job_id: Optional[str] = None,
    device_serial: Optional[str] = None,
    stale_after_seconds: float = 300.0,
) -> bool:
    """Check whether a durable claim still belongs to a live process."""
    value = read_json(resume_claim_path(report_dir))
    if not isinstance(value, Mapping):
        return False
    if job_id and str(value.get("job_id") or "") not in {"", str(job_id)}:
        return False
    if device_serial and str(value.get("device_serial") or "") not in {
        "",
        str(device_serial),
    }:
        return False
    # Once a worker pid has been attached, it is authoritative.  The process
    # that created the claim may remain alive for the whole service lifetime
    # and must not keep a completed/dead worker claim locked.  Older claim
    # files did not have ``worker_pid``; those still use the creator pid while
    # the short lease covers the small window before a child is spawned.
    worker_pid = value.get("worker_pid")
    worker_attached = worker_pid not in (None, "", 0)
    if worker_attached:
        return _process_alive(worker_pid)
    if _process_alive(value.get("owner_pid")):
        return True
    requested_at = value.get("requested_at")
    if requested_at:
        try:
            requested = datetime.fromisoformat(
                str(requested_at).replace("Z", "+00:00")
            ).timestamp()
            return time.time() - requested < max(1.0, float(stale_after_seconds))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return False


def acquire_resume_claim(
    report_dir: Path,
    *,
    job_id: str,
    device_serial: str,
    requested_by: str = "web",
    stale_after_seconds: float = 300.0,
) -> Optional[Dict[str, Any]]:
    """Atomically claim one device for a resume invocation.

    A stale claim is removed only after both recorded process ids are gone (or
    its bounded lease has expired), then the exclusive create is retried once.
    """
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = resume_claim_path(directory)
    payload = {
        "schema_version": 1,
        "claim_id": hashlib.sha256(
            (str(directory) + str(job_id) + str(device_serial) + utc_now()).encode(
                "utf-8"
            )
        ).hexdigest()[:24],
        "job_id": str(job_id),
        "device_serial": str(device_serial),
        "requested_by": str(requested_by),
        "requested_at": utc_now(),
        "owner_pid": os.getpid(),
        "worker_pid": None,
    }
    for attempt in range(2):
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(
                os.fspath(path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
            return payload
        except FileExistsError:
            if resume_claim_active(
                directory,
                job_id=job_id,
                device_serial=device_serial,
                stale_after_seconds=stale_after_seconds,
            ):
                return None
            try:
                path.unlink()
            except OSError:
                return None
        except OSError:
            return None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return None


def update_resume_claim(
    report_dir: Path,
    claim_id: str,
    **values: Any,
) -> Optional[Dict[str, Any]]:
    """Attach the spawned CLI PID (or other metadata) to an existing claim."""
    path = resume_claim_path(report_dir)
    current = read_json(path)
    if not isinstance(current, Mapping) or str(current.get("claim_id") or "") != str(
        claim_id
    ):
        return None
    updated = dict(current)
    updated.update(copy.deepcopy(values))
    updated["updated_at"] = utc_now()
    write_json(path, updated)
    return updated


def release_resume_claim(report_dir: Path, claim_id: Optional[str] = None) -> bool:
    """Release a claim only when it still belongs to the caller's invocation."""
    path = resume_claim_path(report_dir)
    if claim_id:
        current = read_json(path)
        if not isinstance(current, Mapping) or str(current.get("claim_id") or "") != str(
            claim_id
        ):
            return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
