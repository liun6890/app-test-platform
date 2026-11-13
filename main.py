"""Application pressure-test orchestration for local APKs and attached devices.

The module deliberately talks only to devices already visible through the local
ADB server.  It never fetches an APK from a network or connects to a remote device.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import math
import multiprocessing as mp
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # Support both ``python main.py`` and ``python -m app_press_test.main``.
    from .core.configuration import (
        ConfigurationError,
        MAX_SAFE_WORKERS,
        load_config,
        reportable_config,
        require_local_apk,
        resolve_project_path,
    )
    from .core.checkpoint import (
        AUTOMATIC_RECOVERY_STATES,
        MANUALLY_ENDED_STATE,
        MANUAL_RECOVERY_STATES,
        TERMINAL_RESUME_STATES,
        CheckpointHeartbeat,
        classify_recovery,
        completed_cycle_numbers,
        consume_manual_resume_request,
        copy_tail,
        create_recovery_attempt,
        unique_cycle_rows,
        effective_resume_state,
        is_completed_cycle,
        load_checkpoint,
        read_json as read_checkpoint_json,
        sha256_file,
        terminal_resume_state,
        update_recovery_attempt,
        upsert_completed_cycle,
        write_json as write_checkpoint_json,
    )
    from .core.crash_handler import CrashHandler
    from .core.device_manager import DeviceManager
    from .core.executor import PressureEngineError, TestExecutor
    from .core.mutex_controller import MutexController
    from .core.reporting import ReportGenerator, close_logging_handlers, configure_logging
    from .core.replay_plan import build_replay_trajectory
    from .core.stall_monitor import ImageFingerprint, fingerprint_png
    from .core.state_manager import StateManager
    from .scripts.test_scenario import prepare_test_scenario
except ImportError:  # pragma: no cover - exercised by direct script execution
    from core.configuration import (  # type: ignore
        ConfigurationError,
        MAX_SAFE_WORKERS,
        load_config,
        reportable_config,
        require_local_apk,
        resolve_project_path,
    )
    from core.checkpoint import (  # type: ignore
        AUTOMATIC_RECOVERY_STATES,
        MANUALLY_ENDED_STATE,
        MANUAL_RECOVERY_STATES,
        TERMINAL_RESUME_STATES,
        CheckpointHeartbeat,
        classify_recovery,
        completed_cycle_numbers,
        consume_manual_resume_request,
        copy_tail,
        create_recovery_attempt,
        unique_cycle_rows,
        effective_resume_state,
        is_completed_cycle,
        load_checkpoint,
        read_json as read_checkpoint_json,
        sha256_file,
        terminal_resume_state,
        update_recovery_attempt,
        upsert_completed_cycle,
        write_json as write_checkpoint_json,
    )
    from core.crash_handler import CrashHandler  # type: ignore
    from core.device_manager import DeviceManager  # type: ignore
    from core.executor import PressureEngineError, TestExecutor  # type: ignore
    from core.mutex_controller import MutexController  # type: ignore
    from core.reporting import (  # type: ignore
        ReportGenerator,
        close_logging_handlers,
        configure_logging,
        describe_permission_failure,
    )
    from core.replay_plan import build_replay_trajectory  # type: ignore
    from core.stall_monitor import ImageFingerprint, fingerprint_png  # type: ignore
    from core.state_manager import StateManager  # type: ignore
    from scripts.test_scenario import prepare_test_scenario  # type: ignore


from runtime_utils import (  # noqa: F401 - re-exported for tests and patching
    _LOG_SCAN_CHUNK_SIZE, _LOG_SCAN_OVERLAP, _LOG_SEGMENT_LIMIT, _REPORT_LOCKS,
    _REPORT_LOCKS_GUARD, _SERIAL_SAFE, _check_cancel, _log_segment_start, _read_log_segment,
    _report_write_lock, report_directory, safe_serial, utc_timestamp,
)

logger = logging.getLogger(__name__)
_WAIT_COMPLETED = "completed"
_WAIT_CRASH = "crash"
_WAIT_MANUAL_STOP = "manual_stop"
_WAIT_STALL_RECOVERY = "stall_recovery"
_WAIT_STALL_RECOVERY_UNCLEAN = "stall_recovery_unclean"
_WAIT_AUDIT_RECOVERY = "audit_recovery"
_WAIT_AUDIT_RECOVERY_UNCLEAN = "audit_recovery_unclean"
_WAIT_PERMISSION_RECOVERY = "permission_recovery"
_WAIT_PERMISSION_RECOVERY_UNCLEAN = "permission_recovery_unclean"
_WAIT_FOREGROUND_RECOVERY = "foreground_recovery"
_WAIT_PRESSURE_ENGINE_EXIT = "pressure_engine_exit"
_WAIT_PHASE_DEADLINE = "phase_deadline"
_WAIT_PROCESS_EXIT = "app_process_exit"
_WAIT_DEVICE_DISCONNECTED = "device_disconnected"
_MAX_REPLAY_EVENTS = 20000
# Each worker owns a Fastbot process, a screenshot collector, a logcat pipe,
# and a uiautomator2 connection. Keep the scheduler bounded at the supported
# nine-device rig limit so all selected devices can run in parallel.
_MAX_DEVICE_WORKERS = MAX_SAFE_WORKERS
_WORKER_SHUTDOWN_GRACE_SECONDS = 8.0
_WORKER_JOIN_TIMEOUT_SECONDS = 5.0
# The remote cleanup path includes several bounded ADB probes (Fastbot PID,
# uiautomator2 PID, and UiAutomation registration).  Five seconds is shorter
# than a normal release verification on a busy device and turns a healthy
# handoff into a false manual-resume gate.  The cleanup still runs in a daemon
# thread, so this is only the maximum wait for the scheduler's status sample.
_REMOTE_CLEANUP_TIMEOUT_SECONDS = 30.0
_DEVICE_RECONNECT_POLL_SECONDS = 2.0
_REMOTE_TRANSPORT_PROBE_TIMEOUT_SECONDS = 2.0
_REMOTE_CLEANUP_RELEASED = "released"
_REMOTE_CLEANUP_DEVICE_UNAVAILABLE = "device_unavailable"
_REMOTE_CLEANUP_FAILED = "cleanup_failed"
_PARENT_WATCH_INTERVAL_SECONDS = 2.0
_RUNTIME_RECOVERY_ATTEMPTS = 3
_RUNTIME_RECOVERY_AUDIT_ACTIONS = frozenset(
    {"complete_audit_gate", "handle_audit_gate", "audit_gate"}
)
# ``multiprocessing.Queue`` uses a feeder thread in each spawned worker.  A
# worker can therefore be joined before its final result has reached the
# parent's pipe.  Give that result a short, bounded window to arrive before
# synthesizing a failure row.
_WORKER_RESULT_GRACE_SECONDS = 1.5
_RESUME_BACKOFF_SECONDS = (10.0, 30.0, 60.0)
_SCHEDULER_POLL_SECONDS = 0.2


class _DeviceManuallyEnded(RuntimeError):
    """Internal control-flow signal for an operator-ended device worker."""

_FASTBOT_EVENT_PATTERN = re.compile(
    r"\[Fastbot\]\[(?P<timestamp>\d{4}-\d{2}-\d{2} "
    r"\d{2}:\d{2}:\d{2}\.\d{3})\].*?// Event id:\s*(?P<event_id>\d+)",
    re.IGNORECASE,
)


def _scan_log_for_detection(
    path: Optional[Path],
    offset: int,
    detector: Optional[Callable[[str], Any]],
    *,
    chunk_size: int = _LOG_SCAN_CHUNK_SIZE,
    overlap: int = _LOG_SCAN_OVERLAP,
) -> Optional[Dict[str, Any]]:
    """Scan a log range incrementally and return its last matching detection.

    ``_read_log_segment`` is deliberately a tail-only helper for high-frequency
    probes.  It is not sufficient at phase finalization: a busy device can
    append several megabytes after a crash before the host gets to stop the
    collector.  This helper keeps memory bounded while inspecting the complete
    range from the collector's capture boundary.  The detector is called on a
    rolling overlap so multiline records remain parseable across reads.
    """
    if path is None or not callable(detector):
        return None
    try:
        file_size = int(path.stat().st_size)
        requested_offset = max(0, int(offset or 0))
        # A truncated/recreated collector file starts a new segment.
        start = 0 if requested_offset > file_size else requested_offset
        size = max(1024, int(chunk_size))
        keep = max(0, min(size - 1, int(overlap)))
    except (OSError, TypeError, ValueError, OverflowError):
        return None

    latest: Optional[Dict[str, Any]] = None
    carry = b""
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            while True:
                chunk = handle.read(size)
                if not chunk:
                    break
                payload = carry + chunk
                text = payload.decode("utf-8", errors="replace")
                try:
                    value = detector(text)
                except Exception:
                    # A malformed partial record must not prevent later chunks
                    # from being inspected.  Keep the diagnostic at debug level
                    # because this runs during best-effort phase finalization.
                    logger.debug("Log detection failed for %s", path, exc_info=True)
                    value = None
                if isinstance(value, Mapping):
                    latest = dict(value)
                carry = payload[-keep:] if keep else b""
    except (FileNotFoundError, OSError, UnicodeError):
        logger.debug("Unable to scan log %s for runtime evidence", path, exc_info=True)
        return latest
    return latest


def _attach_phase_failure_context(
    error: BaseException,
    *,
    trajectory: Optional[Mapping[str, Any]],
    report_dir: Path,
    cycle: int,
    log_path: Path,
    log_offset: int,
    pressure_log_path: Path,
    pressure_log_offset: int,
    phase_started_at: Optional[str] = None,
    phase_started_clock: Optional[float] = None,
    executor: Any = None,
) -> None:
    """Attach best-effort phase artifacts to an exception before it escapes.

    Fastbot can fail before the normal phase finalizer reaches its return
    statement.  Callers still need the byte ranges and partial event state in
    order to build a durable failed-cycle row.  Exception attributes are used
    as a compatibility bridge because the public phase API intentionally keeps
    raising the original exception.
    """
    values: Dict[str, Any] = (
        dict(trajectory) if isinstance(trajectory, Mapping) else {}
    )
    values.setdefault("exception_type", type(error).__name__)
    values.setdefault("exception", str(error) or type(error).__name__)
    values.setdefault("phase_failure", True)
    values.setdefault("engine", getattr(executor, "pressure_engine", "fastbot"))

    # Preserve executor-side detections that were already observed by the
    # foreground/crash monitors before the phase raised.
    for attribute, key in (
        ("app_crash_detection", "app_crash"),
        ("app_process_exit_detection", "app_process_exit"),
        ("device_disconnect_detection", "device_disconnect"),
        ("app_crash_page_detection", "app_crash_page"),
    ):
        try:
            detected = getattr(executor, attribute, None)
            detected = detected() if callable(detected) else detected
        except Exception:
            detected = None
        if isinstance(detected, Mapping) and key not in values:
            values[key] = dict(detected)

    log_content = _read_log_segment(log_path, log_offset)
    pressure_content = _read_log_segment(pressure_log_path, pressure_log_offset)
    finished_at = datetime.now(timezone.utc).isoformat()
    started_clock = phase_started_clock
    elapsed = (
        round(max(0.0, time.monotonic() - started_clock), 3)
        if started_clock is not None
        else None
    )
    pressure_end = (
        pressure_log_path.stat().st_size
        if pressure_log_path.exists()
        else max(0, int(pressure_log_offset or 0))
    )
    values.setdefault(
        "phase",
        {
            "cycle": cycle,
            "phase": values.get("phase_number"),
            "started_at": phase_started_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed,
            "engine_elapsed_seconds": elapsed,
            "stop_reason": str(values.get("stop_reason") or "exception"),
            "log_provenance": {
                "path": str(pressure_log_path),
                "start_offset": max(0, int(pressure_log_offset or 0)),
                "end_offset": pressure_end,
                "bytes_added": max(
                    0, pressure_end - max(0, int(pressure_log_offset or 0))
                ),
                "current_run_only": False,
            },
        },
    )

    artifacts: Dict[str, Any] = {}
    prior_artifacts = values.get("artifacts")
    if isinstance(prior_artifacts, Mapping):
        artifacts.update(dict(prior_artifacts))
    if log_path.is_file():
        artifacts.setdefault("logcat", str(log_path))
    if pressure_log_path.is_file():
        artifacts.setdefault("fastbot", str(pressure_log_path))
    stall_dir = report_dir / "stall_events"
    if stall_dir.is_dir():
        artifacts.setdefault("stall_events", str(stall_dir))
    values["artifacts"] = artifacts

    context = {
        "trajectory": values,
        "log_content": log_content,
        "pressure_log_content": pressure_content,
        "artifacts": artifacts,
        "log_path": str(log_path),
        "pressure_log_path": str(pressure_log_path),
        "log_offset": max(0, int(log_offset or 0)),
        "pressure_log_offset": max(0, int(pressure_log_offset or 0)),
    }
    try:
        setattr(error, "_phase_failure_context", context)
        # Keep these aliases for adapters that inspect the exception directly.
        setattr(error, "_phase_trajectory", values)
        setattr(error, "_phase_log_content", log_content)
        setattr(error, "_phase_pressure_log_content", pressure_content)
        setattr(error, "_phase_artifacts", artifacts)
    except Exception:
        # Built-in exceptions are normally mutable, but a third-party exception
        # may use slots.  The caller's file-based fallback still works.
        pass


def _phase_failure_snapshot(
    error: BaseException,
    *,
    state_manager: Any,
    executor: Any,
    report_dir: Path,
    cycle: int,
    trajectory: Optional[Mapping[str, Any]] = None,
    artifacts: Optional[Mapping[str, Any]] = None,
    phase_attempted: Optional[bool] = None,
) -> Tuple[Dict[str, Any], str, str, Dict[str, Any]]:
    """Recover partial phase state from an exception and existing artifacts."""
    raw_context = getattr(error, "_phase_failure_context", None)
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    # A preflight/preparation exception can share a report directory with an
    # earlier invocation.  Do not turn those old log files into a new crash.
    # An explicit context attached by the phase runner is authoritative even
    # when a compatibility caller forgot to pass ``phase_attempted``.
    phase_context_present = isinstance(raw_context, Mapping)
    current_phase_active = phase_attempted is not False or phase_context_present
    values: Dict[str, Any] = {}
    contextual = context.get("trajectory")
    if isinstance(contextual, Mapping):
        values.update(dict(contextual))
    if isinstance(trajectory, Mapping):
        values.update(dict(trajectory))
    values.setdefault("exception_type", type(error).__name__)
    values.setdefault("exception", str(error) or type(error).__name__)
    values.setdefault("phase_failure", True)
    values.setdefault("engine", getattr(executor, "pressure_engine", "fastbot"))

    log_path = report_dir / "logcat.log"
    pressure_path = report_dir / "fastbot.log"
    log_offset = context.get("log_offset", 0)
    pressure_offset = context.get("pressure_log_offset", 0)
    try:
        log_offset = max(0, int(log_offset or 0))
    except (TypeError, ValueError, OverflowError):
        log_offset = 0
    try:
        pressure_offset = max(0, int(pressure_offset or 0))
    except (TypeError, ValueError, OverflowError):
        pressure_offset = 0
    log_content = str(context.get("log_content") or "") if current_phase_active else ""
    if current_phase_active and not log_content:
        log_content = _read_log_segment(log_path, log_offset)
    pressure_content = (
        str(context.get("pressure_log_content") or "")
        if current_phase_active
        else ""
    )
    if current_phase_active and not pressure_content:
        pressure_content = _read_log_segment(pressure_path, pressure_offset)

    # A collector may have flushed a structured trace even though the caller
    # never reached its normal collection branch.  Ask the executor once and
    # retain only mapping fields; failures are recorded on the trajectory.
    if current_phase_active and not isinstance(values.get("events"), list):
        reader = getattr(executor, "collect_trajectory", None)
        if callable(reader) and (pressure_path.is_file() or log_content):
            try:
                try:
                    collected = reader(
                        log_path=pressure_path,
                        log_start_offset=pressure_offset,
                        log_end_offset=(
                            pressure_path.stat().st_size
                            if pressure_path.exists()
                            else None
                        ),
                    )
                except TypeError as exc:
                    if "unexpected keyword" not in str(exc).lower():
                        raise
                    collected = reader()
                if isinstance(collected, Mapping):
                    for key, value in collected.items():
                        values.setdefault(key, value)
            except Exception as exc:
                values.setdefault("artifact_errors", []).append(
                    "partial trajectory collection: %s" % exc
                )

    # Executor detections are live in-memory state.  They are valid evidence
    # only after this cycle's pressure phase (or an explicitly attached phase
    # context) has started.  Reading them during a preparation failure can
    # otherwise import a crash marker left by the preceding cycle.
    if current_phase_active:
        for attribute, key in (
            ("app_crash_detection", "app_crash"),
            ("app_process_exit_detection", "app_process_exit"),
            ("device_disconnect_detection", "device_disconnect"),
            ("app_crash_page_detection", "app_crash_page"),
        ):
            try:
                detected = getattr(executor, attribute, None)
                detected = detected() if callable(detected) else detected
            except Exception:
                detected = None
            if isinstance(detected, Mapping) and key not in values:
                values[key] = dict(detected)

    package_name = str(
        getattr(state_manager, "package_name", "")
        or values.get("package_name")
        or ""
    )
    if package_name:
        values.setdefault("package_name", package_name)
        if current_phase_active and not values.get("app_crash_page"):
            try:
                handler = _new_crash_handler(
                    str(getattr(state_manager, "device_serial", "")),
                    package_name,
                    {},
                )
                page = _detect_app_crash_page_in_pressure_log(
                    pressure_content, package_name, handler
                )
                if page:
                    values["app_crash_page"] = dict(page)
            except Exception:
                pass

    merged_artifacts: Dict[str, Any] = {}
    contextual_artifacts = context.get("artifacts")
    if isinstance(contextual_artifacts, Mapping):
        merged_artifacts.update(dict(contextual_artifacts))
    value_artifacts = values.get("artifacts")
    if isinstance(value_artifacts, Mapping):
        merged_artifacts.update(dict(value_artifacts))
    if isinstance(artifacts, Mapping):
        merged_artifacts.update(dict(artifacts))
    if log_path.is_file():
        merged_artifacts.setdefault("logcat", str(log_path))
    if pressure_path.is_file():
        merged_artifacts.setdefault("fastbot", str(pressure_path))
    values["artifacts"] = merged_artifacts
    values.setdefault("phase_attempted", bool(current_phase_active))

    # Persist a partial trajectory immediately.  The normal caller may replace
    # it later with a richer trajectory, but this guarantees an artifact even
    # when the exception happened during startup/cleanup.
    try:
        trajectory_path = _write_trajectory(values, report_dir, cycle)
        merged_artifacts.setdefault("trajectory", trajectory_path)
    except Exception as exc:
        values.setdefault("artifact_errors", []).append(
            "trajectory: %s" % exc
        )
    return values, log_content, pressure_content, merged_artifacts


def _record_failure_crash_evidence(
    report: Dict[str, Any],
    cycle_report: Dict[str, Any],
    *,
    state_manager: Any,
    executor: Any,
    mutex: Any,
    crash_handler: Any,
    report_dir: Path,
    cycle: int,
    trajectory: Dict[str, Any],
    log_content: str,
    config: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Detect and persist a crash when a phase raised before normal finalization."""
    page_detection = (
        trajectory.get("app_crash_page")
        if isinstance(trajectory.get("app_crash_page"), Mapping)
        else None
    )
    try:
        log_crash = crash_handler.detect_crash(log_content)
    except Exception:
        log_crash = None
    page_evidence: Optional[Dict[str, Any]] = None
    if isinstance(page_detection, Mapping) or log_crash is not None or trajectory.get(
        "stopped_on_crash"
    ):
        try:
            page_evidence = _capture_app_crash_page_evidence(
                state_manager,
                mutex,
                crash_handler,
                report_dir,
                cycle,
                page_detection,
                allow_device_access=not _cycle_hard_control_failure(
                    executor, trajectory
                ),
                executor=executor,
            )
        except Exception as exc:
            # Evidence capture is auxiliary. Keep the original phase exception
            # as the primary report error and expose this failure in the trace.
            trajectory.setdefault("artifact_errors", []).append(
                "crash-page evidence: %s" % exc
            )
        if isinstance(page_evidence, Mapping):
            page_evidence = dict(page_evidence)
            _merge_handoff_fields(trajectory, page_evidence)
            trajectory["app_crash_page"] = page_evidence
            cycle_report["app_crash_page"] = page_evidence
            evidence_artifacts = page_evidence.get("artifacts", {})
            if isinstance(evidence_artifacts, Mapping):
                cycle_report.setdefault("artifacts", {}).update(evidence_artifacts)
                report.setdefault("artifacts", {}).update(evidence_artifacts)

    crash = _detect_cycle_crash(
        crash_handler,
        log_content,
        page_detection,
        page_evidence,
        trajectory.get("app_process_exit")
        if isinstance(trajectory.get("app_process_exit"), Mapping)
        else None,
        trajectory.get("app_crash")
        if isinstance(trajectory.get("app_crash"), Mapping)
        else None,
    )
    if not isinstance(crash, Mapping):
        return None, page_evidence

    crash_info = dict(crash)
    # Keep the classification available to an exception caller even when the
    # phase itself raised after the app had already crashed.  The scheduler
    # uses this marker to retain a ``crash`` cycle row instead of overwriting
    # it with a generic ``failed`` status.
    cycle_report["crash_detected"] = True
    if crash_info.get("crash_type"):
        cycle_report["crash_type"] = str(crash_info["crash_type"])
    trajectory.setdefault("stop_reason", _WAIT_CRASH)
    phase_value = crash_info.get("phase")
    phase_details = trajectory.get("phase", {})
    if phase_value is None and isinstance(phase_details, Mapping):
        phase_value = phase_details.get("phase")
    try:
        trajectory_path = _write_crash_trajectory(
            trajectory,
            report_dir,
            cycle,
            phase_value,
            1,
            config.get("crash_replay", {})
            if isinstance(config.get("crash_replay", {}), Mapping)
            else {},
            crash_info.get("detected_at"),
        )
    except Exception as exc:
        trajectory_path = cycle_report.get("artifacts", {}).get("trajectory")
        trajectory.setdefault("artifact_errors", []).append(
            "crash trajectory: %s" % exc
        )
    crash_info.update(
        {
            "cycle": cycle,
            "phase": phase_value,
            "trajectory_path": trajectory_path,
            "reproducible": False,
            "replay_attempts": [],
            "replay_note": "Phase failed before crash replay preparation",
        }
    )
    _add_crash(report, crash_info, cycle)
    if crash_info.get("signature"):
        cycle_report["crash_signature"] = str(crash_info["signature"])
    cycle_report["runtime_failure_count"] = max(
        1, int(cycle_report.get("runtime_failure_count", 0) or 0)
    )
    if trajectory_path:
        cycle_report.setdefault("artifacts", {})["crash_trajectory"] = str(
            trajectory_path
        )
        report.setdefault("artifacts", {})["crash_trajectory"] = str(
            trajectory_path
        )
    return crash_info, page_evidence


def _pressure_log_metrics(content: str) -> Dict[str, Any]:
    """Return event boundaries from only one pressure-engine log segment."""
    matches = list(_FASTBOT_EVENT_PATTERN.finditer(str(content or "")))
    if not matches:
        return {
            "first_event_id": None,
            "last_event_id": None,
            "first_event_at": None,
            "last_event_at": None,
            "event_elapsed_seconds": None,
        }
    first = matches[0]
    last = matches[-1]
    first_text = first.group("timestamp")
    last_text = last.group("timestamp")
    event_elapsed: Optional[float] = None
    try:
        first_at = datetime.strptime(first_text, "%Y-%m-%d %H:%M:%S.%f")
        last_at = datetime.strptime(last_text, "%Y-%m-%d %H:%M:%S.%f")
        event_elapsed = round(max(0.0, (last_at - first_at).total_seconds()), 3)
    except ValueError:
        pass
    return {
        "first_event_id": int(first.group("event_id")),
        "last_event_id": int(last.group("event_id")),
        "first_event_at": first_text,
        "last_event_at": last_text,
        "event_elapsed_seconds": event_elapsed,
    }


def _checkpoint_progress_provider(
    executor: Any,
    process: Any,
    serial: str,
    remote_log_path: Optional[str],
    base_operations: int,
) -> Callable[[], Mapping[str, Any]]:
    """Build a passive ADB probe used by the checkpoint heartbeat thread."""

    progress_state = {"offset": 0, "operations": 0}

    def probe() -> Mapping[str, Any]:
        values: Dict[str, Any] = {}
        adb_runner = getattr(executor, "_run_adb", None)
        if remote_log_path and callable(adb_runner):
            try:
                size_result = adb_runner(
                    ["shell", "wc", "-c", remote_log_path],
                    timeout=5,
                    retries=1,
                )
                size_match = re.search(r"\b(\d+)\b", size_result.stdout or "")
                if size_match:
                    offset = int(size_match.group(1))
                    if offset < progress_state["offset"]:
                        # A recovery started a new engine log; restart counting.
                        progress_state["offset"] = 0
                        progress_state["operations"] = 0
                    values["remote_log_offset"] = offset
                    previous = progress_state["offset"]
                    if offset > previous:
                        # Read only the bytes appended since the last probe.  A
                        # full-file grep every heartbeat meant 13 MB per device
                        # every ten seconds.
                        new_bytes = offset - previous
                        if previous == 0:
                            new_bytes = min(new_bytes, 4 * 1024 * 1024)
                        chunk = adb_runner(
                            ["shell", "tail", "-c", str(max(1, new_bytes)), remote_log_path],
                            timeout=20 if previous == 0 else 10,
                            retries=1,
                        )
                        chunk_text = chunk.stdout or ""
                        touches = len(
                            re.findall(r"Sending Touch \(ACTION_DOWN\)", chunk_text)
                        )
                        if touches:
                            progress_state["operations"] += touches
                        else:
                            event_ids = [
                                int(item)
                                for item in re.findall(
                                    r"//\s*Event id:\s*(\d+)",
                                    chunk_text,
                                    re.IGNORECASE,
                                )
                            ]
                            if event_ids:
                                progress_state["operations"] = max(
                                    progress_state["operations"], max(event_ids) + 1
                                )
                        progress_state["offset"] = offset
                        values["last_operation_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                    if progress_state["operations"]:
                        values["operation_count"] = (
                            max(0, int(base_operations))
                            + progress_state["operations"]
                        )
            except Exception as exc:
                values["heartbeat_probe_error"] = "remote log: %s" % exc

        # Persist actual Fastbot liveness alongside the logical resume state.
        # The detached process handle already knows how to distinguish a
        # finished engine from a temporary ADB probe failure.
        poller = getattr(process, "poll", None)
        if callable(poller):
            try:
                return_code = poller()
                if return_code is None:
                    values["fastbot_alive"] = True
                else:
                    values["fastbot_alive"] = False
                    try:
                        values["fastbot_exit_code"] = int(return_code)
                    except (TypeError, ValueError, OverflowError):
                        pass
            except Exception as exc:
                # A failed liveness probe is not proof that Fastbot exited.
                values["fastbot_probe_error"] = str(exc)

        try:
            result = DeviceManager._run_adb(  # type: ignore[attr-defined]
                ["shell", "dumpsys", "activity", "activities"],
                serial=serial,
                timeout=5,
                retries=1,
            )
            match = re.search(
                r"(?:topResumedActivity|mResumedActivity)[^\n]*?\s"
                r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)",
                result.stdout or "",
            )
            if match:
                values["foreground_component"] = match.group(1)
        except Exception as exc:
            existing = str(values.get("heartbeat_probe_error") or "")
            values["heartbeat_probe_error"] = (
                "%s; foreground: %s" % (existing, exc)
                if existing
                else "foreground: %s" % exc
            )
        remote_pid = getattr(process, "remote_pid", None)
        if remote_pid not in (None, ""):
            values["fastbot_pid"] = str(remote_pid)
        return values

    return probe


def _foreground_recovery_strategy(owner: Any = None) -> str:
    """Return the stable device recovery marker used in persisted artifacts."""
    value: Any = None
    if owner is not None:
        try:
            value = getattr(owner, "foreground_recovery_strategy", None)
            if callable(value):
                value = value()
        except Exception:
            value = None
        if not value:
            try:
                resolver = getattr(owner, "get_foreground_recovery_strategy", None)
                value = resolver() if callable(resolver) else None
            except Exception:
                value = None
    normalized = str(value or "").strip()
    return normalized or "standard_foreground_recovery"


def _write_trajectory(trajectory: Dict[str, Any], report_dir: Path, cycle: int) -> str:
    path = report_dir / ("trajectory_cycle_%03d.json" % cycle)
    ReportGenerator.write_json(trajectory, path)
    return str(path)


def _write_crash_trajectory(
    trajectory: Mapping[str, Any],
    report_dir: Path,
    cycle: int,
    phase: Any = None,
    occurrence: Optional[int] = None,
    replay_options: Optional[Mapping[str, Any]] = None,
    crash_at: Any = None,
) -> str:
    """Persist a crash-specific event sequence instead of a mutable cycle alias."""
    try:
        phase_number = int(phase) if phase is not None else None
    except (TypeError, ValueError):
        phase_number = None
    snapshot = build_replay_trajectory(
        trajectory,
        replay_options,
        package_name=str(trajectory.get("package_name") or ""),
        pressure_log_path=report_dir / "fastbot.log",
        cycle=cycle,
        phase=phase_number,
        crash_at=crash_at,
    )
    snapshot["crash_context"] = {
        "cycle": cycle,
        "phase": phase_number,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    suffix = (
        "cycle_%03d_phase_%03d.json" % (cycle, phase_number)
        if phase_number is not None
        else "cycle_%03d.json" % cycle
    )
    if occurrence is not None and occurrence > 1:
        suffix = suffix[:-5] + "_occurrence_%02d.json" % occurrence
    path = report_dir / ("crash_trajectory_" + suffix)
    ReportGenerator.write_json(snapshot, path)
    return str(path)


def _initial_report(
    config: Mapping[str, Any], run_id: str, serial: str, report_dir: Path
) -> Dict[str, Any]:
    app = config.get("app", {})
    test = config.get("test", {})
    try:
        remaining_seconds = max(
            0,
            int(test.get("fastbot_duration_minutes", 0) or 0)
            * 60
            * int(test.get("max_cycles", 0) or 0),
        ) if isinstance(test, Mapping) else 0
    except (TypeError, ValueError):
        remaining_seconds = 0
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 3,
        "run_id": run_id,
        "device_serial": serial,
        "package_name": app.get("package_name", "")
        if isinstance(app, Mapping)
        else "",
        "started_at": now,
        "finished_at": None,
        "status": "running",
        "resume_state": "running",
        "resume_attempts": 0,
        "remaining_seconds": remaining_seconds,
        "last_heartbeat": now,
        "last_error": None,
        "apk_fingerprint": None,
        "foreground_recovery_strategy": str(
            config.get("foreground_recovery_strategy")
            if isinstance(config, Mapping)
            else ""
        ).strip()
        or "standard_foreground_recovery",
        "report_dir": str(report_dir),
        "config": reportable_config(config),
        "preflight": {},
        "coverage": {},
        "device_info": {},
        "cycles": [],
        "crashes": [],
        "issues": [],
        "artifacts": {},
        "statistics": {},
    }


def _statistics(report: Dict[str, Any]) -> Dict[str, Any]:
    cycles = unique_cycle_rows(report)
    operations = sum(int(cycle.get("operation_count", 0) or 0) for cycle in cycles)
    coverage_values = [
        float(cycle["coverage_rate"])
        for cycle in cycles
        if isinstance(cycle.get("coverage_rate"), (int, float))
    ]
    statuses = [str(cycle.get("status", "")) for cycle in cycles]
    confirmed_crashes = _confirmed_crash_candidates(report.get("crashes", []))
    return {
        "total_cycles": len(cycles),
        "successful_cycles": sum(status == "passed" for status in statuses),
        "crashed_cycles": sum(status == "crash" for status in statuses),
        "failed_cycles": sum(status == "failed" for status in statuses),
        "interrupted_cycles": sum(status == "interrupted" for status in statuses),
        "total_operations": operations,
        "pressure_elapsed_seconds": round(
            sum(
                float(cycle.get("pressure_elapsed_seconds", 0) or 0)
                for cycle in cycles
            ),
            3,
        ),
        "runtime_failure_count": sum(
            int(cycle.get("runtime_failure_count", 0) or 0) for cycle in cycles
        ),
        "runtime_recovery_count": sum(
            int(cycle.get("runtime_recovery_count", 0) or 0) for cycle in cycles
        ),
        "pressure_engine_failure_count": sum(
            int(cycle.get("pressure_engine_failure_count", 0) or 0)
            for cycle in cycles
        ),
        "pressure_engine_recovery_count": sum(
            int(cycle.get("pressure_engine_recovery_count", 0) or 0)
            for cycle in cycles
        ),
        "foreground_recovery_count": sum(
            int(cycle.get("foreground_recovery_count", 0) or 0)
            for cycle in cycles
        ),
        "permission_dialog_count": sum(
            int(cycle.get("permission_dialog_count", 0) or 0)
            for cycle in cycles
        ),
        "permission_recovery_count": sum(
            int(cycle.get("permission_recovery_count", 0) or 0)
            for cycle in cycles
        ),
        "permission_recovery_failure_count": sum(
            int(cycle.get("permission_recovery_failure_count", 0) or 0)
            for cycle in cycles
        ),
        "stall_detection_count": sum(
            int(cycle.get("stall_detection_count", 0) or 0) for cycle in cycles
        ),
        "stall_recovery_requested_count": sum(
            int(
                cycle.get(
                    "stall_recovery_requested_count",
                    int(cycle.get("stall_recovery_count", 0) or 0)
                    + int(cycle.get("stall_recovery_skipped_count", 0) or 0),
                )
                or 0
            )
            for cycle in cycles
        ),
        "stall_recovery_count": sum(
            int(cycle.get("stall_recovery_count", 0) or 0) for cycle in cycles
        ),
        "stall_recovery_skipped_count": sum(
            int(cycle.get("stall_recovery_skipped_count", 0) or 0)
            for cycle in cycles
        ),
        "stall_recovery_failure_count": sum(
            int(cycle.get("stall_recovery_failure_count", 0) or 0)
            for cycle in cycles
        ),
        "fallback_recovery_success_count": sum(
            str(item.get("type") or "") == "fallback_recovery"
            for item in report.get("stall_records", [])
            if isinstance(item, Mapping)
        ),
        "stall_record_count": len(report.get("stall_records", [])),
        "crash_count": len(confirmed_crashes),
        "crash_occurrences": sum(
            max(1, len(crash.get("occurrences", [])))
            for crash in confirmed_crashes
        ),
        "issue_count": len(report.get("issues", [])),
        "coverage_rate": (
            round(sum(coverage_values) / len(coverage_values), 4)
            if coverage_values
            else None
        ),
    }


def _issue_type_for_crash(crash: Mapping[str, Any]) -> str:
    crash_type = str(crash.get("crash_type") or "unknown").lower()
    return {
        "java": "java_crash",
        "anr": "anr",
        "native": "native_crash",
        "app_crash_page": "app_crash_page",
        "process_exit": "app_process_exit",
    }.get(crash_type, "app_crash")


def _issue_label(issue_type: str) -> str:
    if issue_type == "startup_gate_error":
        return "启动门禁准备失败"
    if issue_type == "permission_recovery_error":
        return "系统权限弹窗恢复失败"
    return {
        "java_crash": "Java 崩溃",
        "anr": "应用无响应（ANR）",
        "native_crash": "Native 崩溃",
        "app_crash_page": "应用崩溃页",
        "app_process_exit": "无崩溃页闪退",
        "runtime_process_exit": "Target process exit (no app crash evidence)",
        "stall_recovery_failed": "Monkey 保底恢复失败",
        "device_disconnected": "设备断连",
        "foreground_recovery_error": "前台恢复/ADB 交接失败",
        "pressure_handoff_error": "Fastbot 控制交接失败",
        "pressure_engine_error": "Fastbot 引擎异常退出",
        "collector_error": "证据采集器停止失败",
        "uiautomator_error": "uiautomator2 控制恢复失败",
        "artifact_persistence_error": "运行产物落盘失败",
        "pressure_error": "压测框架或准备失败",
        "app_crash": "应用崩溃",
    }.get(issue_type, issue_type)


def _issue_suggestion(issue_type: str) -> str:
    if issue_type == "startup_gate_error":
        return "启动门禁未完成，先确认审核中、确定和隐私政策步骤均已完成，再单设备复测。"
    if issue_type == "permission_recovery_error":
        return (
            "先查看 permission_recovery_* 目录中的层级 XML、截图、前台组件和 ADB 错误；"
            "确认 Fastbot 已停止后再按厂商权限控制器的按钮文本或 resource-id 补充定位。"
        )
    return {
        "java_crash": "优先检查异常类型、首个业务堆栈及对应页面生命周期。",
        "anr": "优先检查主线程阻塞、锁等待、I/O或网络调用。",
        "native_crash": "优先结合 signal、tombstone及本地库符号定位。",
        "app_crash_page": "检查崩溃页展示的异常内容及进入该页面前的业务操作。",
        "app_process_exit": "检查 ActivityManager 退出原因、LMK记录和应用进程生命周期。",
        "runtime_process_exit": (
            "Review the ActivityManager reason, foreground owner, and recovery evidence; "
            "this event is not an app crash without FATAL/ANR/native/page evidence."
        ),
        "stall_recovery_failed": (
            "先确认页面是否为 Monkey 无法自行退出的正常业务终点；仅在同页反复触发、"
            "恢复失败，或同时存在 ANR/主线程阻塞证据时按应用稳定性问题排查。"
        ),
        "device_disconnected": "检查USB链路、ADB重枚举和设备端守护状态。",
        "foreground_recovery_error": (
            "报告未发现应用崩溃证据；本次是设备回到 Launcher 后，ADB ActivityManager 恢复目标前台超时。"
            "检查设备在线状态、Launcher/系统负载和 `am start` 返回，再单设备复测。"
        ),
        "pressure_handoff_error": (
            "Fastbot 在前台恢复交接期间已经退出，清理阶段不应继续对已消失 PID 发送 SIGCONT。"
            "检查设备/ADB 交接日志；这条记录本身不能作为应用崩溃结论。"
        ),
        "pressure_engine_error": (
            "检查对应阶段 fastbot.log 的最后动作、Fastbot 返回码和设备端运行时文件；"
            "只有同时存在应用崩溃堆栈时才归因于被测应用。"
        ),
        "collector_error": (
            "证据采集线程未能在限定时间退出，先停止或禁用截图/录屏采集器并检查磁盘与 ADB 通道；"
            "这不是应用崩溃证据。"
        ),
        "uiautomator_error": (
            "uiautomator2 与 Fastbot 的 UiAutomation 控制权交接失败；先确认设备仍在线，"
            "再重启该设备上的测试控制进程。"
        ),
        "artifact_persistence_error": (
            "检查运行目录路径长度、磁盘空间和目录写权限；这是框架运行产物落盘失败，"
            "与 Fastbot JAR、ABI、APK 安装或被测应用崩溃无关。"
        ),
        "pressure_error": (
            "先同步并校验 Fastbot 的三个 JAR 与 ABI 对应的 libfastbot_native.so，"
            "再检查 APK 安装和 ADB 返回信息。"
        ),
    }.get(issue_type, "结合关键日志和最后操作定位对应模块。")


def _pressure_issue_type(error_text: Any, executor: Any = None) -> str:
    """Classify pressure-control failures without calling them app crashes."""
    text = str(error_text or "")
    normalized = text.lower()
    if (
        ("runtime_failures" in normalized or "failure.json" in normalized)
        and any(
            marker in normalized
            for marker in (
                "no such file or directory",
                "file not found",
                "filenotfounderror",
                "permission denied",
                "permissionerror",
                "filename too long",
                "path too long",
                "winerror 206",
            )
        )
    ):
        return "artifact_persistence_error"
    if (
        "screenshot collector" in normalized
        or "collector did not stop" in normalized
        or "collector could not stop" in normalized
    ):
        return "collector_error"
    if any(
        marker in normalized
        for marker in ("audit gate", "complete_audit_gate", "startup gate")
    ):
        return "startup_gate_error"
    if any(
        marker in normalized
        for marker in (
            "permission recovery",
            "runtime permission",
            "permission dialog",
            "permission_recovery",
        )
    ):
        return "permission_recovery_error"
    if "uiautomator2 could not be restored" in normalized:
        return "uiautomator_error"
    foreground_failed = bool(
        getattr(executor, "foreground_restore_failed", False)
        or "foreground/adb" in normalized
        or "foreground guard" in normalized
        or "launcher" in normalized
        or "activitymanager" in normalized
        or "activity manager" in normalized
        or "am start" in normalized
        or "target package did not return to foreground" in normalized
    )
    if foreground_failed:
        return "foreground_recovery_error"
    if "unable to resume paused pressure-engine pids" in normalized:
        return "pressure_handoff_error"
    if "fastbot exited with return code" in normalized:
        return "pressure_engine_error"
    return "pressure_error"


def _mapping_artifacts(value: Any) -> Dict[str, str]:
    artifacts: Dict[str, str] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "artifacts" and isinstance(item, Mapping):
                for artifact_name, artifact_path in item.items():
                    if isinstance(artifact_path, (str, Path)):
                        artifacts[str(artifact_name)] = str(artifact_path)
            else:
                artifacts.update(_mapping_artifacts(item))
    elif isinstance(value, list):
        for item in value:
            artifacts.update(_mapping_artifacts(item))
    return artifacts


def _synthesize_issues(report: Dict[str, Any]) -> None:
    """Build concise developer-facing issues from crashes and cycle failures."""
    issues: List[Dict[str, Any]] = []
    stall_records: List[Dict[str, Any]] = []
    cycles = {
        int(cycle.get("cycle", 0) or 0): cycle
        for cycle in unique_cycle_rows(report)
        if isinstance(cycle, Mapping)
    }
    device = report.get("device_info", {})
    if not isinstance(device, Mapping):
        device = {}
    serial = str(report.get("device_serial") or "unknown")
    device_name = " ".join(
        value
        for value in (
            str(device.get("manufacturer") or "").strip(),
            str(device.get("model") or "").strip(),
        )
        if value
    ) or serial

    # Historical reports may still contain process-death rows in the crash
    # array.  They remain useful in runtime-failure evidence, but must not
    # generate an application-crash issue when the report is rebuilt.
    for crash in _confirmed_crash_candidates(report.get("crashes", [])):
        signature = str(crash.get("signature") or "unknown")
        issue_type = _issue_type_for_crash(crash)
        details = [
            dict(item)
            for item in crash.get("occurrence_details", [])
            if isinstance(item, Mapping)
        ]
        if not details:
            details = [
                {"cycle": cycle, "phase": crash.get("phase")}
                for cycle in crash.get("occurrences", [crash.get("cycle", 0)])
            ]
        first = details[0] if details else {}
        cycle_number = int(first.get("cycle", 0) or 0)
        phase_number = first.get("phase")
        cycle = cycles.get(cycle_number, {})
        runtime_failure = crash.get("runtime_failure")
        if not isinstance(runtime_failure, Mapping):
            runtime_failure = next(
                (
                    item
                    for item in cycle.get("runtime_failures", [])
                    if isinstance(item, Mapping)
                    and (
                        phase_number is None or item.get("phase") == phase_number
                    )
                ),
                {},
            )
        evidence = (
            runtime_failure.get("evidence", {})
            if isinstance(runtime_failure, Mapping)
            else {}
        )
        last_action = (
            runtime_failure.get("last_action")
            if isinstance(runtime_failure, Mapping)
            else None
        )
        foreground = ""
        if isinstance(evidence, Mapping):
            foreground = str(evidence.get("foreground_component") or "")
        recovery = (
            dict(runtime_failure.get("recovery", {}))
            if isinstance(runtime_failure, Mapping)
            and isinstance(runtime_failure.get("recovery"), Mapping)
            else {}
        )
        recovery_text = (
            "恢复成功并继续压测"
            if recovery.get("success") and recovery.get("continued_pressure")
            else "恢复成功，但未启动后续压测"
            if recovery.get("success")
            else "恢复失败或未执行"
        )
        description = (
            "%s（%s）在第%s轮%s压测时发生%s；%s。"
            % (
                device_name,
                serial,
                cycle_number or "未知",
                ("第%s阶段" % phase_number) if phase_number else "",
                _issue_label(issue_type),
                recovery_text,
            )
        )
        issue_id = "ISS-%s" % hashlib.sha256(
            signature.encode("utf-8", errors="replace")
        ).hexdigest()[:12].upper()
        issues.append(
            {
                "issue_id": issue_id,
                "type": issue_type,
                "severity": "high",
                "title": "%s：%s" % (_issue_label(issue_type), signature),
                "problem_description": description,
                "signature": signature,
                "device": dict(device),
                "app": dict(report.get("app_info", {}))
                if isinstance(report.get("app_info"), Mapping)
                else {},
                "cycle": cycle_number,
                "phase": phase_number,
                "detected_at": crash.get("detected_at"),
                "foreground_component": foreground,
                "last_action": last_action,
                "key_log_excerpt": _tail_text(
                    str(crash.get("stacktrace") or crash.get("raw_log") or ""),
                    16 * 1024,
                ),
                "occurrences": details,
                "reproducible": bool(crash.get("reproducible", False)),
                "recovery": recovery,
                "artifacts": {
                    **_mapping_artifacts(cycle),
                    **_mapping_artifacts(runtime_failure),
                    **_mapping_artifacts(crash),
                },
                "suggested_investigation": _issue_suggestion(issue_type),
            }
        )

    existing_keys = {(item["type"], item.get("cycle"), item.get("phase")) for item in issues}
    for cycle_number, cycle in cycles.items():
        for event_index, event in enumerate(cycle.get("stall_events", []), 1):
            if not isinstance(event, Mapping):
                continue
            recovery = event.get("recovery", {})
            if not isinstance(recovery, Mapping):
                recovery = {}
            attempted = bool(event.get("recovery_attempted") or recovery.get("attempted"))
            success = bool(event.get("recovery_success") or recovery.get("success"))
            interrupted = bool(recovery.get("interrupted"))
            component = event.get("component") or "unknown page"
            common = {
                "issue_id": "REC-STALL-%03d-%03d" % (cycle_number, event_index),
                "severity": "info",
                "device": dict(device),
                "app": dict(report.get("app_info", {}))
                if isinstance(report.get("app_info"), Mapping)
                else {},
                "cycle": cycle_number,
                "phase": None,
                "detected_at": event.get("detected_at"),
                "foreground_component": component,
                "last_action": None,
                "key_log_excerpt": "",
                "recovery": dict(recovery),
                "artifacts": _mapping_artifacts(event),
            }
            if attempted and not success and not interrupted:
                issues.append(
                    {
                        **common,
                        "issue_id": "ISS-STALL-%03d-%03d" % (cycle_number, event_index),
                        "type": "stall_recovery_failed",
                        "severity": "medium",
                        "title": "%s：%s" % (
                            _issue_label("stall_recovery_failed"), component
                        ),
                        "problem_description": (
                            "%s（%s）在第%s轮页面持续静止%s秒，Monkey 保底恢复未成功。"
                            % (
                                device_name,
                                serial,
                                cycle_number,
                                event.get("unchanged_seconds", "未知"),
                            )
                        ),
                        "suggested_investigation": _issue_suggestion(
                            "stall_recovery_failed"
                        ),
                    }
                )
                continue
            recovered_text = (
                "Monkey 无法继续探索，保底重启目标应用成功并继续压测"
                if success
                else "仅记录到页面静止，未发现 ANR 或崩溃证据"
            )
            stall_records.append(
                {
                    **common,
                    "type": "fallback_recovery" if success else "stall_observation",
                    "title": "%s：%s" % (
                        "Monkey 保底恢复" if success else "页面静止观察",
                        component,
                    ),
                    "problem_description": (
                        "%s（%s）在第%s轮页面持续静止%s秒；%s。"
                        "该记录不等同于应用卡死或稳定性缺陷。"
                        % (
                            device_name,
                            serial,
                            cycle_number,
                            event.get("unchanged_seconds", "未知"),
                            recovered_text,
                        )
                    ),
                    "suggested_investigation": (
                        "无需直接归因应用稳定性；仅在同一页面反复触发、保底恢复失败，"
                        "或同时出现 ANR/主线程阻塞证据时再交开发排查。"
                    ),
                }
            )
        for failure in cycle.get("runtime_failures", []):
            if not isinstance(failure, Mapping) or not isinstance(
                failure.get("device_disconnect"), Mapping
            ):
                continue
            phase = failure.get("phase")
            if ("device_disconnected", cycle_number, phase) in existing_keys:
                continue
            issue_id = "ISS-DISCONNECT-%03d-%s" % (
                cycle_number,
                str(phase or 0),
            )
            issues.append(
                {
                    "issue_id": issue_id,
                    "type": "device_disconnected",
                    "severity": "high",
                    "title": _issue_label("device_disconnected"),
                    "problem_description": (
                        "%s（%s）在第%s轮第%s阶段发生ADB断连，当前阶段已终止。"
                        % (device_name, serial, cycle_number, phase or "未知")
                    ),
                    "device": dict(device),
                    "app": dict(report.get("app_info", {}))
                    if isinstance(report.get("app_info"), Mapping)
                    else {},
                    "cycle": cycle_number,
                    "phase": phase,
                    "detected_at": failure.get("detected_at"),
                    "foreground_component": "",
                    "last_action": failure.get("last_action"),
                    "key_log_excerpt": str(
                        failure["device_disconnect"].get("error") or ""
                    ),
                    "recovery": failure.get("recovery", {}),
                    "artifacts": _mapping_artifacts(failure),
                    "suggested_investigation": _issue_suggestion(
                        "device_disconnected"
                    ),
                }
            )
        for failure in cycle.get("runtime_failures", []):
            if not isinstance(failure, Mapping):
                continue
            process_exit = failure.get("process_exit")
            if not isinstance(process_exit, Mapping):
                continue
            phase = failure.get("phase")
            issue_key = ("runtime_process_exit", cycle_number, phase)
            if issue_key in existing_keys:
                continue
            existing_keys.add(issue_key)
            issues.append(
                {
                    "issue_id": "ISS-PROCESS-EXIT-%03d-%s" % (
                        cycle_number,
                        str(phase or 0),
                    ),
                    "type": "runtime_process_exit",
                    "severity": "medium",
                    "title": _issue_label("runtime_process_exit"),
                    "problem_description": (
                        "%s（%s）第%s轮第%s阶段记录到目标进程退出，"
                        "但没有确认的应用崩溃证据。"
                        % (device_name, serial, cycle_number, phase or "未知")
                    ),
                    "device": dict(device),
                    "app": dict(report.get("app_info", {}))
                    if isinstance(report.get("app_info"), Mapping)
                    else {},
                    "cycle": cycle_number,
                    "phase": phase,
                    "detected_at": failure.get("detected_at"),
                    "foreground_component": "",
                    "last_action": failure.get("last_action"),
                    "key_log_excerpt": str(
                        process_exit.get("raw_log")
                        or process_exit.get("message")
                        or ""
                    ),
                    "recovery": failure.get("recovery", {}),
                    "artifacts": _mapping_artifacts(failure),
                    "suggested_investigation": _issue_suggestion(
                        "runtime_process_exit"
                    ),
                }
            )
        if cycle.get("status") == "failed" and cycle.get("errors"):
            error_text = "; ".join(
                str(item) for item in cycle.get("errors", []) if str(item).strip()
            )
            issue_type = _pressure_issue_type(error_text)
            digest = hashlib.sha256(
                ("%s|%s" % (cycle_number, cycle.get("errors"))).encode("utf-8")
            ).hexdigest()[:10].upper()
            issues.append(
                {
                    "issue_id": "ISS-PRESSURE-%s" % digest,
                    "type": issue_type,
                    "severity": "high",
                    "title": _issue_label(issue_type),
                    "problem_description": (
                        "%s（%s）第%s轮压测失败：%s"
                        % (
                            device_name,
                            serial,
                            cycle_number,
                            error_text,
                        )
                    ),
                    "device": dict(device),
                    "app": dict(report.get("app_info", {}))
                    if isinstance(report.get("app_info"), Mapping)
                    else {},
                    "cycle": cycle_number,
                    "phase": None,
                    "detected_at": cycle.get("finished_at"),
                    "foreground_component": "",
                    "last_action": None,
                    "key_log_excerpt": "\n".join(
                        str(item) for item in cycle.get("errors", [])
                    ),
                    "recovery": {},
                    "artifacts": _mapping_artifacts(cycle),
                    "suggested_investigation": _issue_suggestion(issue_type),
                }
            )
    top_level_errors = [
        str(item)
        for item in report.get("errors", [])
        if str(item).strip()
    ]
    if top_level_errors and str(report.get("status")) in {
        "failed",
        "completed_with_errors",
        "interrupted",
    }:
        error_text = "\n".join(top_level_errors)
        issue_type = _pressure_issue_type(error_text)
        issue_hash = hashlib.sha256(
            (serial + "\n" + error_text).encode("utf-8", errors="replace")
        ).hexdigest()[:12].upper()
        issues.append(
            {
                "issue_id": "ISS-RUN-%s" % issue_hash,
                "type": issue_type,
                "severity": "high"
                if report.get("status") != "interrupted"
                else "medium",
                "title": _issue_label(issue_type),
                "problem_description": (
                    "%s（%s）的压测运行未正常完成：%s"
                    % (device_name, serial, "; ".join(top_level_errors))
                ),
                "device": dict(device),
                "app": dict(report.get("app_info", {}))
                if isinstance(report.get("app_info"), Mapping)
                else {},
                "cycle": None,
                "phase": None,
                "detected_at": report.get("finished_at"),
                "foreground_component": "",
                "last_action": None,
                "key_log_excerpt": _tail_text(error_text, 16 * 1024),
                "recovery": {},
                "artifacts": _mapping_artifacts(report),
                "suggested_investigation": _issue_suggestion(issue_type),
            }
        )
    report["issues"] = issues
    report["stall_records"] = stall_records


def _durable_manual_gate(
    report: Mapping[str, Any], *sources: Any
) -> Optional[Tuple[str, Mapping[str, Any]]]:
    """Return a persisted manual gate that must not be overwritten by a stale worker."""
    ended_candidates: List[Mapping[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if terminal_resume_state(source) == MANUALLY_ENDED_STATE:
            ended_candidates.append(source)
    if ended_candidates:
        source = max(ended_candidates, key=_resume_timestamp)
        return MANUALLY_ENDED_STATE, {
            **dict(source),
            "resume_state": MANUALLY_ENDED_STATE,
        }

    incoming_state = str(
        report.get("resume_state") or report.get("status") or ""
    ).strip().lower()
    if incoming_state == "resuming" or terminal_resume_state(report):
        return None
    candidates: List[Tuple[int, Mapping[str, Any]]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        state = str(source.get("resume_state") or source.get("status") or "").strip().lower()
        if not bool(source.get("manual_stop")) and state not in MANUAL_RECOVERY_STATES:
            continue
        if state not in MANUAL_RECOVERY_STATES:
            state = "paused"
        candidates.append((10 if state == "needs_manual_resume" else 1, {**dict(source), "resume_state": state}))
    if not candidates:
        return None
    priority, source = max(candidates, key=lambda item: item[0])
    _ = priority
    return str(source["resume_state"]), source


def _preserve_durable_manual_gate(
    report: Dict[str, Any], *sources: Any
) -> Optional[str]:
    """Merge a web/operator pause into a worker snapshot before it is written."""
    gate = _durable_manual_gate(report, *sources)
    if gate is None:
        return None
    state, source = gate
    reason = str(
        source.get("last_error")
        or source.get("recovery_reason")
        or (
            "device test ended manually from web console"
            if state == MANUALLY_ENDED_STATE
            else "pressure stopped by web console"
        )
    )
    if state == MANUALLY_ENDED_STATE:
        finished_at = str(
            source.get("finished_at")
            or source.get("manual_end_at")
            or report.get("finished_at")
            or datetime.now(timezone.utc).isoformat()
        )
        report.update(
            {
                "status": "stopped",
                "resume_state": MANUALLY_ENDED_STATE,
                "finished_at": finished_at,
                "manual_end": True,
                "manual_end_at": str(source.get("manual_end_at") or finished_at),
                "manual_stop": True,
                "remaining_seconds": 0.0,
                "waiting_for_device": False,
                "resume_attempt_reserved": False,
                "next_retry_at": None,
                "last_error": reason,
                "recovery_category": "manual_end",
                "recovery_reason": reason,
            }
        )
        current = report.get("current_cycle")
        if isinstance(current, dict):
            current.update(
                {
                    "status": "stopped",
                    "resume_state": MANUALLY_ENDED_STATE,
                    "completed": False,
                    "remaining_seconds": 0.0,
                    "stop_reason": "manual_end",
                    "last_error": reason,
                }
            )
        coverage = report.get("coverage")
        if isinstance(coverage, dict):
            coverage["status"] = "stopped"
        return state

    report.update(
        {
            "status": state,
            "resume_state": state,
            "finished_at": None,
            "last_error": reason,
            "manual_stop": True,
            "recovery_category": str(source.get("recovery_category") or "manual_stop"),
            "recovery_reason": reason,
        }
    )
    current = report.get("current_cycle")
    if isinstance(current, dict):
        current["status"] = state
        current["last_error"] = reason
    return state


def _refresh_durable_manual_end(
    report: Dict[str, Any],
    report_dir: Path,
    run_id: str,
    device_serial: str,
) -> bool:
    """Import an operator-ended marker written by the web service."""
    with _report_write_lock(report_dir):
        durable_report = read_checkpoint_json(Path(report_dir) / "report.json")
        durable_checkpoint = load_checkpoint(
            report_dir,
            run_id=run_id,
            device_serial=device_serial,
        )
        state = effective_resume_state(
            durable_report if isinstance(durable_report, Mapping) else report,
            durable_checkpoint,
            default=str(report.get("resume_state") or "running"),
        )
        if state != MANUALLY_ENDED_STATE:
            return False
        _preserve_durable_manual_gate(
            report,
            durable_report,
            durable_checkpoint,
        )
        return True


def _raise_if_device_manually_ended(
    report: Dict[str, Any],
    report_dir: Path,
    run_id: str,
    device_serial: str,
) -> None:
    if _refresh_durable_manual_end(
        report,
        report_dir,
        run_id,
        device_serial,
    ):
        raise _DeviceManuallyEnded("device test was ended manually")


def _persist_report(report: Dict[str, Any], report_dir: Path) -> None:
    with _report_write_lock(report_dir):
        # A separate web process may have paused this device while the worker
        # was preparing its next heartbeat.  Keep that manual gate monotonic.
        durable_report = read_checkpoint_json(Path(report_dir) / "report.json")
        durable_checkpoint = read_checkpoint_json(Path(report_dir) / "checkpoint.json")
        _preserve_durable_manual_gate(report, durable_report, durable_checkpoint)
        # Keep one durable row per numbered cycle.  This is deliberately a
        # view-level canonicalization: completed rows retain their first
        # persisted contents, while duplicate partial snapshots cannot inflate
        # statistics or pressure time on the next resume.
        if isinstance(report.get("cycles"), list):
            report["cycles"] = list(unique_cycle_rows(report))
        _synthesize_issues(report)
        report["statistics"] = _statistics(report)
        try:
            if report.get("finished_at"):
                manifest = ReportGenerator.write_artifact_manifest(report_dir)
                report.setdefault("artifacts", {})["manifest"] = str(manifest)
            paths = ReportGenerator.write_all(report, report_dir)
            report.setdefault("artifacts", {}).update(paths)
            # Persist once more so artifact paths are present in report.json itself.
            ReportGenerator.write_json(report, report_dir / "report.json")
        except OSError:
            logger.exception("Unable to persist report in %s", report_dir)


def _run_pressure_preflight(
    executor: Any, report: Dict[str, Any]
) -> Dict[str, Any]:
    """Record pressure-engine readiness before installing or starting the app."""
    mute = getattr(executor, "mute_device_audio", None)
    if callable(mute):
        try:
            report["device_muted"] = bool(mute())
        except Exception:
            logger.debug(
                "Unable to silence device %s",
                getattr(executor, "device_serial", "?"),
                exc_info=True,
            )
    # Touch overlays are extra system windows; the default keeps them off so a
    # debugging session cannot silently degrade Fastbot's input guard.
    overlay = getattr(executor, "configure_touch_overlay", None)
    if callable(overlay):
        try:
            report["touch_overlay"] = bool(overlay())
        except Exception:
            logger.debug(
                "Unable to configure touch overlay on %s",
                getattr(executor, "device_serial", "?"),
                exc_info=True,
            )
    checker = getattr(executor, "preflight_pressure_engine", None)
    if not callable(checker):
        result = {
            "success": True,
            "skipped": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "reason": "executor does not expose a preflight check",
        }
        report["preflight"] = result
        return result
    try:
        raw_result = checker()
        result = dict(raw_result) if isinstance(raw_result, Mapping) else {}
        result.setdefault("success", True)
        result.setdefault("checked_at", datetime.now(timezone.utc).isoformat())
        report["preflight"] = result
        return result
    except Exception as exc:
        result = {
            "success": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        report["preflight"] = result
        raise RuntimeError("pressure engine preflight failed: %s" % exc) from exc


def _apply_report_coverage(
    report: Dict[str, Any],
    config: Mapping[str, Any],
    expected_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    """Classify whether the intended pressure budget actually ran."""
    test_options = config.get("test", {})
    if not isinstance(test_options, Mapping):
        test_options = {}
    coverage_options = config.get("coverage", {})
    if not isinstance(coverage_options, Mapping):
        coverage_options = {}
    cycle_target = max(
        1,
        int(
            expected_cycles
            if expected_cycles is not None
            else test_options.get("max_cycles", 1)
        ),
    )
    seconds_per_cycle = max(
        1.0, float(test_options.get("fastbot_duration_minutes", 1)) * 60.0
    )
    expected_seconds = cycle_target * seconds_per_cycle
    statistics = _statistics(report)
    actual_seconds = max(
        0.0, float(statistics.get("pressure_elapsed_seconds", 0) or 0)
    )
    operations = max(0, int(statistics.get("total_operations", 0) or 0))
    ratio = min(1.0, actual_seconds / expected_seconds) if expected_seconds else 0.0
    minimum_ratio = min(
        1.0,
        max(
            0.0,
            float(coverage_options.get("minimum_pressure_duration_ratio", 0.90)),
        ),
    )
    require_operations = bool(
        coverage_options.get("require_nonzero_operations", True)
    )
    preflight = report.get("preflight", {})
    preflight_failed = bool(
        isinstance(preflight, Mapping)
        and preflight.get("success") is False
    )
    preflight_error = (
        str(preflight.get("error") or "") if isinstance(preflight, Mapping) else ""
    )
    report_status = str(report.get("status") or "")
    if preflight_failed and "fastbot files are missing" in preflight_error.lower():
        status = "fastbot_files_missing"
    elif preflight_failed:
        status = "preflight_failed"
    elif report_status == "interrupted":
        status = "interrupted"
    elif report_status in {"failed", "completed_with_errors"}:
        status = "failed"
    elif require_operations and operations == 0:
        status = "zero_events"
    elif len(unique_cycle_rows(report)) < cycle_target or ratio < minimum_ratio:
        status = "partial"
    else:
        status = "complete"
    coverage = {
        "status": status,
        "expected_cycles": cycle_target,
        "completed_cycles": len(unique_cycle_rows(report)),
        "expected_pressure_seconds": round(expected_seconds, 3),
        "actual_pressure_seconds": round(actual_seconds, 3),
        "duration_ratio": round(ratio, 4),
        "minimum_duration_ratio": minimum_ratio,
        "operation_count": operations,
        "require_nonzero_operations": require_operations,
    }
    report["coverage"] = coverage
    report["statistics"] = statistics
    return coverage


def _reset_app(
    state_manager: StateManager,
    clear_data: bool,
    stop_packages: Optional[Iterable[str]] = None,
    clear_task: bool = False,
) -> bool:
    """Reset a state manager while supporting older test doubles."""
    try:
        return bool(
            state_manager.reset_app(
                clear_data=clear_data,
                stop_packages=stop_packages,
                clear_task=clear_task,
            )
        )
    except TypeError as exc:
        unsupported = str(exc)
        if (
            "clear_data" not in unsupported
            and "stop_packages" not in unsupported
            and "clear_task" not in unsupported
        ):
            raise
        # Legacy StateManager adapters expose only ``reset_app()``. Their
        # historical contract already means force-stop/relaunch, so retaining
        # that fallback is safe for compatibility; the production manager
        # accepts ``clear_task`` explicitly and takes the stricter path above.
        try:
            return bool(state_manager.reset_app(clear_data=clear_data))
        except TypeError:
            return bool(state_manager.reset_app())


def _refresh_uiautomator_device(state_manager: Any) -> Any:
    """Refresh a stale uiautomator2 session when the manager supports it."""
    refresher = getattr(state_manager, "refresh_device", None)
    if not callable(refresher):
        return state_manager.get_device()
    try:
        refreshed = refresher()
    except Exception:
        logger.warning(
            "Unable to refresh uiautomator2 session on %s; keeping the existing "
            "device object",
            getattr(state_manager, "device_serial", "unknown-device"),
            exc_info=True,
        )
        return state_manager.get_device()
    return refreshed if refreshed is not None else state_manager.get_device()


def _clear_app_data_for_device(
    config: Mapping[str, Any], device_serial: str
) -> bool:
    """Resolve the default data-clear policy with an explicit device override."""
    test_config = config.get("test", {})
    if not isinstance(test_config, Mapping):
        return True
    default = bool(test_config.get("clear_app_data_each_cycle", True))
    overrides = test_config.get("clear_app_data_each_cycle_by_device", {})
    if isinstance(overrides, Mapping) and device_serial in overrides:
        return bool(overrides[device_serial])
    return default


def _reset_app_for_cycle(
    state_manager: StateManager,
    config: Mapping[str, Any],
    stop_packages: Optional[Iterable[str]] = None,
    *,
    clear_data_override: Optional[bool] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Reset one app and optionally keep testing when vendor data clear is denied."""
    clear_requested = (
        bool(clear_data_override)
        if clear_data_override is not None
        else _clear_app_data_for_device(config, str(state_manager.device_serial))
    )
    reset_details = {
        "data_clear_requested": clear_requested,
        "continued_without_data_clear": False,
    }

    def add_reset_error() -> None:
        error = str(getattr(state_manager, "last_reset_error", "") or "").strip()
        if error:
            reset_details["error"] = error[:2000]

    capability_reader = getattr(state_manager, "data_clear_capability", None)
    if clear_requested and callable(capability_reader):
        capability = capability_reader()
        if isinstance(capability, Mapping) and capability.get("supported") is False:
            clear_requested = False
            reset_details["data_clear_requested"] = False
            reset_details["data_clear_capability"] = dict(capability)
            reset_details["data_clear_skipped_reason"] = str(
                capability.get("reason") or "device does not support app data clear"
            )
            reset_details["continued_without_data_clear"] = True
    if _reset_app(state_manager, clear_requested, stop_packages):
        if callable(capability_reader):
            capability = capability_reader()
            if isinstance(capability, Mapping):
                reset_details["data_clear_capability"] = dict(capability)
        return True, reset_details
    add_reset_error()
    test_config = config.get("test", {})
    continue_without_clear = bool(
        test_config.get("continue_without_data_clear_on_failure", False)
        if isinstance(test_config, Mapping)
        else False
    )
    if not clear_requested or not continue_without_clear:
        return False, reset_details
    logger.warning(
        "Data clear failed on %s; force-stopping and relaunching without data clear "
        "so the configured pressure window can continue",
        state_manager.device_serial,
    )
    reset_details["continued_without_data_clear"] = True
    success = _reset_app(state_manager, False, stop_packages)
    add_reset_error()
    if callable(capability_reader):
        capability = capability_reader()
        if isinstance(capability, Mapping):
            reset_details["data_clear_capability"] = dict(capability)
    return success, reset_details


def _prepare_state(
    state_manager: StateManager, config: Mapping[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Install the local APK once, reset the app, and run the user scenario."""
    configure_input = getattr(state_manager, "configure_input_safety", None)
    if callable(configure_input):
        configure_input(dict(config))
    apk_path = require_local_apk(config)
    if not state_manager.install_app(str(apk_path)):
        raise RuntimeError("Unable to install local APK %s" % apk_path)
    test_config = config.get("test", {})
    scenario_config = config.get("scenario", {})
    stop_packages = (
        scenario_config.get("stop_packages", [])
        if isinstance(scenario_config, Mapping)
        else []
    )
    reset_ok, reset_details = _reset_app_for_cycle(
        state_manager, config, stop_packages
    )
    if not reset_ok:
        reset_error = str(reset_details.get("error") or "").strip()
        raise RuntimeError(
            "Unable to reset package state%s"
            % (": %s" % reset_error if reset_error else "")
        )
    if not _complete_configured_audit_gate(state_manager, config):
        raise RuntimeError(
            "startup audit gate could not be completed after app launch"
        )
    scenario_result = prepare_test_scenario(state_manager, dict(config))
    scenario_result["reset"] = reset_details
    if not scenario_result.get("login_success", True):
        raise RuntimeError("Preparation login failed")
    if not scenario_result.get("navigation_success", True):
        raise RuntimeError("Preparation navigation failed")
    snapshot = state_manager.save_snapshot("initial")
    if snapshot.get("error"):
        raise RuntimeError("Unable to save initial UI snapshot: %s" % snapshot["error"])
    return scenario_result, snapshot


def _detect_live_crash(
    crash_handler: Optional[CrashHandler],
    log_path: Optional[Path],
    log_offset: int,
) -> bool:
    """Probe only newly collected logcat data for a target-app failure."""
    return _detect_live_crash_details(crash_handler, log_path, log_offset) is not None


def _detect_live_crash_details(
    crash_handler: Optional[CrashHandler],
    log_path: Optional[Path],
    log_offset: int,
) -> Optional[Dict[str, Any]]:
    """Return the parsed crash block from the current incremental log segment."""
    if crash_handler is None or log_path is None or not log_path.exists():
        return None
    try:
        segment = _read_log_segment(log_path, log_offset)
        detection = crash_handler.detect_crash(segment)
        return dict(detection) if isinstance(detection, Mapping) else None
    except (OSError, UnicodeError):
        logger.debug("Unable to probe live logcat crash data", exc_info=True)
        return None


def _new_crash_handler(
    device_serial: str, package_name: str, config: Optional[Mapping[str, Any]] = None
) -> CrashHandler:
    """Create a configured handler while retaining older test doubles."""
    options = dict(config.get("crash_detection", {})) if isinstance(config, Mapping) else {}
    if isinstance(config, Mapping) and "input_safety" in config:
        options["input_safety"] = dict(config["input_safety"])
    try:
        return CrashHandler(device_serial, package_name, options)
    except TypeError:
        return CrashHandler(device_serial, package_name)


def _detect_live_process_exit(
    crash_handler: Optional[CrashHandler],
    log_path: Optional[Path],
    log_offset: int,
) -> Optional[Dict[str, Any]]:
    """Return a target process-death event from only the current log segment."""
    detector = getattr(crash_handler, "detect_process_exit", None)
    if not callable(detector) or log_path is None or not log_path.exists():
        return None
    try:
        segment = _read_log_segment(log_path, log_offset)
        detection = detector(segment)
        return dict(detection) if isinstance(detection, Mapping) else None
    except (OSError, UnicodeError):
        logger.debug("Unable to probe process-exit log data", exc_info=True)
        return None


def _record_runtime_detection(
    executor: Any, name: str, detection: Mapping[str, Any]
) -> None:
    recorder = getattr(executor, name, None)
    if callable(recorder):
        recorder(dict(detection))


def _probe_runtime_state(
    executor: Any,
    crash_handler: Optional[CrashHandler],
    log_path: Optional[Path],
    log_offset: int,
    state: Dict[str, Any],
) -> Optional[str]:
    """Classify log/PID/device transitions without using UiAutomation."""
    # A Java FATAL EXCEPTION is the authoritative explanation for a subsequent
    # process-death record.  Check it first so the report keeps the exception
    # type/stack instead of downgrading the same event to ``pid_missing``.
    crash = _detect_live_crash_details(crash_handler, log_path, log_offset)
    if isinstance(crash, Mapping):
        _record_runtime_detection(executor, "record_app_crash", crash)
        return _WAIT_CRASH

    process_exit = _detect_live_process_exit(crash_handler, log_path, log_offset)
    if process_exit is not None:
        _record_runtime_detection(executor, "record_app_process_exit", process_exit)
        return _WAIT_PROCESS_EXIT

    config = getattr(executor, "config", {})
    options = config.get("process_monitor", {}) if isinstance(config, Mapping) else {}
    if not isinstance(options, Mapping) or not options.get("enabled", True):
        return None
    probe = getattr(executor, "probe_target_process", None)
    if not callable(probe):
        return None
    now = time.monotonic()
    interval = max(1.0, float(options.get("sample_interval_seconds", 5)))
    if now < float(state.get("next_probe_at", 0.0)):
        return None
    state["next_probe_at"] = now + interval

    if options.get("foreground_probe_enabled", True):
        page_probe = getattr(executor, "probe_app_crash_page", None)
        if callable(page_probe):
            try:
                if page_probe():
                    return _WAIT_CRASH
            except Exception:
                logger.debug("Foreground crash-page probe failed", exc_info=True)

    try:
        observation = probe()
    except Exception as exc:
        observation = {
            "connected": False,
            "running": None,
            "pids": [],
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
    if not isinstance(observation, Mapping):
        return None
    if observation.get("connected") is not True:
        state["disconnects"] = int(state.get("disconnects", 0)) + 1
        if state["disconnects"] >= int(options.get("disconnect_confirmations", 2)):
            detection = dict(observation)
            detection.update(
                {
                    "detection_source": "adb_process_monitor",
                    "disconnect_confirmations": state["disconnects"],
                    "message": "ADB transport became unavailable during pressure testing",
                }
            )
            _record_runtime_detection(
                executor, "record_device_disconnect", detection
            )
            return _WAIT_DEVICE_DISCONNECTED
        return None
    state["disconnects"] = 0

    pids = {
        int(value)
        for value in observation.get("pids", [])
        if str(value).isdigit()
    }
    known_pids = set(state.get("known_pids", set()))
    grace = max(0.0, float(options.get("startup_grace_seconds", 5)))
    require_exit_evidence = bool(options.get("require_exit_evidence", True))
    if pids:
        if known_pids and not known_pids.intersection(pids):
            if require_exit_evidence:
                # Android may restart an app process for an intentional
                # force-stop/relaunch, WebView isolation, or vendor lifecycle
                # policy.  A PID transition alone is not crash evidence; the
                # current logcat segment above remains authoritative.
                state["known_pids"] = pids
                state["missing"] = 0
                state["unconfirmed_pid_replacements"] = int(
                    state.get("unconfirmed_pid_replacements", 0)
                ) + 1
                return None
            detection = {
                "process_event": "pid_replaced",
                "exception_type": "UnexpectedProcessRestart",
                "previous_pids": sorted(known_pids),
                "current_pids": sorted(pids),
                "detected_at": observation.get("detected_at")
                or datetime.now(timezone.utc).isoformat(),
                "detection_source": "pid_monitor",
                "message": "target application PID changed during pressure testing",
            }
            _record_runtime_detection(
                executor, "record_app_process_exit", detection
            )
            return _WAIT_PROCESS_EXIT
        state["known_pids"] = pids
        state["missing"] = 0
        return None
    if not known_pids or now - float(state.get("started_at", now)) < grace:
        return None
    state["missing"] = int(state.get("missing", 0)) + 1
    if state["missing"] < int(options.get("missing_confirmations", 2)):
        return None
    if require_exit_evidence:
        # Do not turn an unexplained absence into a reported application
        # crash.  Reset the baseline so a later legitimate relaunch can be
        # monitored without repeatedly firing on the old PID.
        state["known_pids"] = set()
        state["missing"] = 0
        state["unconfirmed_pid_missing"] = int(
            state.get("unconfirmed_pid_missing", 0)
        ) + 1
        return None
    detection = {
        "process_event": "pid_missing",
        "exception_type": "UnexpectedProcessExit",
        "previous_pids": sorted(known_pids),
        "current_pids": [],
        "detected_at": observation.get("detected_at")
        or datetime.now(timezone.utc).isoformat(),
        "detection_source": "pid_monitor",
        "missing_confirmations": state["missing"],
        "message": "target application process disappeared during pressure testing",
    }
    _record_runtime_detection(executor, "record_app_process_exit", detection)
    return _WAIT_PROCESS_EXIT


def _detect_live_app_crash_page(executor: Any) -> bool:
    """Return whether the passive foreground probe saw the app crash activity."""
    try:
        return bool(getattr(executor, "app_crash_page_detection", None))
    except Exception:
        logger.debug("Unable to read app crash-page detection", exc_info=True)
        return False


def _mark_crash_stop(executor: Any) -> None:
    marker = getattr(executor, "mark_crash_stop_requested", None)
    if callable(marker):
        marker()
        return
    marker = getattr(executor, "mark_pressure_deadline_reached", None)
    if callable(marker):
        marker()


def _is_app_crash_page_component(
    component: Optional[str], package_name: str
) -> bool:
    package, separator, activity = str(component or "").partition("/")
    if not separator or package != package_name:
        return False
    if activity.startswith("."):
        activity = package + activity
    return activity.endswith(".AhzyCrashActivity")


def _detect_app_crash_page_in_pressure_log(
    content: str,
    package_name: str,
    crash_handler: Optional[CrashHandler] = None,
) -> Optional[Dict[str, Any]]:
    """Use Fastbot's top-activity trace when a foreground probe was missed."""
    handler = crash_handler or CrashHandler("pressure-log", package_name)
    finder = getattr(handler, "find_app_crash_page_component", None)
    component = finder(content) if callable(finder) else None
    if not component:
        return None
    return {
        "component": str(component),
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "detection_source": "pressure_engine_log",
    }


def _pressure_stop_verified(executor: Any, stop_result: Any = None) -> bool:
    """Return whether the device-side pressure process is known to be gone.

    The executor exposes this separately from its overall stop result because
    host return codes and foreground-guard cleanup can fail after the remote
    process has already exited.  Legacy test doubles do not expose the
    property, so their boolean stop result remains the conservative fallback.
    """
    sentinel = object()
    value = getattr(executor, "pressure_stop_verified", sentinel)
    if value is not sentinel:
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if value is not None:
            return bool(value)
    error = str(getattr(executor, "pressure_stop_error", None) or "").lower()
    if any(
        marker in error
        for marker in (
            "device-side fastbot process is still running",
            "device-side fastbot stop verification failed",
        )
    ):
        return False
    return bool(stop_result)


def _pressure_stop_intent_reason(executor: Any) -> Optional[str]:
    """Read the reason published before the pressure engine was signaled."""
    value = getattr(executor, "pressure_stop_intent_reason", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            value = None
    reason = str(value or "").strip().lower()
    return reason or None


def _permission_recovery_timeout(config: Mapping[str, Any]) -> float:
    """Return a bounded permission-handoff timeout for the phase runner.

    Config files are user supplied and older runs occasionally contain a
    string, ``null``, or a non-mapping ``test`` section.  Keep malformed
    values from escaping the recovery branch as an incidental ``TypeError``;
    the executor applies its own final clamp as well.
    """
    test_options = config.get("test", {}) if isinstance(config, Mapping) else {}
    configured = (
        test_options.get("permission_recovery_timeout_seconds")
        if isinstance(test_options, Mapping)
        else None
    )
    try:
        value = float(configured) if configured is not None else 8.0
    except (TypeError, ValueError, OverflowError):
        value = 8.0
    if not math.isfinite(value):
        value = 8.0
    return min(60.0, max(8.0, value))


def _stop_fastbot_after_runtime_event(
    executor: Any, process: Any, message: str
) -> None:
    """Stop Fastbot while preserving a verified foreground-handoff result."""
    try:
        stopped = bool(executor.stop_fastbot(process))
    except Exception as exc:
        # ``stop_fastbot`` also reports host-side cleanup and the child return
        # code.  A crash commonly makes that part non-zero even though the
        # device-side process is already gone.  The remote probe is the safety
        # boundary for collecting crash evidence and starting a later cycle.
        if _pressure_stop_verified(executor):
            logger.warning(
                "%s; device-side Fastbot is stopped, preserving runtime evidence: %s",
                message,
                exc,
            )
            return
        raise RuntimeError(message) from exc
    if stopped:
        return
    if _pressure_stop_verified(executor, stopped):
        logger.warning(
            "%s; device-side Fastbot is stopped, preserving runtime evidence",
            message,
        )
        return
    raise RuntimeError(message)


def _wait_for_fastbot(
    executor: TestExecutor,
    process: Any,
    duration_minutes: int,
    grace_seconds: int,
    cancel_event: Any = None,
    crash_handler: Optional[CrashHandler] = None,
    log_path: Optional[Path] = None,
    log_offset: int = 0,
    duration_seconds: Optional[float] = None,
    stop_at_duration: bool = False,
) -> str:
    """Wait with a hard deadline and make Ctrl+C cancellation observable."""
    window_seconds = (
        float(duration_seconds)
        if duration_seconds is not None
        else float(duration_minutes * 60 + grace_seconds)
    )
    deadline = time.monotonic() + max(1.0, window_seconds)
    runtime_state: Dict[str, Any] = {
        "started_at": time.monotonic(),
        "next_probe_at": 0.0,
        "known_pids": set(),
        "missing": 0,
        "disconnects": 0,
    }
    while process.poll() is None:
        _check_cancel(cancel_event)
        live_crash = _detect_live_crash_details(crash_handler, log_path, log_offset)
        if live_crash is not None:
            _record_runtime_detection(executor, "record_app_crash", live_crash)
        if _detect_live_app_crash_page(executor) or live_crash is not None:
            _mark_crash_stop(executor)
            _stop_fastbot_after_runtime_event(
                executor, process, "Unable to stop Fastbot after a target-app crash"
            )
            return _WAIT_CRASH
        runtime_outcome = _probe_runtime_state(
            executor, crash_handler, log_path, log_offset, runtime_state
        )
        if runtime_outcome in {_WAIT_CRASH, _WAIT_PROCESS_EXIT}:
            _mark_crash_stop(executor)
            _stop_fastbot_after_runtime_event(
                executor,
                process,
                "Unable to stop Fastbot after a target-app runtime failure",
            )
            return runtime_outcome
        if runtime_outcome == _WAIT_DEVICE_DISCONNECTED:
            _stop_fastbot_after_runtime_event(
                executor,
                process,
                "Unable to stop Fastbot after the device disconnected",
            )
            return runtime_outcome
        claim_permission_recovery = getattr(
            executor, "claim_permission_recovery_request", None
        )
        if callable(claim_permission_recovery) and claim_permission_recovery():
            marker = getattr(
                executor, "mark_permission_recovery_stop_requested", None
            )
            if callable(marker):
                marker()
            stopped_cleanly = bool(executor.stop_fastbot(process))
            if getattr(executor, "pressure_stop_reason", None) == "manual":
                if not stopped_cleanly:
                    raise RuntimeError(
                        "Unable to stop Fastbot after manual stop"
                    )
                return _WAIT_MANUAL_STOP
            return (
                _WAIT_PERMISSION_RECOVERY
                if stopped_cleanly
                else _WAIT_PERMISSION_RECOVERY_UNCLEAN
            )
        claim_audit_recovery = getattr(
            executor, "claim_audit_recovery_request", None
        )
        if callable(claim_audit_recovery) and claim_audit_recovery():
            marker = getattr(
                executor, "mark_audit_recovery_stop_requested", None
            )
            if callable(marker):
                marker()
            stopped_cleanly = bool(executor.stop_fastbot(process))
            if getattr(executor, "pressure_stop_reason", None) == "manual":
                if not stopped_cleanly:
                    raise RuntimeError("Unable to stop Fastbot after manual stop")
                return _WAIT_MANUAL_STOP
            return (
                _WAIT_AUDIT_RECOVERY
                if stopped_cleanly
                else _WAIT_AUDIT_RECOVERY_UNCLEAN
            )
        claim_recovery = getattr(executor, "claim_stall_recovery_request", None)
        if callable(claim_recovery) and claim_recovery():
            marker = getattr(executor, "mark_stall_recovery_stop_requested", None)
            if callable(marker):
                marker()
            stopped_cleanly = bool(executor.stop_fastbot(process))
            if getattr(executor, "pressure_stop_reason", None) == "manual":
                if not stopped_cleanly:
                    raise RuntimeError("Unable to stop Fastbot after manual stop")
                return _WAIT_MANUAL_STOP
            if not stopped_cleanly:
                return _WAIT_STALL_RECOVERY_UNCLEAN
            return _WAIT_STALL_RECOVERY
        if time.monotonic() >= deadline:
            if stop_at_duration:
                marker = getattr(executor, "mark_pressure_deadline_reached", None)
                if callable(marker):
                    marker()
                return _WAIT_PHASE_DEADLINE
            executor.stop_fastbot(process)
            raise TimeoutError(
                "Fastbot exceeded its %s-second deadline" % (duration_minutes * 60 + grace_seconds)
            )
        time.sleep(0.5)
    # The foreground guard can intentionally kill a paused detached Fastbot
    # to release a blocked UiAutomation/ActivityManager handoff. Its wrapper
    # then exits before this loop can observe the guard flags. Preserve the
    # stop owner recorded before SIGKILL instead of treating rc=137 as a
    # natural pressure-engine exit.
    if _pressure_stop_intent_reason(executor) == _WAIT_FOREGROUND_RECOVERY:
        return _WAIT_FOREGROUND_RECOVERY
    if _pressure_stop_intent_reason(executor) == _WAIT_PERMISSION_RECOVERY:
        return _WAIT_PERMISSION_RECOVERY
    return _WAIT_COMPLETED


def _wait_for_system_monkey(
    executor: TestExecutor,
    process: Any,
    duration_minutes: int,
    cancel_event: Any = None,
    crash_handler: Optional[CrashHandler] = None,
    log_path: Optional[Path] = None,
    log_offset: int = 0,
    duration_seconds: Optional[float] = None,
) -> str:
    """Keep system Monkey alive until the requested wall-clock duration."""
    window_seconds = (
        float(duration_seconds)
        if duration_seconds is not None
        else float(duration_minutes * 60)
    )
    deadline = time.monotonic() + max(1.0, window_seconds)
    runtime_state: Dict[str, Any] = {
        "started_at": time.monotonic(),
        "next_probe_at": 0.0,
        "known_pids": set(),
        "missing": 0,
        "disconnects": 0,
    }
    while process.poll() is None:
        _check_cancel(cancel_event)
        live_crash = _detect_live_crash_details(crash_handler, log_path, log_offset)
        if live_crash is not None:
            _record_runtime_detection(executor, "record_app_crash", live_crash)
        if _detect_live_app_crash_page(executor) or live_crash is not None:
            _mark_crash_stop(executor)
            _stop_fastbot_after_runtime_event(
                executor, process, "Unable to stop Monkey after a target-app crash"
            )
            return _WAIT_CRASH
        runtime_outcome = _probe_runtime_state(
            executor, crash_handler, log_path, log_offset, runtime_state
        )
        if runtime_outcome in {_WAIT_CRASH, _WAIT_PROCESS_EXIT}:
            _mark_crash_stop(executor)
            _stop_fastbot_after_runtime_event(
                executor,
                process,
                "Unable to stop Monkey after a target-app runtime failure",
            )
            return runtime_outcome
        if runtime_outcome == _WAIT_DEVICE_DISCONNECTED:
            _stop_fastbot_after_runtime_event(
                executor,
                process,
                "Unable to stop Monkey after the device disconnected",
            )
            return runtime_outcome
        claim_permission_recovery = getattr(
            executor, "claim_permission_recovery_request", None
        )
        if callable(claim_permission_recovery) and claim_permission_recovery():
            marker = getattr(
                executor, "mark_permission_recovery_stop_requested", None
            )
            if callable(marker):
                marker()
            stopped_cleanly = bool(executor.stop_fastbot(process))
            if getattr(executor, "pressure_stop_reason", None) == "manual":
                if not stopped_cleanly:
                    raise RuntimeError("Unable to stop Monkey after manual stop")
                return _WAIT_MANUAL_STOP
            return (
                _WAIT_PERMISSION_RECOVERY
                if stopped_cleanly
                else _WAIT_PERMISSION_RECOVERY_UNCLEAN
            )
        claim_recovery = getattr(executor, "claim_stall_recovery_request", None)
        if callable(claim_recovery) and claim_recovery():
            marker = getattr(executor, "mark_stall_recovery_stop_requested", None)
            if callable(marker):
                marker()
            stopped_cleanly = bool(executor.stop_fastbot(process))
            if getattr(executor, "pressure_stop_reason", None) == "manual":
                if not stopped_cleanly:
                    raise RuntimeError("Unable to stop Monkey after manual stop")
                return _WAIT_MANUAL_STOP
            if not stopped_cleanly:
                return _WAIT_STALL_RECOVERY_UNCLEAN
            return _WAIT_STALL_RECOVERY
        if time.monotonic() >= deadline:
            # The caller performs the normal clean stop and artifact cleanup.
            marker = getattr(executor, "mark_pressure_deadline_reached", None)
            if callable(marker):
                marker()
            return _WAIT_COMPLETED
        time.sleep(0.5)
    return _WAIT_COMPLETED


def _tail_text(value: str, limit: int = 512 * 1024) -> str:
    """Return a UTF-8-safe tail for bounded issue evidence."""
    content = str(value or "")
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return content
    return encoded[-limit:].decode("utf-8", errors="replace")


def _last_pressure_action(trajectory: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    events = trajectory.get("events", [])
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if isinstance(event, Mapping):
            return dict(event)
    return None


def _capture_runtime_failure_evidence(
    state_manager: StateManager,
    executor: TestExecutor,
    mutex: MutexController,
    report_dir: Path,
    cycle: int,
    phase: int,
    detection: Mapping[str, Any],
    trajectory: Mapping[str, Any],
    log_content: str,
    pressure_log_content: str,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Capture a self-contained evidence bundle before relaunching the app."""
    directory = (
        report_dir
        / "runtime_failures"
        / ("cycle_%03d" % cycle)
        / ("phase_%03d" % phase)
    )
    directory_error: Optional[str] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        # Preserve and return the in-memory evidence even when storage itself
        # is unavailable; callers must never lose the original crash result.
        directory_error = str(exc)
    captured_at = datetime.now(timezone.utc).isoformat()
    stop_reason = str(
        detection.get("stop_reason")
        or trajectory.get("stop_reason")
        or "runtime_failure"
    )
    evidence: Dict[str, Any] = {
        "captured_at": captured_at,
        "cycle": cycle,
        "phase": phase,
        "stop_reason": stop_reason,
        "detection": dict(detection),
        "last_action": _last_pressure_action(trajectory),
        # An unavailable probe is explicitly unknown.  Empty values are easy
        # for downstream reports to mistake for a successful negative check.
        "foreground_component": str(
            detection.get("foreground_component") or "unknown"
        ),
        "foreground_state": "unknown",
        "trajectory_summary": {
            "stop_reason": stop_reason,
            "operation_count": trajectory.get("operation_count", 0),
            "last_action": _last_pressure_action(trajectory),
        },
        "artifacts": {},
        "errors": [],
    }
    if directory_error:
        evidence["errors"].append("evidence directory: %s" % directory_error)
    manifest_path = directory / "failure.json"
    evidence["artifacts"]["failure_manifest"] = str(manifest_path)

    def persist_manifest() -> None:
        """Persist progress after every artifact attempt, even on partial failure."""
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            ReportGenerator.write_json(evidence, manifest_path)
            return
        except Exception as exc:
            message = "failure manifest: %s" % exc
            if message not in evidence["errors"]:
                evidence["errors"].append(message)
        # Keep a second, independent writer for reporting-layer/test-double
        # failures.  The first write is intentionally before optional probes.
        try:
            write_checkpoint_json(manifest_path, evidence)
        except Exception:
            # Last-resort plain writer for reporting/checkpoint test doubles or
            # a partially unavailable reporting layer.  The manifest is the
            # minimum evidence contract and must remain readable as JSON.
            try:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception:
                logger.warning(
                    "Unable to persist runtime failure manifest", exc_info=True
                )

    persist_manifest()

    logcat_path = directory / "logcat_tail.log"
    try:
        logcat_path.write_text(_tail_text(log_content), encoding="utf-8")
        evidence["artifacts"]["logcat_tail"] = str(logcat_path)
    except Exception as exc:
        evidence["errors"].append("logcat tail: %s" % exc)
    persist_manifest()
    pressure_path = directory / "fastbot_tail.log"
    try:
        pressure_path.write_text(_tail_text(pressure_log_content), encoding="utf-8")
        evidence["artifacts"]["fastbot_tail"] = str(pressure_path)
    except Exception as exc:
        evidence["errors"].append("Fastbot tail: %s" % exc)
    persist_manifest()

    # Preserve the exact phase that triggered the failure.  The cycle-level
    # trajectory is updated after recovery and can otherwise point at a later
    # phase, making replay silently execute the wrong event sequence.
    trajectory_path = directory / "trajectory.json"
    try:
        replay_options = (
            config.get("crash_replay", {})
            if isinstance(config, Mapping)
            and isinstance(config.get("crash_replay", {}), Mapping)
            else {}
        )
        trajectory_snapshot = build_replay_trajectory(
            trajectory,
            replay_options,
            package_name=state_manager.package_name,
            pressure_log_path=report_dir / "fastbot.log",
            cycle=cycle,
            phase=phase,
            crash_at=detection.get("detected_at"),
        )
        trajectory_snapshot["crash_context"] = {
            "cycle": cycle,
            "phase": phase,
            "captured_at": evidence["captured_at"],
        }
        ReportGenerator.write_json(trajectory_snapshot, trajectory_path)
        evidence["artifacts"]["trajectory"] = str(trajectory_path)
    except Exception as exc:
        evidence["errors"].append("trajectory: %s" % exc)
    persist_manifest()

    diagnostics = getattr(executor, "capture_process_diagnostics", None)
    if callable(diagnostics):
        try:
            evidence["artifacts"].update(diagnostics(str(directory / "diagnostics")))
        except Exception as exc:
            evidence["errors"].append("process diagnostics: %s" % exc)
            logger.warning("Unable to capture process diagnostics", exc_info=True)
        persist_manifest()

    acquired = False
    device = None
    hierarchy = ""
    try:
        device = state_manager.get_device()
        acquired = bool(mutex.acquire("uiautomator2", device))
        if not acquired:
            evidence["errors"].append("uiautomator2 control unavailable")
        else:
            component_reader = getattr(state_manager, "current_component", None)
            if callable(component_reader):
                try:
                    evidence["foreground_component"] = str(
                        component_reader() or "unknown"
                    )
                    evidence["foreground_state"] = (
                        "known"
                        if evidence["foreground_component"] != "unknown"
                        else "unknown"
                    )
                except Exception as exc:
                    evidence["errors"].append("foreground component: %s" % exc)
            matcher = getattr(CrashHandler, "is_app_crash_page_component", None)
            if not callable(matcher):
                matcher = _is_app_crash_page_component
            try:
                is_app_crash_page = bool(
                    matcher(
                        evidence.get("foreground_component"),
                        state_manager.package_name,
                    )
                )
            except Exception:
                is_app_crash_page = _is_app_crash_page_component(
                    evidence.get("foreground_component"), state_manager.package_name
                )
            if is_app_crash_page:
                evidence["page_type"] = "app_crash_page"
            persist_manifest()
            screenshot_reader = getattr(
                state_manager, "capture_screenshot_png", None
            )
            if callable(screenshot_reader):
                try:
                    screenshot_path = directory / "screen.png"
                    screenshot_path.write_bytes(screenshot_reader())
                    evidence["artifacts"]["screenshot"] = str(screenshot_path)
                except Exception as exc:
                    evidence["errors"].append("screenshot: %s" % exc)
                persist_manifest()
            hierarchy_reader = getattr(state_manager, "dump_hierarchy_text", None)
            if callable(hierarchy_reader):
                try:
                    hierarchy = str(hierarchy_reader() or "")
                    if hierarchy:
                        hierarchy_path = directory / "ui.xml"
                        hierarchy_path.write_text(hierarchy, encoding="utf-8")
                        evidence["artifacts"]["ui_hierarchy"] = str(hierarchy_path)
                        evidence["ui_hierarchy_sha256"] = hashlib.sha256(
                            hierarchy.encode("utf-8")
                        ).hexdigest()
                except Exception as exc:
                    evidence["errors"].append("hierarchy: %s" % exc)
                persist_manifest()
            if is_app_crash_page:
                # The crash page can be gone by the time the phase returns. Do
                # this while the runtime-failure lock still owns the device.
                text_extractor = getattr(
                    CrashHandler, "extract_app_crash_page_text", None
                )
                if callable(text_extractor):
                    try:
                        evidence["page_text"] = str(text_extractor(hierarchy) or "")
                    except Exception as exc:
                        evidence["page_text"] = hierarchy
                        evidence["errors"].append("crash page text: %s" % exc)
                else:
                    evidence["page_text"] = hierarchy
                copy_selector = None
                try:
                    selector_factory = getattr(
                        getattr(state_manager, "d", None), "__call__", None
                    )
                    if callable(selector_factory):
                        for copy_text in ("\u590d\u5236", "Copy", "\u6fb6\u5d85\u57d7"):
                            candidate = state_manager.d(text=copy_text)
                            if candidate.wait(timeout=1):
                                copy_selector = candidate
                                break
                    if copy_selector is None:
                        evidence["copy_status"] = "not_found"
                    else:
                        copy_selector.click()
                        time.sleep(0.2)
                        copied = str(
                            getattr(state_manager.d, "clipboard", "") or ""
                        ).strip()
                        if copied:
                            copied_path = directory / "crash_page_copied.txt"
                            copied_path.write_text(copied, encoding="utf-8")
                            evidence["copied_text"] = copied
                            evidence["copy_status"] = "captured"
                            evidence["artifacts"]["crash_page_copied_text"] = str(
                                copied_path
                            )
                        else:
                            evidence["copy_status"] = "empty_clipboard"
                except Exception as exc:
                    evidence["copy_status"] = "failed"
                    evidence["errors"].append("copy: %s" % exc)
                    logger.warning(
                        "Unable to copy runtime app crash-page text", exc_info=True
                    )
                persist_manifest()
    except Exception as exc:
        # Device lookup and mutex acquisition are auxiliary evidence probes;
        # neither is allowed to replace the original crash classification.
        evidence["errors"].append("device/control: %s" % exc)
        persist_manifest()
    finally:
        if acquired:
            try:
                released = mutex.release("uiautomator2", device)
                if released is False:
                    release_error = RuntimeError(
                        "uiautomator2 control release returned false during runtime-failure evidence capture"
                    )
                    evidence["errors"].append(str(release_error))
                    _mark_control_release_failure(executor, evidence, release_error)
            except Exception as exc:
                evidence["errors"].append("release uiautomator2: %s" % exc)
                _mark_control_release_failure(executor, evidence, exc)
        persist_manifest()
    return evidence


def _wait_for_target_app_foreground(
    state_manager: StateManager, timeout: float = 8.0
) -> Optional[str]:
    """Wait briefly for a relaunch to settle on the target package.

    Android vendors commonly expose Launcher or a splash Activity for a few
    frames after ``am start``. A single immediate probe turns that normal
    transition into a false recovery failure, so use the StateManager's
    bounded waiter when available and then perform a short final poll.
    """
    deadline = time.monotonic() + max(0.1, min(30.0, float(timeout)))
    component_reader = getattr(state_manager, "current_component", None)

    def read_target() -> Optional[str]:
        if not callable(component_reader):
            return None
        try:
            component = str(component_reader() or "").strip()
        except Exception:
            return None
        if component.split("/", 1)[0] == state_manager.package_name:
            return component
        return None

    current = read_target()
    if current:
        return current

    waiter = getattr(state_manager, "_wait_for_target_foreground", None)
    if callable(waiter):
        remaining = max(0.1, deadline - time.monotonic())
        try:
            waited = waiter(timeout=min(remaining, 12.0))
        except TypeError as exc:
            if "timeout" not in str(exc):
                raise
            waited = waiter()
        except Exception:
            logger.debug(
                "Target foreground waiter failed during relaunch", exc_info=True
            )
            waited = False
        current = read_target()
        if current:
            return current
        if waited is True and not callable(component_reader):
            return "%s/.unknown" % state_manager.package_name

    while time.monotonic() < deadline:
        current = read_target()
        if current:
            return current
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return None


def _config_after_completed_audit_gate(
    config: Mapping[str, Any],
    device_serial: str = "",
) -> Tuple[Dict[str, Any], int]:
    """Copy a scenario while removing startup-gate actions already completed."""
    prepared_config = copy.deepcopy(dict(config))
    scenario = prepared_config.get("scenario", {})
    if not isinstance(scenario, dict):
        return prepared_config, 0

    audit_options = scenario.get("audit_recovery", {})
    if not isinstance(audit_options, Mapping):
        audit_options = {}
    app = prepared_config.get("app", {})
    package_name = (
        str(app.get("package_name", "")).strip()
        if isinstance(app, Mapping)
        else ""
    )
    configured_audit_locator = audit_options.get("audit_locator")
    if not isinstance(configured_audit_locator, Mapping):
        audit_resource_id = str(
            audit_options.get("audit_resource_id", "")
        ).strip()
        if not audit_resource_id and package_name:
            audit_resource_id = "%s:id/auditRadioButton" % package_name
        configured_audit_locator = (
            {"resourceId": audit_resource_id} if audit_resource_id else {}
        )
    configured_confirm_locator = audit_options.get("confirm_locator")
    if not isinstance(configured_confirm_locator, Mapping):
        confirm_text = str(
            audit_options.get("confirm_text", "\u786e\u5b9a")
        ).strip()
        configured_confirm_locator = {"text": confirm_text} if confirm_text else {}

    locator_keys = (
        "xpath",
        "text",
        "textContains",
        "description",
        "descriptionContains",
        "resourceId",
        "resourceIdMatches",
        "className",
        "instance",
    )

    def step_locator(step: Mapping[str, Any]) -> Dict[str, Any]:
        nested = step.get("locator")
        source = nested if isinstance(nested, Mapping) else step
        return {
            key: source[key]
            for key in locator_keys
            if key in source and source[key] is not None
        }

    def locator_matches(
        candidate: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> bool:
        if not candidate or not expected:
            return False
        candidate_values = {key: str(value) for key, value in candidate.items()}
        expected_values = {key: str(value) for key, value in expected.items()}
        if "xpath" in candidate_values or "xpath" in expected_values:
            return (
                candidate_values.get("xpath")
                == expected_values.get("xpath")
                and "xpath" in candidate_values
                and "xpath" in expected_values
            )
        if (
            "instance" in candidate_values or "instance" in expected_values
        ) and candidate_values.get("instance") != expected_values.get("instance"):
            return False
        for key in (
            "resourceId",
            "text",
            "description",
            "className",
            "resourceIdMatches",
            "textContains",
            "descriptionContains",
        ):
            if (
                key in candidate_values
                and key in expected_values
                and candidate_values[key] != expected_values[key]
            ):
                return False
        if candidate_values == expected_values:
            return True
        for key in ("resourceId", "text", "description", "className"):
            if key in candidate and key in expected:
                return str(candidate[key]) == str(expected[key])
        if "resourceIdMatches" in candidate and "resourceIdMatches" in expected:
            return str(candidate["resourceIdMatches"]) == str(
                expected["resourceIdMatches"]
            )
        candidate_resource_id = candidate.get("resourceId")
        expected_resource_pattern = expected.get("resourceIdMatches")
        if candidate_resource_id is not None and expected_resource_pattern is not None:
            try:
                return re.fullmatch(
                    str(expected_resource_pattern), str(candidate_resource_id)
                ) is not None
            except re.error:
                return False
        expected_resource_id = expected.get("resourceId")
        candidate_resource_pattern = candidate.get("resourceIdMatches")
        if expected_resource_id is not None and candidate_resource_pattern is not None:
            try:
                return re.fullmatch(
                    str(candidate_resource_pattern), str(expected_resource_id)
                ) is not None
            except re.error:
                return False
        for exact_key, contains_key in (
            ("text", "textContains"),
            ("description", "descriptionContains"),
        ):
            exact_value = candidate.get(exact_key)
            contains_value = expected.get(contains_key)
            if exact_value is not None and contains_value is not None:
                return str(contains_value) in str(exact_value)
            exact_value = expected.get(exact_key)
            contains_value = candidate.get(contains_key)
            if exact_value is not None and contains_value is not None:
                return str(contains_value) in str(exact_value)
            if contains_key in candidate and contains_key in expected:
                return str(candidate[contains_key]) == str(expected[contains_key])
        return False

    skipped = 0

    def without_audit_steps(
        steps: Any, initial_skip_confirmation: bool = False
    ) -> Tuple[Any, bool]:
        nonlocal skipped
        if not isinstance(steps, list):
            return steps, False
        retained = []
        skip_confirmation = initial_skip_confirmation
        for step in steps:
            if not isinstance(step, Mapping):
                skip_confirmation = False
                retained.append(step)
                continue
            action = str(step.get("action", "")).strip().lower()
            candidate_locator = step_locator(step)
            is_audit_step = (
                action in _RUNTIME_RECOVERY_AUDIT_ACTIONS
                or (
                    action in {
                        "click",
                        "tap",
                        "click_if_exists",
                        "optional_click",
                    }
                    and locator_matches(
                        candidate_locator, configured_audit_locator
                    )
                )
                or (
                    action
                    in {"click", "tap", "click_if_exists", "optional_click"}
                    and str(candidate_locator.get("text") or "")
                    == "\u5ba1\u6838\u4e2d"
                )
            )
            if is_audit_step:
                skipped += 1
                skip_confirmation = True
                continue
            is_confirmation = (
                action in {"click", "tap"}
                and locator_matches(
                    candidate_locator, configured_confirm_locator
                )
            )
            if skip_confirmation and is_confirmation:
                skipped += 1
                skip_confirmation = False
                continue
            skip_confirmation = False
            retained.append(step)
        return retained, skip_confirmation

    for key in ("pre_login_steps", "navigation_steps"):
        per_device = scenario.get("%s_by_device" % key)
        if device_serial:
            carry_confirmation = False
            if isinstance(per_device, dict) and device_serial in per_device:
                per_device[device_serial], carry_confirmation = without_audit_steps(
                    per_device[device_serial]
                )
            if key in scenario:
                scenario[key], _ = without_audit_steps(
                    scenario[key], carry_confirmation
                )
            continue

        if key in scenario:
            scenario[key], _ = without_audit_steps(scenario[key])
        if not isinstance(per_device, dict):
            continue
        for serial, steps in list(per_device.items()):
            per_device[serial], _ = without_audit_steps(steps)
    return prepared_config, skipped


def _optional_recovery_steps(
    config: Mapping[str, Any], device_serial: str
) -> List[Dict[str, Any]]:
    """Return optional scenario steps in the same order used for one device."""
    scenario = config.get("scenario", {})
    if not isinstance(scenario, Mapping):
        return []
    phase_keys = ["navigation_steps"]
    if bool(scenario.get("login_enabled", True)):
        phase_keys.insert(0, "pre_login_steps")

    optional_steps: List[Dict[str, Any]] = []
    for key in phase_keys:
        per_device = scenario.get("%s_by_device" % key, {})
        device_steps = (
            per_device.get(device_serial, [])
            if isinstance(per_device, Mapping)
            else []
        )
        shared_steps = scenario.get(key, [])
        for steps in (device_steps, shared_steps):
            if not isinstance(steps, list):
                continue
            optional_steps.extend(
                copy.deepcopy(dict(step))
                for step in steps
                if isinstance(step, Mapping)
                and str(step.get("action", "")).strip().lower()
                in {"click_if_exists", "optional_click"}
            )
    return optional_steps


def _recover_after_runtime_failure(
    state_manager: StateManager,
    mutex: MutexController,
    config: Mapping[str, Any],
    stop_reason: str,
    cancel_event: Any = None,
    *,
    executor: Any = None,
) -> Dict[str, Any]:
    """Restart only the target app and restore its configured pressure page."""
    result: Dict[str, Any] = {
        "action": "force_stop_clear_task_relaunch_and_prepare",
        "reason": stop_reason,
        "foreground_recovery_strategy": _foreground_recovery_strategy(state_manager),
        "attempted": False,
        "data_cleared": False,
        "reinstalled": False,
        "success": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    attempts: List[Dict[str, Any]] = []
    result["attempts"] = attempts
    input_reason = getattr(executor, "input_recovery_reason", None)
    max_attempts = (
        int(config.get("input_safety", {}).get("recovery_attempts", 3))
        if isinstance(input_reason, str) and input_reason
        else _RUNTIME_RECOVERY_ATTEMPTS
    )
    result["max_attempts"] = max_attempts
    release_failure_messages: List[str] = []
    release_failure_published = False

    options = config.get("crash_recovery", {})
    raw_backoff = (
        options.get("restart_backoff_seconds", 1.0)
        if isinstance(options, Mapping)
        else 1.0
    )
    try:
        backoff = min(60.0, max(0.0, float(raw_backoff)))
    except (TypeError, ValueError):
        backoff = 1.0
    scenario_options = config.get("scenario", {})
    audit_options = (
        scenario_options.get("audit_recovery", {})
        if isinstance(scenario_options, Mapping)
        else {}
    )
    audit_configured = bool(
        isinstance(audit_options, Mapping) and audit_options.get("enabled", False)
    )
    preparation_config, skipped_audit_steps = (
        _config_after_completed_audit_gate(
            config, str(getattr(state_manager, "device_serial", ""))
        )
        if audit_configured
        else (copy.deepcopy(dict(config)), 0)
    )
    if skipped_audit_steps:
        result["skipped_completed_audit_steps"] = skipped_audit_steps
    preparation_config["_input_recovery_managed"] = True

    def wait_before_attempt(delay: float) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        while time.monotonic() < deadline:
            _check_cancel(cancel_event)
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    try:
        _check_cancel(cancel_event)
        if backoff > 0:
            wait_before_attempt(backoff)

        for attempt_number in range(1, max_attempts + 1):
            _check_cancel(cancel_event)
            attempt: Dict[str, Any] = {
                "attempt": attempt_number,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "stage": "acquire_uiautomator2",
                "success": False,
            }
            attempts.append(attempt)
            result["attempted"] = True
            device = None
            acquired = False
            try:
                device = state_manager.get_device()
                if not mutex.acquire("uiautomator2", device):
                    raise RuntimeError(
                        "uiautomator2 control unavailable for runtime recovery"
                    )
                acquired = True

                attempt["stage"] = "reset_app"
                if not _reset_app(
                    state_manager,
                    clear_data=False,
                    stop_packages=None,
                    clear_task=True,
                ):
                    raise RuntimeError("target app force-stop/relaunch failed")

                attempt["stage"] = "complete_audit_gate"
                audit_gate_completed = _complete_configured_audit_gate(
                    state_manager,
                    config,
                    cancel_event=cancel_event,
                )
                if audit_configured:
                    attempt["audit_gate_completed"] = audit_gate_completed
                if not audit_gate_completed:
                    raise RuntimeError(
                        "startup audit gate could not be completed after relaunch"
                    )

                attempt["stage"] = "prepare_target_page"
                scenario = prepare_test_scenario(
                    state_manager, copy.deepcopy(preparation_config)
                )
                if not scenario.get("login_success", True) or not scenario.get(
                    "navigation_success", True
                ):
                    # A process kill can roll the app back to a first-run consent
                    # page. Tap configured optional controls once, then retry the
                    # preparation flow without repeating the completed gate.
                    optional_steps = _optional_recovery_steps(
                        preparation_config, state_manager.device_serial
                    )
                    if optional_steps:
                        navigator = getattr(state_manager, "navigate_to", None)
                        optional_navigation_ok = bool(
                            callable(navigator)
                            and navigator(
                                "RecoveryOptionalPreparation", optional_steps
                            )
                        )
                        optional_preparation = {
                            "attempted": True,
                            "success": optional_navigation_ok,
                            "step_count": len(optional_steps),
                        }
                        attempt["optional_preparation"] = optional_preparation
                        if optional_navigation_ok:
                            scenario = prepare_test_scenario(
                                state_manager, copy.deepcopy(preparation_config)
                            )
                attempt["preparation"] = scenario
                if not scenario.get("login_success", True) or not scenario.get(
                    "navigation_success", True
                ):
                    preparation_error = str(
                        scenario.get("navigation_error") or ""
                    ).strip()
                    raise RuntimeError(
                        "target page preparation failed after relaunch%s"
                        % (
                            ": %s" % preparation_error
                            if preparation_error
                            else ""
                        )
                    )

                attempt["stage"] = "verify_foreground"
                # Older integrations do not expose a foreground-component
                # probe. Preserve their established recovery contract while
                # keeping the production StateManager fail-closed.
                component_reader = getattr(state_manager, "current_component", None)
                if callable(component_reader):
                    component = _wait_for_target_app_foreground(
                        state_manager, timeout=8.0
                    )
                    attempt["foreground_component"] = component
                    if component is None:
                        raise RuntimeError(
                            "target app is not foreground after relaunch"
                        )
                else:
                    attempt["foreground_verification_skipped"] = True
                attempt["stage"] = "complete"
                attempt["success"] = True
            except (InterruptedError, KeyboardInterrupt):
                attempt["stage"] = "cancelled"
                attempt["error"] = "runtime failure recovery was cancelled"
                raise
            except Exception as exc:
                _check_cancel(cancel_event)
                attempt["error"] = str(exc)
                result["error"] = str(exc)
                logger.warning(
                    "Runtime recovery attempt %s/%s failed on %s at %s: %s",
                    attempt_number,
                    max_attempts,
                    state_manager.device_serial,
                    attempt["stage"],
                    exc,
                )
            finally:
                if acquired:
                    release_error: Optional[BaseException] = None
                    try:
                        if mutex.release("uiautomator2", device) is False:
                            release_error = RuntimeError(
                                "uiautomator2 control was not released"
                            )
                    except Exception as exc:
                        release_error = exc
                    if release_error is not None:
                        attempt["stage"] = "release_uiautomator2"
                        attempt["success"] = False
                        attempt["error"] = str(release_error)
                        result["error"] = str(release_error)
                        attempt["control_release_failed"] = True
                        message = str(release_error).strip() or type(release_error).__name__
                        if message not in release_failure_messages:
                            release_failure_messages.append(message)
                attempt["finished_at"] = datetime.now(timezone.utc).isoformat()

            # A blocking reset, scenario step, foreground probe, or control
            # release may finish just after cancellation was requested.
            # Let cancellation win before this attempt is classified or
            # another complete recovery transaction begins.
            _check_cancel(cancel_event)
            if attempt["success"]:
                result["success"] = True
                result["attempt_count"] = len(attempts)
                for key in (
                    "audit_gate_completed",
                    "optional_preparation",
                    "preparation",
                    "foreground_component",
                ):
                    if key in attempt:
                        result[key] = copy.deepcopy(attempt[key])
                result.pop("error", None)
                return result
            if attempt_number < max_attempts and backoff > 0:
                wait_before_attempt(min(60.0, backoff * attempt_number))

        result["attempt_count"] = len(attempts)
        if release_failure_messages:
            _mark_control_release_failure(
                executor,
                result,
                "; ".join(release_failure_messages),
            )
            release_failure_published = True
        return result
    except (InterruptedError, KeyboardInterrupt):
        # Cancellation must reach _run_cycle so the run is reported as
        # interrupted instead of as a failed recovery transaction.
        raise
    except Exception as exc:
        logger.exception(
            "Runtime failure recovery failed on %s", state_manager.device_serial
        )
        result["error"] = str(exc)
        return result
    finally:
        result["attempt_count"] = len(attempts)
        if (
            release_failure_messages
            and not result.get("success")
            and not release_failure_published
        ):
            _mark_control_release_failure(
                executor,
                result,
                "; ".join(release_failure_messages),
            )
        result["completed_at"] = datetime.now(timezone.utc).isoformat()


def _run_fastbot_phase(
    state_manager: StateManager,
    executor: TestExecutor,
    mutex: MutexController,
    config: Mapping[str, Any],
    report_dir: Path,
    cycle: int,
    cancel_event: Any = None,
    *,
    resume_context: Optional[Mapping[str, Any]] = None,
    checkpoint: Optional[CheckpointHeartbeat] = None,
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    """Run one pressure budget, resuming the same cycle after safe recovery."""
    state_manager._input_cycle = cycle
    test_options = config.get("test", {})
    if not isinstance(test_options, Mapping):
        test_options = {}
    duration_seconds = max(
        1, int(test_options.get("fastbot_duration_minutes", 1)) * 60
    )
    configured_cycles = max(1, int(test_options.get("max_cycles", 1) or 1))
    resume_context = resume_context if isinstance(resume_context, Mapping) else {}
    try:
        resumed_elapsed = min(
            float(duration_seconds),
            max(0.0, float(resume_context.get("elapsed_seconds", 0) or 0)),
        )
    except (TypeError, ValueError):
        resumed_elapsed = 0.0
    try:
        total_operations = max(
            0, int(resume_context.get("operation_count", 0) or 0)
        )
    except (TypeError, ValueError):
        total_operations = 0
    recovery_results: List[Dict[str, Any]] = []
    foreground_recovery_results: List[Dict[str, Any]] = []
    pressure_engine_failures: List[Dict[str, Any]] = []
    runtime_failures: List[Dict[str, Any]] = []
    pressure_phases = [
        copy.deepcopy(dict(item))
        for item in resume_context.get("pressure_phases", [])
        if isinstance(item, Mapping)
    ] if isinstance(resume_context.get("pressure_phases", []), list) else []
    replay_events: List[Dict[str, Any]] = []
    replay_events_truncated = False
    phase_log_contents: List[str] = []
    combined_artifacts: Dict[str, Any] = {}
    all_stall_events: List[Dict[str, Any]] = []
    permission_recovery_results: List[Dict[str, Any]] = []
    permission_recovery_attempts = 0
    permission_recoveries_without_progress = 0
    last_permission_recovery_elapsed = resumed_elapsed
    last_permission_recovery_operations = total_operations
    max_permission_recoveries_without_progress = 8
    audit_recovery_attempts = 0
    audit_recoveries_without_progress = 0
    last_audit_recovery_elapsed = 0.0
    max_audit_recoveries_without_progress = 3
    audit_recovery_progress_seconds = 5.0
    foreground_recovery_attempts = 0
    consecutive_input_recoveries = 0
    pressure_engine_restarts = 0
    crash_restarts = 0
    raw_crash_options = config.get("crash_recovery")
    crash_options = (
        raw_crash_options if isinstance(raw_crash_options, Mapping) else {}
    )
    # ``load_config`` materializes the production default (enabled=true).
    # Direct callers and legacy adapters may supply an intentionally minimal
    # config; do not silently launch a multi-attempt recovery transaction for
    # those callers when the feature was never configured.
    crash_recovery_enabled = bool(
        crash_options.get("enabled", False)
        if isinstance(raw_crash_options, Mapping)
        else False
    )
    max_crash_restarts = max(
        0, int(crash_options.get("max_restarts_per_cycle", 2))
    )
    # Crash policy: a crash restarts the app and the run continues with the
    # remaining budget, because the next attempt may reach a different feature
    # branch. Limits are therefore opt-in (0 = unlimited) and only guard against
    # an app that crashes immediately on every launch.
    max_crash_restarts_per_signature = max(
        0, int(crash_options.get("max_restarts_per_signature", 0))
    )
    max_total_crash_restarts = max(
        0, int(crash_options.get("max_total_restarts_per_cycle", 0))
    )
    startup_crash_window = max(
        0.0, float(crash_options.get("startup_crash_window_seconds", 15))
    )
    max_startup_crashes = max(
        1, int(crash_options.get("max_startup_crashes", 5))
    )
    consecutive_startup_crashes = 0
    crash_restarts_by_signature: Dict[str, int] = {}
    minimum_remaining = max(
        1.0, float(crash_options.get("minimum_remaining_seconds", 5))
    )
    last_trajectory: Dict[str, Any] = {}
    try:
        phase_index = max(
            len(pressure_phases), int(resume_context.get("phase", 0) or 0)
        )
    except (TypeError, ValueError):
        phase_index = len(pressure_phases)
    engine_elapsed_total = resumed_elapsed

    def finalize(trajectory: Dict[str, Any]) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
        trajectory.setdefault(
            "foreground_recovery_strategy",
            _foreground_recovery_strategy(state_manager),
        )
        trajectory["operation_count"] = total_operations
        trajectory["pressure_phases"] = list(pressure_phases)
        trajectory["pressure_elapsed_seconds"] = round(engine_elapsed_total, 3)
        # Keep the last phase's control-plane verdict visible after a
        # multi-phase recovery run.  A missing field is left untouched for
        # older executor adapters; real executors publish it explicitly.
        if "pressure_stop_verified" not in trajectory and isinstance(
            last_trajectory, Mapping
        ):
            if "pressure_stop_verified" in last_trajectory:
                trajectory["pressure_stop_verified"] = bool(
                    last_trajectory.get("pressure_stop_verified")
                )
        if "control_handoff_safe" not in trajectory and isinstance(
            last_trajectory, Mapping
        ):
            if "control_handoff_safe" in last_trajectory:
                trajectory["control_handoff_safe"] = bool(
                    last_trajectory.get("control_handoff_safe")
                )
        for key in _CYCLE_HANDOFF_BOOL_FIELDS:
            if (
                key not in trajectory
                and isinstance(last_trajectory, Mapping)
                and isinstance(last_trajectory.get(key), bool)
            ):
                trajectory[key] = bool(last_trajectory.get(key))
        for key in _CYCLE_HANDOFF_ERROR_FIELDS:
            if (
                key not in trajectory
                and isinstance(last_trajectory, Mapping)
                and last_trajectory.get(key) not in (None, "")
            ):
                trajectory[key] = str(last_trajectory.get(key))
        if (
            "screenshot_collection_active" not in trajectory
            and isinstance(last_trajectory, Mapping)
            and isinstance(last_trajectory.get("screenshot_collection_active"), bool)
        ):
            trajectory["screenshot_collection_active"] = bool(
                last_trajectory.get("screenshot_collection_active")
            )
        if replay_events:
            trajectory["events"] = replay_events[-_MAX_REPLAY_EVENTS:]
            trajectory["event_count"] = len(trajectory["events"])
            trajectory["replayable"] = True
            if replay_events_truncated or len(replay_events) > _MAX_REPLAY_EVENTS:
                trajectory["events_truncated"] = True
                trajectory["replay_note"] = (
                    "Only the most recent %d structured events are retained for replay."
                    % _MAX_REPLAY_EVENTS
                )
        if all_stall_events:
            trajectory["stall_events"] = all_stall_events
            trajectory["stall_detection_count"] = len(all_stall_events)
        if recovery_results:
            trajectory["stall_recoveries"] = recovery_results
            _apply_stall_recovery_summary(trajectory, recovery_results)
        if runtime_failures:
            trajectory["runtime_failures"] = runtime_failures
            trajectory["runtime_failure_count"] = len(runtime_failures)
            trajectory["runtime_recovery_count"] = sum(
                1
                for item in runtime_failures
                if isinstance(item.get("recovery"), Mapping)
                and item["recovery"].get("success")
            )
            trajectory["stopped_on_crash"] = True
            latest_process_exit = next(
                (
                    item.get("process_exit")
                    for item in reversed(runtime_failures)
                    if isinstance(item.get("process_exit"), Mapping)
                ),
                None,
            )
            if isinstance(latest_process_exit, Mapping):
                trajectory["app_process_exit"] = dict(latest_process_exit)
            for failure in runtime_failures:
                recovery = failure.get("recovery")
                if not isinstance(recovery, Mapping) or not recovery.get("success"):
                    continue
                failure_phase = int(failure.get("phase", 0) or 0)
                recovery["continued_pressure"] = any(
                    int(item.get("phase", 0) or 0) > failure_phase
                    for item in pressure_phases
                )
        if foreground_recovery_results:
            trajectory["foreground_recoveries"] = list(
                foreground_recovery_results
            )
            trajectory["foreground_recovery_count"] = sum(
                1
                for item in foreground_recovery_results
                if isinstance(item, Mapping) and item.get("attempted")
            )
        if permission_recovery_results:
            trajectory["permission_recoveries"] = list(
                permission_recovery_results
            )
            trajectory["permission_recovery_attempt_count"] = len(
                permission_recovery_results
            )
            trajectory["permission_recovery_success_count"] = sum(
                1
                for item in permission_recovery_results
                if isinstance(item, Mapping) and item.get("success")
            )
            trajectory["permission_recovery_failure_count"] = sum(
                1
                for item in permission_recovery_results
                if isinstance(item, Mapping) and not item.get("success")
            )
        if pressure_engine_failures:
            trajectory["pressure_engine_failures"] = list(
                pressure_engine_failures
            )
            trajectory["pressure_engine_failure_count"] = len(
                pressure_engine_failures
            )
            trajectory["pressure_engine_recovery_count"] = sum(
                1
                for item in pressure_engine_failures
                if isinstance(item.get("recovery"), Mapping)
                and item["recovery"].get("success")
            )
            for failure in pressure_engine_failures:
                recovery = failure.get("recovery")
                if not isinstance(recovery, Mapping) or not recovery.get("success"):
                    continue
                failure_phase = int(failure.get("phase", 0) or 0)
                recovery["continued_pressure"] = any(
                    int(item.get("phase", 0) or 0) > failure_phase
                    for item in pressure_phases
                )
        return trajectory, "\n".join(phase_log_contents), dict(combined_artifacts)

    while True:
        remaining_seconds = max(0.0, duration_seconds - engine_elapsed_total)
        if phase_index and remaining_seconds < 1.0:
            return finalize(last_trajectory)
        phase_config = copy.deepcopy(dict(config))
        phase_config.setdefault("test", {})["fastbot_duration_minutes"] = max(
            1, int(math.ceil(remaining_seconds / 60.0))
        )
        phase_index += 1
        pressure_log_path = report_dir / "fastbot.log"
        logcat_path = report_dir / "logcat.log"
        if checkpoint is not None:
            checkpoint.begin_phase(
                phase_index,
                elapsed_seconds=engine_elapsed_total,
                operation_count=total_operations,
                logcat_offset=logcat_path.stat().st_size if logcat_path.exists() else 0,
                remote_log_offset=int(
                    resume_context.get("remote_log_offset", 0) or 0
                ) if phase_index == int(resume_context.get("phase", 0) or 0) + 1 else 0,
            )
            trajectory, log_content, artifacts = _run_fastbot_phase_once(
                state_manager,
                executor,
                mutex,
                phase_config,
                report_dir,
                cycle,
                cancel_event,
                remaining_seconds=remaining_seconds,
                checkpoint=checkpoint,
                operation_base=total_operations,
            )
        else:
            trajectory, log_content, artifacts = _run_fastbot_phase_once(
                state_manager,
                executor,
                mutex,
                phase_config,
                report_dir,
                cycle,
                cancel_event,
                remaining_seconds=remaining_seconds,
            )
        last_trajectory = trajectory
        phase_log_contents.append(log_content)
        combined_artifacts.update(artifacts)
        phase_events = trajectory.get("events", [])
        if isinstance(phase_events, list):
            for event in phase_events:
                if not isinstance(event, Mapping):
                    continue
                copied_event = dict(event)
                copied_event.setdefault("cycle", cycle)
                copied_event.setdefault("phase", phase_index)
                replay_events.append(copied_event)
            if len(replay_events) > _MAX_REPLAY_EVENTS * 2:
                replay_events = replay_events[-_MAX_REPLAY_EVENTS:]
                replay_events_truncated = True
        operations_before_phase = total_operations
        phase_operation_value = max(
            0, int(trajectory.get("operation_count", 0) or 0)
        )
        # Built-in executors publish a per-invocation delta explicitly.  Keep
        # compatibility with older adapters (which returned a delta by
        # convention) while accepting a cumulative value from newer adapters
        # without adding the already-counted prefix a second time.
        explicit_total = trajectory.get("total_operation_count")
        if explicit_total is not None:
            try:
                phase_operation_value = max(phase_operation_value, int(explicit_total))
            except (TypeError, ValueError, OverflowError):
                pass
        if trajectory.get("operation_count_is_total") is True or trajectory.get(
            "operation_count_mode"
        ) in {"total", "cumulative"} or trajectory.get("total_operation_count") is not None:
            total_operations = max(total_operations, phase_operation_value)
            phase_operation_delta = max(0, total_operations - operations_before_phase)
        else:
            total_operations += phase_operation_value
            phase_operation_delta = phase_operation_value
        all_stall_events.extend(list(trajectory.get("stall_events", [])))
        elapsed_before_phase = engine_elapsed_total
        phase_details = trajectory.get("phase", {})
        if not isinstance(phase_details, Mapping):
            phase_details = {}
        phase_record = dict(phase_details)
        phase_record.update(
            {
                "cycle": cycle,
                "phase": phase_index,
                "operation_count": phase_operation_delta,
                "stop_reason": str(trajectory.get("stop_reason") or ""),
            }
        )
        pressure_phases.append(phase_record)
        phase_elapsed_value = max(
            0.0, float(phase_record.get("engine_elapsed_seconds", 0) or 0)
        )
        if phase_record.get("elapsed_seconds_is_total") is True or phase_record.get(
            "elapsed_seconds_mode"
        ) in {"total", "cumulative"}:
            engine_elapsed_total = min(
                float(duration_seconds), max(engine_elapsed_total, phase_elapsed_value)
            )
            phase_record["engine_elapsed_seconds"] = max(
                0.0, engine_elapsed_total - elapsed_before_phase
            )
        else:
            # A broken/legacy adapter can report wall-clock time beyond the
            # remaining budget.  Cap each invocation to the budget so a retry
            # cannot make pressure seconds accumulate twice.
            phase_elapsed_value = min(
                max(0.0, remaining_seconds), phase_elapsed_value
            )
            phase_record["engine_elapsed_seconds"] = phase_elapsed_value
            engine_elapsed_total = min(
                float(duration_seconds), engine_elapsed_total + phase_elapsed_value
            )
        if checkpoint is not None:
            provenance = phase_record.get("log_provenance", {})
            remote_offset = (
                provenance.get("remote_end_offset")
                if isinstance(provenance, Mapping)
                else None
            )
            checkpoint.update(
                pressure_phases=copy.deepcopy(pressure_phases), write=False
            )
            checkpoint.finish_phase(
                elapsed_seconds=engine_elapsed_total,
                operation_count=total_operations,
                remote_log_offset=remote_offset,
                last_error=str(trajectory.get("pressure_engine_error") or "") or None,
            )

        stop_reason = str(trajectory.get("stop_reason") or "")
        if stop_reason == _WAIT_PRESSURE_ENGINE_EXIT:
            engine_error = str(
                trajectory.get("pressure_engine_error")
                or "Fastbot exited unexpectedly"
            )
            failure: Dict[str, Any] = {
                "failure": len(pressure_engine_failures) + 1,
                "cycle": cycle,
                "phase": phase_index,
                "stop_reason": stop_reason,
                "detected_at": phase_record.get("finished_at")
                or datetime.now(timezone.utc).isoformat(),
                "error": engine_error,
                "process_returncode": phase_record.get("process_returncode"),
                "last_action": _last_pressure_action(trajectory),
                "log_provenance": dict(phase_record.get("log_provenance", {}))
                if isinstance(phase_record.get("log_provenance"), Mapping)
                else {},
            }
            pressure_engine_failures.append(failure)
            remaining_after_phase = max(
                0.0, duration_seconds - engine_elapsed_total
            )
            can_recover = bool(
                crash_options.get("enabled", True)
                and pressure_engine_restarts < max_crash_restarts
                and remaining_after_phase >= minimum_remaining
                and not _cycle_hard_control_failure(executor, trajectory)
            )
            if not can_recover:
                failure["recovery"] = {
                    "attempted": False,
                    "success": False,
                    "skipped": True,
                    "reason": (
                        "pressure engine recovery is disabled"
                        if not crash_options.get("enabled", True)
                        else "pressure engine recovery limit reached"
                        if pressure_engine_restarts >= max_crash_restarts
                        else "insufficient remaining pressure time"
                    ),
                }
                trajectory["pressure_engine_recovery_failed"] = True
                trajectory["pressure_engine_recovery_error"] = engine_error
                return finalize(trajectory)
            recovery = _recover_after_runtime_failure(
                state_manager,
                mutex,
                config,
                _WAIT_PRESSURE_ENGINE_EXIT,
                cancel_event,
                executor=executor,
            )
            _merge_handoff_fields(trajectory, recovery)
            failure["recovery"] = recovery
            pressure_engine_restarts += 1
            if not recovery.get("success"):
                trajectory["pressure_engine_recovery_failed"] = True
                trajectory["pressure_engine_recovery_error"] = str(
                    recovery.get("error") or engine_error
                )
                return finalize(trajectory)
            if checkpoint is not None:
                # The failed phase is retained in ``pressure_engine_failures``
                # as history, but it must not remain the active checkpoint
                # error once the engine has been handed off successfully.
                checkpoint.update(
                    phase_name="resuming",
                    resume_state="running",
                    last_error=None,
                    running=False,
                )
            logger.warning(
                "Pressure engine exited unexpectedly on %s; continuing cycle "
                "with %.1f seconds remaining",
                state_manager.device_serial,
                remaining_after_phase,
            )
            continue
        if stop_reason in {
            _WAIT_CRASH,
            _WAIT_PROCESS_EXIT,
            _WAIT_DEVICE_DISCONNECTED,
        } or trajectory.get("stopped_on_crash"):
            handler = _new_crash_handler(
                state_manager.device_serial, state_manager.package_name, config
            )
            page_detection = trajectory.get("app_crash_page")
            page_evidence: Optional[Dict[str, Any]] = None
            if isinstance(page_detection, Mapping):
                page_evidence = _capture_app_crash_page_evidence(
                    state_manager,
                    mutex,
                    handler,
                    report_dir,
                    cycle,
                    page_detection,
                    executor=executor,
                )
            process_detection = (
                trajectory.get("app_process_exit")
                if isinstance(trajectory.get("app_process_exit"), Mapping)
                else None
            )
            crash = _detect_cycle_crash(
                handler,
                log_content,
                page_detection if isinstance(page_detection, Mapping) else None,
                page_evidence,
                process_detection,
                trajectory.get("app_crash")
                if isinstance(trajectory.get("app_crash"), Mapping)
                else None,
            )
            disconnect = (
                dict(trajectory["device_disconnect"])
                if isinstance(trajectory.get("device_disconnect"), Mapping)
                else None
            )
            detection: Dict[str, Any] = (
                dict(crash)
                if isinstance(crash, Mapping)
                else dict(process_detection)
                if isinstance(process_detection, Mapping)
                else dict(page_detection)
                if isinstance(page_detection, Mapping)
                else dict(disconnect)
                if isinstance(disconnect, Mapping)
                else {
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                    "detection_source": "pressure_wait_loop",
                    "message": "target app runtime failure stopped pressure input",
                }
            )
            log_provenance = phase_record.get("log_provenance", {})
            pressure_content = ""
            if isinstance(log_provenance, Mapping):
                pressure_path = Path(
                    str(log_provenance.get("path") or report_dir / "fastbot.log")
                )
                pressure_content = _read_log_segment(
                    pressure_path, int(log_provenance.get("start_offset", 0) or 0)
                )
            evidence = _capture_runtime_failure_evidence(
                state_manager,
                executor,
                mutex,
                report_dir,
                cycle,
                phase_index,
                detection,
                trajectory,
                log_content,
                pressure_content,
                config,
            )
            failure: Dict[str, Any] = {
                "failure": len(runtime_failures) + 1,
                "cycle": cycle,
                "phase": phase_index,
                "stop_reason": stop_reason or _WAIT_CRASH,
                "detected_at": detection.get("detected_at")
                or datetime.now(timezone.utc).isoformat(),
                "last_action": _last_pressure_action(trajectory),
                "evidence": evidence,
            }
            if isinstance(crash, Mapping):
                failure["crash"] = dict(crash)
            if isinstance(process_detection, Mapping):
                failure["process_exit"] = dict(process_detection)
            if isinstance(disconnect, Mapping):
                failure["device_disconnect"] = dict(disconnect)
            if isinstance(page_evidence, Mapping):
                failure["app_crash_page"] = dict(page_evidence)
            runtime_failures.append(failure)
            combined_artifacts["runtime_failures"] = str(
                report_dir / "runtime_failures"
            )

            remaining_after_failure = max(
                0.0, duration_seconds - engine_elapsed_total
            )
            has_runtime_evidence = bool(
                isinstance(crash, Mapping)
                or isinstance(process_detection, Mapping)
                or isinstance(page_evidence, Mapping)
            )
            recover_for_next_cycle = bool(
                remaining_after_failure < minimum_remaining
                and cycle < configured_cycles
                and has_runtime_evidence
            )
            stop_on_confirmed_crash = bool(
                test_options.get("stop_on_crash", False)
                and (
                    _is_confirmed_app_crash(crash)
                    or _is_confirmed_app_crash(page_evidence)
                )
            )
            stop_on_crash_policy = bool(
                test_options.get("stop_on_crash", False)
                and (
                    stop_on_confirmed_crash
                    or isinstance(crash, Mapping)
                    or isinstance(process_detection, Mapping)
                    or isinstance(page_evidence, Mapping)
                )
            )
            crash_signature = (
                str(crash.get("signature") or "")
                if isinstance(crash, Mapping)
                else ""
            )
            signature_restarts = (
                crash_restarts_by_signature.get(crash_signature, 0)
                if crash_signature
                else crash_restarts
            )
            # An immediate crash on every launch cannot explore anything, so
            # only that pattern is terminal. Any other repetition keeps
            # restarting until the time budget is spent.
            try:
                phase_engine_seconds = float(
                    phase_record.get("engine_elapsed_seconds", 0) or 0
                )
            except (TypeError, ValueError, OverflowError):
                phase_engine_seconds = 0.0
            if phase_engine_seconds < startup_crash_window:
                consecutive_startup_crashes += 1
            else:
                consecutive_startup_crashes = 0
            can_recover = bool(
                stop_reason != _WAIT_DEVICE_DISCONNECTED
                and not stop_on_crash_policy
                and crash_recovery_enabled
                and (
                    max_crash_restarts_per_signature == 0
                    or signature_restarts < max_crash_restarts_per_signature
                )
                and (
                    max_total_crash_restarts == 0
                    or crash_restarts < max_total_crash_restarts
                )
                and consecutive_startup_crashes <= max_startup_crashes
                and (
                    remaining_after_failure >= minimum_remaining
                    or recover_for_next_cycle
                )
                and not _cycle_hard_control_failure(executor, trajectory)
            )
            if not can_recover:
                failure["recovery"] = {
                    "attempted": False,
                    "success": False,
                    "skipped": True,
                    "signature_restarts": signature_restarts,
                    "total_restarts": crash_restarts,
                    "consecutive_startup_crashes": consecutive_startup_crashes,
                    "reason": (
                        "device disconnected"
                        if stop_reason == _WAIT_DEVICE_DISCONNECTED
                        else "stop_on_crash is enabled"
                        if stop_on_crash_policy
                        else "crash recovery is disabled"
                        if not crash_recovery_enabled
                        else "app crashes immediately after every launch"
                        if consecutive_startup_crashes > max_startup_crashes
                        else "the same crash keeps repeating (per-signature "
                        "restart limit reached)"
                        if signature_restarts >= max_crash_restarts_per_signature
                        else "crash recovery limit reached"
                        if crash_restarts >= max_total_crash_restarts
                        else "insufficient remaining pressure time"
                    ),
                }
                return finalize(trajectory)
            recovery = _recover_after_runtime_failure(
                state_manager,
                mutex,
                config,
                stop_reason or _WAIT_CRASH,
                cancel_event,
                executor=executor,
            )
            _merge_handoff_fields(trajectory, recovery)
            failure["recovery"] = recovery
            crash_restarts += 1
            if crash_signature:
                crash_restarts_by_signature[crash_signature] = signature_restarts + 1
            if not recovery.get("success"):
                trajectory["runtime_recovery_failed"] = True
                return finalize(trajectory)
            if recover_for_next_cycle:
                recovery["for_next_cycle"] = True
                recovery["continued_pressure"] = False
                return finalize(trajectory)
            continue

        # A collector/control owner that did not stop is terminal for the
        # current worker.  Runtime crashes above are processed first so their
        # evidence is never discarded, but no UI recovery or new Fastbot phase
        # may begin after this point.
        if _cycle_hard_control_failure(executor, trajectory):
            return finalize(trajectory)

        if stop_reason in {
            _WAIT_PERMISSION_RECOVERY,
            _WAIT_PERMISSION_RECOVERY_UNCLEAN,
        }:
            permission_recovery_attempts += 1
            permission_progress = (
                engine_elapsed_total - last_permission_recovery_elapsed >= 1.0
                and total_operations > last_permission_recovery_operations
            )
            last_permission_recovery_elapsed = engine_elapsed_total
            last_permission_recovery_operations = total_operations
            # Separate prompts can occur throughout a long cycle. Stop only
            # when handoffs keep happening without running pressure events.
            permission_recoveries_without_progress = (
                0 if permission_progress else permission_recoveries_without_progress + 1
            )
            if (
                permission_recoveries_without_progress
                > max_permission_recoveries_without_progress
            ):
                trajectory["permission_recovery_failed"] = True
                trajectory["permission_recovery_error"] = (
                    "permission dialog reappeared repeatedly without pressure progress"
                )
                return finalize(trajectory)

            device = state_manager.get_device()
            acquired_ui = False
            recovery: Dict[str, Any]
            try:
                if not mutex.acquire("uiautomator2", device):
                    raise RuntimeError(
                        "uiautomator2 control unavailable for permission recovery"
                    )
                acquired_ui = True
                recoverer = getattr(
                    executor, "recover_runtime_permission_dialogs", None
                )
                if not callable(recoverer):
                    recoverer = getattr(
                        executor, "recover_runtime_permission_dialog", None
                    )
                if not callable(recoverer):
                    raise RuntimeError(
                        "permission recovery handler is unavailable"
                    )
                output_dir = report_dir / (
                    "permission_recovery_%02d" % permission_recovery_attempts
                )
                # Built-in executors increment their counter inside the
                # recovery method; older adapters use the completion callback
                # below instead. Snapshot the value so the compatibility
                # callback remains correct after the first successful retry.
                permission_count_before = getattr(
                    executor, "permission_recovery_count", 0
                )
                if callable(permission_count_before):
                    permission_count_before = permission_count_before()
                try:
                    permission_count_before = int(permission_count_before or 0)
                except (TypeError, ValueError):
                    permission_count_before = 0
                try:
                    value = recoverer(
                        device=device,
                        timeout=_permission_recovery_timeout(config),
                        output_dir=str(output_dir),
                    )
                except TypeError as exc:
                    if "unexpected keyword" not in str(exc).lower():
                        raise
                    value = recoverer()
                recovery = (
                    dict(value)
                    if isinstance(value, Mapping)
                    else {
                        "attempted": True,
                        "success": bool(value),
                        "status": "completed" if value else "failed",
                    }
                )
            except Exception as exc:
                recovery = {
                    "attempted": True,
                    "success": False,
                    "status": "failed",
                    "error": str(exc),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                logger.exception(
                    "Runtime permission recovery failed on %s",
                    state_manager.device_serial,
                )
            finally:
                if acquired_ui:
                    release_error: Optional[BaseException] = None
                    try:
                        if mutex.release("uiautomator2", device) is False:
                            release_error = RuntimeError(
                                "uiautomator2 control was not released after permission recovery"
                            )
                    except Exception as exc:
                        release_error = exc
                    if release_error is not None:
                        recovery["success"] = False
                        recovery["status"] = "failed"
                        recovery["error"] = str(release_error)
                        _mark_control_release_failure(
                            executor, recovery, release_error
                        )
                        logger.exception(
                            "Unable to release uiautomator2 after permission recovery"
                        )
            permission_recovery_results.append(dict(recovery))
            trajectory["permission_recovery"] = dict(recovery)
            evidence = recovery.get("evidence")
            if isinstance(evidence, Mapping):
                artifacts = evidence.get("artifacts", {})
                if isinstance(artifacts, Mapping):
                    for key, value in artifacts.items():
                        if value:
                            combined_artifacts["permission_%s" % key] = str(value)
            if recovery.get("success"):
                marker = getattr(executor, "mark_permission_recovery_completed", None)
                if callable(marker):
                    # The built-in handler increments its own counter. Legacy
                    # adapters use this callback as their only accounting hook.
                    current_count = getattr(
                        executor, "permission_recovery_count", 0
                    )
                    if callable(current_count):
                        current_count = current_count()
                    try:
                        current_count = int(current_count or 0)
                    except (TypeError, ValueError):
                        current_count = permission_count_before
                    if current_count == permission_count_before:
                        marker(recovery)
                logger.info(
                    "Permission handoff succeeded on %s; continuing with %.1f seconds remaining",
                    state_manager.device_serial,
                    max(0.0, duration_seconds - engine_elapsed_total),
                )
                continue
            recovery_error = str(
                recovery.get("error") or "permission dialog could not be handled"
            )
            serial = state_manager.device_serial
            adb_errors = (
                "device not found",
                "device offline",
                "device is offline",
                "no devices/emulators found",
                "device '%s' not found" % serial.lower(),
            )
            if any(marker in recovery_error.lower() for marker in adb_errors) or (
                not _remote_transport_available(serial)
            ):
                # A reconnect must go through the scheduler's remote cleanup
                # gate. Preserve the completed pressure phases and do not
                # acquire another controller from this worker.
                trajectory["stop_reason"] = _WAIT_DEVICE_DISCONNECTED
                trajectory["control_handoff_safe"] = False
                trajectory["device_disconnect"] = {
                    "detected_at": recovery.get("completed_at"),
                    "error": recovery_error,
                }
                logger.warning("ADB transport lost on %s during permission handoff", serial)
            else:
                trajectory["permission_recovery_failed"] = True
                trajectory["permission_recovery_error"] = recovery_error
            return finalize(trajectory)

        if stop_reason == _WAIT_AUDIT_RECOVERY:
            audit_recovery_attempts += 1
            pressure_progress = max(
                0.0, engine_elapsed_total - last_audit_recovery_elapsed
            )
            last_audit_recovery_elapsed = engine_elapsed_total
            if pressure_progress < audit_recovery_progress_seconds:
                audit_recoveries_without_progress += 1
            else:
                audit_recoveries_without_progress = 0
            if (
                audit_recoveries_without_progress
                > max_audit_recoveries_without_progress
            ):
                trajectory["audit_recovery_failed"] = True
                trajectory["audit_recovery_error"] = (
                    "startup gate reappeared repeatedly without pressure progress"
                )
                return finalize(trajectory)
            device = state_manager.get_device()
            acquired_ui = False
            try:
                if not mutex.acquire("uiautomator2", device):
                    raise RuntimeError(
                        "uiautomator2 control unavailable for audit recovery"
                    )
                acquired_ui = True
                preparer = getattr(executor, "prepare_audit_gate", None)
                audit_ready, device = _prepare_audit_gate_with_retry(
                    state_manager,
                    executor,
                    device,
                    timeout=20.0,
                )
                if not callable(preparer) or not audit_ready:
                    raise RuntimeError(
                        "startup gate did not clear after safe Fastbot handoff"
                    )
                completed = getattr(
                    executor, "mark_audit_recovery_completed", None
                )
                if callable(completed):
                    completed()
            except Exception as exc:
                trajectory["audit_recovery_failed"] = True
                trajectory["audit_recovery_error"] = str(exc)
                logger.exception(
                    "Runtime startup-gate recovery failed on %s",
                    state_manager.device_serial,
                )
                return finalize(trajectory)
            finally:
                if acquired_ui:
                    release_error: Optional[BaseException] = None
                    try:
                        if mutex.release("uiautomator2", device) is False:
                            release_error = RuntimeError(
                                "uiautomator2 control was not released"
                            )
                    except Exception as exc:
                        release_error = exc
                    if release_error is not None:
                        trajectory["audit_recovery_failed"] = True
                        trajectory["audit_recovery_error"] = str(release_error)
                        _mark_control_release_failure(
                            executor, trajectory, release_error
                        )
            if trajectory.get("audit_recovery_failed"):
                return finalize(trajectory)
            continue

        if stop_reason == _WAIT_FOREGROUND_RECOVERY:
            # Only a hard ``True`` counts: adapters and test doubles expose
            # placeholder callables here, and a truthy mock must not turn every
            # foreground handoff into a terminal failure.
            blocked = False
            blocked_check = getattr(executor, "blocking_window_blocked", None)
            if callable(blocked_check):
                try:
                    blocked = blocked_check() is True
                except Exception:
                    blocked = False
            if blocked:
                # A system/IME window keeps window focus above the app and a
                # controlled BACK did not clear it. Restarting the engine cannot
                # help, so end this phase with an explicit, actionable reason
                # instead of looping through foreground recovery.
                trajectory["blocking_window_blocked"] = True
                trajectory["foreground_recovery_failed"] = True
                trajectory["foreground_recovery_error"] = str(
                    getattr(executor, "foreground_restore_error", None)
                    or "input injection blocked by a foreign window (IME or "
                    "system dialog); manual device cleanup required"
                )
                return finalize(trajectory)
            foreground_recovery_attempts += 1
            input_reason = getattr(executor, "input_recovery_reason", None)
            if not isinstance(input_reason, str):
                input_reason = None
            if input_reason:
                consecutive_input_recoveries = (
                    consecutive_input_recoveries + 1
                    if float(phase_record.get("engine_elapsed_seconds", 0) or 0) < 10.0
                    else 1
                )
                limit = int(config.get("input_safety", {}).get("recovery_attempts", 3))
                if consecutive_input_recoveries > limit:
                    trajectory["foreground_recovery_failed"] = True
                    trajectory["foreground_recovery_error"] = "input page recovery exhausted after repeated interruptions"
                    trajectory["input_recovery_exhausted"] = True
                    return finalize(trajectory)
            else:
                consecutive_input_recoveries = 0
            if checkpoint is not None:
                # Fastbot has already been stopped by the phase owner. Keep
                # the in-flight cycle visible as recoverable while Vivo/Oplus
                # foreground handoff runs; elapsed time and operation count
                # remain the values accumulated by the completed phase.
                checkpoint.update(
                    phase_name="foreground_recovery",
                    resume_state="recovering",
                    foreground_recovery_strategy=_foreground_recovery_strategy(
                        state_manager
                    ),
                    foreground_component=getattr(
                        executor, "last_foreground_component", None
                    )
                    or getattr(executor, "_last_foreground_component", None),
                    last_error=str(
                        trajectory.get("foreground_recovery_error")
                        or trajectory.get("foreground_restore_error")
                        or "target app did not return to foreground"
                    ),
                    running=False,
                )
            remaining_after_phase = max(0.0, duration_seconds - engine_elapsed_total)
            if remaining_after_phase < 1.0:
                # The requested pressure budget is already exhausted. Do not
                # turn a late launcher transition during final cleanup into a
                # failed stability result.
                trajectory["foreground_recovery_deferred"] = True
                return finalize(trajectory)
            if remaining_after_phase < minimum_remaining and not input_reason:
                trajectory["foreground_recovery_failed"] = True
                trajectory["foreground_recovery_error"] = str(
                    trajectory.get("foreground_recovery_error")
                    or "insufficient remaining pressure time for foreground recovery"
                )
                return finalize(trajectory)
            recovery = _recover_after_runtime_failure(
                state_manager,
                mutex,
                config,
                _WAIT_FOREGROUND_RECOVERY,
                cancel_event,
                executor=executor,
            )
            _merge_handoff_fields(trajectory, recovery)
            foreground_recovery_results.append(dict(recovery))
            if input_reason:
                recovery["input_safety_reason"] = str(input_reason)
                foreground_recovery_results[-1]["input_safety_reason"] = str(input_reason)
            trajectory["foreground_recovery"] = dict(recovery)
            if not recovery.get("success"):
                trajectory["foreground_recovery_failed"] = True
                trajectory["foreground_recovery_error"] = str(
                    recovery.get("error")
                    or "target app could not be restored after foreground loss"
                )
                return finalize(trajectory)
            if checkpoint is not None:
                checkpoint.update(
                    phase_name="resuming",
                    resume_state="running",
                    foreground_recovery_strategy=_foreground_recovery_strategy(
                        state_manager
                    ),
                    foreground_component=recovery.get("foreground_component"),
                    last_error=None,
                    running=False,
                )
            # The next invocation receives the remaining pressure budget. The
            # recovery itself is control-plane time and is intentionally not
            # counted as pressure coverage.
            continue

        # A watchdog stop is a control-plane interruption, not the end of the
        # cycle.  ``stall_recovery_unclean`` means the host reported a cleanup
        # warning; the phase-level code has already checked whether the
        # device-side Fastbot process is actually gone.  Also honor the
        # explicit request marker for legacy executors that do not preserve a
        # stop reason in their collected trajectory.
        stall_recovery_requested = bool(
            stop_reason in {_WAIT_STALL_RECOVERY, _WAIT_STALL_RECOVERY_UNCLEAN}
            or trajectory.get("stall_recovery_requested") is True
        )
        if not stall_recovery_requested:
            return finalize(trajectory)
        _invoke_stall_recovery(
            trajectory,
            state_manager,
            mutex,
            config,
            cancel_event,
            executor=executor,
        )
        result = trajectory.get("stall_recovery")
        # Stall recovery owns the StateManager directly, so the executor's
        # in-pressure audit counter cannot be updated by the gate helper.
        # Record it once here after a verified target-only relaunch.
        if (
            isinstance(result, Mapping)
            and result.get("success")
            and result.get("audit_gate_completed")
        ):
            mark_audit_recovery = getattr(
                executor, "mark_audit_recovery_completed", None
            )
            if callable(mark_audit_recovery):
                mark_audit_recovery()
            try:
                trajectory["audit_recovery_count"] = max(
                    0, int(getattr(executor, "audit_recovery_count", 0) or 0)
                )
            except (TypeError, ValueError):
                trajectory["audit_recovery_count"] = 1
        remaining_after_recovery = max(0.0, duration_seconds - engine_elapsed_total)
        if isinstance(result, dict):
            # Keep the report self-describing: a successful fallback is only
            # considered continued pressure when there is a remaining budget
            # to hand back to the engine.
            result["continued_pressure"] = bool(
                result.get("success") and remaining_after_recovery >= 1.0
            )
        if isinstance(result, Mapping):
            recovery_results.append(dict(result))
        if not isinstance(result, Mapping) or not result.get("success"):
            return finalize(trajectory)
        if checkpoint is not None:
            checkpoint.update(
                phase_name="resuming",
                resume_state="running",
                last_error=None,
                running=False,
            )
        logger.info(
            "Stall recovery succeeded on %s; continuing cycle with %.1f seconds "
            "of pressure remaining",
            state_manager.device_serial,
            remaining_after_recovery,
        )


def _run_fastbot_phase_once(
    state_manager: StateManager,
    executor: TestExecutor,
    mutex: MutexController,
    config: Mapping[str, Any],
    report_dir: Path,
    cycle: int,
    cancel_event: Any = None,
    *,
    remaining_seconds: Optional[float] = None,
    checkpoint: Optional[CheckpointHeartbeat] = None,
    operation_base: int = 0,
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    """Run one uninterrupted pressure-engine invocation."""
    device = state_manager.get_device()
    log_path = report_dir / "logcat.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    pressure_log_path = report_dir / "fastbot.log"
    pressure_log_offset = (
        pressure_log_path.stat().st_size if pressure_log_path.exists() else 0
    )
    log_process = None
    screenshot_event = None
    fastbot_process = None
    acquired = False
    stopped_on_crash = False
    stopped_on_process_exit = False
    stopped_manually = False
    stopped_for_stall_recovery = False
    stopped_for_permission_recovery = False
    stopped_for_audit_recovery = False
    stopped_for_foreground_recovery = False
    stopped_on_device_disconnect = False
    trajectory: Dict[str, Any] = {
        "foreground_recovery_strategy": _foreground_recovery_strategy(state_manager)
    }
    collection_warnings: List[str] = []
    collector_stopped = True
    recovery_handoff_error: Optional[str] = None
    fastbot_stop_verified = False
    phase_error: Optional[BaseException] = None
    phase_started_at: Optional[str] = None
    phase_finished_at: Optional[str] = None
    phase_started_clock: Optional[float] = None
    engine_started_at: Optional[str] = None
    engine_finished_at: Optional[str] = None
    engine_started_clock: Optional[float] = None
    engine_elapsed_seconds: Optional[float] = None
    remote_log_path: Optional[str] = None
    remote_stop_intent_path: Optional[str] = None
    process_returncode: Optional[int] = None
    pressure_engine_exit_error: Optional[str] = None
    # Cleanup failures must be attached to the phase exception before it is
    # re-raised.  Keep the original body exception as the primary cause when
    # one already exists; ``cleanup_only_error`` is used only when cleanup was
    # the first failure.
    cleanup_only_error: Optional[BaseException] = None
    cleanup_errors: List[str] = []
    requested_seconds = max(
        1.0,
        float(remaining_seconds)
        if remaining_seconds is not None
        else float(int(config["test"]["fastbot_duration_minutes"]) * 60),
    )

    def record_collection_warning(message: str) -> None:
        """Keep collector degradation visible without changing recovery control flow."""
        value = str(message).strip()
        if value and value not in collection_warnings:
            collection_warnings.append(value)

    try:
        if not mutex.acquire("fastbot", device):
            raise RuntimeError("Fastbot could not acquire exclusive device control")
        acquired = True
        log_process = executor.start_logcat_collection(str(log_path))
        capture_offset_reader = getattr(executor, "get_logcat_capture_offset", None)
        if callable(capture_offset_reader):
            try:
                log_offset = max(
                    log_offset,
                    int(capture_offset_reader(log_process, log_offset)),
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring an invalid logcat capture offset on %s",
                    state_manager.device_serial,
                )
        duration = int(config["test"]["fastbot_duration_minutes"])
        screenshot_config = config.get("screenshot", {})
        stall_watchdog = config.get("stall_watchdog", {})
        screenshots_enabled = bool(
            isinstance(screenshot_config, Mapping)
            and screenshot_config.get("enabled", False)
        )
        stall_observation_enabled = bool(
            isinstance(stall_watchdog, Mapping)
            and stall_watchdog.get("enabled", False)
            and stall_watchdog.get("mode") in {"observe", "recover_once", "recover"}
        )
        if screenshots_enabled or stall_observation_enabled:
            screenshot_event = executor.start_screenshot_collection(
                str(report_dir / "screenshots"),
                int(
                    screenshot_config.get("interval_seconds", 10)
                    if isinstance(screenshot_config, Mapping)
                    else 10
                ),
            )
        phase_started_at = datetime.now(timezone.utc).isoformat()
        phase_started_clock = time.monotonic()
        fastbot_process = executor.start_fastbot(
            duration, int(config["test"]["fastbot_throttle_ms"])
        )
        engine_started_at = datetime.now(timezone.utc).isoformat()
        engine_started_clock = time.monotonic()
        remote_log_path = str(
            getattr(fastbot_process, "remote_log_path", "") or ""
        ) or None
        remote_stop_intent_path = str(
            getattr(fastbot_process, "remote_intent_path", "") or ""
        ) or None
        if checkpoint is not None:
            checkpoint.attach_fastbot(
                pid=getattr(fastbot_process, "remote_pid", None)
                or getattr(fastbot_process, "pid", None),
                remote_log_path=remote_log_path,
                provider=_checkpoint_progress_provider(
                    executor,
                    fastbot_process,
                    state_manager.device_serial,
                    remote_log_path,
                    operation_base,
                ),
            )
        # Always stop the current phase long enough to preserve crash evidence.
        # ``stop_on_crash`` still controls whether the remaining cycles run.
        live_crash_handler = _new_crash_handler(
            state_manager.device_serial, state_manager.package_name, config
        )
        if getattr(executor, "pressure_engine", "fastbot") == "system-monkey":
            wait_outcome = _wait_for_system_monkey(
                executor,
                fastbot_process,
                duration,
                cancel_event,
                live_crash_handler,
                log_path,
                log_offset,
                remaining_seconds,
            )
        else:
            wait_outcome = _wait_for_fastbot(
                executor,
                fastbot_process,
                duration,
                int(config["test"].get("fastbot_timeout_grace_seconds", 60)),
                cancel_event,
                live_crash_handler,
                log_path,
                log_offset,
                remaining_seconds,
                True,
            )
        stopped_on_crash = wait_outcome in {_WAIT_CRASH, _WAIT_PROCESS_EXIT}
        stopped_on_process_exit = wait_outcome == _WAIT_PROCESS_EXIT
        stopped_on_device_disconnect = wait_outcome == _WAIT_DEVICE_DISCONNECTED
        stopped_manually = wait_outcome == _WAIT_MANUAL_STOP
        stopped_for_stall_recovery = wait_outcome in {
            _WAIT_STALL_RECOVERY,
            _WAIT_STALL_RECOVERY_UNCLEAN,
        }
        stopped_for_permission_recovery = wait_outcome in {
            _WAIT_PERMISSION_RECOVERY,
            _WAIT_PERMISSION_RECOVERY_UNCLEAN,
        }
        stopped_for_audit_recovery = wait_outcome in {
            _WAIT_AUDIT_RECOVERY,
            _WAIT_AUDIT_RECOVERY_UNCLEAN,
        }
        stopped_for_foreground_recovery = (
            wait_outcome == _WAIT_FOREGROUND_RECOVERY
        )
        if stopped_on_crash:
            # The live monitor already stopped the input engine.  Give logcat
            # one moment to flush the failure block before we collect artifacts.
            time.sleep(1)
            fastbot_stop_verified = _pressure_stop_verified(executor, True)
            fastbot_clean = fastbot_stop_verified
        elif stopped_on_device_disconnect:
            # The wait loop already attempted a verified stop while the
            # transport was failing or recovering.
            fastbot_stop_verified = _pressure_stop_verified(executor, True)
            fastbot_clean = fastbot_stop_verified
        elif stopped_manually:
            # The recovery-claim branch observed the manual-stop marker and
            # already stopped and finalized the pressure process.
            fastbot_stop_verified = _pressure_stop_verified(executor, True)
            fastbot_clean = fastbot_stop_verified
        elif stopped_for_stall_recovery:
            # The main wait loop already made the targeted stop.  ``stop_fastbot``
            # also reports host-side cleanup (return code, guard teardown, and
            # PID handoff) in its boolean result, so a false result does not
            # necessarily mean the device-side engine is still alive.  Once
            # the separate remote-stop probe is positive, recovery can safely
            # reacquire UI control and continue this cycle.
            fastbot_stop_verified = _pressure_stop_verified(
                executor, wait_outcome == _WAIT_STALL_RECOVERY
            )
            # Keep the remote process probe independent from host-side
            # screenshot/UiAutomation cleanup warnings. The recovery owner
            # uses this marker to decide whether it is safe to relaunch only
            # the target package.
            trajectory["stall_recovery_device_stopped"] = bool(
                fastbot_stop_verified
            )
            fastbot_clean = fastbot_stop_verified
        elif stopped_for_permission_recovery:
            # Permission handoff has the same device-side safety requirement
            # as stall recovery, but its next owner is the UI handler rather
            # than the generic app relaunch path.
            fastbot_stop_verified = _pressure_stop_verified(
                executor, wait_outcome == _WAIT_PERMISSION_RECOVERY
            )
            trajectory["permission_recovery_device_stopped"] = bool(
                fastbot_stop_verified
            )
            fastbot_clean = fastbot_stop_verified
        elif stopped_for_audit_recovery:
            # The wait loop stopped the engine before uiautomator2 is restored.
            fastbot_stop_verified = _pressure_stop_verified(
                executor, wait_outcome == _WAIT_AUDIT_RECOVERY
            )
            fastbot_clean = wait_outcome == _WAIT_AUDIT_RECOVERY and fastbot_stop_verified
        else:
            try:
                stop_result = executor.stop_fastbot(fastbot_process)
                fastbot_stop_verified = _pressure_stop_verified(
                    executor, stop_result
                )
                fastbot_clean = bool(stop_result) and fastbot_stop_verified
            except Exception as exc:
                # A foreground guard can lose its UiAutomation session while
                # Fastbot is shutting down. Preserve that control-path failure
                # as a recoverable phase result instead of replacing it with a
                # generic cleanup exception.
                if getattr(executor, "foreground_restore_failed", False):
                    fastbot_clean = False
                    fastbot_stop_verified = _pressure_stop_verified(executor)
                    recovery_handoff_error = str(exc)
                else:
                    raise
        process_returncode = getattr(fastbot_process, "returncode", None)
        engine_finished_at = datetime.now(timezone.utc).isoformat()
        if engine_started_clock is not None:
            engine_elapsed_seconds = round(
                max(0.0, time.monotonic() - engine_started_clock), 3
            )
        fastbot_process = None
        foreground_restore_failed = bool(
            getattr(executor, "foreground_restore_failed", False)
        )
        foreground_recovery_requested = bool(
            getattr(executor, "foreground_recovery_requested", False)
        )
        if (
            foreground_restore_failed
            or foreground_recovery_requested
            or stopped_for_foreground_recovery
        ):
            stopped_for_foreground_recovery = True
            trajectory["foreground_recovery_requested"] = True
            if foreground_restore_failed:
                trajectory["foreground_restore_failed"] = True
            trajectory["foreground_restore_error"] = str(
                getattr(executor, "foreground_restore_error", None)
                or recovery_handoff_error
                or getattr(executor, "pressure_stop_error", None)
                or "target app did not return to foreground"
            )
            if not fastbot_clean and recovery_handoff_error is None:
                recovery_handoff_error = str(
                    getattr(executor, "pressure_stop_error", None)
                    or "Fastbot stopped after foreground recovery failed"
                )
        if not fastbot_clean:
            if stopped_for_foreground_recovery:
                trajectory["stop_reason"] = _WAIT_FOREGROUND_RECOVERY
                if not fastbot_stop_verified:
                    raise PressureEngineError(
                        "Fastbot or its foreground guard did not stop cleanly: "
                        "device-side pressure process is still running"
                    )
                # Host cleanup/return-code errors are secondary once the
                # device-side process is positively gone. Preserve the
                # foreground handoff classification and continue collecting
                # the phase log and trajectory.
                recovery_handoff_error = recovery_handoff_error or str(
                    getattr(executor, "pressure_stop_error", None)
                    or "foreground recovery completed with host cleanup warnings"
                )
            elif stopped_for_audit_recovery:
                raise PressureEngineError(
                    "Fastbot or its foreground guard did not stop cleanly "
                    "for startup-gate recovery"
                )
            elif stopped_for_permission_recovery:
                if not fastbot_stop_verified:
                    raise PressureEngineError(
                        "Fastbot or its foreground guard did not stop cleanly "
                        "for permission recovery"
                    )
                recovery_handoff_error = recovery_handoff_error or str(
                    getattr(executor, "pressure_stop_error", None)
                    or "permission recovery completed with host cleanup warnings"
                )
            elif stopped_for_stall_recovery:
                recovery_handoff_error = (
                    "Fastbot or its foreground guard did not stop cleanly"
                )
            else:
                stop_error = getattr(executor, "pressure_stop_error", None)
                detail = ": %s" % stop_error if stop_error else ""
                if fastbot_stop_verified and not getattr(
                    executor, "foreground_guard_stop_failed", False
                ):
                    pressure_engine_exit_error = (
                        "Fastbot exited with an error%s" % detail
                    )
                else:
                    raise PressureEngineError(
                        "Fastbot exited with an error%s" % detail
                    )
        if screenshot_event is not None:
            try:
                collector_stopped = executor.stop_screenshot_collection(screenshot_event)
            except Exception as exc:
                collector_stopped = False
                collection_error = _mark_screenshot_collection_failure(
                    executor,
                    trajectory,
                    "screenshot collector stop raised: %s" % exc,
                )
                if (
                    stopped_for_stall_recovery
                    or stopped_for_permission_recovery
                    or stopped_for_foreground_recovery
                    or stopped_on_crash
                    or stopped_on_device_disconnect
                ):
                    record_collection_warning(
                        "Screenshot collector stop warning while preserving a "
                        "runtime/recovery event: %s" % collection_error
                    )
                else:
                    raise
            if not collector_stopped:
                collection_error = _mark_screenshot_collection_failure(
                    executor,
                    trajectory,
                    "screenshot collector did not stop cleanly",
                )
                if (
                    stopped_for_stall_recovery
                    or stopped_for_permission_recovery
                    or stopped_for_foreground_recovery
                    or stopped_on_crash
                    or stopped_on_device_disconnect
                ):
                    # A screenshot probe can still be inside a bounded ADB
                    # timeout while a runtime event is being preserved.  Keep
                    # collecting the phase evidence, but publish an unsafe
                    # handoff so no new pressure phase/cycle can start.
                    record_collection_warning(
                        "%s; pressure will not be resumed" % collection_error
                    )
                else:
                    raise RuntimeError("Screenshot collector did not stop cleanly")
            else:
                screenshot_event = None
        if log_process is not None:
            executor.stop_logcat_collection(log_process)
            log_process = None
        # No UI automation is used while this block owns the device.  The
        # trajectory pull is passive ADB collection and must happen before the
        # mutex is released.
        # Collect the text trace for this invocation before releasing device
        # ownership.  Older executor test doubles expose only the original
        # no-argument method, so retain that compatibility path.
        pressure_trace_end = (
            pressure_log_path.stat().st_size if pressure_log_path.exists() else None
        )
        # ``collect_trajectory`` is allowed to return a fresh mapping.  Keep
        # the markers observed by the phase owner (especially runtime-failure
        # evidence) when an older adapter does not echo them back.
        phase_markers = dict(trajectory)
        trajectory_reader = getattr(executor, "collect_trajectory")
        try:
            collected = trajectory_reader(
                log_path=pressure_log_path,
                log_start_offset=pressure_log_offset,
                log_end_offset=pressure_trace_end,
            )
        except TypeError as exc:
            if not any(
                marker in str(exc)
                for marker in ("unexpected keyword", "got an unexpected keyword")
            ):
                raise
            collected = trajectory_reader()
        if isinstance(collected, Mapping):
            trajectory = dict(collected)
            trajectory.update(phase_markers)
        else:
            trajectory = phase_markers
        statistics_reader = getattr(executor, "collect_fastbot_statistics", None)
        if callable(statistics_reader):
            try:
                fastbot_statistics = statistics_reader()
                if "operation_count" in fastbot_statistics:
                    trajectory["operation_count"] = max(
                        int(trajectory.get("operation_count", 0) or 0),
                        int(fastbot_statistics["operation_count"]),
                    )
                if (
                    "coverage_rate" in fastbot_statistics
                    and trajectory.get("coverage_rate") is None
                ):
                    trajectory["coverage_rate"] = fastbot_statistics[
                        "coverage_rate"
                    ]
            except Exception:
                logger.warning("Unable to parse Fastbot statistics", exc_info=True)
        stall_count, stall_events = _consume_stall_observations(executor)
        trajectory["stall_detection_count"] = stall_count
        if stall_events:
            trajectory["stall_events"] = stall_events
        watchdog_status_reader = getattr(executor, "stall_watchdog_status", None)
        if isinstance(watchdog_status_reader, Mapping):
            trajectory["stall_watchdog"] = dict(watchdog_status_reader)
            if watchdog_status_reader.get("state") == "degraded":
                collection_warnings.append(
                    "stall watchdog degraded after repeated probe failures"
                )
        elif callable(watchdog_status_reader):
            try:
                watchdog_status = watchdog_status_reader()
                if isinstance(watchdog_status, Mapping):
                    trajectory["stall_watchdog"] = dict(watchdog_status)
            except Exception:
                logger.debug("Unable to collect stall watchdog status", exc_info=True)
        if getattr(executor, "stall_recovery_claimed", False) is True:
            trajectory["stall_recovery_requested"] = True
        trajectory["foreground_restore_count"] = max(
            0, int(getattr(executor, "foreground_restore_count", 0) or 0)
        )
        trajectory["permission_dialog_count"] = max(
            0, int(getattr(executor, "permission_dialog_count", 0) or 0)
        )
        permission_recovery_count = getattr(
            executor, "permission_recovery_count", 0
        )
        if callable(permission_recovery_count):
            permission_recovery_count = permission_recovery_count()
        try:
            trajectory["permission_recovery_count"] = max(
                0, int(permission_recovery_count or 0)
            )
        except (TypeError, ValueError):
            trajectory["permission_recovery_count"] = 0
        permission_history = getattr(executor, "permission_recovery_history", None)
        if callable(permission_history):
            permission_history = permission_history()
        if isinstance(permission_history, list) and permission_history:
            trajectory["permission_recovery_history"] = [
                dict(item) for item in permission_history if isinstance(item, Mapping)
            ]
        permission_evidence = getattr(executor, "permission_recovery_evidence", None)
        if callable(permission_evidence):
            permission_evidence = permission_evidence()
        if isinstance(permission_evidence, list) and permission_evidence:
            trajectory["permission_recovery_evidence"] = [
                dict(item) for item in permission_evidence if isinstance(item, Mapping)
            ]
        trajectory["audit_recovery_count"] = max(
            0, int(getattr(executor, "audit_recovery_count", 0) or 0)
        )
        trajectory["stopped_on_crash"] = stopped_on_crash
        trajectory["stopped_on_process_exit"] = stopped_on_process_exit
        app_crash_detection = getattr(executor, "app_crash_detection", None)
        if callable(app_crash_detection):
            app_crash_detection = app_crash_detection()
        if isinstance(app_crash_detection, Mapping):
            trajectory["app_crash"] = dict(app_crash_detection)
        process_exit_detection = getattr(
            executor, "app_process_exit_detection", None
        )
        if process_exit_detection:
            trajectory["app_process_exit"] = dict(process_exit_detection)
        disconnect_detection = getattr(
            executor, "device_disconnect_detection", None
        )
        if disconnect_detection:
            trajectory["device_disconnect"] = dict(disconnect_detection)
        app_crash_page = getattr(executor, "app_crash_page_detection", None)
        if not app_crash_page:
            app_crash_page = _detect_app_crash_page_in_pressure_log(
                _read_log_segment(pressure_log_path, pressure_log_offset),
                state_manager.package_name,
                live_crash_handler,
            )
        if app_crash_page:
            trajectory["app_crash_page"] = dict(app_crash_page)

        # A busy device can append megabytes of framework noise after the
        # target process dies.  The live poll above intentionally reads only a
        # bounded tail, so perform the authoritative scan after both collectors
        # have been stopped/flushed.  The overlap-aware scanner keeps memory
        # bounded and still recognizes multiline crash blocks in the middle of
        # a long log.
        final_crash_detection: Optional[Dict[str, Any]] = None
        crash_detector = getattr(live_crash_handler, "detect_crash", None)
        if callable(crash_detector):
            final_crash_detection = _scan_log_for_detection(
                log_path, log_offset, crash_detector
            )
        if isinstance(final_crash_detection, Mapping):
            final_crash_detection = dict(final_crash_detection)
            _record_runtime_detection(
                executor, "record_app_crash", final_crash_detection
            )
            trajectory["app_crash"] = dict(final_crash_detection)

        process_detector = getattr(live_crash_handler, "detect_process_exit", None)
        final_process_exit_detection: Optional[Dict[str, Any]] = None
        if callable(process_detector):
            final_process_exit_detection = _scan_log_for_detection(
                log_path, log_offset, process_detector
            )
        if isinstance(final_process_exit_detection, Mapping):
            final_process_exit_detection = dict(final_process_exit_detection)
            _record_runtime_detection(
                executor, "record_app_process_exit", final_process_exit_detection
            )
            trajectory["app_process_exit"] = dict(final_process_exit_detection)

        final_page_detection = _scan_log_for_detection(
            pressure_log_path,
            pressure_log_offset,
            lambda content: _detect_app_crash_page_in_pressure_log(
                content,
                state_manager.package_name,
                live_crash_handler,
            ),
        )
        if isinstance(final_page_detection, Mapping):
            final_page_detection = dict(final_page_detection)
            recorder = getattr(executor, "_record_app_crash_page", None)
            if callable(recorder):
                try:
                    recorder(str(final_page_detection.get("component") or ""))
                except Exception:
                    logger.debug(
                        "Unable to publish final pressure-log crash-page marker",
                        exc_info=True,
                    )
            trajectory["app_crash_page"] = dict(final_page_detection)

        crash_evidence = trajectory.get("app_crash")
        process_evidence = trajectory.get("app_process_exit")
        page_evidence = trajectory.get("app_crash_page")
        has_crash = isinstance(crash_evidence, Mapping)
        has_process_exit = isinstance(process_evidence, Mapping)
        has_crash_page = isinstance(page_evidence, Mapping)

        # Several asynchronous owners can publish a stop hint.  The complete
        # phase log is authoritative: once it contains a target FATAL or an
        # explicit crash page, later foreground/cleanup markers cannot
        # downgrade the phase to recovery or normal completion.
        raw_executor_reason = getattr(executor, "pressure_stop_reason", None)
        executor_reason = str(raw_executor_reason or "").strip().lower()
        normalized_executor_reason = {
            "crashed": _WAIT_CRASH,
            "app_crash": _WAIT_CRASH,
            "process_exit": _WAIT_PROCESS_EXIT,
            "process_died": _WAIT_PROCESS_EXIT,
            "device_disconnect": _WAIT_DEVICE_DISCONNECTED,
            "disconnect": _WAIT_DEVICE_DISCONNECTED,
            "manual": _WAIT_MANUAL_STOP,
            "pressure_deadline": _WAIT_PHASE_DEADLINE,
            "deadline": _WAIT_PHASE_DEADLINE,
            "engine_exit": _WAIT_PRESSURE_ENGINE_EXIT,
        }.get(executor_reason, executor_reason)
        stop_intent = _pressure_stop_intent_reason(executor)
        if stop_intent:
            trajectory["pressure_stop_intent"] = stop_intent

        # Android commonly emits FATAL followed by ``am_proc_died``.  Preserve
        # both booleans/evidence records while keeping ``crash`` as the primary
        # stop reason.
        stopped_on_process_exit = bool(
            stopped_on_process_exit or has_process_exit
        )
        manual_stop_intent = bool(
            stopped_manually
            or wait_outcome == _WAIT_MANUAL_STOP
            or normalized_executor_reason == _WAIT_MANUAL_STOP
        )
        if manual_stop_intent:
            # Keep crash/process evidence collected above, but the operator's
            # explicit stop owns the phase outcome and must not be auto-resumed.
            trajectory["stop_reason"] = (
                str(raw_executor_reason).strip()
                if executor_reason == "manual"
                else _WAIT_MANUAL_STOP
            )
        elif has_crash or has_crash_page:
            stopped_on_crash = True
            trajectory["stop_reason"] = _WAIT_CRASH
        elif has_process_exit or stopped_on_process_exit or wait_outcome == _WAIT_PROCESS_EXIT:
            stopped_on_crash = True
            trajectory["stop_reason"] = _WAIT_PROCESS_EXIT
        elif stopped_on_device_disconnect or wait_outcome == _WAIT_DEVICE_DISCONNECTED:
            trajectory["stop_reason"] = _WAIT_DEVICE_DISCONNECTED
        elif stopped_manually or wait_outcome == _WAIT_MANUAL_STOP or normalized_executor_reason == _WAIT_MANUAL_STOP:
            # Preserve the legacy literal for older executor adapters.
            trajectory["stop_reason"] = (
                str(raw_executor_reason).strip()
                if executor_reason == "manual"
                else _WAIT_MANUAL_STOP
            )
        elif stopped_for_foreground_recovery or normalized_executor_reason == _WAIT_FOREGROUND_RECOVERY:
            trajectory["stop_reason"] = _WAIT_FOREGROUND_RECOVERY
            trajectory["foreground_recovery_error"] = str(
                trajectory.get("foreground_restore_error")
                or recovery_handoff_error
                or "target app did not return to foreground"
            )
        elif stopped_for_audit_recovery or normalized_executor_reason in {
            _WAIT_AUDIT_RECOVERY,
            _WAIT_AUDIT_RECOVERY_UNCLEAN,
        }:
            trajectory["stop_reason"] = (
                _WAIT_AUDIT_RECOVERY
                if fastbot_stop_verified
                else _WAIT_AUDIT_RECOVERY_UNCLEAN
            )
        elif stopped_for_permission_recovery or normalized_executor_reason in {
            _WAIT_PERMISSION_RECOVERY,
            _WAIT_PERMISSION_RECOVERY_UNCLEAN,
        }:
            trajectory["stop_reason"] = (
                _WAIT_PERMISSION_RECOVERY
                if fastbot_stop_verified
                else _WAIT_PERMISSION_RECOVERY_UNCLEAN
            )
        elif stopped_for_stall_recovery or normalized_executor_reason in {
            _WAIT_STALL_RECOVERY,
            _WAIT_STALL_RECOVERY_UNCLEAN,
        }:
            trajectory["stop_reason"] = (
                _WAIT_STALL_RECOVERY
                if fastbot_stop_verified
                else _WAIT_STALL_RECOVERY_UNCLEAN
            )
        elif pressure_engine_exit_error or normalized_executor_reason == _WAIT_PRESSURE_ENGINE_EXIT:
            trajectory["stop_reason"] = _WAIT_PRESSURE_ENGINE_EXIT
            if pressure_engine_exit_error:
                trajectory["pressure_engine_error"] = pressure_engine_exit_error
        elif normalized_executor_reason:
            trajectory["stop_reason"] = str(raw_executor_reason).strip()
        elif wait_outcome == _WAIT_PHASE_DEADLINE:
            trajectory["stop_reason"] = _WAIT_PHASE_DEADLINE
        elif wait_outcome != _WAIT_COMPLETED:
            trajectory["stop_reason"] = str(wait_outcome)

        trajectory["stopped_on_crash"] = bool(stopped_on_crash)
        trajectory["stopped_on_process_exit"] = bool(stopped_on_process_exit)
        if collection_warnings:
            trajectory["collection_warnings"] = collection_warnings
        # Publish the handoff contract alongside the phase trace.  The cycle
        # scheduler uses this rather than a host return code when deciding
        # whether a failed recovery may be recorded and followed by another
        # cycle.
        trajectory["pressure_stop_verified"] = bool(fastbot_stop_verified)
        trajectory["foreground_guard_stop_failed"] = bool(
            getattr(executor, "foreground_guard_stop_failed", False)
        )
        trajectory["screenshot_collection_failed"] = bool(
            trajectory.get("screenshot_collection_failed")
            or getattr(executor, "screenshot_collection_failed", False)
        )
        screenshot_active = _bool_attribute(executor, "screenshot_collection_active")
        if screenshot_active is not None:
            trajectory["screenshot_collection_active"] = screenshot_active
        for name in (
            "uiautomator_release_failed",
            "control_release_failed",
            "mutex_release_failed",
        ):
            value = _bool_attribute(executor, name)
            if value is not None:
                trajectory[name] = bool(trajectory.get(name) or value)
        control_release_error = getattr(executor, "control_release_error", None)
        if control_release_error:
            trajectory["control_release_error"] = str(control_release_error)
        trajectory["control_handoff_safe"] = bool(
            fastbot_stop_verified
            and not trajectory["foreground_guard_stop_failed"]
            and not trajectory["screenshot_collection_failed"]
            and trajectory.get("screenshot_collection_active") is not True
            and trajectory.get("uiautomator_release_failed") is not True
            and trajectory.get("control_release_failed") is not True
            and trajectory.get("mutex_release_failed") is not True
            and not any(
                marker in str(recovery_handoff_error or "").lower()
                for marker in (
                    "uiautomator2 control",
                    "uiautomator2 could not",
                    "could not be restored",
                    "control was not released",
                )
            )
        )
    except BaseException as exc:
        phase_error = exc
        raise
    finally:
        def note_cleanup_failure(
            cleanup_exc: BaseException, *, fatal: bool = True
        ) -> None:
            """Record cleanup diagnostics and defer a standalone raise."""
            nonlocal phase_error, cleanup_only_error
            message = str(cleanup_exc).strip() or type(cleanup_exc).__name__
            if message not in cleanup_errors:
                cleanup_errors.append(message)
            trajectory.setdefault("cleanup_errors", list(cleanup_errors))
            trajectory["cleanup_error"] = message
            if fatal and phase_error is None:
                cleanup_only_error = cleanup_only_error or cleanup_exc
                phase_error = cleanup_only_error

        if fastbot_process is not None:
            try:
                executor.stop_fastbot(fastbot_process)
            except Exception as exc:
                stop_error = RuntimeError("Fastbot cleanup failed: %s" % exc)
                trajectory["pressure_stop_verified"] = False
                trajectory["pressure_stop_error"] = str(stop_error)
                trajectory["control_handoff_safe"] = False
                _mark_control_release_failure(executor, trajectory, stop_error)
                note_cleanup_failure(
                    stop_error
                )
                logger.exception("Unable to stop Fastbot cleanly")
        if screenshot_event is not None:
            try:
                collector_stopped = executor.stop_screenshot_collection(screenshot_event)
                if not collector_stopped:
                    collection_error = _mark_screenshot_collection_failure(
                        executor,
                        trajectory,
                        "screenshot collector remained active during cleanup",
                    )
                    if stopped_for_stall_recovery or stopped_for_permission_recovery:
                        record_collection_warning(
                            "%s; pressure will not be resumed" % collection_error
                        )
                    else:
                        note_cleanup_failure(
                            RuntimeError(
                                "Screenshot collector remains active; aborting this device run"
                            )
                        )
            except Exception as exc:
                collector_stopped = False
                collection_error = _mark_screenshot_collection_failure(
                    executor,
                    trajectory,
                    "screenshot collector cleanup raised: %s" % exc,
                )
                if stopped_for_stall_recovery or stopped_for_permission_recovery:
                    record_collection_warning(
                        "%s; pressure will not be resumed" % collection_error
                    )
                else:
                    note_cleanup_failure(
                        RuntimeError(
                            "Screenshot collector cleanup failed: %s" % exc
                        )
                    )
                    logger.exception("Unable to stop screenshot collection")
        if log_process is not None:
            try:
                executor.stop_logcat_collection(log_process)
            except Exception as exc:
                trajectory.setdefault("artifact_errors", []).append(
                    "logcat collector cleanup: %s" % exc
                )
                logger.exception("Unable to stop logcat collection")
        if acquired:
            release_error: Optional[BaseException] = None
            try:
                released = mutex.release("fastbot", device)
                if released is False:
                    release_error = RuntimeError(
                        "Fastbot control was released but uiautomator2 could not be restored"
                    )
            except Exception as exc:
                release_error = exc
            if release_error is not None:
                release_message = _mark_control_release_failure(
                    executor, trajectory, release_error
                )
                if stopped_for_stall_recovery:
                    recovery_handoff_error = release_message
                    logger.exception("Unable to release Fastbot control for stall recovery")
                elif stopped_for_permission_recovery:
                    recovery_handoff_error = release_message
                    logger.exception(
                        "Unable to release Fastbot control for permission recovery"
                    )
                elif stopped_for_foreground_recovery:
                    recovery_handoff_error = release_message
                    logger.exception(
                        "Unable to release Fastbot control after foreground recovery failure"
                    )
                elif stopped_on_crash or stopped_on_device_disconnect:
                    recovery_handoff_error = release_message
                    logger.exception(
                        "Unable to release Fastbot control while preserving runtime evidence"
                    )
                elif phase_error is None:
                    note_cleanup_failure(
                        RuntimeError(release_message)
                    )
                else:
                    logger.exception(
                        "Unable to release Fastbot control after an earlier phase failure; "
                        "preserving the original error"
                    )
        if not collector_stopped:
            if (
                not stopped_for_stall_recovery
                and not stopped_for_permission_recovery
                and not stopped_for_foreground_recovery
                and not stopped_on_crash
                and not stopped_on_device_disconnect
                and phase_error is None
            ):
                note_cleanup_failure(
                    RuntimeError(
                        "Screenshot collector remains active; aborting this device run"
                    )
                )
        if checkpoint is not None:
            try:
                checkpoint.update(
                    fastbot_pid=None,
                    fastbot_alive=False,
                    running=False,
                    last_error=(
                        "%s: %s" % (type(phase_error).__name__, phase_error)
                        if phase_error is not None
                        else None
                    ),
                )
            except Exception:
                logger.warning(
                    "Unable to close the pressure checkpoint on %s",
                    state_manager.device_serial,
                    exc_info=True,
                )
        if phase_error is not None:
            _attach_phase_failure_context(
                phase_error,
                trajectory=trajectory,
                report_dir=report_dir,
                cycle=cycle,
                log_path=log_path,
                log_offset=log_offset,
                pressure_log_path=pressure_log_path,
                pressure_log_offset=pressure_log_offset,
                phase_started_at=phase_started_at,
                phase_started_clock=phase_started_clock,
                executor=executor,
            )
    # ``finally`` cannot use a bare raise for a cleanup-only failure without
    # skipping the context attachment above.  Re-raise it after the evidence
    # bridge has been installed; an earlier body exception has already been
    # re-raised by the surrounding ``except`` and remains primary.
    if cleanup_only_error is not None:
        raise cleanup_only_error
    log_content = _read_log_segment(log_path, log_offset)
    pressure_log_content = _read_log_segment(pressure_log_path, pressure_log_offset)
    phase_finished_at = datetime.now(timezone.utc).isoformat()
    phase_elapsed_seconds = (
        round(max(0.0, time.monotonic() - phase_started_clock), 3)
        if phase_started_clock is not None
        else None
    )
    pressure_log_end_offset = (
        pressure_log_path.stat().st_size if pressure_log_path.exists() else 0
    )
    log_metrics = _pressure_log_metrics(pressure_log_content)
    current_marker_present = bool(
        re.search(
            r"^=== (?:Fastbot|System Monkey) started ",
            pressure_log_content,
            flags=re.MULTILINE,
        )
    )
    phase_details: Dict[str, Any] = {
        "started_at": phase_started_at,
        "finished_at": phase_finished_at,
        "elapsed_seconds": phase_elapsed_seconds,
        "requested_seconds": round(requested_seconds, 3),
        "engine_started_at": engine_started_at,
        "engine_finished_at": engine_finished_at,
        "engine_elapsed_seconds": engine_elapsed_seconds,
        "process_returncode": process_returncode,
        "stop_reason": str(trajectory.get("stop_reason") or ""),
        **log_metrics,
        "log_provenance": {
            "path": str(pressure_log_path),
            "start_offset": pressure_log_offset,
            "end_offset": pressure_log_end_offset,
            "bytes_added": max(0, pressure_log_end_offset - pressure_log_offset),
            "current_run_marker_present": current_marker_present,
            "current_run_only": current_marker_present,
            "remote_path": remote_log_path,
            "remote_stop_intent_path": remote_stop_intent_path,
        },
    }
    # These values describe only this Fastbot invocation.  The enclosing cycle
    # accumulator adds them to the durable prefix exactly once; the explicit
    # markers also let adapters return a cumulative counter when available.
    trajectory["operation_count_mode"] = "delta"
    trajectory["operation_count_is_total"] = False
    phase_details["elapsed_seconds_mode"] = "delta"
    phase_details["elapsed_seconds_is_total"] = False
    trajectory["phase"] = phase_details
    trajectory["phase_started_at"] = phase_started_at
    trajectory["phase_finished_at"] = phase_finished_at
    trajectory["phase_elapsed_seconds"] = phase_elapsed_seconds
    artifacts = {"logcat": str(log_path)} if log_path.is_file() else {}
    if pressure_log_path.is_file():
        artifacts["fastbot"] = str(pressure_log_path)
    stall_dir = report_dir / "stall_events"
    if stall_dir.is_dir():
        artifacts["stall_events"] = str(stall_dir)
    if stopped_for_stall_recovery:
        device_stopped = bool(fastbot_stop_verified)
        trajectory["stall_recovery_device_stopped"] = device_stopped
        if recovery_handoff_error:
            if device_stopped:
                # The remote engine is gone. A host-side handoff/collector
                # warning is evidence degradation, not a reason to skip the
                # requested target-app restart.
                warning = (
                    "Fastbot handoff warning after verified device stop: %s"
                    % recovery_handoff_error
                )
                trajectory["stall_recovery_handoff_warning"] = warning
                record_collection_warning(warning)
                trajectory["collection_warnings"] = list(collection_warnings)
            else:
                # Fail closed only when the device-side process could not be
                # verified gone. Relaunching while it may still own
                # UiAutomation would make the next pressure phase unsafe.
                trajectory["stall_recovery_precondition_error"] = (
                    recovery_handoff_error
                )
                trajectory["stall_recovery_abort_run"] = True
    elif recovery_handoff_error:
        # Preserve the existing classification for non-stall phase failures.
        trajectory["stall_recovery_precondition_error"] = recovery_handoff_error
        trajectory["stall_recovery_abort_run"] = True
    return trajectory, log_content, artifacts


def _capture_app_crash_page_evidence_legacy(
    state_manager: StateManager,
    mutex: MutexController,
    crash_handler: CrashHandler,
    report_dir: Path,
    cycle: int,
    detection: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Save the built-in crash page without changing or dismissing it."""
    directory = report_dir / "crash_pages" / ("cycle_%03d" % cycle)
    manifest_path = directory / "crash_page.json"
    evidence: Optional[Dict[str, Any]] = None

    def persist_partial(value: Dict[str, Any]) -> None:
        value.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
        value.setdefault("artifacts", {})["crash_page_evidence"] = str(
            manifest_path
        )
        value.setdefault("errors", [])
        try:
            directory.mkdir(parents=True, exist_ok=True)
            ReportGenerator.write_json(value, manifest_path)
            return
        except Exception as exc:
            message = "manifest: %s" % exc
            if message not in value["errors"]:
                value["errors"].append(message)
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Unable to persist crash-page manifest", exc_info=True)

    device = None
    acquired = False
    try:
        device = state_manager.get_device()
        acquired = bool(mutex.acquire("uiautomator2", device))
    except Exception as exc:
        if isinstance(detection, Mapping):
            evidence = dict(detection)
            evidence.setdefault("errors", []).append("device/control: %s" % exc)
            persist_partial(evidence)
        return evidence
    if not acquired:
        logger.warning("Unable to acquire UI control for crash-page evidence")
        if isinstance(detection, Mapping):
            evidence = dict(detection)
            evidence.setdefault("errors", []).append(
                "uiautomator2 control unavailable"
            )
            persist_partial(evidence)
        return evidence
    try:
        component_reader = getattr(state_manager, "current_component", None)
        current_component = (
            component_reader() if callable(component_reader) else None
        )
        detected_component = str(
            detection.get("component", "") if isinstance(detection, Mapping) else ""
        )
        matcher = getattr(crash_handler, "matches_app_crash_page_component", None)
        current_is_crash_page = bool(
            matcher(current_component, state_manager.package_name)
            if callable(matcher)
            else _is_app_crash_page_component(
                current_component, state_manager.package_name
            )
        )
        component = current_component if current_is_crash_page else detected_component
        component_is_crash_page = bool(
            matcher(component, state_manager.package_name)
            if callable(matcher)
            else _is_app_crash_page_component(component, state_manager.package_name)
        )
        if not component_is_crash_page:
            return None

        directory.mkdir(parents=True, exist_ok=True)
        evidence = {
            "component": component,
            "component_at_capture": current_component,
            "detected_at": (
                detection.get("detected_at")
                if isinstance(detection, Mapping)
                else None
            )
            or datetime.now(timezone.utc).isoformat(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "detection_source": (
                detection.get("detection_source")
                if isinstance(detection, Mapping)
                else "post_crash_component_probe"
            ),
            "artifacts": {},
            "errors": [],
        }

        if current_component and not current_is_crash_page:
            evidence["errors"].append(
                "crash page was no longer foreground when evidence capture began"
            )
            evidence["page_text"] = ""
            persist_partial(evidence)
            return evidence

        screenshot_reader = getattr(state_manager, "capture_screenshot_png", None)
        if callable(screenshot_reader):
            try:
                screenshot_path = directory / "crash_page.png"
                screenshot_path.write_bytes(screenshot_reader())
                evidence["artifacts"]["crash_page_screenshot"] = str(
                    screenshot_path
                )
            except Exception as exc:
                evidence["errors"].append("screenshot: %s" % exc)
                logger.warning("Unable to capture crash-page screenshot", exc_info=True)

        hierarchy = ""
        hierarchy_reader = getattr(state_manager, "dump_hierarchy_text", None)
        if callable(hierarchy_reader):
            try:
                hierarchy = str(hierarchy_reader() or "")
            except Exception as exc:
                evidence["errors"].append("hierarchy: %s" % exc)
                logger.warning("Unable to capture crash-page hierarchy", exc_info=True)
        if hierarchy:
            hierarchy_path = directory / "crash_page.xml"
            hierarchy_path.write_text(hierarchy, encoding="utf-8")
            evidence["artifacts"]["crash_page_hierarchy"] = str(hierarchy_path)

        page_text = crash_handler.extract_app_crash_page_text(hierarchy)
        evidence["page_text"] = page_text
        if page_text:
            text_path = directory / "crash_page.txt"
            text_path.write_text(page_text, encoding="utf-8")
            evidence["artifacts"]["crash_page_text"] = str(text_path)

        copy_selector = None
        try:
            selector_factory = getattr(getattr(state_manager, "d", None), "__call__", None)
            if callable(selector_factory):
                for copy_text in ("复制", "Copy", "澶嶅埗"):
                    candidate = state_manager.d(text=copy_text)
                    if candidate.wait(timeout=1):
                        copy_selector = candidate
                        break
            if copy_selector is None:
                evidence["copy_status"] = "not_found"
            else:
                copy_selector.click()
                time.sleep(0.2)
                copied = str(getattr(state_manager.d, "clipboard", "") or "").strip()
                if copied:
                    copied_path = directory / "crash_page_copied.txt"
                    copied_path.write_text(copied, encoding="utf-8")
                    evidence["copied_text"] = copied
                    evidence["copy_status"] = "captured"
                    evidence["artifacts"]["crash_page_copied_text"] = str(copied_path)
                else:
                    evidence["copy_status"] = "empty_clipboard"
        except Exception as exc:
            evidence["copy_status"] = "failed"
            evidence["errors"].append("copy: %s" % exc)
            logger.warning("Unable to copy app crash-page text", exc_info=True)

        persist_partial(evidence)
        return evidence
    except Exception as exc:
        if evidence is None and isinstance(detection, Mapping):
            evidence = dict(detection)
        if evidence is not None:
            evidence.setdefault("errors", []).append("capture: %s" % exc)
            persist_partial(evidence)
        return evidence
    finally:
        if acquired:
            try:
                released = mutex.release("uiautomator2", device)
                if released is False and evidence is not None:
                    evidence.setdefault("errors", []).append(
                        "uiautomator2 control release returned false"
                    )
            except Exception as exc:
                if evidence is not None:
                    evidence.setdefault("errors", []).append(
                        "release uiautomator2: %s" % exc
                    )
                logger.warning(
                    "Unable to release UI control after crash-page capture",
                    exc_info=True,
                )
            if evidence is not None:
                persist_partial(evidence)


def _capture_app_crash_page_evidence(
    state_manager: StateManager,
    mutex: MutexController,
    crash_handler: CrashHandler,
    report_dir: Path,
    cycle: int,
    detection: Optional[Mapping[str, Any]],
    *,
    allow_device_access: bool = True,
    executor: Any = None,
) -> Optional[Dict[str, Any]]:
    """Capture the application's crash page without losing the root record.

    A crash page is frequently replaced by Launcher while Fastbot is being
    stopped.  Every device/UI operation is therefore best-effort and the
    minimum manifest is persisted before taking the mutex or querying ADB.
    Auxiliary failures are recorded in ``errors`` rather than escaping and
    masking the original crash detection.
    """
    directory = report_dir / "crash_pages" / ("cycle_%03d" % cycle)
    manifest_path = directory / "crash_page.json"
    detection_values: Dict[str, Any] = (
        dict(detection) if isinstance(detection, Mapping) else {}
    )
    capture_requested = bool(detection_values)

    def safe_text(value: Any, default: str = "") -> str:
        try:
            return str(value or default)
        except Exception:
            return default

    detected_component = safe_text(
        detection_values.get("component")
        or detection_values.get("crash_page_component")
    ).strip()
    try:
        state_package_name = getattr(state_manager, "package_name", None)
    except Exception:
        state_package_name = None
    package_name = safe_text(
        state_package_name
        or detection_values.get("package_name")
        or detected_component.partition("/")[0]
    ).strip()
    captured_at = datetime.now(timezone.utc).isoformat()
    evidence: Dict[str, Any] = {
        "cycle": cycle,
        "package_name": package_name,
        "component": detected_component or None,
        "component_at_capture": None,
        "detected_at": safe_text(detection_values.get("detected_at"), captured_at),
        "captured_at": captured_at,
        "detection_source": safe_text(detection_values.get("detection_source"))
        or "post_crash_component_probe",
        "detection": detection_values,
        "page_type": "app_crash_page",
        "page_text": "",
        "artifacts": {"crash_page_evidence": str(manifest_path)},
        "errors": [],
    }

    def add_error(label: str, error: Any) -> None:
        message = "%s: %s" % (label, safe_text(error, "unknown error"))
        errors = evidence.setdefault("errors", [])
        if not isinstance(errors, list):
            errors = []
            evidence["errors"] = errors
        if message not in errors:
            errors.append(message)

    def persist_manifest() -> None:
        """Persist partial progress through independent writer fallbacks."""
        try:
            directory.mkdir(parents=True, exist_ok=True)
            ReportGenerator.write_json(evidence, manifest_path)
            return
        except Exception as exc:
            add_error("manifest", exc)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            write_checkpoint_json(manifest_path, evidence)
            return
        except Exception as exc:
            add_error("checkpoint manifest", exc)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            add_error("plain manifest", exc)
            logger.warning("Unable to persist crash-page manifest", exc_info=True)

    def matches(component: Any) -> bool:
        """Support current and legacy CrashHandler matcher signatures."""
        candidate = safe_text(component).strip()
        if not candidate or not package_name:
            return False
        matcher = getattr(crash_handler, "matches_app_crash_page_component", None)
        if callable(matcher):
            try:
                return bool(matcher(candidate, package_name))
            except TypeError:
                try:
                    return bool(matcher(candidate))
                except Exception:
                    pass
            except Exception:
                pass
        matcher = getattr(crash_handler, "is_app_crash_page_component", None)
        if callable(matcher):
            try:
                return bool(matcher(candidate, package_name))
            except TypeError:
                try:
                    return bool(matcher(candidate))
                except Exception:
                    pass
            except Exception:
                pass
        return _is_app_crash_page_component(candidate, package_name)

    # The trace that triggered this function is enough to retain a page record
    # even when the page has already disappeared from the foreground.
    detected_is_page = matches(detected_component)
    persist_manifest()
    if not allow_device_access:
        add_error(
            "device capture",
            "skipped because the device-control handoff is unsafe",
        )
        persist_manifest()
        return evidence if (capture_requested or detected_is_page) else None

    device = None
    acquired = False
    current_component: Optional[str] = None
    current_is_page = False
    try:
        try:
            device = state_manager.get_device()
        except Exception as exc:
            add_error("device", exc)
            return evidence if (capture_requested or detected_is_page) else None

        try:
            acquired = bool(mutex.acquire("uiautomator2", device))
        except Exception as exc:
            add_error("mutex acquire", exc)
            return evidence if (capture_requested or detected_is_page) else None
        if not acquired:
            add_error("mutex acquire", "uiautomator2 control unavailable")
            return evidence if (capture_requested or detected_is_page) else None

        component_reader = getattr(state_manager, "current_component", None)
        if callable(component_reader):
            try:
                current_component = safe_text(component_reader()).strip() or None
                evidence["component_at_capture"] = current_component
            except Exception as exc:
                add_error("foreground component", exc)
        current_is_page = matches(current_component)
        if current_is_page:
            evidence["component"] = current_component
        elif detected_is_page or capture_requested:
            add_error(
                "foreground component",
                "crash page was no longer foreground when evidence capture began",
            )
        elif current_component:
            # Do not classify an unrelated foreground activity as a crash page.
            evidence["component"] = current_component
            evidence["page_type"] = "unknown"
            persist_manifest()
            return None
        else:
            evidence["page_type"] = "unknown"

        screenshot_reader = getattr(state_manager, "capture_screenshot_png", None)
        if callable(screenshot_reader):
            try:
                screenshot_path = directory / "crash_page.png"
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                screenshot_path.write_bytes(screenshot_reader())
                evidence["artifacts"]["crash_page_screenshot"] = str(screenshot_path)
            except Exception as exc:
                add_error("screenshot", exc)
            persist_manifest()

        hierarchy = ""
        hierarchy_reader = getattr(state_manager, "dump_hierarchy_text", None)
        if callable(hierarchy_reader):
            try:
                hierarchy = safe_text(hierarchy_reader())
            except Exception as exc:
                add_error("hierarchy", exc)
        if hierarchy:
            try:
                hierarchy_path = directory / "crash_page.xml"
                hierarchy_path.parent.mkdir(parents=True, exist_ok=True)
                hierarchy_path.write_text(hierarchy, encoding="utf-8")
                evidence["artifacts"]["crash_page_hierarchy"] = str(hierarchy_path)
                evidence["hierarchy_sha256"] = hashlib.sha256(
                    hierarchy.encode("utf-8", errors="replace")
                ).hexdigest()
            except Exception as exc:
                add_error("hierarchy artifact", exc)
        persist_manifest()

        extractor = getattr(crash_handler, "extract_app_crash_page_text", None)
        if not callable(extractor):
            extractor = getattr(CrashHandler, "extract_app_crash_page_text", None)
        if callable(extractor):
            try:
                evidence["page_text"] = safe_text(extractor(hierarchy))
            except Exception as exc:
                add_error("crash page text", exc)
                evidence["page_text"] = hierarchy
        else:
            evidence["page_text"] = hierarchy
            if not hierarchy:
                add_error("crash page text", "no text extractor is available")
        if evidence.get("page_text"):
            try:
                text_path = directory / "crash_page.txt"
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(str(evidence["page_text"]), encoding="utf-8")
                evidence["artifacts"]["crash_page_text"] = str(text_path)
            except Exception as exc:
                add_error("crash page text artifact", exc)
        persist_manifest()

        # The button is localized.  Include correct Chinese, English, and the
        # historical mojibake label emitted by older versions of the page.
        copy_selector = None
        device_proxy = getattr(state_manager, "d", None)
        selector_factory = getattr(device_proxy, "__call__", None)
        if callable(selector_factory):
            copy_texts = ("\u590d\u5236", "Copy", "\u6fb6\u5d85\u57d7")
            for copy_text in copy_texts:
                try:
                    candidate = device_proxy(text=copy_text)
                    waiter = getattr(candidate, "wait", None)
                    available = bool(waiter(timeout=1)) if callable(waiter) else True
                    if available:
                        copy_selector = candidate
                        break
                except Exception as exc:
                    add_error("copy selector %s" % copy_text, exc)
        if copy_selector is None:
            evidence["copy_status"] = "not_found"
        else:
            try:
                clicker = getattr(copy_selector, "click", None)
                if not callable(clicker):
                    raise RuntimeError("copy selector has no click method")
                clicker()
                time.sleep(0.2)
                clipboard = getattr(device_proxy, "clipboard", "")
                if callable(clipboard):
                    clipboard = clipboard()
                copied = safe_text(clipboard).strip()
                if copied:
                    copied_path = directory / "crash_page_copied.txt"
                    copied_path.parent.mkdir(parents=True, exist_ok=True)
                    copied_path.write_text(copied, encoding="utf-8")
                    evidence["copied_text"] = copied
                    evidence["copy_status"] = "captured"
                    evidence["artifacts"]["crash_page_copied_text"] = str(copied_path)
                else:
                    evidence["copy_status"] = "empty_clipboard"
            except Exception as exc:
                evidence["copy_status"] = "failed"
                add_error("copy", exc)
                logger.warning("Unable to copy app crash-page text", exc_info=True)
        persist_manifest()
        return evidence if (capture_requested or detected_is_page or current_is_page) else None
    except Exception as exc:
        add_error("capture", exc)
        persist_manifest()
        return evidence if (capture_requested or detected_is_page or current_is_page) else None
    finally:
        if acquired:
            release_error: Optional[BaseException] = None
            try:
                released = mutex.release("uiautomator2", device)
                if released is False:
                    release_error = RuntimeError(
                        "uiautomator2 control release returned false during crash-page evidence capture"
                    )
            except Exception as exc:
                release_error = exc
            if release_error is not None:
                add_error("mutex release", release_error)
                _mark_control_release_failure(executor, evidence, release_error)
                logger.warning(
                    "Unable to release UI control after crash-page capture",
                    exc_info=True,
                )
        persist_manifest()


def _detect_cycle_crash(
    crash_handler: CrashHandler,
    log_content: str,
    page_detection: Optional[Mapping[str, Any]],
    page_evidence: Optional[Mapping[str, Any]],
    process_detection: Optional[Mapping[str, Any]] = None,
    app_crash_detection: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Prefer logcat details, falling back to the app's own crash page."""
    crash = crash_handler.detect_crash(log_content)
    if crash is None and isinstance(app_crash_detection, Mapping):
        crash = dict(app_crash_detection)
    evidence = page_evidence or page_detection
    if crash is None and isinstance(evidence, Mapping):
        crash = crash_handler.detect_app_crash_page(
            str(evidence.get("component", "")),
            str(evidence.get("copied_text") or evidence.get("page_text", "")),
            str(evidence.get("detected_at", "")) or None,
        )
    if crash is not None and isinstance(evidence, Mapping):
        crash = dict(crash)
        crash["app_crash_page"] = dict(evidence)
        crash["page_content"] = str(
            evidence.get("copied_text") or evidence.get("page_text", "")
        )
        if evidence.get("copied_text"):
            crash["copied_crash_content"] = str(evidence["copied_text"])
    return crash


_CONFIRMED_APP_CRASH_TYPES = frozenset(
    {"java", "anr", "native", "app_crash_page"}
)


def _is_confirmed_app_crash(value: Any) -> bool:
    """Return whether evidence proves that the target app itself crashed.

    A process-death record is useful runtime evidence, but ActivityManager can
    emit it for force-stop, vendor lifecycle policy, low-memory reclaim, or a
    test recovery handoff.  It is therefore not an app crash unless the record
    is an explicit ``am_crash`` event or stronger crash evidence is present.
    """
    if not isinstance(value, Mapping):
        return False
    crash_type = str(value.get("crash_type") or "").strip().lower()
    if crash_type in _CONFIRMED_APP_CRASH_TYPES:
        return True
    if bool(value.get("confirmed_app_crash")):
        return True
    if crash_type != "process_exit":
        return False
    return str(value.get("process_event") or "").strip().lower() == "am_crash"


def _confirmed_crash_candidates(values: Iterable[Any]) -> List[Dict[str, Any]]:
    """Keep only crash records backed by app-owned crash evidence."""
    return [dict(value) for value in values if _is_confirmed_app_crash(value)]


def _consume_stall_observations(executor: Any) -> Tuple[int, List[Dict[str, Any]]]:
    """Drain one pressure phase while preserving counts beyond the evidence cap."""
    count = max(0, int(getattr(executor, "stall_detection_count", 0) or 0))
    stall_reader = getattr(executor, "consume_stall_events", None)
    events = list(stall_reader()) if callable(stall_reader) else []
    return max(count, len(events)), events


def _stall_recovery_audit_options(
    config: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    scenario = config.get("scenario", {})
    if not isinstance(scenario, Mapping):
        return None
    audit = scenario.get("audit_recovery", {})
    if not isinstance(audit, Mapping) or not audit.get("enabled", False):
        return None
    audit_locator = audit.get("audit_locator")
    if not isinstance(audit_locator, Mapping):
        resource_id = str(audit.get("audit_resource_id", "")).strip()
        if not resource_id:
            app = config.get("app", {})
            package_name = (
                str(app.get("package_name", "")).strip()
                if isinstance(app, Mapping)
                else ""
            )
            if package_name:
                resource_id = "%s:id/auditRadioButton" % package_name
        audit_locator = {"resourceId": resource_id} if resource_id else {}
    confirm_locator = audit.get("confirm_locator")
    if not isinstance(confirm_locator, Mapping):
        confirm_text = str(audit.get("confirm_text", "确定")).strip()
        confirm_locator = {"text": confirm_text} if confirm_text else {}
    if not audit_locator or not confirm_locator:
        raise RuntimeError("stall recovery audit gate requires both locators")
    return {
        "audit_locator": dict(audit_locator),
        "confirm_locator": dict(confirm_locator),
        "activity": str(audit.get("activity", "")),
        "timeout": min(60.0, max(5.0, float(audit.get("timeout", 30)))),
    }


def _complete_configured_audit_gate(
    state_manager: StateManager,
    config: Mapping[str, Any],
    *,
    timeout: Optional[float] = None,
    cancel_event: Any = None,
) -> bool:
    """Complete the configured startup gate after any app relaunch.

    The gate is optional at the framework level: an explicitly disabled rule
    or a configuration without the normalized ``audit_recovery`` block is a
    no-op.  When enabled, the real StateManager must perform the idempotent
    audit selection and confirmation before scenario navigation resumes.
    """
    options = _stall_recovery_audit_options(config)
    if options is None:
        return True
    recover = getattr(state_manager, "recover_audit_gate", None)
    if not callable(recover):
        logger.error(
            "Configured audit gate cannot be checked on %s: StateManager "
            "does not expose recover_audit_gate",
            getattr(state_manager, "device_serial", "unknown-device"),
        )
        return False
    gate_timeout = (
        options["timeout"]
        if timeout is None
        else min(60.0, max(1.0, float(timeout)))
    )
    should_stop = None
    if cancel_event is not None:
        should_stop = lambda: bool(cancel_event.is_set())
    _check_cancel(cancel_event)
    try:
        completed = bool(
            recover(
                options["audit_locator"],
                options["confirm_locator"],
                gate_timeout,
                audit_activity=options["activity"],
                should_stop=should_stop,
            )
        )
    except TypeError as exc:
        # Keep small third-party StateManager test doubles compatible while
        # retaining the full call for the production implementation.
        if not any(
            marker in str(exc)
            for marker in ("audit_activity", "should_stop", "unexpected keyword")
        ):
            raise
        completed = bool(
            recover(
                options["audit_locator"],
                options["confirm_locator"],
                gate_timeout,
            )
        )
    _check_cancel(cancel_event)
    return completed


def _prepare_audit_gate_with_retry(
    state_manager: Any,
    executor: Any,
    device: Any,
    *,
    timeout: float = 20.0,
) -> Tuple[bool, Any]:
    """Prepare startup gates and recover once from a stale uiautomator2 object.

    The first invocation remains the normal fast path.  Only a false result or
    an exception triggers a bounded session refresh and one retry, so successful
    calls keep their existing call count and timing.  The returned device is
    always the object that should be used for the matching mutex release.
    """
    preparer = getattr(executor, "prepare_audit_gate", None)
    if not callable(preparer):
        return True, device

    try:
        budget = min(60.0, max(1.0, float(timeout)))
    except (TypeError, ValueError):
        budget = 20.0

    first_error: Optional[BaseException] = None
    try:
        if bool(preparer(timeout=budget, device=device)):
            return True, device
    except (InterruptedError, KeyboardInterrupt):
        raise
    except Exception as exc:
        first_error = exc

    logger.warning(
        "Startup audit/privacy preparation did not complete on %s; refreshing "
        "uiautomator2 and retrying once%s",
        getattr(state_manager, "device_serial", "unknown-device"),
        ": %s" % first_error if first_error else "",
    )
    refreshed_device = device
    try:
        candidate = _refresh_uiautomator_device(state_manager)
        if candidate is not None:
            refreshed_device = candidate
    except (InterruptedError, KeyboardInterrupt):
        raise
    except Exception:
        logger.warning(
            "Could not refresh uiautomator2 before startup-gate retry on %s",
            getattr(state_manager, "device_serial", "unknown-device"),
            exc_info=True,
        )

    retry_budget = min(budget, 12.0)
    try:
        completed = bool(preparer(timeout=retry_budget, device=refreshed_device))
    except (InterruptedError, KeyboardInterrupt):
        raise
    except Exception as exc:
        logger.warning(
            "Startup audit/privacy retry failed on %s: %s",
            getattr(state_manager, "device_serial", "unknown-device"),
            exc,
            exc_info=True,
        )
        completed = False
    if completed:
        logger.info(
            "Startup audit/privacy preparation recovered on %s after refreshing "
            "uiautomator2",
            getattr(state_manager, "device_serial", "unknown-device"),
        )
    return completed, refreshed_device


def _stall_recovery_action(config: Optional[Mapping[str, Any]] = None) -> str:
    scenario = config.get("scenario", {}) if isinstance(config, Mapping) else {}
    audit = scenario.get("audit_recovery", {}) if isinstance(scenario, Mapping) else {}
    if isinstance(audit, Mapping) and audit.get("enabled", False):
        return "force_stop_clear_task_relaunch_audit"
    return "force_stop_clear_task_relaunch"


def _pending_stall_recovery_event(
    trajectory: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    events = trajectory.get("stall_events", [])
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, Mapping) or event.get("mode") not in {
            "recover_once",
            "recover",
        }:
            continue
        if event.get("recovery_action") is None:
            return event
    return None


def _normalize_android_component(component: Any) -> str:
    """Canonicalize Android's ``package/.Activity`` shorthand."""
    package, separator, activity = str(component or "").strip().partition("/")
    package = package.strip()
    activity = activity.strip()
    if not separator or not package or not activity:
        return str(component or "").strip()
    if activity.startswith("."):
        activity = package + activity
    return "%s/%s" % (package, activity)


def _load_stall_trigger(
    event: Optional[Mapping[str, Any]],
) -> Tuple[str, Optional[ImageFingerprint], Optional[Path], Optional[str]]:
    """Load optional stall evidence without making it a recovery precondition."""
    if not isinstance(event, Mapping):
        raise ValueError("triggering stall event is unavailable")
    component = _normalize_android_component(event.get("component"))
    if not component or "/" not in component:
        raise ValueError("triggering stall component is unavailable")

    expected_hash = str(event.get("hash") or "").strip().lower()
    evidence_text = str(event.get("evidence") or "").strip()
    if event.get("evidence_saved") is not True or not evidence_text:
        return component, None, None, "triggering stall evidence was not saved"
    evidence_path = Path(evidence_text)
    if not expected_hash or re.fullmatch(r"[0-9a-f]+", expected_hash) is None:
        return component, None, evidence_path, "triggering stall fingerprint is invalid"
    try:
        content = evidence_path.read_bytes()
    except OSError as exc:
        return (
            component,
            None,
            evidence_path,
            "triggering stall evidence is unavailable: %s" % exc,
        )
    try:
        fingerprint = fingerprint_png(content)
    except ValueError as exc:
        return (
            component,
            None,
            evidence_path,
            "triggering stall evidence is invalid: %s" % exc,
        )
    if fingerprint.as_hex() != expected_hash:
        return (
            component,
            fingerprint,
            evidence_path,
            "triggering stall evidence fingerprint does not match its event",
        )
    return component, fingerprint, evidence_path, None


def _consume_manual_stop_request(mutex: Any) -> bool:
    """Consume a device-local stop marker only when it is explicitly present."""
    checker = getattr(mutex, "has_stop_request", None)
    if not callable(checker):
        return False
    try:
        requested = checker()
    except Exception:
        logger.warning("Unable to inspect the manual stop marker", exc_info=True)
        return False
    if requested is not True:
        return False
    clear = getattr(mutex, "clear_stop_request", None)
    try:
        if not callable(clear) or clear() is not True:
            logger.warning("Unable to consume the manual stop marker")
    except Exception:
        logger.warning("Unable to consume the manual stop marker", exc_info=True)
    return True


def _recover_stalled_page(
    state_manager: StateManager,
    mutex: MutexController,
    config: Mapping[str, Any],
    trigger_event: Optional[Mapping[str, Any]],
    cancel_event: Any = None,
    *,
    executor: Any = None,
) -> Dict[str, Any]:
    """Restart the target app without clearing data, then resume pressure."""
    result: Dict[str, Any] = {
        "action": _stall_recovery_action(config),
        "attempted": False,
        "skipped": False,
        "data_cleared": False,
        "reinstalled": False,
        "success": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    acquired = False
    device = None
    stop_state = {
        "cancelled": False,
        "manual": False,
        "last_manual_probe": 0.0,
    }

    def recovery_stop_requested(*, force_manual_probe: bool = False) -> bool:
        try:
            _check_cancel(cancel_event)
        except InterruptedError:
            stop_state["cancelled"] = True
        now = time.monotonic()
        if (
            not stop_state["manual"]
            and (
                force_manual_probe
                or now - float(stop_state["last_manual_probe"]) >= 0.5
            )
        ):
            stop_state["last_manual_probe"] = now
            if _consume_manual_stop_request(mutex):
                stop_state["manual"] = True
        return bool(stop_state["cancelled"] or stop_state["manual"])

    def mark_recovery_interrupted() -> None:
        if result.get("attempted"):
            result["interrupted"] = True
        else:
            result["skipped"] = True
        result["cancelled"] = bool(stop_state["cancelled"])
        result["manual_stop_requested"] = bool(stop_state["manual"])
        result["error"] = (
            "stall recovery interrupted because the run was cancelled"
            if stop_state["cancelled"]
            else "stall recovery interrupted because manual stop was requested"
        )

    try:
        # A watchdog request is authoritative even when the bounded evidence
        # record was lost (for example after a screenshot transport failure).
        # Keep explicit, malformed event metadata fail-closed, but provide a
        # target-package-only component for legacy executors that signal a
        # recovery without publishing the event itself.
        if not isinstance(trigger_event, Mapping):
            fallback_component = ""
            component_reader = getattr(state_manager, "current_component", None)
            if callable(component_reader):
                try:
                    fallback_component = _normalize_android_component(
                        component_reader()
                    )
                except Exception:
                    fallback_component = ""
            if not fallback_component or fallback_component.split("/", 1)[0] != state_manager.package_name:
                fallback_component = "%s/.unknown" % state_manager.package_name
            trigger_event = {
                "component": fallback_component,
                "evidence_saved": False,
            }
            result["trigger_metadata_fallback"] = True
        try:
            (
                trigger_component,
                trigger_fingerprint,
                evidence_path,
                evidence_warning,
            ) = _load_stall_trigger(trigger_event)
        except (ValueError, OSError) as exc:
            result["skipped"] = True
            result["error"] = str(exc)
            return result
        if trigger_component.split("/", 1)[0] != state_manager.package_name:
            result["skipped"] = True
            result["error"] = "triggering stall event is not for the target package"
            return result
        result["trigger_component"] = trigger_component
        trigger_hash = (
            trigger_fingerprint.as_hex()
            if trigger_fingerprint is not None
            else str(trigger_event.get("hash") or "").strip().lower()
            if isinstance(trigger_event, Mapping)
            else ""
        )
        if trigger_hash:
            result["trigger_hash"] = trigger_hash
        if evidence_path is not None:
            result["trigger_evidence"] = str(evidence_path)
        if evidence_warning:
            result["evidence_warning"] = evidence_warning

        audit_options = _stall_recovery_audit_options(config)
        device = state_manager.get_device()
        if not mutex.acquire("uiautomator2", device):
            result["skipped"] = True
            result["error"] = "uiautomator2 control unavailable for stall recovery"
            return result
        acquired = True
        if recovery_stop_requested(force_manual_probe=True):
            mark_recovery_interrupted()
            return result

        result["attempted"] = True
        # This is deliberately limited to the target package. It force-stops
        # and starts a fresh task without clearing data or reinstalling.
        if not _reset_app(
            state_manager,
            clear_data=False,
            stop_packages=None,
            clear_task=True,
        ):
            result["error"] = "force-stop and clean relaunch failed"
            return result
        if recovery_stop_requested(force_manual_probe=True):
            mark_recovery_interrupted()
            return result
        # ``reset_app`` can replace a stale uiautomator2 session while waiting
        # for a vendor launcher transition.  Use that live object for startup
        # gate handling and for the matching mutex release below.
        device = state_manager.get_device()
        if audit_options is not None:
            audit_completed = False
            preparer = getattr(executor, "prepare_audit_gate", None)
            if callable(preparer):
                result["audit_gate_method"] = "executor"
                audit_completed, device = _prepare_audit_gate_with_retry(
                    state_manager,
                    executor,
                    device,
                    timeout=audit_options["timeout"],
                )
            if recovery_stop_requested(force_manual_probe=True):
                mark_recovery_interrupted()
                return result

            if not audit_completed:
                # Older executors do not expose the complete audit/privacy
                # chain.  Keep StateManager's locator-based implementation as
                # a compatibility fallback and as a final bounded probe after
                # an inconclusive hierarchy dump.
                recover_audit = getattr(state_manager, "recover_audit_gate", None)
                if not callable(recover_audit):
                    result["error"] = (
                        "audit-gate preparation failed and fallback is unavailable"
                        if callable(preparer)
                        else "audit-gate recovery is unavailable"
                    )
                    return result
                result["audit_gate_method"] = "state_manager"
                result["audit_gate_fallback_used"] = callable(preparer)
                audit_completed = bool(
                    recover_audit(
                        audit_options["audit_locator"],
                        audit_options["confirm_locator"],
                        audit_options["timeout"],
                        audit_activity=audit_options["activity"],
                        should_stop=recovery_stop_requested,
                    )
                )
            result["audit_gate_completed"] = audit_completed
            if recovery_stop_requested(force_manual_probe=True):
                mark_recovery_interrupted()
                return result
            if not audit_completed:
                result["error"] = "audit-gate recovery failed"
                return result
        # StateManager.reset_app already verifies that the target package is
        # foreground. A stall is a coverage-efficiency signal, not evidence of
        # an app defect, so screenshot availability and the resulting Activity
        # must not decide whether this control-plane restart succeeded.
        result["verification_mode"] = "target_foreground_after_relaunch"
        result["success"] = True
        return result
    except Exception as exc:
        logger.exception("Stall recovery failed on %s", state_manager.device_serial)
        if not result.get("attempted"):
            result["skipped"] = True
        result["error"] = str(exc)
        return result
    finally:
        if acquired:
            release_error: Optional[BaseException] = None
            try:
                released = mutex.release("uiautomator2", device)
                if released is False:
                    release_error = RuntimeError(
                        "uiautomator2 control was not released"
                    )
            except Exception as exc:
                release_error = exc
                logger.exception(
                    "Unable to release uiautomator2 after stall recovery on %s",
                    state_manager.device_serial,
                )
            if release_error is not None:
                result["success"] = False
                result["error"] = str(release_error)
                _mark_control_release_failure(executor, result, release_error)
        result["completed_at"] = datetime.now(timezone.utc).isoformat()


def _failed_stall_recovery(
    error: str, config: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Describe a requested recovery skipped before the relaunch was attempted."""
    timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "action": _stall_recovery_action(config),
        "attempted": False,
        "data_cleared": False,
        "reinstalled": False,
        "success": False,
        "skipped": True,
        "error": str(error),
        "started_at": timestamp,
        "completed_at": timestamp,
    }


def _apply_stall_recovery_result(
    trajectory: Dict[str, Any], result: Mapping[str, Any]
) -> None:
    """Attach the bounded recovery outcome to its triggering stall evidence."""
    details = dict(result)
    skipped = bool(details.get("skipped", False))
    attempted = bool(details.get("attempted", not skipped))
    details["attempted"] = attempted
    details["skipped"] = skipped
    trajectory["stall_recovery"] = details
    trajectory["stall_recovery_requested_count"] = 1
    trajectory["stall_recovery_count"] = 1 if attempted else 0
    trajectory["stall_recovery_skipped_count"] = 1 if skipped else 0
    trajectory["stall_recovery_failure_count"] = (
        1
        if attempted
        and not details.get("success")
        and not details.get("interrupted")
        else 0
    )
    events = trajectory.get("stall_events", [])
    if not isinstance(events, list):
        return
    for event in events:
        if not isinstance(event, dict) or event.get("mode") not in {
            "recover_once",
            "recover",
        }:
            continue
        if event.get("recovery_action") is not None:
            continue
        event["recovery_action"] = details.get("action")
        event["recovery_attempted"] = attempted
        event["recovery_skipped"] = skipped
        event["recovery_success"] = bool(details.get("success"))
        event["recovery"] = details
        break


def _apply_stall_recovery_summary(
    trajectory: Dict[str, Any], results: Sequence[Mapping[str, Any]]
) -> None:
    """Aggregate bounded recovery attempts into the phase trajectory."""
    attempted = sum(1 for item in results if item.get("attempted"))
    skipped = sum(1 for item in results if item.get("skipped"))
    failed = sum(
        1
        for item in results
        if item.get("attempted")
        and not item.get("success")
        and not item.get("interrupted")
    )
    trajectory["stall_recovery_requested_count"] = len(results)
    trajectory["stall_recovery_count"] = attempted
    trajectory["stall_recovery_skipped_count"] = skipped
    trajectory["stall_recovery_failure_count"] = failed


def _perform_stall_recovery(
    trajectory: Dict[str, Any],
    state_manager: StateManager,
    mutex: MutexController,
    config: Mapping[str, Any],
    cancel_event: Any = None,
    *,
    executor: Any = None,
) -> None:
    """Apply one recovery request while giving late manual stops precedence."""
    if isinstance(trajectory.get("stall_recovery"), Mapping):
        return
    stop_reason = trajectory.get("stop_reason")
    recovery_requested = bool(
        stop_reason in {_WAIT_STALL_RECOVERY, _WAIT_STALL_RECOVERY_UNCLEAN}
        or trajectory.get("stall_recovery_requested") is True
    )
    if not recovery_requested:
        return
    if stop_reason == "manual":
        recovery_result = _failed_stall_recovery(
            "stall recovery skipped because manual stop won the stop race",
            config,
        )
        recovery_result["manual_stop_requested"] = True
        _apply_stall_recovery_result(trajectory, recovery_result)
        return

    cancelled = False
    try:
        _check_cancel(cancel_event)
    except InterruptedError:
        cancelled = True
    manual_stop = _consume_manual_stop_request(mutex)
    try:
        _check_cancel(cancel_event)
    except InterruptedError:
        cancelled = True

    handoff_error = trajectory.get("stall_recovery_precondition_error")
    device_stopped_marker = trajectory.get("stall_recovery_device_stopped")
    if cancelled or manual_stop:
        reason = (
            "stall recovery skipped because the run was cancelled"
            if cancelled
            else "stall recovery skipped because manual stop was requested"
        )
        recovery_result = _failed_stall_recovery(reason, config)
        recovery_result["cancelled"] = cancelled
        recovery_result["manual_stop_requested"] = manual_stop
        trajectory["stop_reason"] = "manual"
    elif device_stopped_marker is False:
        recovery_result = _failed_stall_recovery(
            "stall recovery skipped because the device-side Fastbot process "
            "was not verified stopped",
            config,
        )
    elif handoff_error and device_stopped_marker is not True:
        # A missing marker is retained for older executors and test doubles:
        # their existing precondition error remains fail-closed. New phases
        # explicitly publish ``True`` when the remote process is gone, which
        # allows a host-only handoff warning to proceed to the App restart.
        recovery_result = _failed_stall_recovery(str(handoff_error), config)
    else:
        if device_stopped_marker is True:
            # Do not let a stale precondition flag from an older phase prevent
            # the verified target-only recovery. Keep it visible as a warning.
            if handoff_error:
                warnings = trajectory.setdefault("collection_warnings", [])
                if isinstance(warnings, list) and str(handoff_error) not in warnings:
                    warnings.append(str(handoff_error))
            trajectory.pop("stall_recovery_precondition_error", None)
            trajectory.pop("stall_recovery_abort_run", None)
        recovery_result = _recover_stalled_page(
            state_manager,
            mutex,
            config,
            _pending_stall_recovery_event(trajectory),
            cancel_event,
            executor=executor,
        )
        if recovery_result.get("cancelled") or recovery_result.get(
            "manual_stop_requested"
        ):
            trajectory["stop_reason"] = "manual"
    _merge_handoff_fields(trajectory, recovery_result)
    if recovery_result.get("control_release_failed"):
        trajectory["stall_recovery_abort_run"] = True
    _apply_stall_recovery_result(trajectory, recovery_result)


def _invoke_stall_recovery(
    trajectory: Dict[str, Any],
    state_manager: StateManager,
    mutex: MutexController,
    config: Mapping[str, Any],
    cancel_event: Any = None,
    *,
    executor: Any = None,
) -> None:
    """Call stall recovery while retaining compatibility with old adapters."""
    runner = _perform_stall_recovery
    # Inspect the callable rather than its module. Decorators, plug-ins, and
    # test adapters can legitimately live in another module while supporting
    # the new keyword, and a production wrapper may expose an old signature.
    signature_target = getattr(runner, "side_effect", None)
    if not callable(signature_target):
        signature_target = inspect.unwrap(runner)
    try:
        parameters = inspect.signature(signature_target).parameters
    except (TypeError, ValueError):
        parameters = None

    if parameters is not None:
        executor_parameter = parameters.get("executor")
        supports_keyword = bool(
            executor_parameter is not None
            and executor_parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        ) or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if not supports_keyword:
            runner(trajectory, state_manager, mutex, config, cancel_event)
            return
        runner(
            trajectory,
            state_manager,
            mutex,
            config,
            cancel_event,
            executor=executor,
        )
        return

    # A few extension callables cannot be introspected. Prefer the modern
    # contract, but only retry an old signature when Python explicitly says
    # that this keyword is unsupported. A TypeError raised inside the recovery
    # function remains visible to the caller.
    try:
        runner(
            trajectory,
            state_manager,
            mutex,
            config,
            cancel_event,
            executor=executor,
        )
    except TypeError as exc:
        message = str(exc).lower()
        if "unexpected keyword argument" not in message or "executor" not in message:
            raise
        runner(trajectory, state_manager, mutex, config, cancel_event)


def _replay_crash(
    state_manager: StateManager,
    crash_handler: CrashHandler,
    mutex: MutexController,
    device: Any,
    snapshot: Dict[str, Any],
    trajectory: Dict[str, Any],
    crash_info: Dict[str, Any],
    attempts: int,
    cancel_event: Any = None,
    *,
    executor: Any = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Replay a detected crash under uiautomator2, one bounded attempt at a time."""
    if not mutex.acquire("uiautomator2", device):
        return False, [{"success": False, "error": "uiautomator2 control unavailable"}]
    results: List[Dict[str, Any]] = []
    try:
        for attempt in range(1, attempts + 1):
            _check_cancel(cancel_event)
            replay_plan = trajectory.get("replay_plan", {})
            has_plan = isinstance(replay_plan, Mapping) and bool(replay_plan)
            preparer = getattr(crash_handler, "prepare_replay_start", None)
            start_result: Dict[str, Any] = {}
            if has_plan and callable(preparer):
                start_result = dict(
                    preparer(
                        trajectory,
                        crash_info.get(
                            "package_name", state_manager.package_name
                        ),
                        "auto",
                    )
                )
                restored = bool(start_result.get("success"))
                if not restored:
                    # An empty ``replay_plan`` is still a Mapping, so the
                    # snapshot fallback used to be unreachable for every
                    # trajectory.  When the anchor cannot be restored, reopen
                    # the package from the cycle-start snapshot instead of
                    # abandoning the replay before injecting a single event.
                    fallback_restored = state_manager.restore_snapshot(snapshot)
                    start_result["snapshot_fallback"] = bool(fallback_restored)
                    if fallback_restored:
                        start_result["fallback_reason"] = str(
                            start_result.get("error") or "page anchor unavailable"
                        )
                        start_result["strategy"] = "snapshot_fallback"
                        start_result.pop("error", None)
                        logger.warning(
                            "Replay anchor could not be reopened (%s); replaying "
                            "from the cycle-start snapshot instead",
                            start_result["fallback_reason"],
                        )
                    restored = bool(fallback_restored)
            else:
                # Trajectories without a bounded plan (legacy reports and the
                # fastbot-only path) keep the initial-snapshot behavior.
                restored = state_manager.restore_snapshot(snapshot)
            if restored:
                replayed = crash_handler.replay_trajectory(
                    trajectory,
                    crash_info.get("package_name", state_manager.package_name),
                )
                replay_result = dict(crash_handler.last_replay_result)
            else:
                replayed = False
                replay_result = {
                    "executed": False,
                    "reason": str(
                        start_result.get("error")
                        or "snapshot restore failed"
                    ),
                    "crash": None,
                }
            classifier = getattr(crash_handler, "classify_reproduction", None)
            if callable(classifier):
                reproduction_match = str(
                    classifier(
                        str(crash_info.get("signature", "")), replay_result
                    )
                )
                reproducible = replayed and reproduction_match != "none"
            else:
                reproduction_match = "exact" if replayed else "none"
                reproducible = replayed and crash_handler.verify_crash_reproducible(
                    str(crash_info.get("signature", "")), replay_result
                )
            results.append(
                {
                    "attempt": attempt,
                    "restored": restored,
                    "start": start_result,
                    "replayed": replayed,
                    "reproducible": reproducible,
                    "reproduction_match": reproduction_match,
                    "result": replay_result,
                }
            )
            if reproducible:
                return True, results
        return False, results
    finally:
        release_error: Optional[BaseException] = None
        try:
            if mutex.release("uiautomator2", device) is False:
                release_error = RuntimeError(
                    "uiautomator2 control release returned false after crash replay"
                )
        except Exception as exc:
            release_error = exc
            logger.exception("Unable to release uiautomator2 control after replay")
        if release_error is not None:
            _mark_control_release_failure(executor, trajectory, release_error)
            crash_info["replay_control_release_error"] = str(release_error)


def _add_crash(report: Dict[str, Any], crash: Dict[str, Any], cycle: int) -> None:
    """Deduplicate crashes by signature while retaining occurrence cycles."""
    if not _is_confirmed_app_crash(crash):
        return
    signature = str(crash.get("signature") or "unknown")
    existing = next(
        (item for item in report["crashes"] if item.get("signature") == signature),
        None,
    )
    if existing is None:
        crash = dict(crash)
        crash["occurrences"] = [cycle]
        crash["occurrence_details"] = [
            {
                "cycle": cycle,
                "phase": crash.get("phase"),
                "detected_at": crash.get("detected_at"),
                "trajectory_path": crash.get("trajectory_path"),
            }
        ]
        report["crashes"].append(crash)
    else:
        existing.setdefault("occurrences", []).append(cycle)
        existing.setdefault("occurrence_details", []).append(
            {
                "cycle": cycle,
                "phase": crash.get("phase"),
                "detected_at": crash.get("detected_at"),
                "trajectory_path": crash.get("trajectory_path"),
            }
        )
        existing["reproducible"] = bool(
            existing.get("reproducible") or crash.get("reproducible")
        )
        existing.setdefault("replay_attempts", []).extend(
            crash.get("replay_attempts", [])
        )


def _abort_remaining_cycles_message(exc: BaseException, executor: Any) -> str:
    """Describe the actual unrecoverable subsystem instead of blaming collectors."""
    detail = str(exc).strip() or exc.__class__.__name__
    normalized = detail.lower()
    if any(
        marker in normalized
        for marker in (
            "device disconnected",
            "device not found",
            "device is offline",
            "device went offline",
        )
    ):
        reason = "the device disconnected"
    elif getattr(executor, "screenshot_collection_failed", False):
        reason = "the screenshot collector could not stop"
    elif getattr(executor, "foreground_guard_stop_failed", False):
        reason = "the foreground guard could not stop"
    elif getattr(executor, "foreground_restore_failed", False):
        reason = "the foreground/ADB handoff could not restore the target app"
    elif any(
        marker in normalized
        for marker in (
            "launcher",
            "activitymanager",
            "activity manager",
            "am start",
            "target package did not return to foreground",
        )
    ):
        reason = "the foreground/ADB handoff could not restore the target app"
    elif "uiautomator2 could not be restored" in normalized:
        reason = "uiautomator2 control could not be restored"
    elif "unable to resume paused pressure-engine pids" in normalized:
        reason = "the Fastbot process ended before its paused PID handoff completed"
    elif isinstance(exc, PressureEngineError):
        reason = "the pressure engine could not be recovered"
    else:
        reason = "device control could not be recovered"
    return "aborting remaining cycles because %s: %s" % (reason, detail)


def _bool_attribute(owner: Any, name: str) -> Optional[bool]:
    """Read a boolean status attribute without trusting truthy test doubles."""
    try:
        value = getattr(owner, name)
    except Exception:
        return None
    if callable(value):
        try:
            value = value()
        except Exception:
            return None
    if isinstance(value, bool):
        return value
    return None


_CYCLE_HANDOFF_BOOL_FIELDS = (
    "pressure_stop_verified",
    "control_handoff_safe",
    "foreground_guard_stop_failed",
    "screenshot_collection_failed",
    "uiautomator_release_failed",
    "control_release_failed",
    "mutex_release_failed",
)
_CYCLE_HANDOFF_ERROR_FIELDS = (
    "pressure_stop_verification_error",
    "pressure_stop_error",
    "control_release_error",
    "screenshot_collection_error",
    "foreground_restore_error",
    "foreground_recovery_error",
)
_HARD_CONTROL_FAILURE_MARKERS = (
    "device-side pressure process is still running",
    "device-side fastbot process is still running",
    "device-side fastbot stop verification failed",
    "fastbot or its foreground guard did not stop cleanly",
    "uiautomator2 control was not released",
    "uiautomator2 could not be restored",
    "uiautomator2 could not acquire exclusive device control",
    "fastbot could not acquire exclusive device control",
    "could not acquire exclusive device control",
    "unable to resume paused pressure-engine pids",
    "foreground guard did not stop",
    "screenshot collector remains active",
    "screenshot collector did not stop cleanly",
    "control release returned false",
    "control release failed",
    "control was not released",
    "release returned false",
)


def _mark_control_release_failure(
    executor: Any,
    trajectory: Optional[Dict[str, Any]],
    error: Any,
) -> str:
    """Publish one failed mutex/uiautomator2 handoff without hiding its cause."""
    message = str(error or "device control release failed").strip()
    marker = getattr(executor, "mark_control_release_failed", None)
    if callable(marker):
        try:
            marker(message)
        except Exception:
            logger.warning(
                "Unable to publish executor control-release failure",
                exc_info=True,
            )
    else:
        # Compatibility for older executors and focused test adapters.  The
        # durable trajectory below remains authoritative even when the object
        # does not expose public status properties.
        for name, value in (
            ("_uiautomator_release_failed", True),
            ("_control_release_failed", True),
            ("_mutex_release_failed", True),
            ("_control_release_error", message),
        ):
            try:
                setattr(executor, name, value)
            except Exception:
                pass
    if isinstance(trajectory, dict):
        trajectory["uiautomator_release_failed"] = True
        trajectory["control_release_failed"] = True
        trajectory["mutex_release_failed"] = True
        trajectory["control_handoff_safe"] = False
        trajectory["control_release_error"] = message
    return message


def _mark_screenshot_collection_failure(
    executor: Any,
    trajectory: Optional[Dict[str, Any]],
    error: Any,
) -> str:
    """Make a collector that failed to stop a hard, reportable handoff fault."""
    message = str(error or "screenshot collector did not stop cleanly").strip()
    marker = getattr(executor, "mark_screenshot_collection_failed", None)
    if callable(marker):
        try:
            marker(message)
        except Exception:
            logger.warning(
                "Unable to publish executor screenshot-stop failure",
                exc_info=True,
            )
    else:
        try:
            setattr(executor, "_screenshot_collection_failed", True)
            setattr(executor, "_screenshot_collection_error", message)
        except Exception:
            pass
    active = _bool_attribute(executor, "screenshot_collection_active")
    if isinstance(trajectory, dict):
        trajectory["screenshot_collection_failed"] = True
        # A false/unknown stop result is unsafe even if an adapter cannot
        # expose its worker thread.  Preserve an explicit live-thread probe
        # when the real executor provides one.
        trajectory["screenshot_collection_active"] = (
            bool(active) if active is not None else True
        )
        trajectory["screenshot_collection_error"] = message
        trajectory["control_handoff_safe"] = False
    return message


def _record_report_handoff_failure(
    report: Dict[str, Any], executor: Any, error: Any
) -> str:
    """Close a report as failed when final device-control cleanup is unsafe."""
    details: Dict[str, Any] = {}
    message = _mark_control_release_failure(executor, details, error)
    rendered = "device control cleanup failed: %s" % message
    for key in _CYCLE_HANDOFF_BOOL_FIELDS + _CYCLE_HANDOFF_ERROR_FIELDS:
        if key in details:
            report[key] = details[key]
    errors = report.setdefault("errors", [])
    if isinstance(errors, list) and rendered not in errors:
        errors.append(rendered)
    cycles = unique_cycle_rows(report)
    if cycles and isinstance(cycles[-1], dict):
        cycle_report = cycles[-1]
        _copy_cycle_handoff_fields(cycle_report, details, executor)
        cycle_errors = cycle_report.setdefault("errors", [])
        if isinstance(cycle_errors, list) and rendered not in cycle_errors:
            cycle_errors.append(rendered)
        cycle_report["abort_run"] = True
        cycle_report["continue_run"] = False
        cycle_report["recoverable_cycle"] = False
        cycle_report["abort_reason"] = rendered
        if cycle_report.get("status") == "passed":
            cycle_report["status"] = "failed"
    report["status"] = "failed"
    report["resume_state"] = "failed"
    report["last_error"] = rendered
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return rendered


def _cleanup_device_control(
    report: Dict[str, Any],
    executor: Optional[Any],
    mutex: Optional[Any],
    state_manager: Optional[Any],
    device_serial: str,
    *,
    publish_failure: bool = True,
) -> Optional[str]:
    """Best-effort final cleanup and durable failure publication.

    Cleanup runs after the normal cycle logic, so an exception here must never
    erase an already captured crash or replace its cycle row with an implicit
    success.  A false release result is treated exactly like an exception.
    """
    cleanup_errors: List[str] = []
    if executor is not None:
        try:
            executor.cleanup()
        except Exception as exc:
            cleanup_errors.append("executor cleanup failed: %s" % exc)
            logger.exception("Executor cleanup failed on %s", device_serial)

    holder = None
    if mutex is not None:
        try:
            holder_reader = getattr(mutex, "get_current_holder", None)
            holder = (
                holder_reader()
                if callable(holder_reader)
                else getattr(mutex, "current_holder", None)
            )
        except Exception as exc:
            cleanup_errors.append("unable to inspect device control: %s" % exc)
            logger.exception(
                "Unable to inspect device control during cleanup on %s",
                device_serial,
            )

    if (
        executor is not None
        and mutex is not None
        and state_manager is not None
        and holder == "fastbot"
    ):
        try:
            released = mutex.release("fastbot", state_manager.get_device())
            if released is False:
                cleanup_errors.append(
                    "Fastbot control release returned false during final cleanup"
                )
        except Exception as exc:
            cleanup_errors.append(
                "Fastbot control release failed during final cleanup: %s" % exc
            )
            logger.exception(
                "Unable to release Fastbot control after cleanup on %s",
                device_serial,
            )

    # Executor-side flags catch failures reported by collector/guard cleanup
    # even when no mutex holder remained by the time we inspected it.
    if executor is not None:
        if _cycle_hard_control_failure(executor, {}):
            cleanup_errors.append(
                str(
                    getattr(executor, "control_release_error", None)
                    or getattr(executor, "screenshot_collection_error", None)
                    or getattr(executor, "pressure_stop_error", None)
                    or "device control cleanup remained unsafe"
                )
            )
        elif _bool_attribute(executor, "pressure_stop_verified") is False:
            cleanup_errors.append(
                str(
                    getattr(executor, "pressure_stop_error", None)
                    or "device-side Fastbot stop verification failed during cleanup"
                )
            )

    if not cleanup_errors:
        return None
    message = "; ".join(dict.fromkeys(cleanup_errors))
    if publish_failure:
        return _record_report_handoff_failure(report, executor, message)
    # The caller may still have an unfinished cycle that is eligible for
    # recovery. Return the evidence without prematurely converting the whole
    # task into a terminal failure.
    return "device control cleanup failed: %s" % message


def _record_recoverable_cleanup_failure(
    report: Dict[str, Any], error: str, cycle: Optional[int] = None
) -> Dict[str, Any]:
    """Publish an unsafe handoff as a bounded recovery/manual gate.

    A cleanup error is not proof that the pressure budget failed. Keeping the
    run non-terminal lets the scheduler preserve the checkpoint and either
    retry after transport recovery or ask the operator to inspect the device.
    """
    text = str(error or "device control cleanup failed")
    decision = classify_recovery(text)
    if decision.get("state") in AUTOMATIC_RECOVERY_STATES:
        state = str(decision["state"])
        automatic = True
        recoverable = True
        category = str(decision.get("category") or "device_transport")
        reason = str(decision.get("reason") or text)
    else:
        current_state = effective_resume_state(report, None, default="")
        state = "paused" if current_state == "paused" else "needs_manual_resume"
        automatic = False
        recoverable = False
        category = "control_handoff"
        reason = text
    _record_resume_interruption(
        report,
        {
            "state": state,
            "recoverable": recoverable,
            "automatic": automatic,
            "category": category,
            "reason": reason,
        },
        text,
        cycle,
    )
    report["finished_at"] = None
    return {
        "state": state,
        "recoverable": recoverable,
        "automatic": automatic,
        "category": category,
        "reason": reason,
    }


def _copy_cycle_handoff_fields(
    cycle_report: Dict[str, Any],
    trajectory: Optional[Mapping[str, Any]],
    executor: Any = None,
) -> None:
    """Keep the device-control verdict on the durable cycle row.

    Exceptions can happen before the normal trajectory finalizer runs.  Copy
    both the phase-published values and any executor-side flags so a report
    never loses the reason a subsequent cycle was (or was not) allowed.
    """
    values = trajectory if isinstance(trajectory, Mapping) else {}
    for key in _CYCLE_HANDOFF_BOOL_FIELDS:
        if key in values and isinstance(values.get(key), bool):
            cycle_report[key] = bool(values[key])
            continue
        value = _bool_attribute(executor, key) if executor is not None else None
        if value is not None:
            cycle_report[key] = value
    for key in _CYCLE_HANDOFF_ERROR_FIELDS:
        if key in values and values.get(key) not in (None, ""):
            cycle_report[key] = str(values[key])
            continue
        if executor is not None:
            try:
                value = getattr(executor, key, None)
            except Exception:
                value = None
            if value not in (None, "") and not callable(value):
                cycle_report[key] = str(value)


def _merge_handoff_fields(
    target: Dict[str, Any], source: Optional[Mapping[str, Any]]
) -> None:
    """Copy explicit control-handoff markers between evidence containers."""
    if not isinstance(source, Mapping):
        return
    for key in _CYCLE_HANDOFF_BOOL_FIELDS:
        if isinstance(source.get(key), bool):
            target[key] = bool(source[key])
    for key in _CYCLE_HANDOFF_ERROR_FIELDS:
        if source.get(key) not in (None, ""):
            target[key] = str(source[key])


def _cycle_hard_control_failure(
    executor: Any,
    trajectory: Optional[Mapping[str, Any]] = None,
    error: Any = None,
) -> bool:
    """Return true when handing the device to another cycle is unsafe."""
    values = trajectory if isinstance(trajectory, Mapping) else {}
    for key in (
        "foreground_guard_stop_failed",
        "screenshot_collection_failed",
        "screenshot_collection_active",
        "uiautomator_release_failed",
        "control_release_failed",
        "mutex_release_failed",
    ):
        if values.get(key) is True or _bool_attribute(executor, key) is True:
            return True
    text_parts: List[str] = []
    if error is not None:
        text_parts.append(str(error))
    for key in _CYCLE_HANDOFF_ERROR_FIELDS + (
        "error",
        "errors",
        "abort_reason",
    ):
        value = values.get(key)
        if isinstance(value, (list, tuple)):
            text_parts.extend(str(item) for item in value)
        elif value not in (None, ""):
            text_parts.append(str(value))
    try:
        executor_error = getattr(executor, "pressure_stop_error", None)
    except Exception:
        executor_error = None
    if executor_error:
        text_parts.append(str(executor_error))
    normalized = " ".join(text_parts).lower()
    return any(marker in normalized for marker in _HARD_CONTROL_FAILURE_MARKERS)


def _cycle_control_handoff_safe(
    executor: Any, trajectory: Optional[Mapping[str, Any]] = None
) -> bool:
    """Return whether another cycle may safely take device control.

    A failed recovery is a cycle-level result only when the old pressure
    process is definitely gone and no controller/collector remains in an
    unsafe state.  Real ``TestExecutor`` instances must publish an explicit
    device-side stop verification.  Minimal legacy test adapters do not have
    that property, so a returned trajectory remains a compatibility fallback
    unless it contains an explicit unsafe marker.
    """
    values = trajectory if isinstance(trajectory, Mapping) else {}

    if values.get("control_handoff_safe") is False:
        return False
    if values.get("device_disconnect") or values.get("device_disconnected"):
        return False
    if str(values.get("stop_reason") or "").lower() == _WAIT_DEVICE_DISCONNECTED:
        return False
    if values.get("stall_recovery_abort_run") is True:
        return False

    # These are hard cleanup failures.  A foreground restore failure itself is
    # recoverable on the next cycle; a guard or screenshot thread that is still
    # alive is not.
    for name in (
        "foreground_guard_stop_failed",
        "screenshot_collection_failed",
        "screenshot_collection_active",
        "uiautomator_release_failed",
        "control_release_failed",
        "mutex_release_failed",
    ):
        if values.get(name) is True or _bool_attribute(executor, name) is True:
            return False

    # Preserve explicit evidence that a release/control transaction failed.
    error_parts: List[str] = []
    for key in (
        "pressure_stop_error",
        "foreground_restore_error",
        "foreground_recovery_error",
        "permission_recovery_error",
        "audit_recovery_error",
        "error",
        "errors",
    ):
        value = values.get(key)
        if isinstance(value, (list, tuple)):
            error_parts.extend(str(item) for item in value)
        elif value:
            error_parts.append(str(value))
    for key in ("pressure_stop_error", "foreground_restore_error"):
        try:
            value = getattr(executor, key, None)
        except Exception:
            value = None
        if value:
            error_parts.append(str(value))
    error_text = " ".join(error_parts).lower()
    if any(
        marker in error_text
        for marker in _HARD_CONTROL_FAILURE_MARKERS
    ):
        return False

    explicit_stop: Any = values.get("pressure_stop_verified", None)
    if not isinstance(explicit_stop, bool):
        explicit_stop = _bool_attribute(executor, "pressure_stop_verified")
    if explicit_stop is False:
        return False
    if explicit_stop is True:
        return True

    stop_reason = str(values.get("stop_reason") or "").lower()
    recovery_stop = stop_reason in {
        _WAIT_CRASH,
        _WAIT_PROCESS_EXIT,
        _WAIT_AUDIT_RECOVERY,
        _WAIT_AUDIT_RECOVERY_UNCLEAN,
        _WAIT_PERMISSION_RECOVERY,
        _WAIT_PERMISSION_RECOVERY_UNCLEAN,
        _WAIT_FOREGROUND_RECOVERY,
        _WAIT_STALL_RECOVERY,
        _WAIT_STALL_RECOVERY_UNCLEAN,
        _WAIT_PRESSURE_ENGINE_EXIT,
    }
    # A real executor cannot safely infer that a remote process is gone from a
    # host return code.  Legacy adapters are allowed to use their returned
    # trajectory for backwards compatibility with the original test harness.
    if isinstance(executor, TestExecutor):
        return False
    return bool(values) and (recovery_stop or values.get("stop_reason") is not None)


def _verify_cycle_pressure_handoff(
    executor: Any, trajectory: Dict[str, Any], *, force: bool = False
) -> Optional[bool]:
    """Populate a cycle failure with an explicit remote-stop verdict.

    Preparation and Fastbot startup can fail before ``_run_fastbot_phase`` has
    a trajectory to return.  A real executor can still make that state
    deterministic by probing and clearing any device-side Fastbot process.  A
    positive result is deliberately published into the trajectory so the
    scheduler may record this cycle as failed and begin the next one; an
    unknown/negative result remains fail-closed.
    """
    if not isinstance(trajectory, dict):
        return None
    verified = _bool_attribute(executor, "pressure_stop_verified")
    verifier = getattr(executor, "verify_pressure_engine_stopped", None)
    if callable(verifier) and (force or verified is None):
        try:
            verified = bool(verifier())
        except Exception as exc:
            verified = False
            trajectory["pressure_stop_verification_error"] = str(exc)
            logger.warning(
                "Unable to verify Fastbot cleanup after cycle failure",
                exc_info=True,
            )
    if verified is not None:
        trajectory["pressure_stop_verified"] = bool(verified)
        if (
            verified
            and trajectory.get("control_handoff_safe") is not False
            and not _cycle_hard_control_failure(executor, trajectory)
        ):
            trajectory["control_handoff_safe"] = True
        elif not verified:
            trajectory["control_handoff_safe"] = False
    return verified


def _is_preparation_failure(cycle_report: Mapping[str, Any]) -> bool:
    """Return whether a cycle failed before any pressure event was injected.

    Preparation is flaky on vendor ROMs: a full-screen ad or a system prompt can
    hide the startup gate, while the same cold start usually works. Such a
    failure produced no pressure evidence, so it is safe to retry the cycle from
    a clean reset instead of ending the device run.
    """
    if str(cycle_report.get("status") or "") != "failed":
        return False
    errors = cycle_report.get("errors") or []
    if not isinstance(errors, (list, tuple)):
        errors = [errors]
    text = " ".join(str(item) for item in errors).lower()
    markers = (
        "preparation scenario failed",
        "startup gate failed",
        "audit gate",
        "审核",
        "input recovery",
    )
    if not any(marker in text for marker in markers):
        return False
    try:
        elapsed = float(cycle_report.get("pressure_elapsed_seconds", 0) or 0)
        operations = int(cycle_report.get("operation_count", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return elapsed <= 0.0 and operations == 0


def _mark_cycle_failure(
    cycle_report: Dict[str, Any],
    executor: Any,
    trajectory: Optional[Mapping[str, Any]],
    message: str,
    *,
    abort_reason: Optional[str] = None,
) -> bool:
    """Record a failed cycle and return whether the next cycle is allowed."""
    cycle_report["status"] = "failed"
    _copy_cycle_handoff_fields(cycle_report, trajectory, executor)
    errors = cycle_report.setdefault("errors", [])
    if isinstance(errors, list) and message not in errors:
        errors.append(message)
    safe = _cycle_control_handoff_safe(executor, trajectory)
    cycle_report["recoverable"] = False
    cycle_report["recoverable_cycle"] = bool(safe)
    cycle_report["continue_run"] = bool(safe)
    if safe:
        cycle_report.pop("abort_run", None)
        cycle_report.pop("abort_reason", None)
    else:
        cycle_report["abort_run"] = True
        cycle_report["abort_reason"] = abort_reason or (
            "aborting remaining cycles because device control could not be safely handed off"
        )
    return safe


def _prepare_resumed_cycle(
    state_manager: StateManager,
    mutex: MutexController,
    config: Mapping[str, Any],
    cancel_event: Any = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Re-establish a partial cycle without reinstalling or clearing its data."""
    _check_cancel(cancel_event)
    device = state_manager.get_device()
    acquired = False
    component: Optional[str] = None
    primary_error: Optional[BaseException] = None
    try:
        if not mutex.acquire("uiautomator2", device):
            raise RuntimeError(
                "uiautomator2 could not acquire exclusive device control for resume"
            )
        acquired = True
        component = _wait_for_target_app_foreground(state_manager, timeout=3.0)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if acquired:
            release_error: Optional[BaseException] = None
            try:
                if mutex.release("uiautomator2", device) is False:
                    release_error = RuntimeError(
                        "uiautomator2 control was not released during cycle resume"
                    )
            except BaseException as exc:
                release_error = exc
            if release_error is not None:
                if primary_error is None:
                    raise RuntimeError(
                        "uiautomator2 control was not released during cycle resume"
                    ) from release_error
                try:
                    setattr(primary_error, "_control_release_error", str(release_error))
                except Exception:
                    pass
                logger.exception(
                    "Unable to release uiautomator2 after resume foreground probe",
                    exc_info=(
                        type(release_error),
                        release_error,
                        release_error.__traceback__,
                    ),
                )

    recovery: Dict[str, Any] = {
        "attempted": False,
        "success": bool(component),
        "data_cleared": False,
        "reinstalled": False,
        "foreground_component": component,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if component is None:
        recovery = _recover_after_runtime_failure(
            state_manager,
            mutex,
            config,
            _WAIT_FOREGROUND_RECOVERY,
            cancel_event,
        )
        if not recovery.get("success"):
            raise RuntimeError(
                "partial-cycle foreground recovery failed: %s"
                % str(
                    recovery.get("error")
                    or "target app did not return to foreground"
                )
            )

    snapshot: Optional[Dict[str, Any]] = None
    acquired = False
    primary_error = None
    try:
        if not mutex.acquire("uiautomator2", device):
            raise RuntimeError(
                "uiautomator2 control unavailable after partial-cycle recovery"
            )
        acquired = True
        snapshot_value = state_manager.save_snapshot("resumed_cycle")
        if isinstance(snapshot_value, Mapping) and not snapshot_value.get("error"):
            snapshot = dict(snapshot_value)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if acquired:
            release_error = None
            try:
                if mutex.release("uiautomator2", device) is False:
                    release_error = RuntimeError(
                        "uiautomator2 control was not released after resume snapshot"
                    )
            except BaseException as exc:
                release_error = exc
            if release_error is not None:
                if primary_error is None:
                    raise RuntimeError(
                        "uiautomator2 control was not released after resume snapshot"
                    ) from release_error
                try:
                    setattr(primary_error, "_control_release_error", str(release_error))
                except Exception:
                    pass
                logger.exception(
                    "Unable to release uiautomator2 after resume snapshot failure",
                    exc_info=(
                        type(release_error),
                        release_error,
                        release_error.__traceback__,
                    ),
                )
    recovery["finished_at"] = datetime.now(timezone.utc).isoformat()
    return recovery, snapshot


def _run_cycle(
    state_manager: StateManager,
    executor: TestExecutor,
    crash_handler: CrashHandler,
    mutex: MutexController,
    config: Mapping[str, Any],
    report: Dict[str, Any],
    report_dir: Path,
    cycle: int,
    cancel_event: Any = None,
    *,
    resume_context: Optional[Mapping[str, Any]] = None,
    checkpoint: Optional[CheckpointHeartbeat] = None,
    resume_requested: bool = False,
) -> Dict[str, Any]:
    state_manager._input_cycle = cycle
    configure_input = getattr(state_manager, "configure_input_safety", None)
    if callable(configure_input):
        configure_input(dict(config))
    # Keep cycle-level outcome classification on the same configuration path as
    # ``_run_fastbot_phase``.  The foreground-recovery branch below needs the
    # configured pressure budget to decide whether a deferred handoff reached
    # its deadline.  Previously this local was only initialized in the phase
    # runner, so a normal foreground recovery raised ``NameError`` and was
    # incorrectly counted as an automatic-resume failure.
    test_options = config.get("test", {})
    if not isinstance(test_options, Mapping):
        test_options = {}
    resume_context = resume_context if isinstance(resume_context, Mapping) else {}
    resume_requested = bool(
        resume_requested or resume_context.get("resume_requested")
    )
    try:
        resumed_elapsed = max(
            0.0, float(resume_context.get("elapsed_seconds", 0) or 0)
        )
    except (TypeError, ValueError):
        resumed_elapsed = 0.0
    try:
        resumed_operations = max(
            0, int(resume_context.get("operation_count", 0) or 0)
        )
    except (TypeError, ValueError):
        resumed_operations = 0
    resume_options = _resume_options(config)
    try:
        resumed_phase = max(0, int(resume_context.get("phase", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        resumed_phase = 0
    resumed_pressure_phases = resume_context.get("pressure_phases", [])
    has_resume_progress = bool(
        resumed_elapsed > 0
        or resumed_operations > 0
        or resumed_phase > 0
        or (
            isinstance(resumed_pressure_phases, list)
            and any(isinstance(item, Mapping) for item in resumed_pressure_phases)
        )
    )
    # The safe default is to preserve the app/data state for a partial-cycle
    # continuation.  An explicit opt-in can request a clean relaunch while
    # still consuming only the cycle's remaining pressure budget.
    clear_data_on_resume = bool(
        resume_requested and resume_options.get("clear_app_data_on_resume")
    )
    preserve_resume_state = bool(resume_requested and not clear_data_on_resume)
    cycle_report: Dict[str, Any] = {
        "cycle": cycle,
        "started_at": resume_context.get("cycle_started_at")
        or datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "foreground_recovery_strategy": _foreground_recovery_strategy(state_manager),
        "operation_count": resumed_operations,
        "resume_requested": resume_requested,
        "resumed_from_seconds": round(resumed_elapsed, 3),
        "resumed_from_operations": resumed_operations,
        "resumed_from_phase": resumed_phase,
        "artifacts": {},
        "errors": [],
    }
    # Resolve the device inside the guarded body so a transport failure still
    # produces a durable failed-cycle row instead of aborting the whole worker.
    device = None
    snapshot: Optional[Dict[str, Any]] = None
    trajectory: Dict[str, Any] = {}
    log_content = ""
    pressure_log_content = ""
    artifacts: Dict[str, Any] = {}
    phase_attempted = False

    # The executor survives across cycles for efficiency.  Clear all
    # cycle-scoped runtime markers before any reset/audit work can fail.
    begin_cycle = getattr(executor, "begin_cycle", None)
    if callable(begin_cycle):
        try:
            begin_cycle(cycle)
        except TypeError as exc:
            # Compatibility with adapters exposing a no-argument lifecycle
            # hook; the built-in executor accepts the cycle number.
            if "argument" not in str(exc).lower() and "keyword" not in str(exc).lower():
                raise
            begin_cycle()

    def recover_exception_artifacts(
        error: BaseException,
    ) -> Optional[Dict[str, Any]]:
        """Populate the cycle row from files left by a failed phase."""
        nonlocal trajectory, log_content, pressure_log_content, artifacts
        crash_info: Optional[Dict[str, Any]] = None
        try:
            recovered, log_value, pressure_value, recovered_artifacts = (
                _phase_failure_snapshot(
                    error,
                    state_manager=state_manager,
                    executor=executor,
                    report_dir=report_dir,
                    cycle=cycle,
                    trajectory=trajectory,
                    artifacts=artifacts,
                    phase_attempted=phase_attempted,
                )
            )
            trajectory = recovered
            log_content = log_value
            pressure_log_content = pressure_value
            artifacts.update(recovered_artifacts)
            cycle_report.setdefault("artifacts", {}).update(recovered_artifacts)
            cycle_report["operation_count"] = max(
                int(cycle_report.get("operation_count", 0) or 0),
                int(trajectory.get("operation_count", 0) or 0),
            )
            phase_details = trajectory.get("phase", {})
            if isinstance(phase_details, Mapping):
                cycle_report["pressure_elapsed_seconds"] = float(
                    trajectory.get("pressure_elapsed_seconds", 0)
                    or phase_details.get("engine_elapsed_seconds", 0)
                    or 0
                )
                cycle_report["pressure_phases"] = list(
                    trajectory.get("pressure_phases", [])
                ) or [dict(phase_details)]
        except Exception as snapshot_error:
            trajectory.setdefault("artifact_errors", []).append(
                "phase failure snapshot: %s" % snapshot_error
            )
        try:
            report.setdefault("crashes", [])
            crash_info, _page_evidence = _record_failure_crash_evidence(
                report,
                cycle_report,
                state_manager=state_manager,
                executor=executor,
                mutex=mutex,
                crash_handler=crash_handler,
                report_dir=report_dir,
                cycle=cycle,
                trajectory=trajectory,
                log_content=log_content,
                config=config,
            )
        except Exception as crash_error:
            trajectory.setdefault("artifact_errors", []).append(
                "failure crash detection: %s" % crash_error
            )
        cycle_report.setdefault("artifacts", {}).update(artifacts)
        _copy_cycle_handoff_fields(cycle_report, trajectory, executor)
        return crash_info

    try:
        _check_cancel(cancel_event)
        device = state_manager.get_device()
        if preserve_resume_state and has_resume_progress:
            resume_preparation, snapshot = _prepare_resumed_cycle(
                state_manager, mutex, config, cancel_event
            )
            cycle_report["resume"] = resume_preparation
        elif preserve_resume_state:
            # The checkpoint may have been written before the first pressure
            # event. Re-establish the app with a force-stop/relaunch while
            # preserving its data, then run the normal scenario preparation.
            cycle_report["resume"] = {
                "attempted": True,
                "success": True,
                "data_cleared": False,
                "reinstalled": False,
                "reason": "resume checkpoint had no pressure progress",
            }
        if not mutex.acquire("uiautomator2", device):
            raise RuntimeError("uiautomator2 could not acquire exclusive device control")
        preparation_error: Optional[BaseException] = None
        audit_prepared = False
        try:
            scenario_config = config.get("scenario", {})
            stop_packages = (
                scenario_config.get("stop_packages", [])
                if isinstance(scenario_config, Mapping)
                else []
            )
            if preserve_resume_state and has_resume_progress:
                reset_ok, reset_details = True, {
                    "attempted": False,
                    "success": True,
                    "skipped": True,
                    "reason": "partial-cycle resume preserves application data",
                    "clear_data": False,
                }
            else:
                reset_ok, reset_details = _reset_app_for_cycle(
                    state_manager,
                    config,
                    stop_packages,
                    clear_data_override=(
                        True
                        if clear_data_on_resume
                        else False
                        if resume_requested
                        else None
                    ),
                )
                if clear_data_on_resume:
                    reset_details["resume_clean_reset"] = True
                elif resume_requested:
                    reset_details["resume_relaunch"] = True
            cycle_report["reset"] = reset_details
            if not reset_ok:
                reset_error = str(reset_details.get("error") or "").strip()
                raise RuntimeError(
                    "application reset failed%s"
                    % (": %s" % reset_error if reset_error else "")
                )
            # ``reset_app`` may reconnect uiautomator2 while recovering from a
            # short transport/session failure. Do not keep passing the object
            # captured before reset into the gate probe or the final release.
            device = state_manager.get_device()
            # Capture the audit controls while uiautomator2 still owns the
            # device.  Custom Fastbot keeps the UiAutomation registration
            # even when SIGSTOP'd, so the runtime guard may need these bounds
            # as a safe fallback when the page reappears mid-pressure.
            audit_preparer = getattr(executor, "prepare_audit_gate", None)
            if (resumed_elapsed <= 0 or clear_data_on_resume) and callable(
                audit_preparer
            ):
                audit_ready, device = _prepare_audit_gate_with_retry(
                    state_manager,
                    executor,
                    device,
                    timeout=20.0,
                )
                if not audit_ready:
                    raise RuntimeError(
                        "审核页面未能在场景准备前完成，已阻止后续随机点击"
                    )
                audit_prepared = True
            if preserve_resume_state and has_resume_progress:
                scenario = {
                    "resumed": True,
                    "login_success": True,
                    "navigation_success": True,
                    "data_cleared": False,
                    "reinstalled": False,
                    "resume": dict(resume_preparation),
                }
            else:
                preparation_config = dict(config)
                if audit_prepared:
                    # The executor has already completed and verified the
                    # startup gate while it owned the device.  Running the
                    # same audit action again through a possibly stale
                    # uiautomator2 session can block on SplashActivity even
                    # though the gate is gone.  Remove only the duplicate
                    # gate/confirmation steps; all other navigation remains
                    # unchanged.
                    preparation_config, skipped_audit_steps = (
                        _config_after_completed_audit_gate(
                            config, str(getattr(state_manager, "device_serial", ""))
                        )
                    )
                    if skipped_audit_steps:
                        cycle_report["preparation_audit_steps_skipped"] = (
                            skipped_audit_steps
                        )
                state_manager._input_cycle = cycle
                scenario = prepare_test_scenario(state_manager, preparation_config)
                if resume_requested:
                    scenario["resumed"] = True
                    scenario["data_cleared"] = bool(clear_data_on_resume)
                    scenario["reinstalled"] = False
            cycle_report["preparation"] = scenario
            if not scenario.get("login_success", True) or not scenario.get(
                "navigation_success", True
            ):
                preparation_error = str(
                    scenario.get("navigation_error")
                    or scenario.get("login_error")
                    or ""
                ).strip()
                raise RuntimeError(
                    "preparation scenario failed%s"
                    % (": %s" % preparation_error if preparation_error else "")
                )
            if not (preserve_resume_state and has_resume_progress):
                snapshot = state_manager.save_snapshot("initial_cycle_%03d" % cycle)
                if snapshot.get("error"):
                    raise RuntimeError("snapshot failed: %s" % snapshot["error"])
        except BaseException as exc:
            # Preserve the preparation failure as the primary error while the
            # finally block below records any independent handoff failure.
            preparation_error = exc
            raise
        finally:
            release_error: Optional[BaseException] = None
            try:
                released = mutex.release("uiautomator2", device)
                if released is False:
                    release_error = RuntimeError(
                        "uiautomator2 control release returned false during cycle preparation"
                    )
            except BaseException as exc:
                release_error = exc
            if release_error is not None:
                # A failed release means the next owner cannot be trusted. The
                # marker is consumed by _cycle_control_handoff_safe even when
                # the preparation operation itself also failed.
                _mark_control_release_failure(
                    executor,
                    trajectory,
                    "uiautomator2 control release failed during cycle preparation: %s"
                    % release_error,
                )
                release_message = (
                    "uiautomator2 control release failed during cycle preparation: %s"
                    % release_error
                )
                if release_message not in cycle_report["errors"]:
                    cycle_report["errors"].append(release_message)
                if preparation_error is None:
                    raise RuntimeError(release_message) from release_error
                logger.exception(
                    "Unable to release uiautomator2 control after preparation failure",
                    exc_info=(
                        type(release_error),
                        release_error,
                        release_error.__traceback__,
                    ),
                )

        phase_attempted = True
        if resume_context or checkpoint is not None:
            trajectory, log_content, artifacts = _run_fastbot_phase(
                state_manager,
                executor,
                mutex,
                config,
                report_dir,
                cycle,
                cancel_event,
                resume_context=resume_context,
                checkpoint=checkpoint,
            )
        else:
            trajectory, log_content, artifacts = _run_fastbot_phase(
                state_manager,
                executor,
                mutex,
                config,
                report_dir,
                cycle,
                cancel_event,
            )
        runtime_failures = [
            dict(item)
            for item in trajectory.get("runtime_failures", [])
            if isinstance(item, Mapping)
        ]
        page_detection = trajectory.get("app_crash_page")
        logcat_crash_detected = crash_handler.detect_crash(log_content) is not None
        page_evidence: Optional[Dict[str, Any]] = None
        if isinstance(page_detection, Mapping) or (
            not runtime_failures
            and (trajectory.get("stopped_on_crash") or logcat_crash_detected)
        ):
            page_evidence = _capture_app_crash_page_evidence(
                state_manager,
                mutex,
                crash_handler,
                report_dir,
                cycle,
                page_detection if isinstance(page_detection, Mapping) else None,
                executor=executor,
            )
            if page_evidence is not None:
                _merge_handoff_fields(trajectory, page_evidence)
                trajectory["app_crash_page"] = dict(page_evidence)
        trajectory.setdefault(
            "engine", getattr(executor, "pressure_engine", "fastbot")
        )
        if _cycle_hard_control_failure(executor, trajectory):
            trajectory.setdefault(
                "stall_recovery_skipped_reason",
                "skipped because device-control handoff is unsafe",
            )
        else:
            _invoke_stall_recovery(
                trajectory,
                state_manager,
                mutex,
                config,
                cancel_event,
                executor=executor,
            )
        trajectory_path = _write_trajectory(trajectory, report_dir, cycle)
        cycle_report["artifacts"] = dict(artifacts)
        cycle_report["artifacts"]["trajectory"] = trajectory_path
        if page_evidence is not None:
            cycle_report["app_crash_page"] = dict(page_evidence)
            cycle_report["artifacts"].update(page_evidence.get("artifacts", {}))
        report.setdefault("artifacts", {}).update(artifacts)
        cycle_report["operation_count"] = max(
            0, int(trajectory.get("operation_count", 0) or 0)
        )
        cycle_report["pressure_phases"] = list(
            trajectory.get("pressure_phases", [])
        )
        cycle_report["pressure_elapsed_seconds"] = float(
            trajectory.get("pressure_elapsed_seconds", 0) or 0
        )
        if runtime_failures:
            cycle_report["runtime_failures"] = runtime_failures
            cycle_report["runtime_failure_count"] = len(runtime_failures)
            cycle_report["runtime_recovery_count"] = int(
                trajectory.get("runtime_recovery_count", 0) or 0
            )
        cycle_report["pressure_engine_failure_count"] = int(
            trajectory.get("pressure_engine_failure_count", 0) or 0
        )
        cycle_report["pressure_engine_recovery_count"] = int(
            trajectory.get("pressure_engine_recovery_count", 0) or 0
        )
        if trajectory.get("pressure_engine_failures"):
            cycle_report["pressure_engine_failures"] = list(
                trajectory["pressure_engine_failures"]
            )
        cycle_report["engine"] = trajectory.get("engine", "fastbot")
        cycle_report["foreground_restore_count"] = int(
            trajectory.get("foreground_restore_count", 0) or 0
        )
        cycle_report["permission_dialog_count"] = int(
            trajectory.get("permission_dialog_count", 0) or 0
        )
        cycle_report["permission_recovery_count"] = int(
            trajectory.get("permission_recovery_count", 0) or 0
        )
        cycle_report["permission_recovery_attempt_count"] = int(
            trajectory.get("permission_recovery_attempt_count", 0) or 0
        )
        cycle_report["permission_recovery_success_count"] = int(
            trajectory.get("permission_recovery_success_count", 0) or 0
        )
        cycle_report["permission_recovery_failure_count"] = int(
            trajectory.get("permission_recovery_failure_count", 0) or 0
        )
        if trajectory.get("permission_recoveries"):
            cycle_report["permission_recoveries"] = list(
                trajectory["permission_recoveries"]
            )
        if trajectory.get("permission_recovery_failed"):
            cycle_report["permission_recovery_failed"] = True
            cycle_report["permission_recovery_error"] = str(
                trajectory.get("permission_recovery_error")
                or "permission dialog could not be handled"
            )
        cycle_report["audit_recovery_count"] = int(
            trajectory.get("audit_recovery_count", 0) or 0
        )
        cycle_report["foreground_recovery_count"] = int(
            trajectory.get("foreground_recovery_count", 0) or 0
        )
        if trajectory.get("foreground_recoveries"):
            cycle_report["foreground_recoveries"] = list(
                trajectory["foreground_recoveries"]
            )
        if trajectory.get("foreground_restore_error"):
            cycle_report["foreground_restore_error"] = str(
                trajectory["foreground_restore_error"]
            )
        cycle_report["stall_detection_count"] = int(
            trajectory.get("stall_detection_count", 0) or 0
        )
        cycle_report["stall_recovery_requested_count"] = int(
            trajectory.get("stall_recovery_requested_count", 0) or 0
        )
        cycle_report["stall_recovery_count"] = int(
            trajectory.get("stall_recovery_count", 0) or 0
        )
        cycle_report["stall_recovery_skipped_count"] = int(
            trajectory.get("stall_recovery_skipped_count", 0) or 0
        )
        cycle_report["stall_recovery_failure_count"] = int(
            trajectory.get("stall_recovery_failure_count", 0) or 0
        )
        if trajectory.get("stall_events"):
            cycle_report["stall_events"] = list(trajectory["stall_events"])
        if isinstance(trajectory.get("stall_recovery"), Mapping):
            cycle_report["stall_recovery"] = dict(trajectory["stall_recovery"])
        if trajectory.get("stall_recovery_abort_run"):
            cycle_report["abort_run"] = True
        if trajectory.get("collection_warnings"):
            cycle_report["collection_warnings"] = list(
                trajectory["collection_warnings"]
            )
        if trajectory.get("coverage_rate") is not None:
            cycle_report["coverage_rate"] = trajectory["coverage_rate"]
        if trajectory.get("stop_reason"):
            cycle_report["stop_reason"] = str(trajectory["stop_reason"])
        _copy_cycle_handoff_fields(cycle_report, trajectory, executor)

        crash_candidates: List[Dict[str, Any]] = []
        for failure in runtime_failures:
            candidate = failure.get("crash")
            item = (
                dict(candidate)
                if _is_confirmed_app_crash(candidate)
                else {}
            )
            if item:
                item["phase"] = failure.get("phase")
                item["runtime_failure"] = failure
                crash_candidates.append(item)
        if not crash_candidates:
            fallback_crash = _detect_cycle_crash(
                crash_handler,
                log_content,
                page_detection if isinstance(page_detection, Mapping) else None,
                page_evidence,
                trajectory.get("app_process_exit")
                if isinstance(trajectory.get("app_process_exit"), Mapping)
                else None,
                trajectory.get("app_crash")
                if isinstance(trajectory.get("app_crash"), Mapping)
                else None,
            )
            if fallback_crash is not None:
                # Crash handlers are supplied by adapters and test doubles;
                # only a mapping is a durable crash record. Treat an
                # indeterminate value as no crash evidence instead of letting
                # ``dict(Mock())`` abort otherwise valid cycle completion.
                if isinstance(fallback_crash, Mapping):
                    crash_candidates.append(dict(fallback_crash))

        crash_info: Optional[Dict[str, Any]] = None
        replay_options = (
            config.get("crash_replay", {})
            if isinstance(config.get("crash_replay", {}), Mapping)
            else {}
        )
        crash_signatures: List[str] = []
        for candidate_index, candidate in enumerate(crash_candidates):
            crash_info = dict(candidate)
            crash_info["cycle"] = cycle
            phase_value = crash_info.get("phase")
            runtime_failure = crash_info.get("runtime_failure")
            if phase_value is None and isinstance(runtime_failure, Mapping):
                phase_value = runtime_failure.get("phase")
            candidate_trajectory = build_replay_trajectory(
                trajectory,
                replay_options,
                package_name=state_manager.package_name,
                pressure_log_path=report_dir / "fastbot.log",
                cycle=cycle,
                phase=phase_value,
                crash_at=crash_info.get("detected_at"),
            )
            exact_trajectory_path = None
            if isinstance(runtime_failure, Mapping):
                evidence = runtime_failure.get("evidence", {})
                if isinstance(evidence, Mapping):
                    evidence_artifacts = evidence.get("artifacts", {})
                    if isinstance(evidence_artifacts, Mapping):
                        value = evidence_artifacts.get("trajectory")
                        if value and Path(str(value)).is_file():
                            exact_trajectory_path = str(value)
            if exact_trajectory_path is None:
                exact_trajectory_path = _write_crash_trajectory(
                    trajectory,
                    report_dir,
                    cycle,
                    phase_value,
                    candidate_index + 1,
                    replay_options,
                    crash_info.get("detected_at"),
                )
            crash_info["phase"] = phase_value
            crash_info["trajectory_path"] = exact_trajectory_path
            if isinstance(runtime_failure, dict):
                runtime_failure["trajectory_path"] = exact_trajectory_path
            is_last = candidate_index == len(crash_candidates) - 1
            if is_last:
                # Keep the historical alias, but point it at the same short,
                # crash-specific plan used by automatic and manual replay.
                crash_trajectory_path = report_dir / "crash_trajectory.json"
                ReportGenerator.write_json(candidate_trajectory, crash_trajectory_path)
                cycle_report["artifacts"]["crash_trajectory"] = str(
                    crash_trajectory_path
                )
                report.setdefault("artifacts", {})["crash_trajectory"] = str(
                    crash_trajectory_path
                )
            if trajectory.get("stop_reason") == "manual":
                replay_ok, replay_results = False, []
                crash_info["replay_note"] = (
                    "Replay skipped because a manual stop was requested"
                )
            elif not is_last and len(crash_candidates) > 1:
                replay_ok, replay_results = False, []
                crash_info["replay_note"] = (
                    "Replay skipped for an earlier same-cycle failure"
                )
            elif snapshot is not None:
                replay_ok, replay_results = _replay_crash(
                    state_manager,
                    crash_handler,
                    mutex,
                    device,
                    snapshot,
                    candidate_trajectory,
                    crash_info,
                    int(config["test"].get("crash_replay_attempts", 0)),
                    cancel_event,
                    executor=executor,
                )
            else:
                replay_ok, replay_results = False, []
            crash_info["reproducible"] = replay_ok
            crash_info["reproduction_match"] = next(
                (
                    str(item.get("reproduction_match"))
                    for item in replay_results
                    if isinstance(item, Mapping) and item.get("reproducible")
                ),
                "none",
            )
            crash_info["replay_attempts"] = replay_results
            _add_crash(report, crash_info, cycle)
            if crash_info.get("signature"):
                crash_signatures.append(str(crash_info["signature"]))
        if crash_signatures:
            cycle_report["crash_signature"] = crash_signatures[-1]
            cycle_report["crash_signatures"] = crash_signatures

        runtime_crash_failure = any(
            _is_confirmed_app_crash(item.get("crash"))
            or _is_confirmed_app_crash(item.get("app_crash_page"))
            for item in runtime_failures
            if isinstance(item, Mapping)
        )
        crash_evidence_present = bool(
            _is_confirmed_app_crash(crash_info)
            or runtime_crash_failure
            or _is_confirmed_app_crash(trajectory.get("app_crash"))
            or _is_confirmed_app_crash(trajectory.get("app_crash_page"))
        )

        if trajectory.get("stop_reason") in {"manual", _WAIT_MANUAL_STOP}:
            cycle_report["status"] = "interrupted"
            cycle_report["errors"].append("pressure stopped by manual stop command")
        elif trajectory.get("stop_reason") == _WAIT_DEVICE_DISCONNECTED or any(
            isinstance(item.get("device_disconnect"), Mapping)
            for item in runtime_failures
        ):
            cycle_report["status"] = "failed"
            cycle_report["errors"].append(
                "device disconnected during pressure testing"
            )
            disconnect = trajectory.get("device_disconnect")
            if isinstance(disconnect, Mapping):
                cycle_report["device_disconnect"] = dict(disconnect)
            cycle_report["recoverable"] = False
            cycle_report["recoverable_cycle"] = False
            cycle_report["continue_run"] = False
            cycle_report["abort_run"] = True
            cycle_report["abort_reason"] = (
                "aborting remaining cycles because the device disconnected"
            )
        elif crash_evidence_present:
            # Crash evidence is the primary cycle outcome.  Foreground/UI
            # recovery flags may accompany the same event (especially when a
            # vendor launcher replaces the crashed activity), but must never
            # rewrite a recorded crash as a generic failed or passed cycle.
            cycle_report["status"] = "crash"
            if trajectory.get("foreground_recovery_failed"):
                message = str(
                    trajectory.get("foreground_recovery_error")
                    or trajectory.get("foreground_restore_error")
                    or "target app foreground recovery failed after crash"
                )
                if message not in cycle_report["errors"]:
                    cycle_report["errors"].append(
                        "foreground recovery after crash: %s" % message
                    )
            if trajectory.get("runtime_recovery_failed"):
                message = str(
                    trajectory.get("runtime_recovery_error")
                    or "target application runtime recovery failed after crash"
                )
                if message not in cycle_report["errors"]:
                    cycle_report["errors"].append(message)
            if _cycle_hard_control_failure(executor, trajectory):
                handoff_error = str(
                    trajectory.get("control_release_error")
                    or trajectory.get("screenshot_collection_error")
                    or trajectory.get("pressure_stop_error")
                    or "device control handoff failed after crash"
                )
                cycle_report["errors"].append(
                    "device control handoff after crash: %s" % handoff_error
                )
                cycle_report["recoverable"] = False
                cycle_report["recoverable_cycle"] = False
                cycle_report["continue_run"] = False
                cycle_report["abort_run"] = True
                cycle_report["abort_reason"] = (
                    "aborting remaining cycles because device control could not "
                    "be safely handed off after crash"
                )
        elif runtime_failures:
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "target application process exited without confirmed app-crash evidence",
                abort_reason=(
                    "aborting remaining cycles because target process recovery "
                    "could not be completed"
                ),
            )
        elif _cycle_hard_control_failure(executor, trajectory):
            handoff_error = str(
                trajectory.get("control_release_error")
                or trajectory.get("screenshot_collection_error")
                or trajectory.get("pressure_stop_error")
                or "a pressure collector or device-control owner did not stop cleanly"
            )
            synthetic_error = RuntimeError(handoff_error)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "device control handoff failed: %s" % handoff_error,
                abort_reason=_abort_remaining_cycles_message(
                    synthetic_error, executor
                ),
            )
        elif trajectory.get("audit_recovery_failed"):
            audit_error = str(
                trajectory.get("audit_recovery_error") or "unknown recovery error"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "startup-gate recovery failed: %s" % audit_error,
                abort_reason=(
                "aborting remaining cycles because the startup audit gate "
                "could not be recovered: %s" % audit_error
                ),
            )
        elif trajectory.get("permission_recovery_failed"):
            permission_error = str(
                trajectory.get("permission_recovery_error")
                or "permission dialog could not be handled"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "runtime permission recovery failed: %s" % permission_error,
                abort_reason=(
                    "aborting remaining cycles because runtime permission recovery "
                    "could not complete: %s" % permission_error
                ),
            )
        elif trajectory.get("foreground_recovery_failed"):
            foreground_error = str(
                trajectory.get("foreground_recovery_error")
                or trajectory.get("foreground_restore_error")
                or "target app could not be restored after foreground loss"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "foreground recovery failed: %s" % foreground_error,
                abort_reason=(
                    "aborting remaining cycles because the foreground/ADB handoff "
                    "could not restore the target app: %s" % foreground_error
                ),
            )
        elif trajectory.get("pressure_engine_recovery_failed"):
            engine_error = str(
                trajectory.get("pressure_engine_recovery_error")
                or "Fastbot pressure engine could not be restarted"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "pressure engine recovery failed: %s" % engine_error,
                abort_reason=(
                    "aborting remaining cycles because the pressure engine could "
                    "not be recovered: %s" % engine_error
                ),
            )
        elif trajectory.get("runtime_recovery_failed"):
            recovery_error = str(
                trajectory.get("runtime_recovery_error")
                or "target application runtime recovery failed"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "runtime recovery failed: %s" % recovery_error,
                abort_reason=(
                    "aborting remaining cycles because runtime recovery could not "
                    "safely hand off device control: %s" % recovery_error
                ),
            )
        elif (
            trajectory.get("stop_reason")
            in {_WAIT_STALL_RECOVERY, _WAIT_STALL_RECOVERY_UNCLEAN}
            and not bool(
                isinstance(trajectory.get("stall_recovery"), Mapping)
                and trajectory["stall_recovery"].get("success")
            )
        ):
            recovery = trajectory.get("stall_recovery", {})
            error = (
                str(recovery.get("error"))
                if isinstance(recovery, Mapping) and recovery.get("error")
                else "unknown recovery error"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "stall recovery failed: %s" % error,
                abort_reason=(
                    "aborting remaining cycles because stall recovery could not "
                    "safely hand off device control: %s" % error
                ),
            )
        elif str(trajectory.get("stop_reason") or "") == _WAIT_FOREGROUND_RECOVERY:
            # A deferred foreground handoff is not a successful pressure
            # completion unless the engine explicitly reached its deadline and
            # the device-control handoff is safe.  This prevents a late
            # launcher transition from silently producing a green cycle.
            pressure_elapsed = max(
                0.0, float(trajectory.get("pressure_elapsed_seconds", 0) or 0)
            )
            expected_pressure = max(
                1.0,
                float(test_options.get("fastbot_duration_minutes", 1) or 1)
                * 60.0,
            )
            stop_intent = str(
                trajectory.get("pressure_stop_intent") or ""
            ).strip().lower()
            deadline_intent = stop_intent in {
                _WAIT_PHASE_DEADLINE,
                "deadline",
                "pressure_deadline",
            }
            if (
                pressure_elapsed + 0.5 >= expected_pressure
                and deadline_intent
                and _cycle_control_handoff_safe(executor, trajectory)
            ):
                cycle_report["status"] = "passed"
            else:
                foreground_error = str(
                    trajectory.get("foreground_recovery_error")
                    or trajectory.get("foreground_restore_error")
                    or "target app did not return to foreground"
                )
                _mark_cycle_failure(
                    cycle_report,
                    executor,
                    trajectory,
                    "foreground recovery unresolved: %s" % foreground_error,
                    abort_reason=(
                        "aborting remaining cycles because the foreground/ADB "
                        "handoff remained unresolved: %s" % foreground_error
                    ),
                )
        else:
            cycle_report["status"] = "passed"
    except (InterruptedError, KeyboardInterrupt) as exc:
        recover_exception_artifacts(exc)
        cycle_report["status"] = "interrupted"
        cycle_report["errors"].append("test run cancelled")
    except Exception as exc:
        logger.exception("Cycle %s failed on %s", cycle, report["device_serial"])
        error_text = str(exc) or type(exc).__name__
        if error_text not in cycle_report["errors"]:
            cycle_report["errors"].append(error_text)
        detected_crash = recover_exception_artifacts(exc)
        # A phase can fail before its normal finalizer returns a trajectory.
        # Keep the exception and any executor-side handoff flags available to
        # the cycle classifier and to the durable report row.
        trajectory.setdefault("exception_type", type(exc).__name__)
        trajectory.setdefault("exception", error_text)
        attached_release_error = getattr(exc, "_control_release_error", None)
        if attached_release_error:
            _mark_control_release_failure(
                executor, trajectory, attached_release_error
            )
        _copy_cycle_handoff_fields(cycle_report, trajectory, executor)
        normalized_error = str(exc).lower()
        audit_gate_failure = any(
            marker in normalized_error
            for marker in (
                "audit gate was not completed",
                "complete_audit_gate",
                "审核页面未能",
                "startup gate did not clear",
            )
        )
        device_failure = any(
            marker in normalized_error
            for marker in (
                "device disconnected",
                "device is offline",
                "device not found",
                "device is not ready",
                "transport error",
                "no devices/emulators found",
            )
        )
        permission_recovery_failure = any(
            marker in normalized_error
            for marker in (
                "permission recovery",
                "permission dialog",
                "runtime permission",
            )
        )
        foreground_recovery_failure = any(
            marker in normalized_error
            for marker in (
                "foreground recovery",
                "target app did not return to foreground",
                "target package did not reach the foreground",
                "target application launch failed",
            )
        )
        device_failure = device_failure or isinstance(
            trajectory.get("device_disconnect"), Mapping
        )
        hard_control_failure = _cycle_hard_control_failure(
            executor, trajectory, error_text
        )

        # Device loss and an incomplete control-plane cleanup are terminal for
        # this worker.  Continuing would risk a second Fastbot instance or
        # random input being injected while another owner still holds the
        # device.
        if device_failure or hard_control_failure:
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                error_text,
                abort_reason=_abort_remaining_cycles_message(exc, executor),
            )
            cycle_report["recoverable"] = False
            cycle_report["recoverable_cycle"] = False
            cycle_report["continue_run"] = False
            cycle_report["abort_run"] = True
            cycle_report["abort_reason"] = _abort_remaining_cycles_message(
                exc, executor
            )
        else:
            verifier = getattr(executor, "verify_pressure_engine_stopped", None)
            verifier_available = callable(verifier)
            if audit_gate_failure:
                if verifier_available or trajectory:
                    trajectory.setdefault("stop_reason", _WAIT_AUDIT_RECOVERY_UNCLEAN)
                cycle_report["audit_recovery_failed"] = True
                cycle_report["audit_recovery_error"] = error_text
            elif permission_recovery_failure:
                if verifier_available or trajectory:
                    trajectory.setdefault(
                        "stop_reason", _WAIT_PERMISSION_RECOVERY_UNCLEAN
                    )
                cycle_report["permission_recovery_failed"] = True
                cycle_report["permission_recovery_error"] = error_text
            elif foreground_recovery_failure:
                if verifier_available or trajectory:
                    trajectory.setdefault("stop_reason", _WAIT_FOREGROUND_RECOVERY)
                cycle_report["foreground_recovery_failed"] = True
                cycle_report["foreground_recovery_error"] = error_text
            elif isinstance(exc, PressureEngineError):
                if verifier_available or trajectory:
                    trajectory.setdefault("stop_reason", _WAIT_PRESSURE_ENGINE_EXIT)
            # A startup exception often occurs before a process handle exists.
            # Probe the device even when an older stop result is cached; this
            # prevents a stale ``True`` from authorizing the next cycle.
            _verify_cycle_pressure_handoff(
                executor, trajectory, force=verifier_available
            )
            # A phase can raise after the target process has already crashed
            # (for example while stopping a collector).  Preserve that fact as
            # a crash cycle and allow the next configured cycle when the device
            # handoff is explicitly safe.  Only a live/unknown control owner,
            # or an explicit stop-on-crash policy, turns it into a terminal
            # failed cycle.
            crash_detected = bool(
                _is_confirmed_app_crash(detected_crash)
                or cycle_report.get("crash_detected")
            )
            if crash_detected and _cycle_control_handoff_safe(executor, trajectory):
                cycle_report["status"] = "crash"
                cycle_report["recoverable"] = False
                stop_on_crash = bool(
                    isinstance(config.get("test", {}), Mapping)
                    and config.get("test", {}).get("stop_on_crash", False)
                )
                if stop_on_crash:
                    cycle_report["recoverable_cycle"] = False
                    cycle_report["continue_run"] = False
                    cycle_report["abort_run"] = True
                    cycle_report["abort_reason"] = (
                        "aborting remaining cycles because stop_on_crash is enabled"
                    )
                else:
                    cycle_report["recoverable_cycle"] = True
                    cycle_report["continue_run"] = True
                    cycle_report.pop("abort_run", None)
                    cycle_report.pop("abort_reason", None)
            else:
                _mark_cycle_failure(
                    cycle_report,
                    executor,
                    trajectory,
                    error_text,
                    abort_reason=_abort_remaining_cycles_message(exc, executor),
                )
        _copy_cycle_handoff_fields(cycle_report, trajectory, executor)
    if "stall_detection_count" not in cycle_report:
        if "stall_detection_count" in trajectory or "stall_events" in trajectory:
            stall_count = max(
                0, int(trajectory.get("stall_detection_count", 0) or 0)
            )
            stall_events = list(trajectory.get("stall_events", []))
        else:
            stall_count, stall_events = _consume_stall_observations(executor)
        cycle_report["stall_detection_count"] = max(stall_count, len(stall_events))
        if stall_events:
            cycle_report["stall_events"] = stall_events
    cycle_report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return cycle_report


def _resume_options(config: Mapping[str, Any]) -> Dict[str, Any]:
    def as_bool(value: Any, default: bool) -> bool:
        """Parse JSON and legacy string booleans without ``bool('false')``."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        return default

    value = config.get("resume", {})
    options = dict(value) if isinstance(value, Mapping) else {}
    defaults = {
        "enabled": True,
        "mode": "auto",
        "max_attempts": 3,
        "checkpoint_interval_seconds": 10,
        "wait_for_device_seconds": 1800,
        "resume_partial_cycle": True,
        "clear_app_data_on_resume": False,
        "reinstall_on_resume": False,
    }
    for key, default in defaults.items():
        options.setdefault(key, default)
    options["enabled"] = as_bool(options["enabled"], True)
    options["mode"] = (
        str(options["mode"]) if str(options["mode"]) in {"auto", "manual"} else "auto"
    )
    try:
        options["max_attempts"] = max(0, min(20, int(options["max_attempts"])))
    except (TypeError, ValueError, OverflowError):
        options["max_attempts"] = 3
    try:
        options["checkpoint_interval_seconds"] = max(
            5, min(10, int(options["checkpoint_interval_seconds"]))
        )
    except (TypeError, ValueError, OverflowError):
        options["checkpoint_interval_seconds"] = 10
    try:
        options["wait_for_device_seconds"] = max(
            1, min(86400, int(options["wait_for_device_seconds"]))
        )
    except (TypeError, ValueError, OverflowError):
        options["wait_for_device_seconds"] = 1800
    for key in (
        "resume_partial_cycle",
        "clear_app_data_on_resume",
        "reinstall_on_resume",
    ):
        options[key] = as_bool(options[key], bool(defaults[key]))
    return options


def _wait_for_device_ready(
    device_serial: str,
    timeout_seconds: float,
    *,
    cancel_event: Any = None,
    heartbeat: Optional[CheckpointHeartbeat] = None,
    report: Optional[Dict[str, Any]] = None,
    wait_started_at: Optional[str] = None,
    wait_deadline_at: Optional[str] = None,
) -> bool:
    """Poll a temporarily absent device without spinning a new worker.

    The worker owns no UI/ADB controller while waiting, so reconnect polling is
    safe and bounded.  A timeout is deliberately returned to the caller for
    classification as ``needs_manual_resume`` rather than an unbounded retry.
    """
    timeout = max(1.0, float(timeout_seconds))
    wall_started = time.time()
    started = time.monotonic()
    persisted_started = _resume_wall_clock_epoch(wait_started_at)
    persisted_deadline = _resume_wall_clock_epoch(wait_deadline_at)
    if persisted_deadline is not None:
        # Rebase the durable wall-clock deadline onto this process' monotonic
        # clock. An expired deadline remains expired after a restart.
        deadline = started + (persisted_deadline - wall_started)
        if persisted_started is None:
            persisted_started = persisted_deadline - timeout
    elif persisted_started is not None:
        persisted_deadline = persisted_started + timeout
        deadline = started + (persisted_deadline - wall_started)
    else:
        persisted_started = wall_started
        persisted_deadline = wall_started + timeout
        deadline = started + timeout
    persisted_started_text = (
        str(wait_started_at)
        if wait_started_at not in (None, "") and persisted_started is not None
        else datetime.fromtimestamp(persisted_started, timezone.utc).isoformat()
    )
    persisted_deadline_text = (
        str(wait_deadline_at)
        if wait_deadline_at not in (None, "") and persisted_deadline is not None
        else datetime.fromtimestamp(persisted_deadline, timezone.utc).isoformat()
    )
    last_persist = 0.0
    last_error = "device is not ready or authorized: %s" % device_serial
    while True:
        _check_cancel(cancel_event)
        now = time.monotonic()
        if now >= deadline:
            return False
        try:
            if DeviceManager.is_device_ready(device_serial):
                return True
        except Exception as exc:
            last_error = str(exc) or type(exc).__name__
        elapsed = max(0.0, now - started)
        remaining = max(0.0, deadline - now)
        if heartbeat is not None:
            try:
                heartbeat_snapshot = heartbeat.snapshot()
            except Exception:
                heartbeat_snapshot = {}
            if not isinstance(heartbeat_snapshot, Mapping):
                heartbeat_snapshot = {}
            heartbeat.update(
                resume_state="waiting_for_device",
                phase_name="waiting_for_device",
                last_error=last_error,
                waiting_for_device=True,
                wait_started_at=persisted_started_text,
                waiting_seconds=round(elapsed, 3),
                wait_deadline_at=persisted_deadline_text,
                write=False,
            )
        if report is not None:
            report["resume_state"] = "waiting_for_device"
            report["status"] = "waiting_for_device"
            report["last_error"] = last_error
            report["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            report["waiting_for_device"] = True
            report["wait_started_at"] = persisted_started_text
            report["wait_deadline_at"] = persisted_deadline_text
        if now >= deadline:
            return False
        # Persist a visible waiting state at a modest cadence; the heartbeat
        # thread still writes the full checkpoint every configured interval.
        if report is not None and (last_persist == 0.0 or now - last_persist >= 5.0):
            try:
                # The caller's report directory is represented by the
                # heartbeat path; no additional path argument is required.
                report_dir = getattr(heartbeat, "report_dir", None)
                _persist_report(
                    report,
                    Path(str(report_dir)) if report_dir else Path("."),
                )
            except Exception:
                logger.debug("Unable to persist device waiting state", exc_info=True)
            last_persist = now
        wait_seconds = min(2.0, max(0.05, remaining))
        waiter = getattr(cancel_event, "wait", None)
        if callable(waiter):
            waiter(wait_seconds)
        else:
            time.sleep(wait_seconds)


def _load_existing_device_report(
    report_dir: Path,
    config: Mapping[str, Any],
    run_id: str,
    serial: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    value = read_checkpoint_json(report_dir / "report.json")
    checkpoint = load_checkpoint(
        report_dir, run_id=run_id, device_serial=serial
    )
    if not isinstance(value, Mapping):
        if checkpoint is None:
            return None, None
        # A heartbeat can be atomically committed immediately before the
        # report replacement.  Reconstruct only the mutable in-flight row so
        # a host restart continues the original job rather than reinstalling
        # and starting cycle one over again.
        report = _initial_report(config, run_id, serial, report_dir)
        state = effective_resume_state(None, checkpoint, default="recovering")
        report.update(
            {
                "started_at": checkpoint.get("cycle_started_at")
                or checkpoint.get("saved_at")
                or report["started_at"],
                "status": state,
                "resume_state": state,
                "resume_attempts": int(
                    checkpoint.get("resume_attempts", 0) or 0
                ),
                "last_heartbeat": checkpoint.get("last_heartbeat")
                or checkpoint.get("saved_at"),
                "last_error": checkpoint.get("last_error"),
                "apk_fingerprint": checkpoint.get("apk_fingerprint"),
                "foreground_recovery_strategy": checkpoint.get(
                    "foreground_recovery_strategy"
                )
                or "standard_foreground_recovery",
                "remaining_seconds": max(
                    0.0,
                    float(checkpoint.get("remaining_seconds", 0) or 0),
                ),
            }
        )
        cycle = int(checkpoint.get("cycle", 0) or 0)
        if cycle > 0:
            report["current_cycle"] = {
                "cycle": cycle,
                "phase": int(checkpoint.get("phase", 0) or 0),
                "started_at": checkpoint.get("cycle_started_at"),
                "status": report["resume_state"],
                "pressure_elapsed_seconds": float(
                    checkpoint.get("elapsed_seconds", 0) or 0
                ),
                "remaining_seconds": float(
                    checkpoint.get("remaining_seconds", 0) or 0
                ),
                "operation_count": int(
                    checkpoint.get("operation_count", 0) or 0
                ),
                "last_error": checkpoint.get("last_error"),
            }
        return report, None
    report = copy.deepcopy(dict(value))
    if str(report.get("device_serial") or serial) != serial:
        return None, "checkpoint report belongs to another device"
    if str(report.get("run_id") or run_id) != run_id:
        return None, "checkpoint report belongs to another job"
    app = config.get("app", {})
    package_name = str(app.get("package_name") or "") if isinstance(app, Mapping) else ""
    if str(report.get("package_name") or package_name) != package_name:
        return report, "APK package mismatch in existing device report"
    report.setdefault("schema_version", 3)
    report.setdefault("resume_state", str(report.get("status") or "running"))
    report.setdefault("resume_attempts", 0)
    report.setdefault("remaining_seconds", 0)
    report.setdefault("last_heartbeat", report.get("started_at"))
    report.setdefault("last_error", None)
    report.setdefault("foreground_recovery_strategy", "standard_foreground_recovery")
    report.setdefault("interruptions", [])
    if not isinstance(report.get("cycles"), list):
        report["cycles"] = []
    if not isinstance(report.get("crashes"), list):
        report["crashes"] = []
    if not isinstance(report.get("issues"), list):
        report["issues"] = []
    if not isinstance(report.get("artifacts"), dict):
        report["artifacts"] = {}
    # Collapse any duplicate cycle rows from an older/interrupted writer while
    # preserving the earliest completed copy.
    report["cycles"] = list(unique_cycle_rows(report))
    # Merge a newer heartbeat into the mutable in-flight view. Completed cycle
    # rows are deliberately handled by ``upsert_completed_cycle``/the
    # heartbeat writer and are never replaced here.
    if isinstance(checkpoint, Mapping):
        report_attempts = _report_nonnegative_int(
            report.get("resume_attempts", 0)
        )
        checkpoint_attempts = _report_nonnegative_int(
            checkpoint.get("resume_attempts", 0)
        )
        report["resume_attempts"] = max(report_attempts, checkpoint_attempts)
        if _resume_timestamp(checkpoint) >= _resume_timestamp(report):
            _merge_checkpoint_progress_into_report(report, checkpoint)
        # A terminal marker is monotonic across the two atomic files, even if
        # the checkpoint was written immediately before the report replacement.
        state = effective_resume_state(report, checkpoint, default="running")
        if state in TERMINAL_RESUME_STATES | AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES:
            report["resume_state"] = state
            if state in TERMINAL_RESUME_STATES or not str(report.get("status") or "").strip():
                report["status"] = state
        report["last_heartbeat"] = (
            checkpoint.get("last_heartbeat")
            if _resume_timestamp(checkpoint) >= _resume_timestamp(report)
            else report.get("last_heartbeat")
        ) or report.get("last_heartbeat")
        if checkpoint.get("last_error") and not report.get("last_error"):
            report["last_error"] = checkpoint.get("last_error")
    return report, None


def _configured_pressure_seconds(config: Mapping[str, Any]) -> Tuple[float, float]:
    test = config.get("test", {})
    if not isinstance(test, Mapping):
        return 60.0, 60.0
    cycle_seconds = max(
        1.0, float(int(test.get("fastbot_duration_minutes", 1) or 1) * 60)
    )
    total_seconds = cycle_seconds * max(1, int(test.get("max_cycles", 1) or 1))
    return cycle_seconds, total_seconds


def _resume_context_for_cycle(
    report: Mapping[str, Any],
    checkpoint: Optional[Mapping[str, Any]],
    cycle: int,
    enabled: bool,
) -> Dict[str, Any]:
    if not enabled:
        return {}
    values: list[Mapping[str, Any]] = []
    if isinstance(checkpoint, Mapping):
        try:
            if int(checkpoint.get("cycle", 0) or 0) == cycle:
                values.append(checkpoint)
        except (TypeError, ValueError):
            pass
    current = report.get("current_cycle")
    if isinstance(current, Mapping):
        try:
            if int(current.get("cycle", 0) or 0) == cycle:
                values.append(current)
        except (TypeError, ValueError):
            pass
    if not values:
        return {}
    # Prefer the newest snapshot for descriptive fields, then merge progress
    # counters monotonically. This handles report/checkpoint replacement races
    # without allowing a stale snapshot to restart a cycle from zero.
    context = copy.deepcopy(
        dict(
            max(
                values,
                key=lambda value: _resume_timestamp(value),
            )
        )
    )
    for value in values:
        if value is context:
            continue
        for key in ("elapsed_seconds", "pressure_elapsed_seconds"):
            if key in value:
                try:
                    context["elapsed_seconds"] = max(
                        float(context.get("elapsed_seconds", 0) or 0),
                        float(value.get(key, 0) or 0),
                    )
                except (TypeError, ValueError):
                    pass
        try:
            context["operation_count"] = max(
                int(context.get("operation_count", 0) or 0),
                int(value.get("operation_count", 0) or 0),
            )
        except (TypeError, ValueError):
            pass
        try:
            context["phase"] = max(
                int(context.get("phase", 0) or 0),
                int(value.get("phase", 0) or 0),
            )
        except (TypeError, ValueError):
            pass
    # Keep one monotonic, de-duplicated phase history for the current cycle.
    # Reports and checkpoints can be flushed in either order, so selecting
    # only the newest file would occasionally make the next invocation reuse a
    # lower phase number.
    phase_values: list[Mapping[str, Any]] = []
    for value in values:
        raw_phases = value.get("pressure_phases")
        if isinstance(raw_phases, list):
            phase_values.extend(
                item for item in raw_phases if isinstance(item, Mapping)
            )
    if phase_values:
        merged_phases: list[Dict[str, Any]] = []
        seen_phases: set[tuple[Any, ...]] = set()
        for item in phase_values:
            copied = copy.deepcopy(dict(item))
            marker = (
                copied.get("cycle"),
                copied.get("phase"),
                copied.get("started_at"),
                copied.get("finished_at"),
                copied.get("stop_reason"),
            )
            if marker in seen_phases:
                continue
            seen_phases.add(marker)
            merged_phases.append(copied)
        merged_phases.sort(
            key=lambda item: int(item.get("phase", 0) or 0)
            if isinstance(item, Mapping)
            else 0
        )
        context["pressure_phases"] = merged_phases
    if "elapsed_seconds" not in context:
        context["elapsed_seconds"] = context.get("pressure_elapsed_seconds", 0)
    else:
        context["elapsed_seconds"] = max(
            float(context.get("elapsed_seconds", 0) or 0),
            float(context.get("pressure_elapsed_seconds", 0) or 0),
        )
    context["cycle"] = cycle
    # Presence of a same-cycle durable snapshot is itself an explicit resume
    # request.  Elapsed time may still be zero when the host died between the
    # cycle marker and the first Fastbot heartbeat.
    context["resume_requested"] = True
    return context


def _reload_durable_cycle_state(
    report: Dict[str, Any],
    report_dir: Path,
    run_id: str,
    device_serial: str,
    checkpoint_value: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Merge the newest report/checkpoint into a live worker report.

    A heartbeat thread and the cycle loop can publish the two files in either
    order.  Reloading under the per-report lock before each cycle makes the
    loop idempotent after a host restart while keeping the original ``report``
    object (which ``CheckpointHeartbeat`` references) alive.
    """
    report_path = Path(report_dir) / "report.json"
    with _report_write_lock(report_dir):
        durable_report = read_checkpoint_json(report_path)
        durable_checkpoint = load_checkpoint(
            report_dir,
            run_id=run_id,
            device_serial=device_serial,
        )
        if durable_checkpoint is None and isinstance(checkpoint_value, Mapping):
            durable_checkpoint = dict(checkpoint_value)

        # A manual stop may have been published by the web console while the
        # worker was between phase transitions.  Import that gate before
        # merging progress so a stale in-memory ``running`` state cannot
        # reopen the device on the next cycle.
        _preserve_durable_manual_gate(report, durable_report, durable_checkpoint)

        # Completed cycle rows are immutable.  Import them one by one instead
        # of replacing ``report['cycles']`` so a heartbeat cannot erase rows
        # already committed by a previous process.
        if isinstance(durable_report, Mapping):
            durable_cycles = durable_report.get("cycles", [])
            if isinstance(durable_cycles, list):
                for value in durable_cycles:
                    if isinstance(value, Mapping) and is_completed_cycle(value):
                        try:
                            upsert_completed_cycle(report, value)
                        except (TypeError, ValueError, OverflowError):
                            continue

            durable_terminal = terminal_resume_state(durable_report)
            memory_terminal = terminal_resume_state(report)
            if durable_terminal and not memory_terminal:
                # A terminal report wins over an older in-flight heartbeat.
                report["resume_state"] = durable_terminal
                report["status"] = str(
                    durable_report.get("status") or durable_terminal
                )
                report["finished_at"] = durable_report.get("finished_at")
                report.pop("current_cycle", None)
            elif not memory_terminal:
                # Carry forward scheduler/heartbeat metadata monotonically.
                if _resume_timestamp(durable_report) >= _resume_timestamp(report):
                    for key in (
                        "last_heartbeat",
                        "last_error",
                        "foreground_component",
                        "apk_fingerprint",
                        "remote_log_path",
                        "remote_log_offset",
                        "logcat_offset",
                        "resume_state",
                        "status",
                    ):
                        if durable_report.get(key) not in (None, ""):
                            report[key] = copy.deepcopy(durable_report.get(key))
                report["resume_attempts"] = max(
                    _report_nonnegative_int(report.get("resume_attempts"), 0),
                    _report_nonnegative_int(durable_report.get("resume_attempts"), 0),
                )

                durable_current = durable_report.get("current_cycle")
                if isinstance(durable_current, Mapping):
                    try:
                        current_number = int(
                            durable_current.get("cycle")
                            or durable_current.get("cycle_number")
                            or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        current_number = 0
                    if current_number > 0 and current_number not in completed_cycle_numbers(
                        report
                    ):
                        current_snapshot = dict(durable_current)
                        current_snapshot["cycle"] = current_number
                        current_snapshot["phase"] = current_snapshot.get("phase", 0)
                        current_snapshot["elapsed_seconds"] = current_snapshot.get(
                            "elapsed_seconds",
                            current_snapshot.get("pressure_elapsed_seconds", 0),
                        )
                        _merge_checkpoint_progress_into_report(
                            report, current_snapshot
                        )

        if isinstance(durable_checkpoint, Mapping):
            _merge_checkpoint_progress_into_report(report, durable_checkpoint)

        # Keep the in-memory report canonical before the next cycle decision;
        # this also prevents a duplicate completed row from being re-emitted
        # by a later heartbeat or scheduler transition.
        report["cycles"] = list(unique_cycle_rows(report))

        # Never leave a partial current-cycle row for a cycle that was imported
        # as completed from the durable report.
        current = report.get("current_cycle")
        if isinstance(current, Mapping):
            try:
                current_number = int(current.get("cycle", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                current_number = 0
            if current_number in completed_cycle_numbers(report):
                report.pop("current_cycle", None)
        return dict(durable_checkpoint) if isinstance(durable_checkpoint, Mapping) else None


def _report_is_complete(
    report: Mapping[str, Any],
    max_cycles: int,
    checkpoint: Optional[Mapping[str, Any]] = None,
) -> bool:
    completed = completed_cycle_numbers(report)
    target = max(1, int(max_cycles))
    if all(cycle in completed for cycle in range(1, target + 1)):
        return True
    # A terminal run-level marker is authoritative even when an older writer
    # did not emit every cycle row. The explicit resume endpoint can still
    # override this gate for an operator-requested continuation.
    return effective_resume_state(report, checkpoint, default="") in TERMINAL_RESUME_STATES


def _record_resume_interruption(
    report: Dict[str, Any],
    decision: Mapping[str, Any],
    error: str,
    cycle: Optional[int] = None,
) -> None:
    item = {
        "at": datetime.now(timezone.utc).isoformat(),
        "cycle": cycle,
        "state": str(decision.get("state") or "failed"),
        "category": str(decision.get("category") or "fatal"),
        "reason": str(decision.get("reason") or ""),
        "error": str(error),
        "attempt": int(report.get("resume_attempts", 0) or 0),
    }
    interruptions = report.setdefault("interruptions", [])
    if isinstance(interruptions, list):
        interruptions.append(item)
    report["last_error"] = str(error)
    report["resume_state"] = item["state"]
    report["status"] = item["state"]
    report["finished_at"] = None if item["state"] != "failed" else item["at"]
    if item["state"] in MANUAL_RECOVERY_STATES:
        # A reserved automatic attempt that hit a manual gate is no longer an
        # in-flight launch.  Clear the reservation so an explicit operator
        # resume receives its own count.
        report["resume_attempt_reserved"] = False


def _recovery_adb_error(
    error: Any,
    decision: Optional[Mapping[str, Any]] = None,
    checkpoint: Optional[Mapping[str, Any]] = None,
    executor: Optional[Any] = None,
) -> str:
    """Extract the most useful transport/control error for one retry bundle.

    Recovery classification intentionally works with a compact high-level
    message, while operators need the original ADB stderr/timeout detail.  A
    best-effort extraction keeps that detail in a dedicated field without
    making evidence capture itself capable of failing the worker.
    """

    candidates: List[Any] = []

    def collect(value: Any) -> None:
        if value is None:
            return
        # ``Mock``-based executor adapters expose every attribute lazily;
        # their repr is not diagnostic evidence and would hide the real error
        # collected from the decision/checkpoint.
        if getattr(value, "_is_coroutine", False) or hasattr(value, "_mock_name"):
            return
        if isinstance(value, Mapping):
            # Prefer explicitly named transport fields over a generic mapping
            # representation.  Preserve all nested values as a fallback for
            # adapters that use a different diagnostic shape.
            for key in (
                "adb_error",
                "adb_error_text",
                "stderr",
                "transport_error",
                "device_error",
                "last_error",
                "error",
                "message",
            ):
                if value.get(key) not in (None, "", []):
                    candidates.append(value.get(key))
            for key, nested in value.items():
                if str(key).lower() in {
                    "artifacts",
                    "events",
                    "config",
                    "device_info",
                    "timeline",
                }:
                    continue
                if isinstance(nested, (Mapping, list, tuple)):
                    collect(nested)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                collect(nested)
        else:
            text = str(value).strip()
            if text:
                candidates.append(text)

    if executor is not None:
        for name in (
            "adb_error",
            "last_adb_error",
            "transport_error",
            "device_disconnect_error",
            "foreground_restore_error",
            "pressure_stop_error",
        ):
            try:
                collect(getattr(executor, name, None))
            except Exception:
                continue
    collect(error)
    collect(decision)
    collect(checkpoint)
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            candidate = dict(candidate)
        text = str(candidate).strip()
        if text:
            return text[:24000]
    return ""


def _save_recovery_attempt_evidence(
    report_dir: Path,
    attempt: int,
    decision: Mapping[str, Any],
    error: str,
    checkpoint: Optional[Mapping[str, Any]],
    state_manager: Optional[Any] = None,
    executor: Optional[Any] = None,
    mutex: Optional[Any] = None,
) -> Optional[Path]:
    """Persist one immutable recovery bundle before another worker is started."""
    adb_error = _recovery_adb_error(error, decision, checkpoint, executor)
    try:
        try:
            directory = create_recovery_attempt(
                report_dir,
                attempt,
                state=str(decision.get("state") or "recovering"),
                error=error,
                checkpoint=checkpoint,
                adb_error=adb_error,
            )
        except TypeError as exc:
            # Third-party adapters may still provide the pre-ADB-error helper
            # signature.  The subsequent manifest update keeps the explicit
            # field for those adapters as well.
            if "adb_error" not in str(exc).lower():
                raise
            directory = create_recovery_attempt(
                report_dir,
                attempt,
                state=str(decision.get("state") or "recovering"),
                error=error,
                checkpoint=checkpoint,
            )
    except Exception:
        logger.warning("Unable to create recovery evidence directory", exc_info=True)
        return None

    checkpoint_value = dict(checkpoint or {})
    cleanup_before = _recovery_pid_snapshot(
        state_manager=state_manager, executor=executor, mutex=mutex
    )
    update_recovery_attempt(
        directory,
        adb_error=adb_error,
        fastbot_pid=checkpoint_value.get("fastbot_pid")
        or cleanup_before.get("fastbot_pid"),
        worker_pid=checkpoint_value.get("worker_pid")
        or cleanup_before.get("worker_pid")
        or os.getpid(),
        remote_log_path=checkpoint_value.get("remote_log_path"),
        remote_log_offset=checkpoint_value.get("remote_log_offset", 0),
        logcat_path=checkpoint_value.get("logcat_path")
        or str(Path(report_dir) / "logcat.log"),
        logcat_offset=checkpoint_value.get("logcat_offset", 0),
        foreground_component=checkpoint_value.get("foreground_component"),
        cleanup_before_pid_snapshot=cleanup_before,
        timeline_event={
            "event": "cleanup_before_snapshot",
            "holder": cleanup_before.get("holder"),
        },
    )
    # Always leave a distinct file behind.  An empty file is meaningful: it
    # records that the attempt was captured even when the transport did not
    # expose stderr before it disappeared.
    adb_error_path = directory / "adb_error.txt"
    try:
        adb_error_path.write_text(adb_error, encoding="utf-8")
        update_recovery_attempt(directory, adb_error_path=str(adb_error_path))
    except OSError as exc:
        update_recovery_attempt(directory, errors={"adb_error": str(exc)})
    artifacts: Dict[str, str] = {}
    for source_name, destination_name in (
        ("logcat.log", "logcat_tail.log"),
        ("fastbot.log", "fastbot_tail.log"),
    ):
        source = Path(report_dir) / source_name
        destination = directory / destination_name
        if source.is_file() and copy_tail(source, destination):
            artifacts[destination_name] = str(destination)

    if state_manager is not None:
        component_reader = getattr(state_manager, "current_component", None)
        if callable(component_reader):
            try:
                component = component_reader()
                if component:
                    (directory / "foreground_component.txt").write_text(
                        str(component), encoding="utf-8"
                    )
                    artifacts["foreground_component"] = str(
                        directory / "foreground_component.txt"
                    )
            except Exception as exc:
                update_recovery_attempt(
                    directory,
                    errors={"foreground_component": str(exc)},
                )
        screenshot_reader = getattr(state_manager, "capture_screenshot_png", None)
        if callable(screenshot_reader):
            try:
                target = directory / "screen.png"
                target.write_bytes(screenshot_reader())
                artifacts["screenshot"] = str(target)
            except Exception as exc:
                update_recovery_attempt(directory, errors={"screenshot": str(exc)})
        hierarchy_reader = getattr(state_manager, "dump_hierarchy_text", None)
        if callable(hierarchy_reader):
            try:
                target = directory / "ui.xml"
                target.write_text(str(hierarchy_reader() or ""), encoding="utf-8")
                artifacts["ui_hierarchy"] = str(target)
            except Exception as exc:
                update_recovery_attempt(directory, errors={"ui_hierarchy": str(exc)})

    if executor is not None:
        diagnostics = getattr(executor, "capture_process_diagnostics", None)
        if callable(diagnostics):
            try:
                values = diagnostics(str(directory / "diagnostics"))
                if isinstance(values, Mapping):
                    artifacts.update(
                        {str(key): str(value) for key, value in values.items() if value}
                    )
                    diagnostic_adb_error = _recovery_adb_error(
                        values, decision, checkpoint, executor
                    )
                    if diagnostic_adb_error and diagnostic_adb_error != adb_error:
                        adb_error = (
                            "%s\n[diagnostics]\n%s" % (adb_error, diagnostic_adb_error)
                            if adb_error
                            else diagnostic_adb_error
                        )[:24000]
                        try:
                            adb_error_path.write_text(adb_error, encoding="utf-8")
                        except OSError as exc:
                            update_recovery_attempt(
                                directory, errors={"adb_error": str(exc)}
                            )
                        update_recovery_attempt(
                            directory,
                            adb_error=adb_error,
                            adb_error_path=str(adb_error_path),
                        )
            except Exception as exc:
                update_recovery_attempt(directory, errors={"diagnostics": str(exc)})

    finished_at = datetime.now(timezone.utc).isoformat()
    update_recovery_attempt(
        directory,
        finished_at=finished_at,
        # ``recovery_finished_at`` is completed after the caller releases all
        # device control.  Keep the field present even if the process exits
        # before that finalizer runs.
        recovery_finished_at=finished_at,
        artifacts=artifacts,
        timeline_event={
            "event": "recovery_evidence_captured",
            "category": str(decision.get("category") or "unknown"),
            "foreground_component": str(
                (checkpoint or {}).get("foreground_component") or ""
            ),
        },
    )
    return directory


def _recovery_pid_snapshot(
    *,
    state_manager: Optional[Any] = None,
    executor: Optional[Any] = None,
    mutex: Optional[Any] = None,
) -> Dict[str, Any]:
    """Capture best-effort device process ownership for one recovery attempt."""
    snapshot: Dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "worker_pid": os.getpid(),
        "fastbot_pid": None,
        "fastbot_processes": {},
        "uiautomator2_processes": {},
        "holder": None,
        "errors": [],
    }
    if executor is not None:
        try:
            process = getattr(executor, "_fastbot_process", None)
            value = getattr(process, "remote_pid", None) if process is not None else None
            if value not in (None, ""):
                snapshot["fastbot_pid"] = str(value)
        except Exception as exc:
            snapshot["errors"].append("executor fastbot pid: %s" % exc)
    if mutex is not None:
        for name, key in (
            ("_remote_processes", "fastbot_processes"),
            ("_uiautomator_processes", "uiautomator2_processes"),
        ):
            try:
                reader = getattr(mutex, name, None)
                value = reader() if callable(reader) else None
                snapshot[key] = dict(value) if isinstance(value, Mapping) else value
                if key == "fastbot_processes" and not snapshot.get("fastbot_pid"):
                    if isinstance(value, Mapping) and value:
                        snapshot["fastbot_pid"] = str(next(iter(value)))
            except Exception as exc:
                snapshot["errors"].append("%s: %s" % (name, exc))
        try:
            reader = getattr(mutex, "get_current_holder", None)
            holder = reader() if callable(reader) else getattr(mutex, "current_holder", None)
            snapshot["holder"] = holder
        except Exception as exc:
            snapshot["errors"].append("holder: %s" % exc)
    return snapshot


def _finalize_recovery_attempt_evidence(
    directories: Iterable[Path],
    *,
    state_manager: Optional[Any] = None,
    executor: Optional[Any] = None,
    mutex: Optional[Any] = None,
) -> None:
    """Append post-cleanup state without mutating earlier recovery evidence."""
    for directory in list(directories):
        try:
            after = _recovery_pid_snapshot(
                state_manager=state_manager, executor=executor, mutex=mutex
            )
            update_recovery_attempt(
                Path(directory),
                cleanup_after_pid_snapshot=after,
                recovery_finished_at=datetime.now(timezone.utc).isoformat(),
                timeline_event={
                    "event": "cleanup_after_snapshot",
                    "holder": after.get("holder"),
                },
            )
        except Exception:
            logger.warning(
                "Unable to finalize recovery evidence at %s", directory, exc_info=True
            )


def _device_apk_identity(serial: str, package_name: str) -> Dict[str, Any]:
    """Return package presence and a best-effort SHA-256 of its base APK."""
    result: Dict[str, Any] = {
        "installed": False,
        "fingerprint": None,
        "remote_path": None,
        "fingerprint_supported": False,
    }
    try:
        packages = DeviceManager._run_adb(  # type: ignore[attr-defined]
            ["shell", "pm", "path", package_name],
            serial=serial,
            timeout=20,
            retries=1,
        )
    except Exception as exc:
        text = str(exc)
        if "unknown package" in text.lower() or "not found" in text.lower():
            result["error"] = text
            return result
        raise
    remote_paths = [
        line.partition(":")[2].strip()
        for line in (packages.stdout or "").splitlines()
        if line.strip().startswith("package:")
    ]
    if not remote_paths:
        return result
    result["installed"] = True
    remote_path = next(
        (path for path in remote_paths if path.endswith("/base.apk")),
        remote_paths[0],
    )
    if not re.fullmatch(r"/[A-Za-z0-9_+.,=@%:/-]+\.apk", remote_path):
        result["error"] = "device returned an invalid APK path"
        return result
    result["remote_path"] = remote_path
    try:
        digest = DeviceManager._run_adb(  # type: ignore[attr-defined]
            ["shell", "sha256sum", remote_path],
            serial=serial,
            timeout=30,
            retries=1,
        )
        match = re.search(r"\b([A-Fa-f0-9]{64})\b", digest.stdout or "")
        if match:
            result["fingerprint"] = match.group(1).lower()
            result["fingerprint_supported"] = True
    except Exception as exc:
        result["fingerprint_error"] = str(exc)
    return result


def _install_apk_for_device(
    state_manager: StateManager,
    apk_path: Path,
    *,
    reinstall: bool,
) -> bool:
    installer = getattr(state_manager, "install_app")
    if reinstall:
        try:
            return bool(installer(str(apk_path), reinstall=True))
        except TypeError as exc:
            if not any(
                marker in str(exc).lower()
                for marker in ("unexpected keyword", "positional argument")
            ):
                raise
    return bool(installer(str(apk_path)))


def _run_test_on_device_legacy(
    device_serial: str,
    config: Mapping[str, Any],
    run_id: Optional[str] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Execute all configured cycles on one already-attached ADB device."""
    run_id = run_id or utc_timestamp()
    require_local_apk(config)
    report_dir = report_directory(config, run_id, device_serial)
    worker_config = copy.deepcopy(dict(config))
    worker_config["_report_dir"] = str(report_dir)
    log_config = worker_config.get("logging", {})
    log_dir = resolve_project_path(
        str(log_config.get("log_dir", "./logs"))
        if isinstance(log_config, Mapping)
        else "./logs"
    )
    configure_logging(
        str(log_dir),
        str(log_config.get("level", "INFO")) if isinstance(log_config, Mapping) else "INFO",
        "%s_%s.log" % (run_id, safe_serial(device_serial)),
    )
    report = _initial_report(worker_config, run_id, device_serial, report_dir)
    state_manager: Optional[StateManager] = None
    executor: Optional[TestExecutor] = None
    mutex: Optional[MutexController] = None
    try:
        _check_cancel(cancel_event)
        if not DeviceManager.is_device_ready(device_serial):
            raise RuntimeError("device is not ready or authorized: %s" % device_serial)
        report["device_info"] = DeviceManager.get_device_info(device_serial)
        package_name = str(worker_config["app"]["package_name"])
        state_manager = StateManager(device_serial, package_name)
        executor = TestExecutor(device_serial, package_name, worker_config)
        mutex = MutexController(device_serial)
        report["foreground_recovery_strategy"] = _foreground_recovery_strategy(
            state_manager
        )
        _run_pressure_preflight(executor, report)
        _persist_report(report, report_dir)
        # Validate and install once before the cycle loop. This is always a local file.
        apk_path = require_local_apk(worker_config)
        if not state_manager.install_app(str(apk_path)):
            raise RuntimeError("Unable to install local APK %s" % apk_path)

        for cycle in range(1, int(worker_config["test"]["max_cycles"]) + 1):
            _check_cancel(cancel_event)
            cycle_report = _run_cycle(
                state_manager,
                executor,
                _new_crash_handler(device_serial, package_name, worker_config),
                mutex,
                worker_config,
                report,
                report_dir,
                cycle,
                cancel_event,
            )
            report["cycles"].append(cycle_report)
            _persist_report(report, report_dir)
            if cycle_report.get("status") == "interrupted":
                raise InterruptedError("test run cancelled")
            if cycle_report.get("abort_run"):
                abort_reason = str(
                    cycle_report.get("abort_reason")
                    or "aborting remaining cycles because device control could not be recovered"
                )
                report.setdefault("errors", []).append(abort_reason)
                break
            if cycle_report.get("status") == "crash" and bool(
                worker_config.get("test", {}).get("stop_on_crash", False)
            ):
                report.setdefault("errors", []).append(
                    "pressure stopped after target-app crash on cycle %s" % cycle
                )
                break
    except (InterruptedError, KeyboardInterrupt):
        report["status"] = "interrupted"
    except Exception as exc:
        logger.exception("Test run failed on %s", device_serial)
        report["status"] = "failed"
        report.setdefault("errors", []).append(str(exc))
    finally:
        try:
            if report["status"] == "running":
                statuses = [str(cycle.get("status")) for cycle in report["cycles"]]
                if any(status == "interrupted" for status in statuses):
                    report["status"] = "interrupted"
                elif any(status == "failed" for status in statuses):
                    report["status"] = "completed_with_errors"
                elif report["crashes"]:
                    report["status"] = "completed_with_crashes"
                else:
                    report["status"] = "completed"
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            _cleanup_device_control(
                report, executor, mutex, state_manager, device_serial
            )
            _apply_report_coverage(report, worker_config)
            _persist_report(report, report_dir)
        finally:
            close_logging_handlers()
    return {
        "device_serial": device_serial,
        "status": report["status"],
        "coverage_status": report.get("coverage", {}).get("status"),
        "report_dir": str(report_dir),
        "report": report,
    }


def run_test_on_device(
    device_serial: str,
    config: Mapping[str, Any],
    run_id: Optional[str] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Run one device idempotently, continuing an unfinished cycle in place."""
    run_id = run_id or utc_timestamp()
    report_dir = report_directory(config, run_id, device_serial)
    worker_config = copy.deepcopy(dict(config))
    worker_config["_report_dir"] = str(report_dir)
    resume_options = _resume_options(worker_config)
    cycle_seconds, total_seconds = _configured_pressure_seconds(worker_config)
    max_cycles = max(1, int(worker_config.get("test", {}).get("max_cycles", 1)))
    log_config = worker_config.get("logging", {})
    log_dir = resolve_project_path(
        str(log_config.get("log_dir", "./logs"))
        if isinstance(log_config, Mapping)
        else "./logs"
    )
    configure_logging(
        str(log_dir),
        str(log_config.get("level", "INFO"))
        if isinstance(log_config, Mapping)
        else "INFO",
        "%s_%s.log" % (run_id, safe_serial(device_serial)),
    )

    existing, report_identity_error = _load_existing_device_report(
        report_dir, worker_config, run_id, device_serial
    )
    report = existing or _initial_report(
        worker_config, run_id, device_serial, report_dir
    )
    checkpoint_value = load_checkpoint(
        report_dir, run_id=run_id, device_serial=device_serial
    )
    has_durable_state = existing is not None or checkpoint_value is not None
    if has_durable_state and _refresh_durable_manual_end(
        report,
        report_dir,
        run_id,
        device_serial,
    ):
        # An operator-ended device belongs to the old report only.  A stale
        # scheduler invocation must not consume a resume request, install the
        # APK, or acquire device control for it again.
        close_logging_handlers()
        return {
            "device_serial": device_serial,
            "status": MANUALLY_ENDED_STATE,
            "resume_state": MANUALLY_ENDED_STATE,
            "resume_attempts": max(
                _report_nonnegative_int(report.get("resume_attempts", 0)),
                _report_nonnegative_int(
                    (checkpoint_value or {}).get("resume_attempts", 0)
                ),
            ),
            "remaining_seconds": 0.0,
            "recoverable": False,
            "automatic_resume": False,
            "recovery_category": "manual_end",
            "recovery_reason": str(
                report.get("recovery_reason")
                or report.get("last_error")
                or "device test was ended manually"
            ),
            "coverage_status": "stopped",
            "report_dir": str(report_dir),
            "report": report,
            "skipped_manual_end": True,
        }
    manual_resume_request = (
        consume_manual_resume_request(report_dir) if has_durable_state else None
    )
    prior_resume_state = effective_resume_state(
        report, checkpoint_value, default="running"
    ).strip()
    persisted_wait_started_at = None
    persisted_wait_deadline_at = None
    if prior_resume_state == "waiting_for_device":
        for source in (report, checkpoint_value):
            if not isinstance(source, Mapping):
                continue
            if persisted_wait_started_at in (None, "") and source.get(
                "wait_started_at"
            ) not in (None, ""):
                persisted_wait_started_at = str(source.get("wait_started_at"))
            if persisted_wait_deadline_at in (None, "") and source.get(
                "wait_deadline_at"
            ) not in (None, ""):
                persisted_wait_deadline_at = str(source.get("wait_deadline_at"))
    if prior_resume_state in {"interrupted", "stopped"}:
        prior_resume_state = (
            "paused"
            if "web console" in str(report.get("last_error") or "").lower()
            else "recovering"
        )
    terminal_state = terminal_resume_state(report) or terminal_resume_state(
        checkpoint_value
    )
    terminal_success = {"completed", "passed"}
    if has_durable_state and _report_is_complete(
        report, max_cycles, checkpoint_value
    ) and not (
        manual_resume_request and terminal_state and terminal_state not in terminal_success
    ):
        # A second dispatch of a completed device is a no-op. In particular,
        # no installer, reset, counter, or report timestamp is touched.
        close_logging_handlers()
        return {
            "device_serial": device_serial,
            "status": str(
                effective_resume_state(report, checkpoint_value, default="completed")
            ),
            "resume_state": effective_resume_state(
                report, checkpoint_value, default="completed"
            ),
            "resume_attempts": max(
                _report_nonnegative_int(report.get("resume_attempts", 0)),
                _report_nonnegative_int(
                    (checkpoint_value or {}).get("resume_attempts", 0)
                ),
            ),
            # A terminal report is authoritative even when checkpoint.json is
            # an older in-flight heartbeat.  Never expose that stale remainder
            # as resumable work.
            "remaining_seconds": 0.0,
            "coverage_status": report.get("coverage", {}).get("status")
            if isinstance(report.get("coverage"), Mapping)
            else None,
            "report_dir": str(report_dir),
            "report": report,
            "skipped_completed": True,
        }

    # Manual stops and operator-gated failures remain inert until the explicit
    # web resume request is consumed.  This also makes an accidental duplicate
    # scheduler dispatch a harmless no-op.
    if has_durable_state and (
        prior_resume_state in MANUAL_RECOVERY_STATES
        or prior_resume_state in {"failed", "completed_with_errors"}
    ) and not manual_resume_request:
        report["resume_state"] = prior_resume_state
        report["status"] = prior_resume_state
        report["finished_at"] = None
        close_logging_handlers()
        return {
            "device_serial": device_serial,
            "status": prior_resume_state,
            "resume_state": prior_resume_state,
            "resume_attempts": report.get("resume_attempts", 0),
            "remaining_seconds": report.get("remaining_seconds", 0),
            "recoverable": False,
            "automatic_resume": False,
            "recovery_category": "manual_gate"
            if prior_resume_state in MANUAL_RECOVERY_STATES
            else "previous_run_terminal",
            "recovery_reason": str(report.get("last_error") or "manual resume required"),
            "coverage_status": report.get("coverage", {}).get("status")
            if isinstance(report.get("coverage"), Mapping)
            else None,
            "report_dir": str(report_dir),
            "report": report,
            "skipped_manual": True,
        }
    # ``resume.mode=manual`` applies to automatic interruption states too.  A
    # host restart must leave the checkpoint available for the operator rather
    # than silently starting a new worker just because the device is online.
    if (
        has_durable_state
        and not manual_resume_request
        and resume_options.get("mode") == "manual"
        and prior_resume_state in AUTOMATIC_RECOVERY_STATES | {"recovering", "resuming"}
    ):
        report["resume_state"] = prior_resume_state
        report["status"] = prior_resume_state
        report["finished_at"] = None
        close_logging_handlers()
        return {
            "device_serial": device_serial,
            "status": prior_resume_state,
            "resume_state": prior_resume_state,
            "resume_attempts": report.get("resume_attempts", 0),
            "remaining_seconds": report.get("remaining_seconds", 0),
            "recoverable": True,
            "automatic_resume": False,
            "recovery_category": "manual_mode",
            "recovery_reason": "manual resume mode is enabled",
            "coverage_status": report.get("coverage", {}).get("status")
            if isinstance(report.get("coverage"), Mapping)
            else None,
            "report_dir": str(report_dir),
            "report": report,
            "skipped_manual": True,
        }

    state_manager: Optional[StateManager] = None
    executor: Optional[TestExecutor] = None
    mutex: Optional[MutexController] = None
    heartbeat: Optional[CheckpointHeartbeat] = None
    recovery_evidence_directories: List[Path] = []
    local_fingerprint = ""
    terminal = False
    current_cycle_number: Optional[int] = None
    result_decision: Dict[str, Any] = {
        "state": "running",
        "recoverable": False,
        "automatic": False,
        "category": "running",
        "reason": "",
    }
    # The scheduler can reserve an attempt before it launches this process.
    # Read the marker from both atomic files because either one may have won a
    # report/checkpoint replacement race.  A direct ``run_test_on_device`` call
    # has no marker and therefore claims its own resume attempt below.
    attempt_reserved = bool(
        report.get("resume_attempt_reserved")
        or (checkpoint_value or {}).get("resume_attempt_reserved")
    )

    try:
        _raise_if_device_manually_ended(
            report, report_dir, run_id, device_serial
        )
        apk_path = require_local_apk(worker_config)
        local_fingerprint = sha256_file(apk_path)
        previous_fingerprint = str(
            (checkpoint_value or {}).get("apk_fingerprint")
            or report.get("apk_fingerprint")
            or ""
        ).lower()
        if report_identity_error:
            raise RuntimeError(report_identity_error)
        if previous_fingerprint and previous_fingerprint != local_fingerprint:
            raise RuntimeError(
                "APK fingerprint mismatch: checkpoint=%s current=%s"
                % (previous_fingerprint, local_fingerprint)
            )

        is_resume = has_durable_state
        if is_resume and not resume_options["enabled"] and not manual_resume_request:
            raise RuntimeError("unfinished report exists but resume is disabled")
        persisted_attempts = max(
            _report_nonnegative_int(report.get("resume_attempts", 0)),
            _report_nonnegative_int((checkpoint_value or {}).get("resume_attempts", 0)),
        )
        if (
            is_resume
            and not attempt_reserved
            and not manual_resume_request
            and prior_resume_state in AUTOMATIC_RECOVERY_STATES | {"recovering"}
            and persisted_attempts >= int(resume_options.get("max_attempts", 3) or 0)
        ):
            timeout_error = "automatic resume attempt limit reached"
            _record_resume_interruption(
                report,
                {
                    "state": "needs_manual_resume",
                    "recoverable": False,
                    "automatic": False,
                    "category": "resume_attempt_limit",
                    "reason": timeout_error,
                },
                timeout_error,
                None,
            )
            result_decision = {
                "state": "needs_manual_resume",
                "recoverable": False,
                "automatic": False,
                "category": "resume_attempt_limit",
                "reason": timeout_error,
            }
            terminal = False
            return {
                "device_serial": device_serial,
                "status": report["status"],
                "resume_state": report.get("resume_state"),
                "resume_attempts": report.get("resume_attempts", 0),
                "remaining_seconds": report.get("remaining_seconds", 0),
                "recoverable": False,
                "automatic_resume": False,
                "recovery_category": "resume_attempt_limit",
                "recovery_reason": timeout_error,
                "report_dir": str(report_dir),
                "report": report,
            }
        if is_resume:
            # A scheduler-reserved attempt has already been counted.  Consume
            # the reservation once the worker owns the device; a later retry
            # can then reserve a fresh number without double counting this
            # launch.  Direct/manual invocations without a reservation claim
            # the next number here.
            report["resume_attempts"] = (
                persisted_attempts
                if attempt_reserved
                else persisted_attempts + 1
            )
            report["resume_attempt_reserved"] = False
            report["resume_state"] = "resuming"
            report["status"] = "resuming"
            report["finished_at"] = None
            if manual_resume_request:
                # The web console may have queued this request while the
                # device was offline. Remove the old file-level stop marker so
                # a later heartbeat cannot reassert the manual gate.
                report["manual_stop"] = False
                report["waiting_for_device"] = False
        else:
            report["resume_attempts"] = 0
            report["resume_state"] = "running"
            report["status"] = "running"
        report["apk_fingerprint"] = local_fingerprint
        report["last_error"] = None

        heartbeat = CheckpointHeartbeat(
            report_dir,
            report,
            interval_seconds=resume_options["checkpoint_interval_seconds"],
            cycle_seconds=cycle_seconds,
            total_seconds=total_seconds,
            apk_fingerprint=local_fingerprint,
            report_lock=_report_write_lock(report_dir),
            foreground_recovery_strategy=_foreground_recovery_strategy(
                state_manager
            )
            if state_manager is not None
            else str(
                report.get("foreground_recovery_strategy")
                or "standard_foreground_recovery"
            ),
        )
        heartbeat.update(
            resume_state="resuming" if is_resume else "running",
            resume_attempts=report["resume_attempts"],
            resume_attempt_reserved=False,
            phase_name="resuming" if is_resume else "initializing",
            write=False,
        )
        heartbeat.start()

        _raise_if_device_manually_ended(
            report, report_dir, run_id, device_serial
        )
        _check_cancel(cancel_event)
        try:
            device_ready = bool(DeviceManager.is_device_ready(device_serial))
        except Exception:
            device_ready = False
        if not device_ready:
            if not is_resume or (
                not resume_options["enabled"] and not manual_resume_request
            ):
                # Preserve the fast-fail contract for a brand-new run. Only a
                # worker that has durable progress may wait for a reconnect.
                raise RuntimeError(
                    "device is not ready or authorized: %s" % device_serial
                )
            report["resume_state"] = "waiting_for_device"
            report["status"] = "waiting_for_device"
            report["finished_at"] = None
            report["last_error"] = (
                "device is not ready or authorized: %s" % device_serial
            )
            heartbeat.update(
                resume_state="waiting_for_device",
                phase_name="waiting_for_device",
                waiting_for_device=True,
                last_error=report["last_error"],
            )
            _persist_report(report, report_dir)
            if not _wait_for_device_ready(
                device_serial,
                resume_options["wait_for_device_seconds"],
                cancel_event=cancel_event,
                heartbeat=heartbeat,
                report=report,
                wait_started_at=persisted_wait_started_at,
                wait_deadline_at=persisted_wait_deadline_at,
            ):
                raise RuntimeError(
                    "wait_for_device_timeout: device %s was not reconnected "
                    "within %s seconds"
                    % (device_serial, resume_options["wait_for_device_seconds"])
                )
            _raise_if_device_manually_ended(
                report, report_dir, run_id, device_serial
            )
            report["resume_state"] = "resuming"
            report["status"] = "resuming"
            report["last_error"] = None
            report["waiting_for_device"] = False
            heartbeat.update(
                resume_state="resuming",
                phase_name="resuming",
                waiting_for_device=False,
                last_error=None,
            )
            _persist_report(report, report_dir)
        report["device_info"] = DeviceManager.get_device_info(device_serial)
        package_name = str(worker_config["app"]["package_name"])
        state_manager = StateManager(device_serial, package_name)
        executor = TestExecutor(device_serial, package_name, worker_config)
        mutex = MutexController(device_serial)
        strategy = _foreground_recovery_strategy(state_manager)
        report["foreground_recovery_strategy"] = strategy
        if heartbeat is not None:
            heartbeat.update(
                foreground_recovery_strategy=strategy,
                write=False,
            )
            # The first heartbeat is normally written before the uiautomator2
            # connection is created so an offline device is visible quickly.
            # Flush once after identity detection as well, ensuring a Vivo
            # X80's specialized strategy is present even if the worker is
            # interrupted before the periodic thread gets another turn.
            heartbeat.flush()
        _raise_if_device_manually_ended(
            report, report_dir, run_id, device_serial
        )
        _run_pressure_preflight(executor, report)
        _persist_report(report, report_dir)
        _raise_if_device_manually_ended(
            report, report_dir, run_id, device_serial
        )

        if not is_resume:
            if not _install_apk_for_device(
                state_manager, apk_path, reinstall=False
            ):
                raise RuntimeError("Unable to install local APK %s" % apk_path)
        else:
            identity = _device_apk_identity(device_serial, package_name)
            # Resuming an existing task must preserve the installed app/data.
            # The only authoritative reasons to install are a missing package
            # or a confirmed fingerprint mismatch.  Keep the legacy option in
            # the parsed config for compatibility, but do not let it turn a
            # matching APK into a fresh install.
            reinstall = False
            reinstall_reason = "identity_matches"
            if not identity.get("installed"):
                reinstall = True
                reinstall_reason = "package_missing"
            elif (
                identity.get("fingerprint_supported")
                and identity.get("fingerprint")
                and identity.get("fingerprint") != local_fingerprint
            ):
                reinstall = True
                reinstall_reason = "device_fingerprint_mismatch"
            elif not identity.get("fingerprint_supported"):
                reinstall_reason = "fingerprint_unavailable"
            report["resume_apk_check"] = {
                **identity,
                "local_fingerprint": local_fingerprint,
                "reinstall": reinstall,
                "reason": reinstall_reason if reinstall else "identity_matches",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if reinstall and not _install_apk_for_device(
                state_manager, apk_path, reinstall=True
            ):
                raise RuntimeError(
                    "Unable to reinstall local APK after %s" % reinstall_reason
                )
            if reinstall:
                verified = _device_apk_identity(device_serial, package_name)
                if (
                    verified.get("fingerprint_supported")
                    and verified.get("fingerprint") != local_fingerprint
                ):
                    raise RuntimeError(
                        "APK fingerprint mismatch after reinstall: expected=%s actual=%s"
                        % (local_fingerprint, verified.get("fingerprint"))
                    )

        # A stability run starts fully permissioned: an unanswered system prompt
        # blocks random input, and one accidental "deny" hides whole feature
        # branches.  ``pm install -g`` already granted the declared dangerous
        # permissions; this covers explicit lists, appops-only switches, and
        # OEM verifier settings.  Failures are recorded, never swallowed.
        permission_options: Dict[str, Any] = {}
        test_section = worker_config.get("test")
        if isinstance(test_section, Mapping):
            permission_options = {
                "grant_permissions": list(
                    test_section.get("grant_permissions") or []
                ),
                "appops_permissions": dict(
                    test_section.get("appops_permissions") or {}
                ),
                "disable_package_verifier": bool(
                    test_section.get("disable_package_verifier", False)
                ),
            }
        permission_applier = getattr(
            state_manager, "apply_device_permissions", None
        )
        if callable(permission_applier) and any(
            (
                permission_options.get("grant_permissions"),
                permission_options.get("appops_permissions"),
                permission_options.get("disable_package_verifier"),
            )
        ):
            try:
                report["device_permissions"] = permission_applier(
                    permission_options
                )
                _persist_report(report, report_dir)
            except Exception as exc:
                logger.warning(
                    "Device permission setup failed on %s: %s",
                    device_serial,
                    exc,
                )

        completed = completed_cycle_numbers(report)
        for cycle in range(1, max_cycles + 1):
            # Refresh durable state for every round.  A previous invocation or
            # the heartbeat thread may have committed a completed row after
            # this worker loaded its initial snapshot.
            checkpoint_value = _reload_durable_cycle_state(
                report,
                report_dir,
                run_id,
                device_serial,
                checkpoint_value,
            )
            _raise_if_device_manually_ended(
                report, report_dir, run_id, device_serial
            )
            completed = completed_cycle_numbers(report)
            if cycle in completed:
                logger.info(
                    "Skipping immutable completed cycle %s on %s",
                    cycle,
                    device_serial,
                )
                continue
            current_cycle_number = cycle
            context = _resume_context_for_cycle(
                report,
                checkpoint_value,
                cycle,
                bool(resume_options["resume_partial_cycle"]),
            )
            resume_requested = bool(context.get("resume_requested"))
            heartbeat.begin_cycle(
                cycle,
                elapsed_seconds=float(context.get("elapsed_seconds", 0) or 0),
                operation_count=int(context.get("operation_count", 0) or 0),
                phase=int(context.get("phase", 0) or 0),
                started_at=str(context.get("cycle_started_at") or "") or None,
                resuming=resume_requested,
                pressure_phases=context.get("pressure_phases", []),
            )
            report["resume_state"] = "running"
            report["status"] = "running"
            heartbeat.update(resume_state="running")
            _check_cancel(cancel_event)
            preparation_retries = 0
            _test_section = worker_config.get("test")
            _retry_option = (
                _test_section.get("preparation_retries", 3)
                if isinstance(_test_section, Mapping)
                else 3
            )
            try:
                max_preparation_retries = max(0, int(_retry_option))
            except (TypeError, ValueError, OverflowError):
                max_preparation_retries = 3
            while True:
                cycle_report = _run_cycle(
                    state_manager,
                    executor,
                    _new_crash_handler(device_serial, package_name, worker_config),
                    mutex,
                    worker_config,
                    report,
                    report_dir,
                    cycle,
                    cancel_event,
                    resume_context=context,
                    checkpoint=heartbeat,
                    resume_requested=resume_requested,
                )
                if (
                    not _is_preparation_failure(cycle_report)
                    or preparation_retries >= max_preparation_retries
                ):
                    break
                preparation_retries += 1
                logger.warning(
                    "Preparation failed on %s (attempt %s/%s): %s; retrying the "
                    "cycle from a clean reset",
                    device_serial,
                    preparation_retries,
                    max_preparation_retries,
                    "; ".join(str(item) for item in cycle_report.get("errors") or []),
                )
                if cancel_event is not None and getattr(
                    cancel_event, "is_set", lambda: False
                )():
                    break
                time.sleep(2)
            if preparation_retries:
                cycle_report["preparation_retries"] = preparation_retries
            _raise_if_device_manually_ended(
                report, report_dir, run_id, device_serial
            )
            cycle_status = str(cycle_report.get("status") or "failed")
            decision = classify_recovery(
                cycle_report,
                manual_stop=cycle_status == "interrupted",
            )
            # A terminal cycle failure can still be a successful run-level
            # handoff: the failed cycle is durably recorded and the next
            # configured cycle starts from a fresh app reset.  Do not let the
            # generic recovery classifier turn that explicit decision into a
            # manual-resume pause.
            continue_after_cycle_failure = bool(
                cycle_report.get("continue_run")
                and cycle_report.get("recoverable_cycle")
                and cycle_report.get("recoverable") is False
            )
            should_pause_cycle = cycle_status == "interrupted" or (
                cycle_status == "failed"
                and not continue_after_cycle_failure
                and str(decision.get("state")) in (
                    AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES
                )
            )
            if should_pause_cycle:
                checkpoint_snapshot = heartbeat.snapshot()
                partial = copy.deepcopy(cycle_report)
                partial["completed"] = False
                partial["recoverable"] = bool(decision.get("recoverable"))
                partial["status"] = str(decision.get("state") or cycle_status)
                partial["pressure_elapsed_seconds"] = max(
                    float(partial.get("pressure_elapsed_seconds", 0) or 0),
                    float(checkpoint_snapshot.get("elapsed_seconds", 0) or 0),
                )
                partial["operation_count"] = max(
                    int(partial.get("operation_count", 0) or 0),
                    int(checkpoint_snapshot.get("operation_count", 0) or 0),
                )
                report["current_cycle"] = partial
                error = "; ".join(
                    str(item)
                    for item in cycle_report.get("errors", [])
                    if str(item).strip()
                ) or str(cycle_report.get("abort_reason") or decision.get("reason"))
                evidence_directory = _save_recovery_attempt_evidence(
                    report_dir,
                    int(report.get("resume_attempts", 0) or 0),
                    decision,
                    error,
                    checkpoint_snapshot,
                    state_manager,
                    executor,
                    mutex,
                )
                if evidence_directory is not None:
                    recovery_evidence_directories.append(evidence_directory)
                _record_resume_interruption(report, decision, error, cycle)
                heartbeat.update(
                    resume_state=report["resume_state"],
                    elapsed_seconds=partial["pressure_elapsed_seconds"],
                    operation_count=partial["operation_count"],
                    last_error=error,
                    phase_name=report["resume_state"],
                    running=False,
                )
                result_decision = dict(decision)
                break

            upsert_completed_cycle(report, cycle_report)
            report.pop("current_cycle", None)
            completed.add(cycle)
            checkpoint_value = None
            heartbeat.update(
                cycle=0,
                phase=0,
                phase_name="between_cycles",
                elapsed_seconds=0,
                operation_count=0,
                fastbot_pid=None,
                remote_log_path=None,
                remote_log_offset=0,
                last_error=None,
                running=False,
            )
            _persist_report(report, report_dir)
            if cycle_report.get("abort_run"):
                abort_reason = str(
                    cycle_report.get("abort_reason")
                    or "aborting remaining cycles because device control could not be recovered"
                )
                report.setdefault("errors", []).append(abort_reason)
                break
            if cycle_status == "crash" and bool(
                worker_config.get("test", {}).get("stop_on_crash", False)
            ):
                report.setdefault("errors", []).append(
                    "pressure stopped after target-app crash on cycle %s" % cycle
                )
                break

        _raise_if_device_manually_ended(
            report, report_dir, run_id, device_serial
        )
        if report.get("resume_state") in AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES:
            terminal = False
        else:
            statuses = [str(cycle.get("status")) for cycle in report["cycles"]]
            if any(status == "failed" for status in statuses):
                report["status"] = "completed_with_errors"
            elif report.get("crashes"):
                report["status"] = "completed_with_crashes"
            else:
                report["status"] = "completed"
            report["resume_state"] = "completed"
            report["remaining_seconds"] = 0
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            report["last_error"] = None
            report.pop("current_cycle", None)
            result_decision = {
                "state": "completed",
                "recoverable": False,
                "automatic": False,
                "category": "completed",
                "reason": "pressure budget completed",
            }
            terminal = True
    except _DeviceManuallyEnded:
        # The web service already persisted the authoritative terminal
        # report/checkpoint.  Keep cleanup best effort and return without
        # manufacturing another interruption or retry attempt.
        _refresh_durable_manual_end(
            report, report_dir, run_id, device_serial
        )
        result_decision = {
            "state": MANUALLY_ENDED_STATE,
            "recoverable": False,
            "automatic": False,
            "category": "manual_end",
            "reason": str(
                report.get("recovery_reason")
                or report.get("last_error")
                or "device test was ended manually"
            ),
        }
        terminal = True
    except (InterruptedError, KeyboardInterrupt) as exc:
        result_decision = classify_recovery(exc, manual_stop=True)
        checkpoint_snapshot = heartbeat.snapshot() if heartbeat is not None else load_checkpoint(
            report_dir, run_id=run_id, device_serial=device_serial
        )
        evidence_directory = _save_recovery_attempt_evidence(
            report_dir,
            int(report.get("resume_attempts", 0) or 0),
            result_decision,
            "test run cancelled",
            checkpoint_snapshot,
            state_manager,
            executor,
            mutex,
        )
        if evidence_directory is not None:
            recovery_evidence_directories.append(evidence_directory)
        _record_resume_interruption(
            report,
            result_decision,
            "test run cancelled",
            current_cycle_number,
        )
    except Exception as exc:
        logger.exception("Test run failed on %s", device_serial)
        result_decision = classify_recovery(exc)
        if resume_options["enabled"] and result_decision.get("state") in (
            AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES
        ):
            checkpoint_snapshot = heartbeat.snapshot() if heartbeat is not None else load_checkpoint(
                report_dir, run_id=run_id, device_serial=device_serial
            )
            evidence_directory = _save_recovery_attempt_evidence(
                report_dir,
                int(report.get("resume_attempts", 0) or 0),
                result_decision,
                str(exc),
                checkpoint_snapshot,
                state_manager,
                executor,
                mutex,
            )
            if evidence_directory is not None:
                recovery_evidence_directories.append(evidence_directory)
            _record_resume_interruption(
                report, result_decision, str(exc), current_cycle_number
            )
        else:
            report["status"] = "failed"
            report["resume_state"] = "failed"
            report["last_error"] = str(exc)
            errors = report.setdefault("errors", [])
            if isinstance(errors, list) and str(exc) not in errors:
                errors.append(str(exc))
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            result_decision["state"] = "failed"
            terminal = True
    finally:
        try:
            cleanup_error = _cleanup_device_control(
                report,
                executor,
                mutex,
                state_manager,
                device_serial,
                publish_failure=False,
            )
            manually_ended = _refresh_durable_manual_end(
                report, report_dir, run_id, device_serial
            )
            if cleanup_error and not manually_ended:
                checkpoint_snapshot = (
                    heartbeat.snapshot()
                    if heartbeat is not None
                    else load_checkpoint(
                        report_dir, run_id=run_id, device_serial=device_serial
                    )
                )
                cleanup_decision = _record_recoverable_cleanup_failure(
                    report, cleanup_error, current_cycle_number
                )
                evidence_directory = _save_recovery_attempt_evidence(
                    report_dir,
                    int(report.get("resume_attempts", 0) or 0),
                    cleanup_decision,
                    cleanup_error,
                    checkpoint_snapshot,
                    state_manager,
                    executor,
                    mutex,
                )
                if evidence_directory is not None:
                    recovery_evidence_directories.append(evidence_directory)
                result_decision = cleanup_decision
                terminal = False
            _finalize_recovery_attempt_evidence(
                recovery_evidence_directories,
                state_manager=state_manager,
                executor=executor,
                mutex=mutex,
            )
            if str(report.get("resume_state") or "").strip().lower() in MANUAL_RECOVERY_STATES:
                report["resume_attempt_reserved"] = False
                pending_checkpoint = load_checkpoint(
                    report_dir, run_id=run_id, device_serial=device_serial
                )
                if isinstance(pending_checkpoint, Mapping) and not terminal_resume_state(
                    pending_checkpoint
                ):
                    pending_checkpoint = dict(pending_checkpoint)
                    pending_checkpoint["resume_attempt_reserved"] = False
                    try:
                        write_checkpoint_json(
                            report_dir / "checkpoint.json", pending_checkpoint
                        )
                    except Exception:
                        logger.debug(
                            "Unable to clear resume reservation on %s",
                            device_serial,
                            exc_info=True,
                        )
            _apply_report_coverage(report, worker_config)
            if heartbeat is not None:
                try:
                    heartbeat.stop(str(report.get("resume_state") or "failed"))
                except Exception:
                    logger.exception(
                        "Unable to persist final checkpoint on %s", device_serial
                    )
            if _refresh_durable_manual_end(
                report, report_dir, run_id, device_serial
            ):
                result_decision = {
                    "state": MANUALLY_ENDED_STATE,
                    "recoverable": False,
                    "automatic": False,
                    "category": "manual_end",
                    "reason": str(
                        report.get("recovery_reason")
                        or report.get("last_error")
                        or "device test was ended manually"
                    ),
                }
                terminal = True
            if terminal and not report.get("finished_at"):
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
            _persist_report(report, report_dir)
        finally:
            close_logging_handlers()

    return {
        "device_serial": device_serial,
        "status": report["status"],
        "resume_state": report.get("resume_state"),
        "resume_attempts": report.get("resume_attempts", 0),
        "remaining_seconds": report.get("remaining_seconds", 0),
        "last_heartbeat": report.get("last_heartbeat"),
        "recoverable": bool(result_decision.get("recoverable")),
        "automatic_resume": bool(result_decision.get("automatic")),
        "recovery_category": result_decision.get("category"),
        "recovery_reason": result_decision.get("reason"),
        "coverage_status": report.get("coverage", {}).get("status")
        if isinstance(report.get("coverage"), Mapping)
        else None,
        "report_dir": str(report_dir),
        "report": report,
    }


def prepare_device(device_serial: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    """Run only local APK installation and the uiautomator2 preparation stage."""
    require_local_apk(config)
    if not DeviceManager.is_device_ready(device_serial):
        raise RuntimeError("device is not ready or authorized: %s" % device_serial)
    package_name = str(config["app"]["package_name"])
    state_manager = StateManager(device_serial, package_name)
    mutex = MutexController(device_serial)
    try:
        if not mutex.acquire("uiautomator2", state_manager.get_device()):
            raise RuntimeError("uiautomator2 control unavailable")
        preparation_error: Optional[BaseException] = None
        try:
            scenario, snapshot = _prepare_state(state_manager, config)
        except BaseException as exc:
            preparation_error = exc
            raise
        finally:
            release_error: Optional[BaseException] = None
            try:
                if mutex.release("uiautomator2", state_manager.get_device()) is False:
                    release_error = RuntimeError(
                        "uiautomator2 control release returned false during device preparation"
                    )
            except BaseException as exc:
                release_error = exc
            if release_error is not None:
                if preparation_error is None:
                    raise RuntimeError(
                        "uiautomator2 control release failed during device preparation"
                    ) from release_error
                logger.exception(
                    "Unable to release uiautomator2 after preparation failure",
                    exc_info=(
                        type(release_error),
                        release_error,
                        release_error.__traceback__,
                    ),
                )
        return {"device_serial": device_serial, "scenario": scenario, "snapshot": snapshot}
    finally:
        # Leave the app on the prepared page for a following ``fastbot`` call.
        close_logging_handlers()


def _close_fastbot_only_exception_cycle(
    report: Dict[str, Any],
    report_dir: Path,
    state_manager: Any,
    executor: Any,
    mutex: Any,
    config: Mapping[str, Any],
    trajectory: Dict[str, Any],
    artifacts: Dict[str, Any],
    error: BaseException,
    *,
    interrupted: bool,
    existing_cycle: Optional[Dict[str, Any]] = None,
    phase_attempted: bool = False,
) -> str:
    """Persist a complete standalone cycle even when its phase raised."""
    error_text = str(error).strip() or type(error).__name__
    try:
        recovered, recovered_log, _recovered_pressure, recovered_artifacts = (
            _phase_failure_snapshot(
                error,
                state_manager=state_manager,
                executor=executor,
                report_dir=report_dir,
                cycle=1,
                trajectory=trajectory,
                artifacts=artifacts,
                phase_attempted=phase_attempted,
            )
        )
        trajectory.clear()
        trajectory.update(recovered)
        artifacts.update(recovered_artifacts)
        phase_log_content = recovered_log
    except Exception as snapshot_error:
        phase_log_content = ""
        trajectory.setdefault("artifact_errors", []).append(
            "phase failure snapshot: %s" % snapshot_error
        )
    trajectory.setdefault("exception_type", type(error).__name__)
    trajectory.setdefault("exception", error_text)
    trajectory.setdefault("engine", getattr(executor, "pressure_engine", "fastbot"))

    # A standalone invocation can also fail before its pressure phase starts;
    # only import executor-side detections when that phase actually ran.
    if phase_attempted or isinstance(
        getattr(error, "_phase_failure_context", None), Mapping
    ):
        for attribute, key in (
            ("app_crash_detection", "app_crash"),
            ("app_process_exit_detection", "app_process_exit"),
            ("device_disconnect_detection", "device_disconnect"),
            ("app_crash_page_detection", "app_crash_page"),
        ):
            try:
                value = getattr(executor, attribute, None)
                value = value() if callable(value) else value
            except Exception:
                value = None
            if isinstance(value, Mapping) and key not in trajectory:
                trajectory[key] = dict(value)

    attached_release_error = getattr(error, "_control_release_error", None)
    if attached_release_error:
        _mark_control_release_failure(executor, trajectory, attached_release_error)

    normalized_error = error_text.lower()
    device_disconnected = bool(
        isinstance(trajectory.get("device_disconnect"), Mapping)
        or any(
            marker in normalized_error
            for marker in (
                "device disconnected",
                "device is offline",
                "device not found",
                "transport error",
                "no devices/emulators found",
            )
        )
    )
    verifier = getattr(executor, "verify_pressure_engine_stopped", None)
    _verify_cycle_pressure_handoff(
        executor, trajectory, force=callable(verifier)
    )
    if device_disconnected:
        trajectory["control_handoff_safe"] = False

    log_path = report_dir / "logcat.log"
    pressure_path = report_dir / "fastbot.log"
    # A preflight failure has no current pressure phase.  Reading the complete
    # report directory here would classify a stale crash from an earlier run as
    # belonging to this invocation.  A phase-attached context remains valid
    # even if the explicit flag was omitted by an older caller.
    has_phase_context = isinstance(
        getattr(error, "_phase_failure_context", None), Mapping
    )
    if phase_attempted or has_phase_context:
        log_content = phase_log_content or _read_log_segment(log_path, 0)
    else:
        log_content = phase_log_content
    if log_path.is_file():
        artifacts.setdefault("logcat", str(log_path))
    if pressure_path.is_file():
        artifacts.setdefault("fastbot", str(pressure_path))
    try:
        artifacts["trajectory"] = _write_trajectory(trajectory, report_dir, 1)
    except Exception as exc:
        logger.warning(
            "Unable to persist standalone failure trajectory on %s",
            report.get("device_serial"),
            exc_info=True,
        )
        trajectory.setdefault("artifact_errors", []).append(
            "trajectory: %s" % exc
        )

    phase = trajectory.get("phase", {})
    if not isinstance(phase, Mapping):
        phase = {}
    cycle_report = existing_cycle if isinstance(existing_cycle, dict) else {
        "cycle": 1,
        "started_at": phase.get("started_at") or report.get("started_at"),
        "operation_count": max(
            0, int(trajectory.get("operation_count", 0) or 0)
        ),
        "pressure_elapsed_seconds": float(
            trajectory.get("pressure_elapsed_seconds", 0)
            or phase.get("engine_elapsed_seconds", 0)
            or 0
        ),
        "pressure_phases": list(trajectory.get("pressure_phases", [])),
        "engine": trajectory.get("engine", "fastbot"),
        "errors": [],
        "artifacts": {},
    }
    cycle_report["finished_at"] = datetime.now(timezone.utc).isoformat()
    cycle_report.setdefault("errors", [])
    cycle_report.setdefault("artifacts", {}).update(artifacts)
    cycle_report["operation_count"] = max(
        int(cycle_report.get("operation_count", 0) or 0),
        int(trajectory.get("operation_count", 0) or 0),
    )
    if interrupted:
        cycle_report["status"] = "interrupted"
        if error_text not in cycle_report["errors"]:
            cycle_report["errors"].append(error_text)
        report["status"] = "interrupted"
    else:
        _mark_cycle_failure(
            cycle_report,
            executor,
            trajectory,
            error_text,
            abort_reason=_abort_remaining_cycles_message(error, executor),
        )
        if device_disconnected:
            cycle_report["recoverable_cycle"] = False
            cycle_report["continue_run"] = False
            cycle_report["abort_run"] = True
            cycle_report["abort_reason"] = (
                "aborting remaining cycles because the device disconnected"
            )
        report["status"] = "failed"
    report_errors = report.setdefault("errors", [])
    if isinstance(report_errors, list) and error_text not in report_errors:
        report_errors.append(error_text)
    _copy_cycle_handoff_fields(cycle_report, trajectory, executor)

    crash_handler = _new_crash_handler(
        str(report.get("device_serial") or ""),
        str(report.get("package_name") or ""),
        config,
    )
    page_detection = (
        trajectory.get("app_crash_page")
        if isinstance(trajectory.get("app_crash_page"), Mapping)
        else None
    )
    page_evidence: Optional[Dict[str, Any]] = None
    if isinstance(page_detection, Mapping):
        page_evidence = _capture_app_crash_page_evidence(
            state_manager,
            mutex,
            crash_handler,
            report_dir,
            1,
            page_detection,
            allow_device_access=not _cycle_hard_control_failure(
                executor, trajectory
            ),
            executor=executor,
        )
        if isinstance(page_evidence, Mapping):
            trajectory["app_crash_page"] = dict(page_evidence)
            cycle_report["app_crash_page"] = dict(page_evidence)
            artifacts.update(page_evidence.get("artifacts", {}))
            cycle_report["artifacts"].update(page_evidence.get("artifacts", {}))

    crash = _detect_cycle_crash(
        crash_handler,
        log_content,
        page_detection,
        page_evidence,
        trajectory.get("app_process_exit")
        if isinstance(trajectory.get("app_process_exit"), Mapping)
        else None,
        trajectory.get("app_crash")
        if isinstance(trajectory.get("app_crash"), Mapping)
        else None,
    )
    if isinstance(crash, Mapping):
        crash_info = dict(crash)
        cycle_report["crash_detected"] = True
        trajectory.setdefault("stop_reason", _WAIT_CRASH)
        try:
            crash_trajectory = _write_crash_trajectory(
                trajectory,
                report_dir,
                1,
                crash_info.get("phase"),
                1,
                config.get("crash_replay", {})
                if isinstance(config.get("crash_replay", {}), Mapping)
                else {},
                crash_info.get("detected_at"),
            )
            artifacts["crash_trajectory"] = crash_trajectory
            cycle_report["artifacts"]["crash_trajectory"] = crash_trajectory
        except Exception:
            crash_trajectory = artifacts.get("trajectory")
            logger.warning(
                "Unable to persist standalone crash trajectory on %s",
                report.get("device_serial"),
                exc_info=True,
            )
        crash_info.update(
            {
                "cycle": 1,
                "trajectory_path": crash_trajectory,
                "reproducible": False,
                "replay_attempts": [],
                "replay_note": "Fastbot-only mode failed before replay preparation",
            }
        )
        _add_crash(report, crash_info, 1)
        if crash_info.get("signature"):
            cycle_report["crash_signature"] = str(crash_info["signature"])
        cycle_report["runtime_failure_count"] = max(
            1, int(cycle_report.get("runtime_failure_count", 0) or 0)
        )

        # Preserve an application crash classification when the surrounding
        # phase raised during cleanup/evidence collection.  A safe standalone
        # run has no later cycle to resume, but the cycle row and run status
        # must still distinguish a real app crash from an infrastructure
        # failure.
        if (
            not interrupted
            and not device_disconnected
            and _cycle_control_handoff_safe(executor, trajectory)
        ):
            cycle_report["status"] = "crash"
            cycle_report["recoverable"] = False
            stop_on_crash = bool(
                isinstance(config.get("test", {}), Mapping)
                and config.get("test", {}).get("stop_on_crash", False)
            )
            cycle_report["recoverable_cycle"] = not stop_on_crash
            cycle_report["continue_run"] = not stop_on_crash
            if stop_on_crash:
                cycle_report["abort_run"] = True
                cycle_report["abort_reason"] = (
                    "aborting remaining cycles because stop_on_crash is enabled"
                )
            else:
                cycle_report.pop("abort_run", None)
                cycle_report.pop("abort_reason", None)
            report["status"] = "completed_with_crashes"

    cycles = report.setdefault("cycles", [])
    if isinstance(cycles, list):
        replaced = False
        for index, item in enumerate(cycles):
            if isinstance(item, Mapping) and int(item.get("cycle", 0) or 0) == 1:
                cycles[index] = cycle_report
                replaced = True
                break
        if not replaced:
            cycles.append(cycle_report)
    report.setdefault("artifacts", {}).update(artifacts)
    return log_content


def run_fastbot_only(
    device_serial: str,
    config: Mapping[str, Any],
    duration_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Run Fastbot against a prepared local app while preserving the mutex rule."""
    require_local_apk(config)
    if not DeviceManager.is_device_ready(device_serial):
        raise RuntimeError("device is not ready or authorized: %s" % device_serial)
    worker_config = copy.deepcopy(dict(config))
    # Keep the standalone command within the local-APK contract even though
    # preparation may have happened in an earlier command.
    require_local_apk(worker_config)
    if duration_minutes is not None:
        if int(duration_minutes) <= 0:
            raise ValueError("duration must be positive")
        worker_config.setdefault("test", {})["fastbot_duration_minutes"] = int(
            duration_minutes
        )
    run_id = utc_timestamp()
    report_dir = report_directory(worker_config, run_id, device_serial)
    worker_config["_report_dir"] = str(report_dir)
    package_name = str(worker_config["app"]["package_name"])
    state_manager = StateManager(device_serial, package_name)
    executor = TestExecutor(device_serial, package_name, worker_config)
    mutex = MutexController(device_serial)
    report = _initial_report(worker_config, run_id, device_serial, report_dir)
    try:
        report["device_info"] = DeviceManager.get_device_info(device_serial)
    except RuntimeError as exc:
        report["device_info"] = {"serial": device_serial, "error": str(exc)}
    _persist_report(report, report_dir)
    trajectory: Dict[str, Any] = {}
    log_content = ""
    artifacts: Dict[str, Any] = {}
    cycle_report: Optional[Dict[str, Any]] = None
    phase_attempted = False
    result: Dict[str, Any] = {}
    try:
        _run_pressure_preflight(executor, report)
        _persist_report(report, report_dir)
        # Mark this immediately before entering the pressure runner.  A
        # preflight exception must not inspect log files left by an older run.
        phase_attempted = True
        trajectory, log_content, artifacts = _run_fastbot_phase(
            state_manager,
            executor,
            mutex,
            worker_config,
            report_dir,
            1,
        )
        runtime_failures = [
            dict(item)
            for item in trajectory.get("runtime_failures", [])
            if isinstance(item, Mapping)
        ]
        page_detection = trajectory.get("app_crash_page")
        phase_crash_handler = _new_crash_handler(
            device_serial, package_name, worker_config
        )
        logcat_crash_detected = (
            phase_crash_handler.detect_crash(log_content) is not None
        )
        page_evidence: Optional[Dict[str, Any]] = None
        if isinstance(page_detection, Mapping) or (
            not runtime_failures
            and (trajectory.get("stopped_on_crash") or logcat_crash_detected)
        ):
            page_evidence = _capture_app_crash_page_evidence(
                state_manager,
                mutex,
                phase_crash_handler,
                report_dir,
                1,
                page_detection if isinstance(page_detection, Mapping) else None,
                executor=executor,
            )
            if page_evidence is not None:
                _merge_handoff_fields(trajectory, page_evidence)
                trajectory["app_crash_page"] = dict(page_evidence)
        trajectory.setdefault(
            "engine", getattr(executor, "pressure_engine", "fastbot")
        )
        if _cycle_hard_control_failure(executor, trajectory):
            trajectory.setdefault(
                "stall_recovery_skipped_reason",
                "skipped because device-control handoff is unsafe",
            )
        else:
            _invoke_stall_recovery(
                trajectory,
                state_manager,
                mutex,
                worker_config,
                executor=executor,
            )
        trajectory_path = _write_trajectory(trajectory, report_dir, 1)
        artifacts["trajectory"] = trajectory_path
        phase_details = trajectory.get("phase", {})
        if not isinstance(phase_details, Mapping):
            phase_details = {}
        cycle_report: Dict[str, Any] = {
            "cycle": 1,
            "started_at": phase_details.get("started_at") or report["started_at"],
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed",
            "operation_count": max(
                0, int(trajectory.get("operation_count", 0) or 0)
            ),
            "artifacts": dict(artifacts),
            "errors": [],
            "engine": trajectory.get("engine", "fastbot"),
            "foreground_restore_count": int(
                trajectory.get("foreground_restore_count", 0) or 0
            ),
            "permission_dialog_count": int(
                trajectory.get("permission_dialog_count", 0) or 0
            ),
            "audit_recovery_count": int(
                trajectory.get("audit_recovery_count", 0) or 0
            ),
            "foreground_recovery_count": int(
                trajectory.get("foreground_recovery_count", 0) or 0
            ),
            "pressure_engine_failure_count": int(
                trajectory.get("pressure_engine_failure_count", 0) or 0
            ),
            "pressure_engine_recovery_count": int(
                trajectory.get("pressure_engine_recovery_count", 0) or 0
            ),
            "stall_detection_count": int(
                trajectory.get("stall_detection_count", 0) or 0
            ),
            "stall_recovery_requested_count": int(
                trajectory.get("stall_recovery_requested_count", 0) or 0
            ),
            "stall_recovery_count": int(
                trajectory.get("stall_recovery_count", 0) or 0
            ),
            "stall_recovery_skipped_count": int(
                trajectory.get("stall_recovery_skipped_count", 0) or 0
            ),
            "stall_recovery_failure_count": int(
                trajectory.get("stall_recovery_failure_count", 0) or 0
            ),
            "pressure_phases": list(
                trajectory.get("pressure_phases", [])
            ) or ([dict(phase_details)] if phase_details else []),
            "pressure_elapsed_seconds": float(
                trajectory.get("pressure_elapsed_seconds", 0)
                or phase_details.get("engine_elapsed_seconds", 0)
                or 0
            ),
        }
        if runtime_failures:
            cycle_report["runtime_failures"] = runtime_failures
            cycle_report["runtime_failure_count"] = len(runtime_failures)
            cycle_report["runtime_recovery_count"] = int(
                trajectory.get("runtime_recovery_count", 0) or 0
            )
        if trajectory.get("stall_events"):
            cycle_report["stall_events"] = list(trajectory["stall_events"])
        if isinstance(trajectory.get("stall_recovery"), Mapping):
            cycle_report["stall_recovery"] = dict(trajectory["stall_recovery"])
        if trajectory.get("collection_warnings"):
            cycle_report["collection_warnings"] = list(
                trajectory["collection_warnings"]
            )
        if trajectory.get("foreground_recoveries"):
            cycle_report["foreground_recoveries"] = list(
                trajectory["foreground_recoveries"]
            )
        if trajectory.get("pressure_engine_failures"):
            cycle_report["pressure_engine_failures"] = list(
                trajectory["pressure_engine_failures"]
            )
        if trajectory.get("coverage_rate") is not None:
            cycle_report["coverage_rate"] = trajectory["coverage_rate"]
        if trajectory.get("stop_reason"):
            cycle_report["stop_reason"] = str(trajectory["stop_reason"])
        _copy_cycle_handoff_fields(cycle_report, trajectory, executor)
        crash_handler = _new_crash_handler(device_serial, package_name, worker_config)
        crash_candidates: List[Dict[str, Any]] = []
        for failure in runtime_failures:
            candidate = failure.get("crash")
            item = (
                dict(candidate)
                if _is_confirmed_app_crash(candidate)
                else {}
            )
            if item:
                item["phase"] = failure.get("phase")
                item["runtime_failure"] = failure
                crash_candidates.append(item)
        if not crash_candidates:
            fallback = _detect_cycle_crash(
                crash_handler,
                log_content,
                page_detection if isinstance(page_detection, Mapping) else None,
                page_evidence,
                trajectory.get("app_process_exit")
                if isinstance(trajectory.get("app_process_exit"), Mapping)
                else None,
                trajectory.get("app_crash")
                if isinstance(trajectory.get("app_crash"), Mapping)
                else None,
            )
            if fallback is not None:
                crash_candidates.append(dict(fallback))
        crash: Optional[Dict[str, Any]] = None
        if crash_candidates:
            crash_trajectory_path = report_dir / "crash_trajectory.json"
            ReportGenerator.write_json(trajectory, crash_trajectory_path)
            artifacts["crash_trajectory"] = str(crash_trajectory_path)
            if page_evidence is not None:
                cycle_report["app_crash_page"] = dict(page_evidence)
                artifacts.update(page_evidence.get("artifacts", {}))
            cycle_report["artifacts"] = dict(artifacts)
        crash_signatures: List[str] = []
        for candidate in crash_candidates:
            crash = dict(candidate)
            phase_value = crash.get("phase")
            runtime_failure = crash.get("runtime_failure")
            if phase_value is None and isinstance(runtime_failure, Mapping):
                phase_value = runtime_failure.get("phase")
            exact_trajectory_path = None
            if isinstance(runtime_failure, Mapping):
                evidence = runtime_failure.get("evidence", {})
                if isinstance(evidence, Mapping):
                    evidence_artifacts = evidence.get("artifacts", {})
                    if isinstance(evidence_artifacts, Mapping):
                        value = evidence_artifacts.get("trajectory")
                        if value and Path(str(value)).is_file():
                            exact_trajectory_path = str(value)
            if exact_trajectory_path is None:
                exact_trajectory_path = _write_crash_trajectory(
                    trajectory,
                    report_dir,
                    1,
                    phase_value,
                    1,
                    config.get("crash_replay", {})
                    if isinstance(config.get("crash_replay", {}), Mapping)
                    else {},
                    crash.get("detected_at"),
                )
            crash.update(
                {
                    "cycle": 1,
                    "phase": phase_value,
                    "trajectory_path": exact_trajectory_path,
                    "reproducible": False,
                    "replay_attempts": [],
                    "replay_note": "Fastbot-only mode has no preparation snapshot",
                }
            )
            _add_crash(report, crash, 1)
            if crash.get("signature"):
                crash_signatures.append(str(crash["signature"]))
        if crash_signatures:
            cycle_report["crash_signature"] = crash_signatures[-1]
            cycle_report["crash_signatures"] = crash_signatures
        if trajectory.get("stop_reason") == "manual":
            cycle_report["status"] = "interrupted"
            cycle_report["errors"].append("pressure stopped by manual stop command")
            report["status"] = "interrupted"
        elif trajectory.get("stop_reason") == _WAIT_DEVICE_DISCONNECTED or any(
            isinstance(item.get("device_disconnect"), Mapping)
            for item in runtime_failures
        ):
            disconnect_error = "device disconnected during pressure testing"
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                disconnect_error,
                abort_reason=(
                    "aborting remaining cycles because the device disconnected"
                ),
            )
            cycle_report["recoverable_cycle"] = False
            cycle_report["continue_run"] = False
            cycle_report["abort_run"] = True
            cycle_report["abort_reason"] = (
                "aborting remaining cycles because the device disconnected"
            )
            report["status"] = "completed_with_errors"
        elif _cycle_hard_control_failure(executor, trajectory):
            handoff_error = str(
                trajectory.get("control_release_error")
                or trajectory.get("screenshot_collection_error")
                or trajectory.get("pressure_stop_error")
                or "a pressure collector or device-control owner did not stop cleanly"
            )
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "device control handoff failed: %s" % handoff_error,
                abort_reason=_abort_remaining_cycles_message(
                    RuntimeError(handoff_error), executor
                ),
            )
            report["status"] = "completed_with_errors"
        elif trajectory.get("audit_recovery_failed"):
            audit_error = str(
                trajectory.get("audit_recovery_error") or "unknown recovery error"
            )
            _verify_cycle_pressure_handoff(executor, trajectory, force=True)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "startup-gate recovery failed: %s" % audit_error,
                abort_reason=(
                    "aborting remaining cycles because the startup audit gate "
                    "could not be recovered: %s" % audit_error
                ),
            )
            report["status"] = "completed_with_errors"
        elif trajectory.get("permission_recovery_failed"):
            permission_error = str(
                trajectory.get("permission_recovery_error")
                or "permission dialog could not be handled"
            )
            _verify_cycle_pressure_handoff(executor, trajectory, force=True)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "runtime permission recovery failed: %s" % permission_error,
                abort_reason=(
                    "aborting remaining cycles because runtime permission recovery "
                    "could not complete: %s" % permission_error
                ),
            )
            report["status"] = "completed_with_errors"
        elif trajectory.get("foreground_recovery_failed"):
            foreground_error = str(
                trajectory.get("foreground_recovery_error")
                or trajectory.get("foreground_restore_error")
                or "target app could not be restored after foreground loss"
            )
            _verify_cycle_pressure_handoff(executor, trajectory, force=True)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "foreground recovery failed: %s" % foreground_error,
                abort_reason=(
                    "aborting remaining cycles because the foreground/ADB handoff "
                    "could not restore the target app: %s" % foreground_error
                ),
            )
            report["status"] = "completed_with_errors"
        elif trajectory.get("pressure_engine_recovery_failed"):
            engine_error = str(
                trajectory.get("pressure_engine_recovery_error")
                or "Fastbot pressure engine could not be restarted"
            )
            _verify_cycle_pressure_handoff(executor, trajectory, force=True)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "pressure engine recovery failed: %s" % engine_error,
                abort_reason=(
                    "aborting remaining cycles because the pressure engine could "
                    "not be recovered: %s" % engine_error
                ),
            )
            report["status"] = "completed_with_errors"
        elif trajectory.get("runtime_recovery_failed"):
            runtime_error = str(
                trajectory.get("runtime_recovery_error")
                or "target application runtime recovery failed"
            )
            _verify_cycle_pressure_handoff(executor, trajectory, force=True)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "runtime recovery failed: %s" % runtime_error,
                abort_reason=(
                    "aborting remaining cycles because runtime recovery could not "
                    "safely hand off device control: %s" % runtime_error
                ),
            )
            report["status"] = "completed_with_errors"
        elif (
            trajectory.get("stop_reason")
            in {_WAIT_STALL_RECOVERY, _WAIT_STALL_RECOVERY_UNCLEAN}
            and not bool(
                isinstance(trajectory.get("stall_recovery"), Mapping)
                and trajectory["stall_recovery"].get("success")
            )
        ):
            recovery = trajectory.get("stall_recovery", {})
            error = (
                str(recovery.get("error"))
                if isinstance(recovery, Mapping) and recovery.get("error")
                else "unknown recovery error"
            )
            _verify_cycle_pressure_handoff(executor, trajectory, force=True)
            _mark_cycle_failure(
                cycle_report,
                executor,
                trajectory,
                "stall recovery failed: %s" % error,
                abort_reason=(
                    "aborting remaining cycles because stall recovery could not "
                    "safely hand off device control: %s" % error
                ),
            )
            report["status"] = "completed_with_errors"
        elif runtime_failures:
            cycle_report["status"] = "failed"
            cycle_report["errors"].append(
                "target application process exited without confirmed app-crash evidence"
            )
            report["status"] = "completed_with_errors"
        elif crash is not None:
            cycle_report["status"] = "crash"
            report["status"] = "completed_with_crashes"
        else:
            report["status"] = "completed"
        _copy_cycle_handoff_fields(cycle_report, trajectory, executor)
        report["cycles"].append(cycle_report)
        report["artifacts"].update(artifacts)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _apply_report_coverage(report, worker_config, expected_cycles=1)
        _persist_report(report, report_dir)
        artifacts.update(
            {
                key: value
                for key, value in report.get("artifacts", {}).items()
                if key in {"json", "html"}
            }
        )
        result = {
            "device_serial": device_serial,
            "status": report["status"],
            "coverage_status": report.get("coverage", {}).get("status"),
            "report_dir": str(report_dir),
            "artifacts": artifacts,
            "operation_count": trajectory.get("operation_count", 0),
            "log_bytes": len(log_content.encode("utf-8")),
        }
    except (InterruptedError, KeyboardInterrupt) as exc:
        _close_fastbot_only_exception_cycle(
            report,
            report_dir,
            state_manager,
            executor,
            mutex,
            worker_config,
            trajectory,
            artifacts,
            exc,
            interrupted=True,
            existing_cycle=cycle_report,
            phase_attempted=phase_attempted,
        )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _apply_report_coverage(report, worker_config, expected_cycles=1)
        _persist_report(report, report_dir)
        result = {
            "device_serial": device_serial,
            "status": report["status"],
            "coverage_status": report.get("coverage", {}).get("status"),
            "report_dir": str(report_dir),
            "artifacts": dict(report.get("artifacts", {})),
            "operation_count": report.get("statistics", {}).get(
                "total_operations", 0
            ),
            "log_bytes": 0,
        }
    except Exception as exc:
        logger.exception("Fastbot-only run failed on %s", device_serial)
        _close_fastbot_only_exception_cycle(
            report,
            report_dir,
            state_manager,
            executor,
            mutex,
            worker_config,
            trajectory,
            artifacts,
            exc,
            interrupted=False,
            existing_cycle=cycle_report,
            phase_attempted=phase_attempted,
        )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _apply_report_coverage(report, worker_config, expected_cycles=1)
        _persist_report(report, report_dir)
        result = {
            "device_serial": device_serial,
            "status": report["status"],
            "coverage_status": report.get("coverage", {}).get("status"),
            "report_dir": str(report_dir),
            "artifacts": dict(report.get("artifacts", {})),
            "operation_count": report.get("statistics", {}).get(
                "total_operations", 0
            ),
            "log_bytes": 0,
        }
    finally:
        try:
            cleanup_error = _cleanup_device_control(
                report, executor, mutex, state_manager, device_serial
            )
            if cleanup_error:
                # The helper may run after the success path has already
                # persisted a cycle; persist the hard handoff verdict again so
                # the final report cannot claim completion with live control.
                _apply_report_coverage(report, worker_config, expected_cycles=1)
                _persist_report(report, report_dir)
        finally:
            close_logging_handlers()
    result.update(
        {
            "device_serial": device_serial,
            "status": report.get("status", "failed"),
            "coverage_status": report.get("coverage", {}).get("status")
            if isinstance(report.get("coverage"), Mapping)
            else None,
            "report_dir": str(report_dir),
            "artifacts": dict(report.get("artifacts", {})),
            "operation_count": report.get("statistics", {}).get(
                "total_operations", 0
            ),
            "log_bytes": len(log_content.encode("utf-8")),
        }
    )
    return result


def select_devices(
    config: Mapping[str, Any], requested: Optional[Iterable[str]] = None
) -> List[str]:
    """Resolve configured/CLI serials against devices already listed by local ADB."""
    connected = DeviceManager.get_connected_devices()
    connected_set = set(connected)
    configured = config.get("devices") or [{"serial": "auto"}]
    aliases = {
        str(item.get("alias")): str(item.get("serial"))
        for item in configured
        if isinstance(item, Mapping)
        and item.get("serial") not in (None, "", "auto")
        and item.get("alias")
    }
    if requested is not None:
        if isinstance(requested, str):
            requested = [requested]
        selected = [
            aliases.get(str(item).strip(), str(item).strip())
            for item in requested
            if str(item).strip()
        ]
    else:
        configured_serials = [
            str(item.get("serial", "")).strip()
            for item in configured
            if isinstance(item, Mapping)
        ]
        selected = connected if "auto" in configured_serials else configured_serials
    selected = list(dict.fromkeys(selected))
    if not selected:
        raise RuntimeError("no local ADB devices are connected")
    unknown = [serial for serial in selected if serial not in connected_set]
    if unknown:
        raise RuntimeError("requested devices are not connected: %s" % ", ".join(unknown))
    not_ready = [serial for serial in selected if not DeviceManager.is_device_ready(serial)]
    if not_ready:
        raise RuntimeError("devices are not ready/unlocked: %s" % ", ".join(not_ready))
    return selected


def _resolve_device_targets(
    config: Mapping[str, Any],
    requested: Optional[Iterable[str]],
    device_states: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    """Resolve intended devices without dropping unavailable serials."""
    configured = config.get("devices") or [{"serial": "auto"}]
    aliases = {
        str(item.get("alias")): str(item.get("serial"))
        for item in configured
        if isinstance(item, Mapping)
        and item.get("serial") not in (None, "", "auto")
        and item.get("alias")
    }
    if requested is not None:
        requested_values = [requested] if isinstance(requested, str) else requested
        selected = [
            aliases.get(str(item).strip(), str(item).strip())
            for item in requested_values
            if str(item).strip()
        ]
    else:
        configured_serials = [
            str(item.get("serial", "")).strip()
            for item in configured
            if isinstance(item, Mapping)
        ]
        if "auto" in configured_serials:
            selected = [
                serial
                for serial, details in device_states.items()
                if details.get("adb_state") == "device"
            ]
        else:
            selected = configured_serials
    selected = list(dict.fromkeys(item for item in selected if item))
    if not selected:
        raise RuntimeError("no target devices were configured or listed by local ADB")
    return selected


def _unavailable_device_result(
    serial: str, state: Optional[Mapping[str, Any]], coverage_status: str
) -> Dict[str, Any]:
    adb_state = str(state.get("adb_state") if state else "missing")
    reason = {
        "missing": "device is not listed by the local ADB server",
        "unauthorized": "device is listed but USB debugging is not authorized",
        "not_ready": "device is offline, booting, locked, or otherwise not ready",
    }.get(coverage_status, "device cannot start pressure testing")
    return {
        "device_serial": serial,
        "status": "not_run",
        "execution_status": "not_run",
        "coverage_status": coverage_status,
        "adb_state": adb_state,
        "ready": False,
        "operation_count": 0,
        "pressure_elapsed_seconds": 0.0,
        "duration_ratio": 0.0,
        "error": reason,
    }


def _report_error_summary(report: Mapping[str, Any]) -> str:
    """Flatten run, preflight, and per-cycle errors for the coverage matrix."""
    messages: List[str] = []

    def append_values(value: Any, prefix: str = "") -> None:
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, Mapping):
                item = item.get("message") or item.get("error") or str(dict(item))
            message = str(item or "").strip()
            if not message:
                continue
            rendered = "%s%s" % (prefix, message)
            if rendered not in messages:
                messages.append(rendered)

    append_values(report.get("errors", []))
    preflight = report.get("preflight", {})
    if isinstance(preflight, Mapping):
        append_values(preflight.get("error"), "preflight: ")
        append_values(preflight.get("errors", []), "preflight: ")
    cycles = unique_cycle_rows(report)
    if isinstance(cycles, list):
        for index, cycle in enumerate(cycles, start=1):
            if not isinstance(cycle, Mapping):
                continue
            cycle_number = cycle.get("cycle", index)
            append_values(describe_permission_failure(cycle), "cycle %s: " % cycle_number)
            append_values(cycle.get("errors", []), "cycle %s: " % cycle_number)
    return "; ".join(messages)


def _device_worker_entry(
    serial: str,
    config: Dict[str, Any],
    run_id: str,
    cancel_event: Any,
    result_queue: Any,
) -> None:
    # A hard host termination (for example Windows 0xC000009A) does not run
    # the scheduler's ``finally`` block.  Keep a lightweight child-side
    # watchdog so an orphaned worker stops injecting events and cleans the
    # device-side Fastbot process on its own.
    parent_watch_stop = threading.Event()
    parent_watchdog = _start_parent_watchdog(
        serial, cancel_event, parent_watch_stop
    )
    try:
        result = run_test_on_device(serial, config, run_id, cancel_event)
    except BaseException as exc:  # keep the parent scheduler informed
        result = {
            "device_serial": serial,
            "status": "interrupted"
            if isinstance(exc, KeyboardInterrupt)
            else "failed",
            "error": str(exc),
        }
    finally:
        parent_watch_stop.set()
        if parent_watchdog is not None:
            parent_watchdog.join(timeout=1.0)
    report = result.get("report", {})
    if not isinstance(report, Mapping):
        report = {}
    statistics = report.get("statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    coverage = report.get("coverage", {})
    if not isinstance(coverage, Mapping):
        coverage = {}
    device_info = report.get("device_info", {})
    if not isinstance(device_info, Mapping):
        device_info = {}
    report_error = _report_error_summary(report)
    try:
        result_queue.put(
            {
                "device_serial": result.get("device_serial", serial),
                "status": result.get("status", "failed"),
                "execution_status": result.get("status", "failed"),
                "resume_state": result.get("resume_state")
                or report.get("resume_state"),
                "resume_attempts": result.get("resume_attempts")
                or report.get("resume_attempts", 0),
                "remaining_seconds": result.get("remaining_seconds")
                if result.get("remaining_seconds") is not None
                else report.get("remaining_seconds", 0),
                "last_heartbeat": result.get("last_heartbeat")
                or report.get("last_heartbeat"),
                "recoverable": bool(result.get("recoverable")),
                "automatic_resume": bool(result.get("automatic_resume")),
                "recovery_category": result.get("recovery_category"),
                "recovery_reason": result.get("recovery_reason"),
                "coverage_status": result.get("coverage_status")
                or coverage.get("status")
                or "failed",
                "report_dir": result.get("report_dir"),
                "error": result.get("error") or report_error or None,
                "operation_count": statistics.get("total_operations", 0),
                "pressure_elapsed_seconds": statistics.get(
                    "pressure_elapsed_seconds", 0
                ),
                "duration_ratio": coverage.get("duration_ratio", 0),
                "preflight": report.get("preflight", {}),
                "device_info": dict(device_info),
            }
        )
    except Exception:
        pass


def _start_parent_watchdog(
    serial: str, cancel_event: Any, stop_event: threading.Event
) -> Optional[threading.Thread]:
    """Monitor the scheduler from a spawned worker and clean orphaned runs."""
    try:
        parent = mp.parent_process()
    except (AttributeError, RuntimeError):
        parent = None
    if parent is None:
        return None

    def watch() -> None:
        while not stop_event.wait(_PARENT_WATCH_INTERVAL_SECONDS):
            try:
                alive = parent.is_alive()
            except (AssertionError, OSError, ValueError):
                # A transient native-handle failure is not enough evidence
                # that the scheduler disappeared.
                continue
            if alive:
                continue
            logger.error(
                "Parent scheduler disappeared; cancelling orphaned worker on %s",
                serial,
            )
            try:
                cancel_event.set()
            except Exception:
                pass
            _best_effort_remote_worker_cleanup(serial)
            return

    thread = threading.Thread(
        target=watch,
        name="parent-watch-%s" % safe_serial(serial),
        daemon=True,
    )
    thread.start()
    return thread


def _worker_is_alive(process: Any) -> bool:
    """Read a multiprocessing worker state without masking shutdown progress."""
    try:
        return bool(process.is_alive())
    except (AssertionError, OSError, ValueError):
        # ``is_alive`` raises for a process that failed before ``start`` or
        # whose native handle has already been closed.  It is safe to treat
        # both cases as stopped and continue the bounded cleanup.
        return False


def _worker_started(process: Any) -> bool:
    """Return whether a process may need termination after a partial start."""
    if _worker_is_alive(process):
        return True
    try:
        return (
            getattr(process, "pid", None) is not None
            and getattr(process, "exitcode", None) is None
        )
    except (AssertionError, OSError, ValueError):
        return False


def _worker_exitcode(process: Any) -> Any:
    """Read a worker exit code even when its native handle is already closed."""
    try:
        return getattr(process, "exitcode", None)
    except (AssertionError, OSError, ValueError):
        return None


def _worker_join(process: Any, timeout: Optional[float] = None) -> None:
    """Join a worker and release its native process handle when supported."""
    try:
        if timeout is None:
            process.join()
        else:
            process.join(timeout=max(0.0, float(timeout)))
    except (AssertionError, OSError, ValueError, TypeError):
        # A worker can disappear between the liveness probe and join.  Cleanup
        # must remain best effort and must never hide the scheduler result.
        pass
    if _worker_is_alive(process):
        return
    close = getattr(process, "close", None)
    if callable(close):
        try:
            close()
        except (AssertionError, OSError, ValueError):
            pass


def _worker_result_row(
    serial: str, status: str, error: str, *, exitcode: Any = None
) -> Dict[str, Any]:
    """Build the minimal coverage row used when a worker cannot report itself."""
    row: Dict[str, Any] = {
        "device_serial": serial,
        "status": status,
        "execution_status": status,
        "coverage_status": "interrupted" if status == "interrupted" else "failed",
        "error": str(error),
        "operation_count": 0,
        "pressure_elapsed_seconds": 0.0,
        "duration_ratio": 0.0,
    }
    if exitcode is not None:
        row["worker_exitcode"] = exitcode
    return row


def _mark_cleanup_gate_failure(
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
    serial: str,
    reason: str,
) -> None:
    """Persist a manual gate when the previous device controller is unknown."""
    path = _worker_report_path(config, run_id, serial)
    if path is None:
        return
    checkpoint_path = path.parent / "checkpoint.json"
    report_value: Any = None
    try:
        if path.is_file():
            report_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        report_value = None
    checkpoint_value = load_checkpoint(
        path.parent, run_id=run_id, device_serial=serial
    ) if checkpoint_path.is_file() else None
    manual_gate = _durable_manual_gate({}, report_value, checkpoint_value)
    if manual_gate is not None:
        # A user stop wins a concurrent scheduler cleanup failure.  Keep the
        # original pause reason and never turn it into an automatic retry.
        _preserve_durable_manual_gate(
            report_value,
            report_value,
            checkpoint_value,
        ) if isinstance(report_value, dict) else None
        if isinstance(checkpoint_value, Mapping):
            state_name, source = manual_gate
            reason = str(
                source.get("last_error")
                or source.get("recovery_reason")
                or "pressure stopped by web console"
            )
            checkpoint_copy = dict(checkpoint_value)
            checkpoint_copy.update(
                {
                    "resume_state": state_name,
                    "manual_stop": True,
                    "recovery_category": str(
                        source.get("recovery_category") or "manual_stop"
                    ),
                    "recovery_reason": reason,
                    "last_error": reason,
                }
            )
            try:
                write_checkpoint_json(checkpoint_path, checkpoint_copy)
            except Exception:
                logger.warning(
                    "Unable to preserve manual stop checkpoint for %s",
                    serial,
                    exc_info=True,
                )
        return
    terminal_guard = terminal_resume_state(report_value) or terminal_resume_state(
        checkpoint_value
    )
    if isinstance(report_value, dict) and not terminal_guard:
        report_value["status"] = "needs_manual_resume"
        report_value["resume_state"] = "needs_manual_resume"
        report_value["finished_at"] = None
        report_value["last_error"] = reason
        report_value["waiting_for_device"] = False
        # The reserved attempt was blocked by the cleanup gate.  An explicit
        # operator resume must be allowed to claim a fresh attempt.
        report_value["resume_attempt_reserved"] = False
        errors = report_value.setdefault("errors", [])
        if isinstance(errors, list) and reason not in errors:
            errors.append(reason)
        try:
            _persist_report(report_value, path.parent)
        except Exception:
            logger.warning(
                "Unable to persist cleanup gate failure for %s", serial, exc_info=True
            )
    if isinstance(checkpoint_value, Mapping) and not terminal_guard:
        checkpoint_copy = dict(checkpoint_value)
        checkpoint_copy.update(
            {
                "resume_state": "needs_manual_resume",
                "last_error": reason,
                "resume_attempt_reserved": False,
            }
        )
        try:
            write_checkpoint_json(checkpoint_path, checkpoint_copy)
        except Exception:
            logger.warning(
                "Unable to persist checkpoint cleanup gate for %s",
                serial,
                exc_info=True,
            )


def _mark_device_waiting(
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
    serial: str,
    reason: str,
    *,
    wait_started_at: Optional[str] = None,
    wait_deadline_at: Optional[str] = None,
) -> None:
    """Persist a recoverable transport wait without closing the report."""
    path = _worker_report_path(config, run_id, serial)
    if path is None:
        return
    checkpoint_path = path.parent / "checkpoint.json"
    report_value: Any = None
    try:
        if path.is_file():
            report_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        report_value = None
    checkpoint_value = load_checkpoint(
        path.parent, run_id=run_id, device_serial=serial
    ) if checkpoint_path.is_file() else None
    manual_gate = _durable_manual_gate({}, report_value, checkpoint_value)
    if manual_gate is not None:
        # Do not let a late transport poll replace an explicit operator stop.
        _preserve_durable_manual_gate(
            report_value,
            report_value,
            checkpoint_value,
        ) if isinstance(report_value, dict) else None
        if isinstance(checkpoint_value, Mapping):
            state_name, source = manual_gate
            reason = str(
                source.get("last_error")
                or source.get("recovery_reason")
                or "pressure stopped by web console"
            )
            checkpoint_copy = dict(checkpoint_value)
            checkpoint_copy.update(
                {
                    "resume_state": state_name,
                    "manual_stop": True,
                    "recovery_category": str(
                        source.get("recovery_category") or "manual_stop"
                    ),
                    "recovery_reason": reason,
                    "last_error": reason,
                }
            )
            try:
                write_checkpoint_json(checkpoint_path, checkpoint_copy)
            except Exception:
                logger.warning(
                    "Unable to preserve manual stop checkpoint for %s",
                    serial,
                    exc_info=True,
                )
        return
    terminal_guard = terminal_resume_state(report_value) or terminal_resume_state(
        checkpoint_value
    )
    if isinstance(report_value, dict) and not terminal_guard:
        report_value["status"] = "waiting_for_device"
        report_value["resume_state"] = "waiting_for_device"
        report_value["finished_at"] = None
        report_value["last_error"] = reason
        report_value["waiting_for_device"] = True
        if wait_started_at:
            report_value["wait_started_at"] = wait_started_at
        if wait_deadline_at:
            report_value["wait_deadline_at"] = wait_deadline_at
        errors = report_value.setdefault("errors", [])
        if isinstance(errors, list) and reason not in errors:
            errors.append(reason)
        try:
            _persist_report(report_value, path.parent)
        except Exception:
            logger.warning(
                "Unable to persist device reconnect wait for %s", serial, exc_info=True
            )
    if isinstance(checkpoint_value, Mapping) and not terminal_guard:
        checkpoint_copy = dict(checkpoint_value)
        checkpoint_copy.update(
            {
                "resume_state": "waiting_for_device",
                "last_error": reason,
                "waiting_for_device": True,
            }
        )
        if wait_started_at:
            checkpoint_copy["wait_started_at"] = wait_started_at
        if wait_deadline_at:
            checkpoint_copy["wait_deadline_at"] = wait_deadline_at
        try:
            write_checkpoint_json(checkpoint_path, checkpoint_copy)
        except Exception:
            logger.warning(
                "Unable to persist checkpoint reconnect wait for %s",
                serial,
                exc_info=True,
            )


def _worker_report_path(
    config: Optional[Mapping[str, Any]], run_id: Optional[str], serial: str
) -> Optional[Path]:
    """Return a worker's report path without creating directories.

    This is used only as a recovery path when the IPC result was lost.  Keep
    the resolver side-effect free so a missing/partial report cannot create a
    false historical run while the scheduler is shutting down.
    """
    if not isinstance(config, Mapping) or not run_id:
        return None
    paths = config.get("paths", {})
    reports_root = (
        str(paths.get("reports_dir", "./reports"))
        if isinstance(paths, Mapping)
        else "./reports"
    )
    try:
        root = resolve_project_path(reports_root)
    except (ConfigurationError, OSError, ValueError):
        return None
    return root / ("%s_%s" % (str(run_id), safe_serial(serial))) / "report.json"


def _manual_resume_request_exists(
    config: Optional[Mapping[str, Any]], run_id: Optional[str], serial: str
) -> bool:
    """Return whether an explicit operator resume claim is waiting on disk."""
    report_path = _worker_report_path(config, run_id, serial)
    if report_path is None:
        return False
    try:
        return (report_path.parent / "resume.request.json").is_file()
    except OSError:
        return False


def _persisted_resume_metadata(
    config: Optional[Mapping[str, Any]], run_id: Optional[str], serial: str
) -> Dict[str, Any]:
    """Read the durable resume counter/state used to seed a new scheduler.

    ``run_devices`` can be entered again after a host restart.  Its in-memory
    retry map must therefore start from the report/checkpoint, otherwise the
    same device could receive another three automatic attempts on every host
    restart.
    """
    result: Dict[str, Any] = {
        "state": "",
        "attempts": 0,
        "history": [],
        "report": None,
        "checkpoint": None,
        "path": None,
    }
    path = _worker_report_path(config, run_id, serial)
    result["path"] = path
    report: Mapping[str, Any] = {}
    if path is not None and path.is_file():
        value = read_checkpoint_json(path)
        if isinstance(value, Mapping):
            report = value
            result["report"] = dict(value)
    checkpoint = (
        load_checkpoint(path.parent, run_id=run_id, device_serial=serial)
        if path is not None and path.parent.exists()
        else None
    )
    result["checkpoint"] = checkpoint
    # Resolve report/checkpoint atomically from the same task/device pair. A
    # terminal marker always wins over a stale heartbeat; otherwise the newest
    # snapshot determines the in-flight state.
    state = effective_resume_state(report, checkpoint, default="")
    result["state"] = state
    attempts = max(
        _report_nonnegative_int(report.get("resume_attempts", 0)),
        _report_nonnegative_int((checkpoint or {}).get("resume_attempts", 0)),
    )
    result["attempts"] = attempts
    history_values: List[Dict[str, Any]] = []
    for source in (report, checkpoint or {}):
        for key in ("retry_history", "resume_history", "interruptions"):
            values = source.get(key) if isinstance(source, Mapping) else None
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, Mapping):
                    history_values.append(copy.deepcopy(dict(item)))
    # Preserve order while avoiding duplicate references when a report stores
    # the same interruption in both compatibility fields.
    seen_history: set[str] = set()
    for item in history_values:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen_history:
            continue
        seen_history.add(marker)
        result["history"].append(item)
    return result


def _resume_timestamp(value: Any) -> float:
    """Return a sortable epoch for a report/checkpoint heartbeat value."""
    if isinstance(value, Mapping):
        candidates = [
            value.get("last_heartbeat"),
            value.get("saved_at"),
            value.get("updated_at"),
            value.get("scheduler_updated_at"),
            value.get("finished_at"),
        ]
    else:
        candidates = [value]
    latest = 0.0
    for item in candidates:
        if not item:
            continue
        try:
            parsed = datetime.fromisoformat(str(item).replace("Z", "+00:00"))
            latest = max(latest, parsed.timestamp())
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return latest


def _resume_wall_clock_epoch(value: Any) -> Optional[float]:
    """Parse a durable wall-clock timestamp used by scheduler recovery."""
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None
    try:
        parsed_datetime = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
        parsed = parsed_datetime.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return parsed if math.isfinite(parsed) else None


def _restored_device_wait_window(
    persisted: Mapping[str, Any],
    wait_seconds: float,
    *,
    wall_now: Optional[float] = None,
    monotonic_now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Restore a reconnect window without extending it after a host restart.

    Wall-clock timestamps are durable but unsuitable for loop scheduling;
    monotonic timestamps are safe for scheduling but disappear with the
    process. Convert the persisted deadline into the new monotonic clock and
    retain the original values for report/checkpoint updates.
    """
    if str(persisted.get("state") or "").strip().lower() != "waiting_for_device":
        return None
    wall_value = time.time() if wall_now is None else float(wall_now)
    monotonic_value = (
        time.monotonic() if monotonic_now is None else float(monotonic_now)
    )
    duration = max(1.0, float(wait_seconds))
    sources = [
        value
        for value in (persisted.get("report"), persisted.get("checkpoint"))
        if isinstance(value, Mapping)
    ]
    # Report/checkpoint are replaced separately, so either side may briefly
    # lag. Prefer the newest file that contains each valid value.
    sources.sort(key=_resume_timestamp, reverse=True)

    started_raw: Any = None
    started_epoch: Optional[float] = None
    deadline_raw: Any = None
    deadline_epoch: Optional[float] = None
    for source in sources:
        candidate = source.get("wait_deadline_at")
        candidate_epoch = _resume_wall_clock_epoch(candidate)
        if deadline_epoch is None and candidate_epoch is not None:
            deadline_raw = candidate
            deadline_epoch = candidate_epoch
        candidate = source.get("wait_started_at")
        candidate_epoch = _resume_wall_clock_epoch(candidate)
        if started_epoch is None and candidate_epoch is not None:
            started_raw = candidate
            started_epoch = candidate_epoch

    if deadline_epoch is None and started_epoch is None:
        return None
    if started_epoch is None:
        assert deadline_epoch is not None
        started_epoch = deadline_epoch - duration
        started_raw = datetime.fromtimestamp(started_epoch, timezone.utc).isoformat()
    if deadline_epoch is None:
        deadline_epoch = started_epoch + duration
        deadline_raw = datetime.fromtimestamp(deadline_epoch, timezone.utc).isoformat()
    if started_epoch > deadline_epoch:
        # The deadline is the safety boundary. A contradictory start timestamp
        # must not move it or produce negative elapsed time.
        started_epoch = deadline_epoch - duration
        started_raw = datetime.fromtimestamp(started_epoch, timezone.utc).isoformat()

    elapsed = max(0.0, wall_value - started_epoch)
    return {
        "started": monotonic_value - elapsed,
        "deadline": monotonic_value + (deadline_epoch - wall_value),
        "next_poll": monotonic_value,
        "wait_started_at": str(started_raw),
        "wait_deadline_at": str(deadline_raw),
    }


def _merge_checkpoint_progress_into_report(
    report: Dict[str, Any], checkpoint: Mapping[str, Any]
) -> None:
    """Merge monotonic in-flight progress without touching completed cycles."""
    if terminal_resume_state(report):
        # A final report is immutable at the run level. Keep diagnostic
        # identity/counter fields, but never reopen a pre-final cycle.
        for key in (
            "apk_fingerprint",
            "foreground_component",
            "remote_log_path",
            "remote_log_offset",
            "logcat_offset",
        ):
            if checkpoint.get(key) not in (None, ""):
                report.setdefault(key, copy.deepcopy(checkpoint.get(key)))
        report["resume_attempts"] = max(
            _report_nonnegative_int(report.get("resume_attempts"), 0),
            _report_nonnegative_int(checkpoint.get("resume_attempts"), 0),
        )
        return
    for key in (
        "last_heartbeat",
        "apk_fingerprint",
        "foreground_component",
        "fastbot_pid",
        "remote_log_path",
        "remote_log_offset",
        "logcat_offset",
    ):
        if checkpoint.get(key) not in (None, ""):
            report[key] = copy.deepcopy(checkpoint.get(key))
    if "remaining_seconds" in checkpoint:
        report["remaining_seconds"] = _report_nonnegative_float(
            checkpoint.get("remaining_seconds"),
            _report_nonnegative_float(report.get("remaining_seconds"), 0.0),
        )
    completed = completed_cycle_numbers(report)
    cycle = _report_nonnegative_int(checkpoint.get("cycle"), 0)
    if cycle <= 0 or cycle in completed:
        return
    current = report.get("current_cycle")
    merged: Dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    merged["cycle"] = cycle
    field_pairs = (
        ("phase", "phase"),
        ("cycle_started_at", "started_at"),
        ("elapsed_seconds", "pressure_elapsed_seconds"),
        ("operation_count", "operation_count"),
        ("remaining_seconds", "remaining_seconds"),
        ("last_error", "last_error"),
        ("foreground_recovery_strategy", "foreground_recovery_strategy"),
    )
    for source, target in field_pairs:
        if source not in checkpoint:
            continue
        value = checkpoint.get(source)
        if target in {"phase", "operation_count"}:
            incoming = _report_nonnegative_int(value, 0)
            existing = _report_nonnegative_int(merged.get(target), 0)
            merged[target] = max(existing, incoming)
        elif target == "pressure_elapsed_seconds":
            incoming = _report_nonnegative_float(value, 0.0)
            existing = _report_nonnegative_float(merged.get(target), 0.0)
            merged[target] = max(existing, incoming)
        elif target == "remaining_seconds":
            incoming = _report_nonnegative_float(value, 0.0)
            existing = _report_nonnegative_float(merged.get(target), 0.0)
            # A positive remaining value is useful when the report has not
            # yet received the heartbeat; once both are known, the smaller
            # value represents the furthest observed progress.
            if existing <= 0:
                merged[target] = incoming
            elif incoming > 0:
                merged[target] = min(existing, incoming)
        elif value not in (None, ""):
            merged[target] = value
    merged.setdefault("status", str(checkpoint.get("resume_state") or "running"))
    report["current_cycle"] = merged


def _merge_retry_history(
    existing: Any, incoming: Optional[Iterable[Mapping[str, Any]]]
) -> Optional[List[Dict[str, Any]]]:
    """Union retry entries while keeping the first durable copy of evidence."""
    if incoming is None:
        return None
    values: List[Dict[str, Any]] = []
    for source in (existing, incoming):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, Mapping):
                continue
            candidate = copy.deepcopy(dict(item))
            marker = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)
            if any(
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                == marker
                for value in values
            ):
                continue
            values.append(candidate)
    return values


def _persist_scheduler_resume_state(
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
    serial: str,
    *,
    state: str,
    attempts: int,
    error: Optional[str] = None,
    history: Optional[Iterable[Mapping[str, Any]]] = None,
    next_retry_at: Optional[str] = None,
    attempt_reserved: Optional[bool] = None,
) -> bool:
    """Persist scheduler-only recovery metadata without touching cycles.

    ``resume_attempts`` is a durable budget, so a scheduler may reserve the
    next attempt before spawning a process.  ``attempt_reserved`` tells the
    child worker that this increment has already been accounted for; without
    that marker a retry would be counted once by the scheduler and once again
    when the worker opens the existing report.
    """
    path = _worker_report_path(config, run_id, serial)
    if path is None:
        logger.warning(
            "Unable to persist scheduler resume state for %s: report path is unavailable",
            serial,
        )
        return False
    checkpoint_path = path.parent / "checkpoint.json"
    try:
        value = read_checkpoint_json(path) if path.is_file() else None
        report = dict(value) if isinstance(value, Mapping) else None
        checkpoint = load_checkpoint(
            path.parent, run_id=run_id, device_serial=serial
        ) if checkpoint_path.is_file() else None

        # A heartbeat can win the race between the initial report read and
        # this scheduler transition. Preserve its newest progress snapshot
        # before applying scheduler metadata.
        if report is not None and isinstance(checkpoint, Mapping):
            if _resume_timestamp(checkpoint) >= _resume_timestamp(report):
                _merge_checkpoint_progress_into_report(report, checkpoint)

        report_state = str(
            (report or {}).get("resume_state")
            or (report or {}).get("status")
            or ""
        ).strip().lower()
        checkpoint_state = str(
            (checkpoint or {}).get("resume_state") or ""
        ).strip().lower()
        terminal_states = TERMINAL_RESUME_STATES | {
            "passed",
            "completed_with_crashes",
        }
        # A terminal marker is monotonic.  Even when an older scheduler sees a
        # stale in-flight checkpoint, it must never turn a completed report
        # back into ``recovering`` (or overwrite a completed checkpoint).
        terminal_state = bool(
            terminal_resume_state(report) or terminal_resume_state(checkpoint)
        ) or report_state in terminal_states or checkpoint_state in terminal_states
        requested_state = str(state or "recovering")
        manual_gate = _durable_manual_gate({}, report, checkpoint)
        if manual_gate is not None and requested_state.strip().lower() != "resuming":
            # A scheduler poll can race a user stop.  Keep the explicit
            # manual gate authoritative until the resume endpoint launches a
            # worker in the ``resuming`` state.
            requested_state, manual_source = manual_gate
            error = str(
                manual_source.get("last_error")
                or manual_source.get("recovery_reason")
                or error
                or "pressure stopped by web console"
            )
            attempt_reserved = False
            next_retry_at = None
        # Nothing needs to be written for an already-terminal device.  Treat
        # this as success so callers do not manufacture a manual gate while a
        # final report is being observed.
        if terminal_state:
            return True
        if report is None and not isinstance(checkpoint, Mapping):
            logger.warning(
                "Unable to persist scheduler resume state for %s: no durable report or checkpoint",
                serial,
            )
            return False
        report_verified = report is None
        checkpoint_verified = not isinstance(checkpoint, Mapping)
        if report is not None and not terminal_state:
            report["resume_state"] = requested_state
            report["status"] = requested_state
            report["resume_attempts"] = max(
                _report_nonnegative_int(report.get("resume_attempts", 0)),
                max(0, int(attempts)),
            )
            if attempt_reserved is not None:
                report["resume_attempt_reserved"] = bool(attempt_reserved)
            if error:
                report["last_error"] = str(error)
            if next_retry_at:
                report["next_retry_at"] = str(next_retry_at)
            merged_history = _merge_retry_history(report.get("retry_history"), history)
            if merged_history is not None:
                report["retry_history"] = merged_history
            current_cycle = report.get("current_cycle")
            if isinstance(current_cycle, dict):
                current_cycle["status"] = requested_state
                if error:
                    current_cycle["last_error"] = str(error)
            report["finished_at"] = None
            # Do not manufacture a heartbeat timestamp for a scheduler-only
            # transition; it must remain clear which fields came from the
            # device heartbeat and which came from the host scheduler.
            report["scheduler_updated_at"] = datetime.now(timezone.utc).isoformat()
            _persist_report(report, path.parent)
            persisted_report = read_checkpoint_json(path)
            report_verified = bool(
                isinstance(persisted_report, Mapping)
                and str(persisted_report.get("resume_state") or "").strip().lower()
                == requested_state.strip().lower()
                and _report_nonnegative_int(
                    persisted_report.get("resume_attempts"), 0
                )
                >= _report_nonnegative_int(report.get("resume_attempts"), 0)
            )

        if isinstance(checkpoint, Mapping) and not terminal_state:
            checkpoint_value = dict(checkpoint)
            checkpoint_state = str(checkpoint_value.get("resume_state") or "").lower()
            if checkpoint_state not in terminal_states:
                checkpoint_value["resume_state"] = requested_state
                checkpoint_value["resume_attempts"] = max(
                    _report_nonnegative_int(checkpoint_value.get("resume_attempts", 0)),
                    max(0, int(attempts)),
                )
                if attempt_reserved is not None:
                    checkpoint_value["resume_attempt_reserved"] = bool(
                        attempt_reserved
                    )
                if error:
                    checkpoint_value["last_error"] = str(error)
                if next_retry_at:
                    checkpoint_value["next_retry_at"] = str(next_retry_at)
                merged_history = _merge_retry_history(
                    checkpoint_value.get("retry_history"), history
                )
                if merged_history is not None:
                    checkpoint_value["retry_history"] = merged_history
                write_checkpoint_json(checkpoint_path, checkpoint_value)
                persisted_checkpoint = read_checkpoint_json(checkpoint_path)
                checkpoint_verified = bool(
                    isinstance(persisted_checkpoint, Mapping)
                    and str(
                        persisted_checkpoint.get("resume_state") or ""
                    ).strip().lower()
                    == requested_state.strip().lower()
                    and _report_nonnegative_int(
                        persisted_checkpoint.get("resume_attempts"), 0
                    )
                    >= _report_nonnegative_int(
                        checkpoint_value.get("resume_attempts"), 0
                    )
                )
        if not report_verified or not checkpoint_verified:
            raise OSError("scheduler resume state write could not be verified")
        return True
    except Exception:
        logger.warning(
            "Unable to persist scheduler resume state for %s", serial, exc_info=True
        )
        return False


def _report_nonnegative_int(value: Any, default: int = 0) -> int:
    """Parse a report counter without allowing malformed JSON to abort shutdown."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def _report_nonnegative_float(value: Any, default: float = 0.0) -> float:
    """Parse a finite non-negative report duration/ratio defensively."""
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(0.0, parsed)


def _worker_result_from_report(
    serial: str,
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Reconstruct the scheduler row from a terminal on-disk report.

    A report is accepted only after the worker wrote a terminal status.  This
    prevents a report that was still being flushed when a process disappeared
    from being presented as a successful run.
    """
    path = _worker_report_path(config, run_id, serial)
    if path is None:
        return None
    report_value: Mapping[str, Any] = {}
    report_available = False
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, Mapping):
            report_value = loaded
            report_available = True
    checkpoint_value = (
        load_checkpoint(path.parent, run_id=run_id, device_serial=serial)
        if path.parent.exists()
        else None
    )
    if not report_available and checkpoint_value is None:
        return None
    report_serial = str(report_value.get("device_serial") or serial)
    if report_serial != str(serial):
        return None
    report_job_id = str(
        report_value.get("run_id") or report_value.get("job_id") or ""
    ).strip()
    if report_job_id and run_id and report_job_id != str(run_id):
        return None
    raw_status = str(report_value.get("status") or "").strip()
    state = effective_resume_state(report_value, checkpoint_value, default="")
    terminal_state = state in TERMINAL_RESUME_STATES
    worker_disappeared = state in {
        "running",
        "resuming",
        "recovering",
        "waiting_for_device",
    }
    if worker_disappeared and report_available and checkpoint_value is None:
        # A legacy report may contain only ``status: running`` because the
        # process disappeared before the first durable heartbeat. There is no
        # evidence that it belongs to a recoverable worker.
        has_resume_metadata = any(
            key in report_value
            for key in (
                "resume_state",
                "resume_attempts",
                "remaining_seconds",
                "last_heartbeat",
                "current_cycle",
            )
        )
        if not has_resume_metadata:
            return None
    status = state or raw_status or "recovering"
    if status in {"running", "resuming"} and worker_disappeared:
        status = "recovering"
    coverage = report_value.get("coverage", {})
    if not isinstance(coverage, Mapping):
        coverage = {}
    statistics = report_value.get("statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    device_info = report_value.get("device_info", {})
    if not isinstance(device_info, Mapping):
        device_info = {}
    cycles = unique_cycle_rows(report_value)
    completed_values = [
        item
        for item in cycles
        if isinstance(item, Mapping) and is_completed_cycle(item)
    ]
    completed_numbers = completed_cycle_numbers(report_value)
    completed_elapsed = sum(
        _report_nonnegative_float(item.get("pressure_elapsed_seconds", 0))
        for item in completed_values
    )
    completed_operations = sum(
        _report_nonnegative_int(item.get("operation_count", 0))
        for item in completed_values
    )
    # Once a run-level terminal marker exists, an older heartbeat may still
    # describe an unfinished cycle.  That heartbeat is diagnostic only: it must
    # never be added back into the terminal report's elapsed time, operations,
    # or remaining budget.
    current: Mapping[str, Any] = (
        report_value.get("current_cycle")
        if not terminal_state and isinstance(report_value.get("current_cycle"), Mapping)
        else {}
    )
    checkpoint_cycle = _report_nonnegative_int(
        (checkpoint_value or {}).get("cycle"), 0
    )
    if not terminal_state and checkpoint_cycle > 0 and checkpoint_cycle not in completed_numbers:
        current = checkpoint_value or current
    current_is_partial = bool(current) and (
        _report_nonnegative_int(
            current.get("cycle") or current.get("cycle_number"), 0
        )
        not in completed_numbers
    )
    partial_elapsed = (
        _report_nonnegative_float(
            current.get("elapsed_seconds")
            if "elapsed_seconds" in current
            else current.get("pressure_elapsed_seconds"),
            0.0,
        )
        if current_is_partial
        else 0.0
    )
    partial_operations = (
        _report_nonnegative_int(current.get("operation_count"), 0)
        if current_is_partial
        else 0
    )
    aggregate_elapsed = _report_nonnegative_float(
        statistics.get("pressure_elapsed_seconds", 0)
    )
    aggregate_operations = _report_nonnegative_int(
        statistics.get("total_operations", 0)
    )
    elapsed = max(aggregate_elapsed, completed_elapsed + partial_elapsed)
    operations = max(aggregate_operations, completed_operations + partial_operations)
    target_seconds = 0.0
    if isinstance(config, Mapping):
        test = config.get("test", {})
        if isinstance(test, Mapping):
            try:
                target_seconds = max(
                    0.0,
                    float(test.get("fastbot_duration_minutes", 0) or 0)
                    * 60.0
                    * max(1, int(test.get("max_cycles", 1) or 1)),
                )
            except (TypeError, ValueError, OverflowError):
                target_seconds = 0.0
    remaining = (
        0.0
        if terminal_state
        else _report_nonnegative_float(
            report_value.get("remaining_seconds"),
            _report_nonnegative_float(
                (checkpoint_value or {}).get("remaining_seconds"),
                max(0.0, target_seconds - elapsed) if target_seconds else 0.0,
            ),
        )
    )
    if checkpoint_value is not None and not terminal_state:
        cp_remaining = _report_nonnegative_float(
            checkpoint_value.get("remaining_seconds"), remaining
        )
        if remaining <= 0:
            remaining = cp_remaining
        elif cp_remaining > 0:
            remaining = min(remaining, cp_remaining)
    if target_seconds and not terminal_state:
        remaining = min(remaining, max(0.0, target_seconds - elapsed))
    duration_ratio = min(
        1.0,
        _report_nonnegative_float(coverage.get("duration_ratio", 0)),
    )
    if not duration_ratio and target_seconds:
        duration_ratio = min(1.0, elapsed / target_seconds)
    coverage_status = str(coverage.get("status") or "").strip()
    if not coverage_status:
        if status in {"completed", "passed", "completed_with_crashes"}:
            coverage_status = "complete"
        elif status == MANUALLY_ENDED_STATE:
            coverage_status = "stopped"
        elif status in AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES:
            coverage_status = status
        else:
            coverage_status = "failed"
    resume_attempts = max(
        _report_nonnegative_int(report_value.get("resume_attempts", 0)),
        _report_nonnegative_int((checkpoint_value or {}).get("resume_attempts", 0)),
    )
    last_error = (
        (report_value.get("last_error") or _report_error_summary(report_value))
        if terminal_state
        else (
            report_value.get("last_error")
            or (checkpoint_value or {}).get("last_error")
            or (current.get("last_error") if isinstance(current, Mapping) else None)
        )
    )
    return {
        "device_serial": serial,
        "status": status,
        "execution_status": status,
        "resume_state": status,
        "resume_attempts": resume_attempts,
        "remaining_seconds": remaining,
        "last_heartbeat": report_value.get("last_heartbeat")
        or (checkpoint_value or {}).get("last_heartbeat"),
        "recoverable": bool(
            status in AUTOMATIC_RECOVERY_STATES
            or (worker_disappeared and status not in TERMINAL_RESUME_STATES)
        ),
        "automatic_resume": bool(
            status in AUTOMATIC_RECOVERY_STATES
            or (worker_disappeared and status not in TERMINAL_RESUME_STATES)
        ),
        "recovery_category": (
            "manual_end"
            if status == MANUALLY_ENDED_STATE
            else "worker_exit"
            if worker_disappeared
            else None
        ),
        "checkpoint": checkpoint_value,
        "coverage_status": coverage_status,
        "report_dir": str(path.parent),
        "error": str(last_error or _report_error_summary(report_value) or "") or None,
        "operation_count": operations,
        "pressure_elapsed_seconds": elapsed,
        "duration_ratio": duration_ratio,
        "preflight": report_value.get("preflight", {}),
        "device_info": dict(device_info),
        "result_source": "report_fallback" if report_available else "checkpoint_fallback",
    }


def _worker_result_terminal_state(value: Any) -> Optional[str]:
    """Return a terminal scheduler state without trusting coverage alone."""
    state = terminal_resume_state(value)
    return str(state) if state else None


def _normalize_terminal_worker_result(
    serial: str, value: Mapping[str, Any], state: str
) -> Dict[str, Any]:
    """Make all scheduler-facing state fields agree with a terminal outcome."""
    result = dict(value)
    terminal_state = str(state).strip().lower()
    result.update(
        {
            "device_serial": serial,
            "status": terminal_state,
            "execution_status": terminal_state,
            "resume_state": terminal_state,
            "recoverable": False,
            "automatic_resume": False,
            "waiting_for_device": False,
            "remaining_seconds": 0.0,
        }
    )
    if (
        terminal_state in {"completed", "passed", "completed_with_crashes"}
        and not str(result.get("coverage_status") or "").strip()
    ):
        result["coverage_status"] = "complete"
    elif terminal_state == MANUALLY_ENDED_STATE:
        result["coverage_status"] = "stopped"
        result["recovery_category"] = "manual_end"
    return result


def _worker_result_progress_key(
    value: Mapping[str, Any], source_rank: int
) -> Tuple[float, float, int, int]:
    """Sort non-terminal snapshots by durable time and monotonic progress."""
    return (
        _resume_timestamp(value),
        _report_nonnegative_float(value.get("pressure_elapsed_seconds"), 0.0),
        _report_nonnegative_int(value.get("operation_count"), 0),
        int(source_rank),
    )


def _merge_worker_result_with_disk(
    serial: str,
    current: Optional[Mapping[str, Any]],
    incoming: Optional[Mapping[str, Any]],
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
    *,
    disk_result: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Merge IPC and durable rows while making terminal state monotonic.

    Queue feeder delivery is asynchronous, so an older in-flight row can
    arrive after the worker has atomically finalized its report.  A terminal
    disk row is authoritative; without one, a terminal IPC/current row still
    wins over any non-terminal snapshot.  Non-terminal rows are selected by
    heartbeat/progress so retries cannot move backwards.
    """
    disk = (
        dict(disk_result)
        if isinstance(disk_result, Mapping)
        else _worker_result_from_report(serial, config, run_id)
    )
    candidates: List[Tuple[str, Dict[str, Any], int]] = []
    if isinstance(current, Mapping):
        candidates.append(("current", dict(current), 0))
    if isinstance(incoming, Mapping):
        candidates.append(("incoming", dict(incoming), 1))
    if isinstance(disk, Mapping):
        candidates.append(("disk", dict(disk), 2))
    if not candidates:
        return None

    terminal_candidates = [
        (source, value, rank, _worker_result_terminal_state(value))
        for source, value, rank in candidates
        if _worker_result_terminal_state(value)
    ]
    if terminal_candidates:
        # The report/checkpoint pair is the durable source of truth.  If it is
        # not terminal yet, prefer a terminal IPC result over an older cached
        # terminal row because the IPC item represents the latest worker exit.
        disk_terminal = next(
            (item for item in terminal_candidates if item[0] == "disk"), None
        )
        incoming_terminal = next(
            (item for item in terminal_candidates if item[0] == "incoming"),
            None,
        )
        winner = disk_terminal or incoming_terminal or terminal_candidates[0]
        _source, winning_value, _rank, terminal_state = winner
    else:
        _source, winning_value, _rank = max(
            candidates,
            key=lambda item: _worker_result_progress_key(item[1], item[2]),
        )
        terminal_state = None

    merged: Dict[str, Any] = {}
    for _source, value, _rank in candidates:
        if value is winning_value:
            continue
        merged.update(value)
    merged.update(winning_value)
    merged["device_serial"] = serial
    if terminal_state:
        return _normalize_terminal_worker_result(serial, merged, terminal_state)
    # Disk reconstruction intentionally contains only fields that can be
    # derived from report/checkpoint files. Keep richer in-memory diagnostics
    # (for example the timeout category and absolute reconnect deadline) when
    # the disk row has an absent/empty value instead of erasing them during the
    # post-join refresh.
    for key in (
        "recovery_category",
        "recovery_reason",
        "wait_started_at",
        "wait_deadline_at",
        "next_retry_at",
    ):
        if merged.get(key) not in (None, "", []):
            continue
        for _source, value, _rank in reversed(candidates):
            candidate = value.get(key)
            if candidate not in (None, "", []):
                merged[key] = candidate
                break
    return merged


def _refresh_worker_results_from_disk(
    results: Dict[str, Dict[str, Any]],
    serials: Iterable[str],
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
) -> None:
    """Refresh scheduler rows after worker joins and before summary output."""
    for serial_value in serials:
        serial = str(serial_value)
        disk = _worker_result_from_report(serial, config, run_id)
        merged = _merge_worker_result_with_disk(
            serial,
            results.get(serial),
            None,
            config,
            run_id,
            disk_result=disk,
        )
        if merged is not None:
            results[serial] = merged


def _wait_for_worker_results(
    results: Dict[str, Dict[str, Any]],
    serials: Iterable[str],
    drain: Callable[[], None],
    *,
    timeout: float = _WORKER_RESULT_GRACE_SECONDS,
    config: Optional[Mapping[str, Any]] = None,
    run_id: Optional[str] = None,
) -> None:
    """Drain late IPC rows and, if needed, consult terminal report files."""
    targets = {str(serial) for serial in serials}
    pending = {serial for serial in targets if serial not in results}
    if not pending:
        _refresh_worker_results_from_disk(results, targets, config, run_id)
        return
    deadline = time.monotonic() + max(0.0, float(timeout))
    while pending and time.monotonic() < deadline:
        try:
            drain()
        except Exception:
            logger.debug("Unable to drain late worker results", exc_info=True)
        pending.difference_update(results)
        if not pending:
            _refresh_worker_results_from_disk(results, targets, config, run_id)
            return
        for serial in list(pending):
            recovered = _worker_result_from_report(serial, config, run_id)
            if recovered is not None:
                merged = _merge_worker_result_with_disk(
                    serial,
                    results.get(serial),
                    None,
                    config,
                    run_id,
                    disk_result=recovered,
                )
                if merged is not None:
                    results[serial] = merged
                pending.discard(serial)
        if pending:
            time.sleep(0.05)
    # One final non-blocking drain closes the small race between the deadline
    # check and Queue feeder delivery.
    try:
        drain()
    except Exception:
        logger.debug("Unable to perform final worker-result drain", exc_info=True)
    for serial in list(pending):
        recovered = _worker_result_from_report(serial, config, run_id)
        if recovered is not None:
            merged = _merge_worker_result_with_disk(
                serial,
                results.get(serial),
                None,
                config,
                run_id,
                disk_result=recovered,
            )
            if merged is not None:
                results[serial] = merged
    _refresh_worker_results_from_disk(results, targets, config, run_id)


def _remote_transport_available(serial: str) -> bool:
    """Check only whether ADB can currently reach an authorized transport.

    ``DeviceManager.is_device_ready`` also checks boot completion and the
    keyguard.  Those checks are intentionally excluded here: cleanup must be
    able to distinguish a device that has disappeared from one that is online
    but still has an unreleased automation controller.
    """
    runner = getattr(DeviceManager, "_run_adb", None)
    if not callable(runner):
        return False
    try:
        result = runner(
            ["get-state"],
            serial=serial,
            timeout=max(1, int(_REMOTE_TRANSPORT_PROBE_TIMEOUT_SECONDS)),
            retries=1,
        )
    except Exception as exc:
        logger.debug(
            "ADB transport probe failed for %s: %s", serial, exc, exc_info=True
        )
        return False
    return str(getattr(result, "stdout", "") or "").strip().lower() == "device"


def _best_effort_remote_worker_cleanup_status(serial: str) -> str:
    """Return a three-state result for cleanup after a worker interruption.

    ``device_unavailable`` means the ADB transport is absent and is therefore
    eligible for bounded reconnect polling.  ``cleanup_failed`` means ADB is
    still reachable but a Fastbot/uiautomator2 release could not be proven; in
    that case starting another worker would risk two input controllers.
    """
    if not _remote_transport_available(serial):
        logger.warning(
            "Device %s is unavailable before remote worker cleanup", serial
        )
        return _REMOTE_CLEANUP_DEVICE_UNAVAILABLE

    finished = threading.Event()
    outcome = {"status": _REMOTE_CLEANUP_FAILED}

    def classify_probe_failure(step: str) -> None:
        # A failed remote probe is ambiguous until the transport is checked
        # again.  This second get-state probe is what separates a USB
        # re-enumeration from a live device with a genuinely stuck controller.
        if not _remote_transport_available(serial):
            outcome["status"] = _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
            logger.warning(
                "Remote worker cleanup for %s lost the ADB transport during %s",
                serial,
                step,
            )
        else:
            outcome["status"] = _REMOTE_CLEANUP_FAILED
            logger.warning(
                "Unable to verify remote worker cleanup during %s on %s",
                step,
                serial,
            )

    def cleanup() -> None:
        try:
            controller = MutexController(serial)
            probes = (
                ("Fastbot stop", "_kill_fastbot_if_running"),
                ("uiautomator2 process cleanup", "_stop_remote_uiautomator_processes"),
                ("UiAutomation release", "_wait_for_uiautomation_release"),
            )
            for step, method_name in probes:
                probe = getattr(controller, method_name, None)
                if not callable(probe):
                    continue
                try:
                    succeeded = bool(probe())
                except Exception:
                    logger.warning(
                        "Remote worker cleanup probe failed during %s on %s",
                        step,
                        serial,
                        exc_info=True,
                    )
                    succeeded = False
                if not succeeded:
                    classify_probe_failure(step)
                    return
            outcome["status"] = _REMOTE_CLEANUP_RELEASED
        except Exception:
            logger.warning(
                "Unable to perform device-side worker cleanup on %s",
                serial,
                exc_info=True,
            )
            classify_probe_failure("controller setup")
        finally:
            finished.set()

    thread = threading.Thread(
        target=cleanup,
        name="worker-cleanup-%s" % safe_serial(serial),
        daemon=True,
    )
    thread.start()
    if not finished.wait(_REMOTE_CLEANUP_TIMEOUT_SECONDS):
        # The cleanup thread is deliberately daemonized because an ADB call
        # can outlive its host-side timeout.  Recheck transport before
        # classifying the timeout so a disconnected device enters reconnect
        # polling instead of an unnecessary manual gate.
        status = (
            _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
            if not _remote_transport_available(serial)
            else _REMOTE_CLEANUP_FAILED
        )
        logger.warning(
            "Device-side worker cleanup still running after %.1fs on %s; status=%s",
            _REMOTE_CLEANUP_TIMEOUT_SECONDS,
            serial,
            status,
        )
        return status
    return str(outcome.get("status") or _REMOTE_CLEANUP_FAILED)


def _best_effort_remote_worker_cleanup(serial: str) -> bool:
    """Compatibility wrapper returning whether remote cleanup was released."""
    return (
        _best_effort_remote_worker_cleanup_status(serial)
        == _REMOTE_CLEANUP_RELEASED
    )


def _apply_shutdown_cleanup_state(
    serial: str,
    results: Dict[str, Dict[str, Any]],
    cleanup_status: str,
    *,
    interrupted: bool,
    reason: str,
    config: Optional[Mapping[str, Any]],
    run_id: Optional[str],
) -> None:
    """Keep an interrupted worker resumable until remote ownership is known.

    Host-side process termination does not prove that Fastbot or uiautomator2
    released its device-side handles.  A failed cleanup probe therefore must
    be reflected in the durable device state before the scheduler returns a
    row; otherwise a later invocation could start a second controller.
    """
    if cleanup_status == _REMOTE_CLEANUP_RELEASED:
        return
    existing = results.get(serial)
    row = dict(existing) if isinstance(existing, Mapping) else _worker_result_row(
        serial,
        "paused" if interrupted else "recovering",
        reason,
    )
    if str(row.get("resume_state") or row.get("status") or "").lower() in TERMINAL_RESUME_STATES:
        # A completed result is monotonic.  Keep it even if a late cleanup
        # probe reports an ambiguous transport state.
        return
    if cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
        target_state = "paused" if interrupted else "waiting_for_device"
        target_reason = (
            "%s; device transport is unavailable while releasing previous "
            "Fastbot/uiautomator2 control" % reason
        )
        automatic = not interrupted
    else:
        target_state = "paused" if interrupted else "needs_manual_resume"
        target_reason = (
            "%s; previous Fastbot/uiautomator2 control could not be verified "
            "released" % reason
        )
        automatic = False
    row.update(
        {
            "device_serial": serial,
            "status": target_state,
            "execution_status": target_state,
            "coverage_status": target_state,
            "resume_state": target_state,
            "recoverable": automatic,
            "automatic_resume": automatic,
            "waiting_for_device": target_state == "waiting_for_device",
            "recovery_category": (
                "device_transport" if target_state == "waiting_for_device" else "cleanup_gate"
            ),
            "recovery_reason": target_reason,
            "error": target_reason,
            "cleanup_status": cleanup_status,
        }
    )
    results[serial] = row
    attempts = _report_nonnegative_int(row.get("resume_attempts"), 0)
    if target_state == "waiting_for_device":
        wait_seconds = 1800.0
        if isinstance(config, Mapping):
            resume_config = config.get("resume", {})
            if isinstance(resume_config, Mapping):
                try:
                    wait_seconds = max(
                        1.0,
                        float(resume_config.get("wait_for_device_seconds", 1800) or 1800),
                    )
                except (TypeError, ValueError, OverflowError):
                    wait_seconds = 1800.0
        wait_started = datetime.now(timezone.utc).isoformat()
        wait_deadline = datetime.fromtimestamp(
            time.time() + wait_seconds, timezone.utc
        ).isoformat()
        _mark_device_waiting(
            config,
            run_id,
            serial,
            target_reason,
            wait_started_at=wait_started,
            wait_deadline_at=wait_deadline,
        )
    elif target_state == "needs_manual_resume":
        _mark_cleanup_gate_failure(config, run_id, serial, target_reason)
    else:
        persisted_ok = _persist_scheduler_resume_state(
            config,
            run_id,
            serial,
            state=target_state,
            attempts=attempts,
            error=target_reason,
            attempt_reserved=False,
        )
        if persisted_ok is False:
            # Do not leave an in-memory recovering row that can be dispatched
            # again after a host restart when the scheduler marker was not
            # durably written.
            _mark_cleanup_gate_failure(
                config,
                run_id,
                serial,
                "resume state could not be persisted; manual resume required",
            )


def _shutdown_device_workers(
    active: Dict[str, Any],
    cancel_event: Any,
    results: Dict[str, Dict[str, Any]],
    drain: Optional[Callable[[], None]],
    *,
    status: str,
    reason: str,
    config: Optional[Mapping[str, Any]] = None,
    run_id: Optional[str] = None,
) -> None:
    """Stop every active worker through one bounded, reusable shutdown path."""
    shutdown_serials = set(active)
    try:
        cancel_event.set()
    except Exception:
        logger.debug("Unable to signal device-worker cancellation", exc_info=True)

    deadline = time.monotonic() + _WORKER_SHUTDOWN_GRACE_SECONDS
    while active and time.monotonic() < deadline:
        if drain is not None:
            try:
                drain()
            except Exception:
                logger.debug("Unable to drain worker results during shutdown", exc_info=True)
        finished_workers: List[Tuple[str, Any, Any]] = []
        for serial, process in list(active.items()):
            if _worker_started(process):
                continue
            exitcode = _worker_exitcode(process)
            _worker_join(process, timeout=0)
            active.pop(serial, None)
            finished_workers.append((serial, exitcode))
        # Give the Queue feeder one last chance to publish a report from a
        # worker that stopped while cancellation was being signalled.
        if finished_workers and drain is not None:
            try:
                drain()
            except Exception:
                logger.debug("Unable to drain worker results after join", exc_info=True)
        if finished_workers and drain is not None:
            _wait_for_worker_results(
                results=results,
                serials=(serial for serial, _exitcode in finished_workers),
                drain=drain,
                config=config,
                run_id=run_id,
            )
        for serial, exitcode in finished_workers:
            if serial not in results:
                recovered = _worker_result_from_report(serial, config, run_id)
                results[serial] = recovered or _worker_result_row(
                    serial,
                    status,
                    reason,
                    exitcode=exitcode,
                )
        if active:
            time.sleep(0.1)

    for serial, process in list(active.items()):
        was_started = _worker_started(process)
        if was_started:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except (AssertionError, OSError, ValueError):
                    logger.debug(
                        "Unable to terminate worker %s cleanly", serial, exc_info=True
                    )
            _worker_join(process, timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
            if _worker_is_alive(process):
                kill = getattr(process, "kill", None)
                if callable(kill):
                    try:
                        kill()
                    except (AssertionError, OSError, ValueError):
                        logger.debug(
                            "Unable to kill worker %s after terminate timeout",
                            serial,
                            exc_info=True,
                        )
                _worker_join(process, timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
            # Keep the compatibility wrapper as the single cleanup operation
            # (older integrations and tests patch this entry point).  When it
            # reports failure, a transport probe distinguishes a disconnected
            # device from a live device whose controller is still held.
            cleanup_released = _best_effort_remote_worker_cleanup(serial)
            if cleanup_released:
                cleanup_status = _REMOTE_CLEANUP_RELEASED
            elif _remote_transport_available(serial):
                cleanup_status = _REMOTE_CLEANUP_FAILED
            else:
                cleanup_status = _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
            _apply_shutdown_cleanup_state(
                serial,
                results,
                cleanup_status,
                interrupted=status == "interrupted",
                reason=reason,
                config=config,
                run_id=run_id,
            )
        else:
            _worker_join(process, timeout=0)
        exitcode = _worker_exitcode(process)
        if serial not in results:
            recovered = _worker_result_from_report(serial, config, run_id)
            results[serial] = recovered or _worker_result_row(
                serial, status, reason, exitcode=exitcode
            )
        active.pop(serial, None)
    if drain is not None:
        try:
            drain()
        except Exception:
            logger.debug("Unable to drain final worker results", exc_info=True)
    _refresh_worker_results_from_disk(
        results, shutdown_serials, config, run_id
    )


def run_devices(
    devices: Sequence[str],
    config: Mapping[str, Any],
    run_id: Optional[str] = None,
    device_states: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """Run ready devices and retain a coverage row for every intended serial."""
    devices = list(dict.fromkeys(str(device) for device in devices if str(device)))
    if not devices:
        return {}, False
    run_id = run_id or utc_timestamp()
    run_started_at = datetime.now(timezone.utc).isoformat()
    listed_states = (
        {str(key): dict(value) for key, value in device_states.items()}
        if device_states is not None
        else DeviceManager.get_device_states()
    )
    resume_options = _resume_options(config)
    manual_resume_mode = str(resume_options.get("mode") or "auto") == "manual"
    results: Dict[str, Dict[str, Any]] = {}
    # Read durable state once up front.  Besides seeding retry budgets below,
    # this lets the initial dispatch apply the same old-controller cleanup gate
    # used by later automatic retries.
    persisted_resume: Dict[str, Dict[str, Any]] = {
        serial: _persisted_resume_metadata(config, run_id, serial)
        for serial in devices
    }
    runnable: List[str] = []
    initial_waiting: List[str] = []
    # This set is consulted by the initial durable-state normalization as well
    # as by the later scheduler/shutdown paths.  Initialize it before the
    # first device can enter ``hold_for_manual_resume``; otherwise a failed
    # persistence probe during startup is misreported as a scheduler crash.
    persistence_failed_serials: set[str] = set()

    def hold_for_manual_resume(
        serial: str,
        persisted: Mapping[str, Any],
        listed_state: Optional[Mapping[str, Any]] = None,
        *,
        force_waiting: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        """Keep durable progress visible without starting a second worker.

        Manual mode is a deliberate operator gate.  Returning a normal
        ``run_devices`` result here is important: the CLI can exit cleanly,
        while the web service still discovers the same checkpoint and exposes
        the device-level resume action.
        """
        report = persisted.get("report")
        checkpoint = persisted.get("checkpoint")
        row = _worker_result_from_report(serial, config, run_id)
        if not isinstance(row, Mapping):
            prior_state = str(persisted.get("state") or "recovering").strip()
            row = _worker_result_row(
                serial,
                prior_state or "recovering",
                reason or "manual resume is required",
            )
        row = dict(row)
        prior_state_value: Any = persisted.get("state")
        if not prior_state_value and isinstance(checkpoint, Mapping):
            prior_state_value = checkpoint.get("resume_state")
        if not prior_state_value and isinstance(report, Mapping):
            prior_state_value = report.get("resume_state") or report.get("status")
        prior_state = str(prior_state_value or "recovering").strip()
        if force_waiting:
            state_name = "waiting_for_device"
        elif prior_state in {"paused", "needs_manual_resume"}:
            state_name = prior_state
        elif prior_state in {"waiting_for_device", "recovering"}:
            state_name = prior_state
        else:
            state_name = "recovering"
        message = str(
            reason
            or row.get("error")
            or (
                "device is not ready or authorized: %s" % serial
                if force_waiting
                else "manual resume is required; existing checkpoint was preserved"
            )
        )
        row.update(
            {
                "device_serial": serial,
                "status": state_name,
                "execution_status": state_name,
                "coverage_status": state_name,
                "resume_state": state_name,
                "recoverable": state_name in AUTOMATIC_RECOVERY_STATES,
                "automatic_resume": False,
                "waiting_for_device": state_name == "waiting_for_device",
                "error": message,
                "recovery_reason": message,
                "recovery_category": "manual_mode",
                "resume_attempts": max(
                    _report_nonnegative_int(row.get("resume_attempts", 0)),
                    _report_nonnegative_int(persisted.get("attempts", 0)),
                ),
            }
        )
        if isinstance(checkpoint, Mapping):
            row.setdefault(
                "remaining_seconds",
                _report_nonnegative_float(checkpoint.get("remaining_seconds", 0)),
            )
            row.setdefault(
                "operation_count",
                _report_nonnegative_int(checkpoint.get("operation_count", 0)),
            )
            row.setdefault(
                "pressure_elapsed_seconds",
                _report_nonnegative_float(checkpoint.get("elapsed_seconds", 0)),
            )
            row.setdefault("last_heartbeat", checkpoint.get("last_heartbeat"))
        if isinstance(listed_state, Mapping):
            row.setdefault("adb_state", listed_state.get("adb_state", "unknown"))
        results[serial] = row
        # A host restart may leave a report marked ``running``.  Normalize the
        # durable state once so subsequent service/CLI restarts keep the same
        # manual gate instead of treating it as a fresh launch.
        if state_name in AUTOMATIC_RECOVERY_STATES and prior_state != state_name:
            try:
                persisted_ok = _persist_scheduler_resume_state(
                    config,
                    run_id,
                    serial,
                    state=state_name,
                    attempts=int(row.get("resume_attempts", 0) or 0),
                    error=message,
                )
                if persisted_ok is False:
                    persistence_reason = (
                        "resume state could not be persisted; manual resume required"
                    )
                    row.update(
                        {
                            "status": "needs_manual_resume",
                            "execution_status": "needs_manual_resume",
                            "coverage_status": "needs_manual_resume",
                            "resume_state": "needs_manual_resume",
                            "recoverable": False,
                            "automatic_resume": False,
                            "recovery_category": "resume_state_persistence",
                            "recovery_reason": persistence_reason,
                            "error": persistence_reason,
                        }
                    )
                    results[serial] = row
                    persistence_failed_serials.add(serial)
                    _mark_cleanup_gate_failure(
                        config,
                        run_id,
                        serial,
                        persistence_reason,
                    )
            except Exception:
                row.update(
                    {
                        "status": "needs_manual_resume",
                        "execution_status": "needs_manual_resume",
                        "coverage_status": "needs_manual_resume",
                        "resume_state": "needs_manual_resume",
                        "recoverable": False,
                        "automatic_resume": False,
                        "recovery_category": "resume_state_persistence",
                        "recovery_reason": "resume state could not be persisted; manual resume required",
                        "error": "resume state could not be persisted; manual resume required",
                    }
                )
                results[serial] = row
                persistence_failed_serials.add(serial)
                _mark_cleanup_gate_failure(
                    config,
                    run_id,
                    serial,
                    "resume state could not be persisted; manual resume required",
                )
                logger.debug(
                    "Unable to persist manual resume gate for %s",
                    serial,
                    exc_info=True,
                )

    for serial in devices:
        persisted = persisted_resume.get(serial, {})
        persisted_report = persisted.get("report")
        persisted_checkpoint = persisted.get("checkpoint")
        persisted_state = str(persisted.get("state") or "").strip()
        has_persisted_progress = bool(
            isinstance(persisted_report, Mapping) or persisted_checkpoint is not None
        )
        manual_request_pending = _manual_resume_request_exists(
            config, run_id, serial
        )

        if has_persisted_progress and persisted_state == MANUALLY_ENDED_STATE:
            terminal_row = _worker_result_from_report(serial, config, run_id)
            if isinstance(terminal_row, Mapping):
                results[serial] = _normalize_terminal_worker_result(
                    serial, terminal_row, MANUALLY_ENDED_STATE
                )
            else:
                results[serial] = _normalize_terminal_worker_result(
                    serial,
                    _worker_result_row(
                        serial,
                        MANUALLY_ENDED_STATE,
                        "device test was ended manually",
                    ),
                    MANUALLY_ENDED_STATE,
                )
                results[serial]["coverage_status"] = "stopped"
            continue

        # Completed devices are immutable and do not need to be online for a
        # later scheduler invocation to report them as complete.
        if has_persisted_progress and persisted_state in {
            "completed",
            "passed",
            "completed_with_crashes",
        }:
            terminal_row = _worker_result_from_report(serial, config, run_id)
            if isinstance(terminal_row, Mapping):
                results[serial] = dict(terminal_row)
            else:
                results[serial] = _worker_result_row(
                    serial,
                    "completed",
                    "device already completed this run",
                )
                results[serial]["coverage_status"] = "complete"
            continue

        state = listed_states.get(serial)
        if state is None:
            prior_report = _worker_report_path(config, run_id, serial)
            prior_checkpoint = (
                load_checkpoint(
                    prior_report.parent,
                    run_id=run_id,
                    device_serial=serial,
                )
                if prior_report is not None and prior_report.parent.exists()
                else None
            )
            if (
                resume_options.get("enabled", True)
                and (
                    (prior_report is not None and prior_report.is_file())
                    or prior_checkpoint is not None
                )
            ):
                if manual_resume_mode and not manual_request_pending:
                    hold_for_manual_resume(
                        serial,
                        persisted,
                        None,
                        force_waiting=True,
                    )
                else:
                    initial_waiting.append(serial)
                continue
            results[serial] = _unavailable_device_result(
                serial, None, "missing"
            )
            continue
        adb_state = str(state.get("adb_state") or "unknown")
        if adb_state in {"unauthorized", "no_permissions"}:
            if manual_resume_mode and has_persisted_progress:
                hold_for_manual_resume(
                    serial,
                    persisted,
                    state,
                    reason="device is unauthorized; manual resume is required",
                )
                continue
            results[serial] = _unavailable_device_result(
                serial, state, "unauthorized"
            )
            continue
        readiness_error = ""
        if adb_state != "device":
            device_ready = False
        else:
            try:
                device_ready = bool(DeviceManager.is_device_ready(serial))
            except Exception as exc:
                # A transient ADB timeout belongs to this device's recovery
                # state. It must not abort sibling workers in the same job.
                device_ready = False
                readiness_error = str(exc) or type(exc).__name__
        if adb_state != "device" or not device_ready:
            # A persisted report/checkpoint means this is an interrupted
            # device from the same job, not a brand-new unavailable target.
            # Let the worker enter waiting_for_device so reconnect polling can
            # resume it under the original run id.
            prior_report = _worker_report_path(config, run_id, serial)
            prior_checkpoint = (
                load_checkpoint(
                    prior_report.parent,
                    run_id=run_id,
                    device_serial=serial,
                )
                if prior_report is not None and prior_report.parent.exists()
                else None
            )
            if (
                resume_options.get("enabled", True)
                and (
                    (prior_report is not None and prior_report.is_file())
                    or prior_checkpoint is not None
                )
            ):
                if manual_resume_mode and not manual_request_pending:
                    hold_for_manual_resume(
                        serial,
                        persisted,
                        state,
                        force_waiting=True,
                        reason=readiness_error or None,
                    )
                else:
                    initial_waiting.append(serial)
                continue
            results[serial] = _unavailable_device_result(
                serial, state, "not_ready"
            )
            if readiness_error:
                results[serial]["error"] = readiness_error
            continue
        # Manual/operator gates are never picked up by the automatic queue,
        # regardless of whether the current run uses auto recovery for other
        # devices.  The explicit resume endpoint is the only owner claim.
        if (
            has_persisted_progress
            and persisted_state in MANUAL_RECOVERY_STATES
            and not manual_request_pending
        ):
            hold_for_manual_resume(serial, persisted, state)
            continue
        if (
            has_persisted_progress
            and persisted_state == "failed"
            and not manual_request_pending
        ):
            decision = classify_recovery(persisted_report or {})
            if not decision.get("automatic"):
                hold_for_manual_resume(
                    serial,
                    persisted,
                    state,
                    reason=str(
                        (persisted_report or {}).get("last_error")
                        if isinstance(persisted_report, Mapping)
                        else "previous device run failed; manual resume is required"
                    ),
                )
                continue
        # A report/checkpoint in a live-ish state may belong to a worker that
        # survived a host/service restart.  Reclaim its device-side owners
        # before placing a new process in the queue; otherwise two Fastbot
        # sessions could inject concurrently.  Completed/manual reports are
        # left untouched until the explicit resume path handles them.
        if has_persisted_progress and persisted_state in {
            "running",
            "preparing",
            "installing",
            "resuming",
            "recovering",
            "waiting_for_device",
            "paused",
            "needs_manual_resume",
        }:
            if manual_resume_mode and not manual_request_pending:
                hold_for_manual_resume(serial, persisted, state)
                continue
            cleanup_status = _best_effort_remote_worker_cleanup_status(serial)
            if cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
                initial_waiting.append(serial)
                continue
            if cleanup_status == _REMOTE_CLEANUP_FAILED:
                reason = (
                    "device is online but previous Fastbot/uiautomator2 "
                    "control could not be verified released; manual resume required"
                )
                row = _worker_result_row(serial, "needs_manual_resume", reason)
                row["resume_state"] = "needs_manual_resume"
                row["recovery_category"] = "cleanup_gate"
                row["recoverable"] = False
                row["automatic_resume"] = False
                results[serial] = row
                _mark_cleanup_gate_failure(config, run_id, serial, reason)
                continue
        runnable.append(serial)

    context = mp.get_context("spawn")
    cancel_event = context.Event()
    result_queue = context.Queue()
    parallel = config.get("parallel", {})
    requested_workers = int(
        parallel.get("max_workers", len(devices))
        if isinstance(parallel, Mapping)
        else len(devices)
    )
    requested_workers = max(1, requested_workers)
    max_workers = max(1, min(requested_workers, _MAX_DEVICE_WORKERS, len(runnable) or 1))
    if requested_workers > max_workers:
        logger.warning(
            "Limiting device concurrency from %s to %s to protect host/ADB resources",
            requested_workers,
            max_workers,
        )
    pending = list(runnable)
    # Seed retry budgets from disk.  A host/service restart must not reset the
    # automatic-recovery allowance and silently grant another three attempts.
    retry_counts: Dict[str, int] = {
        serial: int(persisted_resume.get(serial, {}).get("attempts", 0) or 0)
        for serial in devices
    }
    retry_due: Dict[str, float] = {serial: 0.0 for serial in pending}
    retry_history: Dict[str, List[Dict[str, Any]]] = {
        serial: list(persisted_resume.get(serial, {}).get("history", []) or [])
        for serial in devices
    }
    waiting_for_device: Dict[str, Dict[str, Any]] = {}
    active: Dict[str, mp.Process] = {}
    interrupted = False
    scheduler_error: Optional[str] = None

    def enter_device_wait(
        serial: str,
        result: Dict[str, Any],
        *,
        restore_persisted_window: bool = False,
    ) -> None:
        """Move one recoverable worker into non-blocking reconnect polling."""
        now = time.monotonic()
        current = waiting_for_device.get(serial)
        if current is None:
            wait_seconds = float(resume_options["wait_for_device_seconds"])
            current = (
                _restored_device_wait_window(
                    persisted_resume.get(serial, {}),
                    wait_seconds,
                    monotonic_now=now,
                )
                if restore_persisted_window
                else None
            )
            if current is None:
                wall_now = time.time()
                current = {
                    "started": now,
                    "deadline": now + wait_seconds,
                    "next_poll": now,
                    "wait_started_at": datetime.fromtimestamp(
                        wall_now, timezone.utc
                    ).isoformat(),
                    "wait_deadline_at": datetime.fromtimestamp(
                        wall_now + wait_seconds, timezone.utc
                    ).isoformat(),
                }
            waiting_for_device[serial] = current
        result["status"] = "waiting_for_device"
        result["execution_status"] = "waiting_for_device"
        result["coverage_status"] = "waiting_for_device"
        result["resume_state"] = "waiting_for_device"
        result["recoverable"] = True
        result["automatic_resume"] = True
        result["waiting_for_device"] = True
        result["wait_started_at"] = current["wait_started_at"]
        result["wait_deadline_at"] = current["wait_deadline_at"]
        result["waiting_seconds"] = round(
            max(0.0, now - float(current["started"])), 3
        )
        result["error"] = str(
            result.get("error")
            or "device transport is temporarily unavailable; waiting for reconnect"
        )
        results[serial] = result
        _mark_device_waiting(
            config,
            run_id,
            serial,
            result["error"],
            wait_started_at=str(current["wait_started_at"]),
            wait_deadline_at=str(current["wait_deadline_at"]),
        )

    def mark_manual_resume(
        serial: str, result: Dict[str, Any], reason: str, category: str
    ) -> None:
        """Close a recovery attempt when an automatic gate is unsafe."""
        waiting_for_device.pop(serial, None)
        result["status"] = "needs_manual_resume"
        result["execution_status"] = "needs_manual_resume"
        result["resume_state"] = "needs_manual_resume"
        result["recoverable"] = False
        result["automatic_resume"] = False
        result["recovery_category"] = category
        result["recovery_reason"] = reason
        result["error"] = reason
        result["waiting_for_device"] = False
        results[serial] = result
        _mark_cleanup_gate_failure(config, run_id, serial, reason)

    def requeue_after_device_cleanup(serial: str, result: Dict[str, Any]) -> None:
        """Return a reconnected device to the normal worker queue."""
        persisted = _persisted_resume_metadata(config, run_id, serial)
        if str(persisted.get("state") or "").strip().lower() == MANUALLY_ENDED_STATE:
            waiting_for_device.pop(serial, None)
            retry_due.pop(serial, None)
            disk = _worker_result_from_report(serial, config, run_id)
            results[serial] = _normalize_terminal_worker_result(
                serial,
                disk or result,
                MANUALLY_ENDED_STATE,
            )
            results[serial]["coverage_status"] = "stopped"
            return
        waiting_for_device.pop(serial, None)
        result = dict(result)
        result["status"] = "recovering"
        result["execution_status"] = "recovering"
        result["resume_state"] = "recovering"
        result["recoverable"] = True
        result["automatic_resume"] = True
        result["waiting_for_device"] = False
        result["error"] = None
        results[serial] = result
        retry_due[serial] = max(retry_due.get(serial, 0.0), time.monotonic())
        if serial not in pending and serial not in active:
            pending.append(serial)

    def poll_waiting_devices() -> None:
        """Poll all disconnected devices without blocking other workers."""
        now = time.monotonic()
        for serial, wait_state in list(waiting_for_device.items()):
            persisted = _persisted_resume_metadata(config, run_id, serial)
            if str(persisted.get("state") or "").strip().lower() == MANUALLY_ENDED_STATE:
                waiting_for_device.pop(serial, None)
                retry_due.pop(serial, None)
                disk = _worker_result_from_report(serial, config, run_id)
                results[serial] = _normalize_terminal_worker_result(
                    serial,
                    disk or results.get(serial, {}),
                    MANUALLY_ENDED_STATE,
                )
                results[serial]["coverage_status"] = "stopped"
                continue
            result = dict(results.get(serial, {}))
            deadline = float(wait_state.get("deadline", now))
            if now >= deadline:
                timeout_reason = (
                    "wait_for_device_timeout: device %s was not reconnected "
                    "within %s seconds"
                    % (serial, resume_options["wait_for_device_seconds"])
                )
                mark_manual_resume(
                    serial, result, timeout_reason, "device_wait_timeout"
                )
                continue
            if now < float(wait_state.get("next_poll", 0.0)):
                continue
            wait_state["next_poll"] = now + _DEVICE_RECONNECT_POLL_SECONDS
            if not _remote_transport_available(serial):
                result["waiting_seconds"] = round(
                    max(0.0, now - float(wait_state.get("started", now))), 3
                )
                results[serial] = result
                continue
            cleanup_status = _best_effort_remote_worker_cleanup_status(serial)
            if cleanup_status == _REMOTE_CLEANUP_RELEASED:
                requeue_after_device_cleanup(serial, result)
            elif cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
                # The device can disappear between get-state and cleanup.
                results[serial] = result
            else:
                mark_manual_resume(
                    serial,
                    result,
                    "device is online but previous Fastbot/uiautomator2 "
                    "control could not be verified released; manual resume required",
                    "cleanup_gate",
                )

    # Do not consume a worker slot for a device that was already absent when
    # this scheduler started. It will be promoted to ``pending`` only after
    # the transport returns and the same cleanup gate passes.
    for serial in initial_waiting:
        state = listed_states.get(serial, {})
        enter_device_wait(
            serial,
            {
                "device_serial": serial,
                "status": "waiting_for_device",
                "execution_status": "waiting_for_device",
                "resume_state": "waiting_for_device",
                "coverage_status": "waiting_for_device",
                "recoverable": True,
                "automatic_resume": True,
                "error": "device is not ready or authorized: %s" % serial,
                "adb_state": state.get("adb_state", "missing")
                if isinstance(state, Mapping)
                else "missing",
            },
            restore_persisted_window=True,
        )

    def drain() -> None:
        while True:
            try:
                item = result_queue.get_nowait()
            except queue.Empty:
                return
            except (EOFError, OSError, ValueError, AttributeError):
                return
            if not isinstance(item, Mapping):
                continue
            serial = str(item.get("device_serial") or "").strip()
            if not serial or serial not in devices:
                logger.warning("Ignoring worker result for unexpected device %r", serial)
                continue
            merged = _merge_worker_result_with_disk(
                serial,
                results.get(serial),
                item,
                config,
                run_id,
            )
            if merged is not None:
                results[serial] = merged

    def reserve_resume_attempt_for_dispatch(
        serial: str, result: Optional[Mapping[str, Any]] = None
    ) -> bool:
        """Reserve exactly one durable resume number before spawning a worker.

        Automatic retries reserve their number when they are queued.  Devices
        that were waiting for reconnect (or were discovered after a host
        restart) reach this dispatch point without that reservation, so make
        the same transition here.  The worker consumes the marker and never
        increments the already-reserved number a second time.
        """
        metadata = _persisted_resume_metadata(config, run_id, serial)
        report = metadata.get("report")
        checkpoint = metadata.get("checkpoint")
        if not isinstance(report, Mapping) and checkpoint is None:
            # A brand-new device run starts its counter at zero.
            return True
        state = str(metadata.get("state") or "").strip().lower()
        if state in TERMINAL_RESUME_STATES:
            disk = _worker_result_from_report(serial, config, run_id)
            if isinstance(disk, Mapping):
                results[serial] = _normalize_terminal_worker_result(
                    serial, disk, state
                )
            return False
        reserved = bool(
            (isinstance(report, Mapping) and report.get("resume_attempt_reserved"))
            or (isinstance(checkpoint, Mapping) and checkpoint.get("resume_attempt_reserved"))
        )
        attempts = _report_nonnegative_int(metadata.get("attempts", 0))
        if not reserved:
            max_attempts = int(resume_options.get("max_attempts", 3) or 0)
            manual_request = _manual_resume_request_exists(config, run_id, serial)
            if (
                not manual_request
                and state in AUTOMATIC_RECOVERY_STATES | {"recovering", "resuming"}
                and attempts >= max_attempts
            ):
                exhausted_reason = (
                    "automatic resume attempt limit reached (%s/%s)"
                    % (attempts, max_attempts)
                )
                exhausted = dict(result or {})
                exhausted.setdefault("device_serial", serial)
                exhausted.update(
                    {
                        "status": "needs_manual_resume",
                        "execution_status": "needs_manual_resume",
                        "coverage_status": "needs_manual_resume",
                        "resume_state": "needs_manual_resume",
                        "recoverable": False,
                        "automatic_resume": False,
                        "recovery_category": "resume_attempt_limit",
                        "recovery_reason": exhausted_reason,
                        "error": exhausted_reason,
                        "resume_attempts": attempts,
                    }
                )
                results[serial] = exhausted
                persisted_ok = _persist_scheduler_resume_state(
                    config,
                    run_id,
                    serial,
                    state="needs_manual_resume",
                    attempts=attempts,
                    error=exhausted_reason,
                    history=retry_history.get(serial, []),
                    attempt_reserved=False,
                )
                if persisted_ok is False:
                    exhausted["recovery_reason"] = (
                        "resume state could not be persisted; manual resume required"
                    )
                    exhausted["error"] = exhausted["recovery_reason"]
                return False
            attempts += 1
            persisted_ok = _persist_scheduler_resume_state(
                config,
                run_id,
                serial,
                state="resuming",
                attempts=attempts,
                error=(result or {}).get("error") if isinstance(result, Mapping) else None,
                history=retry_history.get(serial, []),
                attempt_reserved=True,
            )
            if persisted_ok is False:
                # Never start a worker with an unrecorded reservation.  A
                # subsequent host restart would otherwise grant the same
                # device another attempt and could launch two owners.
                persistence_reason = (
                    "resume state could not be persisted; manual resume required"
                )
                failed_result = dict(result or {})
                failed_result.setdefault("device_serial", serial)
                failed_result.update(
                    {
                        "status": "needs_manual_resume",
                        "execution_status": "needs_manual_resume",
                        "coverage_status": "needs_manual_resume",
                        "resume_state": "needs_manual_resume",
                        "recoverable": False,
                        "automatic_resume": False,
                        "recovery_category": "resume_state_persistence",
                        "recovery_reason": persistence_reason,
                        "error": persistence_reason,
                        "resume_attempts": _report_nonnegative_int(
                            metadata.get("attempts"), 0
                        ),
                    }
                )
                results[serial] = failed_result
                _mark_cleanup_gate_failure(
                    config,
                    run_id,
                    serial,
                    persistence_reason,
                )
                retry_counts[serial] = max(
                    retry_counts.get(serial, 0),
                    _report_nonnegative_int(metadata.get("attempts"), 0),
                )
                return False
        retry_counts[serial] = max(retry_counts.get(serial, 0), attempts)
        if isinstance(result, dict):
            result["resume_attempts"] = attempts
            result["resume_state"] = "resuming"
            result["status"] = "resuming"
            result["execution_status"] = "resuming"
        return True

    try:
        while pending or active or waiting_for_device:
            poll_waiting_devices()
            while pending and len(active) < max_workers and not cancel_event.is_set():
                now = time.monotonic()
                ready_index = next(
                    (
                        index
                        for index, candidate in enumerate(pending)
                        if retry_due.get(candidate, 0.0) <= now
                    ),
                    None,
                )
                if ready_index is None:
                    break
                serial = pending.pop(ready_index)
                if not reserve_resume_attempt_for_dispatch(serial, results.get(serial)):
                    continue
                process = context.Process(
                    target=_device_worker_entry,
                    args=(serial, dict(config), run_id, cancel_event, result_queue),
                    name="app-press-%s" % safe_serial(serial),
                )
                try:
                    # A daemon worker is a last-resort guard for ordinary
                    # interpreter shutdown; explicit shutdown below remains
                    # authoritative and is required for device-side cleanup.
                    process.daemon = True
                    process.start()
                except BaseException as exc:
                    # ``start`` can fail after creating the native process,
                    # so retain it in ``active`` long enough for the common
                    # shutdown path to terminate and join it.
                    if _worker_started(process):
                        active[serial] = process
                    if isinstance(exc, KeyboardInterrupt):
                        interrupted = True
                    scheduler_error = (
                        "worker %s could not start: %s"
                        % (serial, str(exc) or type(exc).__name__)
                    )
                    logger.exception("Unable to start worker for %s", serial)
                    if serial not in active:
                        results[serial] = _worker_result_row(
                            serial,
                            "interrupted" if interrupted else "failed",
                            scheduler_error,
                        )
                    pending.clear()
                    cancel_event.set()
                    break
                active[serial] = process
            drain()
            finished_workers: List[Tuple[str, Any, Any]] = []
            for serial, process in list(active.items()):
                if not _worker_is_alive(process):
                    exitcode = _worker_exitcode(process)
                    _worker_join(process, timeout=0)
                    del active[serial]
                    finished_workers.append((serial, process, exitcode))
            # A worker can finish just before its Queue feeder flushes the
            # final report. Drain once more after joining before synthesizing a
            # missing result, otherwise a healthy device may be shown as failed.
            if finished_workers:
                drain()
                _wait_for_worker_results(
                    results=results,
                    serials=(serial for serial, _process, _exitcode in finished_workers),
                    drain=drain,
                    config=config,
                    run_id=run_id,
                )
                for serial, _process, exitcode in finished_workers:
                    if serial in results:
                        continue
                    recovered = _worker_result_from_report(serial, config, run_id)
                    results[serial] = recovered or _worker_result_row(
                        serial,
                        "failed",
                        "worker exited with code %s" % exitcode,
                        exitcode=exitcode,
                    )
                # A worker that durably classified its interruption as
                # recoverable is re-dispatched under the same job/run id. The
                # previous process has already been joined; the explicit
                # remote cleanup probe prevents overlapping Fastbot sessions.
                for serial, _process, exitcode in finished_workers:
                    result = results.get(serial, {})
                    # The worker increments the durable counter when it
                    # opens an existing report.  Re-read it here so a result
                    # arriving through the IPC fallback cannot make the
                    # in-memory budget move backwards.
                    persisted = _persisted_resume_metadata(config, run_id, serial)
                    retry_counts[serial] = max(
                        retry_counts.get(serial, 0),
                        int(persisted.get("attempts", 0) or 0),
                        _report_nonnegative_int(result.get("resume_attempts", 0)),
                    )
                    if persisted.get("history") and not retry_history.get(serial):
                        retry_history[serial] = list(persisted["history"])
                    recoverable = bool(
                        result.get("recoverable")
                        or result.get("automatic_resume")
                        or str(result.get("resume_state") or "")
                        in AUTOMATIC_RECOVERY_STATES
                    )
                    max_attempts = int(resume_options.get("max_attempts", 3))
                    result_state = str(
                        result.get("resume_state") or result.get("status") or ""
                    ).strip().lower()
                    if (
                        _worker_result_terminal_state(result)
                        or result_state in MANUAL_RECOVERY_STATES
                    ):
                        continue
                    if (
                        not recoverable
                        or not resume_options.get("enabled", True)
                        or resume_options.get("mode") != "auto"
                    ):
                        reason = (
                            "worker exited without a terminal report and automatic "
                            "resume is unavailable; manual resume required "
                            "(exit code %s)" % exitcode
                        )
                        mark_manual_resume(
                            serial,
                            dict(result),
                            reason,
                            "worker_exit",
                        )
                        continue
                    if retry_counts.get(serial, 0) >= max_attempts:
                        exhausted_reason = (
                            "automatic resume attempt limit reached (%s/%s)"
                            % (retry_counts.get(serial, 0), max_attempts)
                        )
                        exhausted_result = dict(result)
                        exhausted_result["status"] = "needs_manual_resume"
                        exhausted_result["execution_status"] = "needs_manual_resume"
                        exhausted_result["resume_state"] = "needs_manual_resume"
                        exhausted_result["recoverable"] = False
                        exhausted_result["automatic_resume"] = False
                        exhausted_result["recovery_category"] = "resume_attempt_limit"
                        exhausted_result["recovery_reason"] = exhausted_reason
                        exhausted_result["error"] = exhausted_reason
                        results[serial] = exhausted_result
                        persisted_ok = _persist_scheduler_resume_state(
                            config,
                            run_id,
                            serial,
                            state="needs_manual_resume",
                            attempts=retry_counts.get(serial, 0),
                            error=exhausted_reason,
                            history=retry_history.get(serial, []),
                        )
                        if persisted_ok is False:
                            _mark_cleanup_gate_failure(
                                config,
                                run_id,
                                serial,
                                "resume state could not be persisted; manual resume required",
                            )
                        continue
                    attempt = retry_counts.get(serial, 0) + 1
                    retry_counts[serial] = attempt
                    delay = _RESUME_BACKOFF_SECONDS[
                        min(attempt - 1, len(_RESUME_BACKOFF_SECONDS) - 1)
                    ]
                    decision = classify_recovery(result)
                    history_item = {
                        "attempt": attempt,
                        "state": str(
                            result.get("resume_state")
                            or decision.get("state")
                            or "recovering"
                        ),
                        "category": str(
                            result.get("recovery_category")
                            or decision.get("category")
                            or "unknown"
                        ),
                        "scheduled_at": datetime.now(timezone.utc).isoformat(),
                        "backoff_seconds": delay,
                        "error": str(
                            result.get("error")
                            or result.get("recovery_reason")
                            or decision.get("reason")
                            or "recoverable worker interruption"
                        ),
                    }
                    retry_history.setdefault(serial, []).append(history_item)
                    retry_due[serial] = time.monotonic() + delay
                    result = dict(result)
                    result["status"] = "recovering"
                    result["execution_status"] = "recovering"
                    result["resume_state"] = "recovering"
                    result["recoverable"] = True
                    result["automatic_resume"] = True
                    result["resume_attempts"] = attempt
                    result["retry_history"] = list(retry_history[serial])
                    result["next_retry_at"] = datetime.fromtimestamp(
                        time.time() + delay, timezone.utc
                    ).isoformat()
                    results[serial] = result
                    retry_history[serial] = list(retry_history[serial])
                    persisted_ok = _persist_scheduler_resume_state(
                        config,
                        run_id,
                        serial,
                        state="recovering",
                        attempts=attempt,
                        error=str(result.get("error") or result.get("recovery_reason") or ""),
                        history=retry_history[serial],
                        next_retry_at=result["next_retry_at"],
                        attempt_reserved=True,
                    )
                    if persisted_ok is False:
                        # A retry reservation that was not durably recorded is
                        # unsafe to enqueue: a host restart could grant the
                        # same device another owner. Close this attempt at a
                        # manual gate and remove all local dispatch markers.
                        retry_due.pop(serial, None)
                        mark_manual_resume(
                            serial,
                            result,
                            "resume state could not be persisted; manual resume required",
                            "resume_state_persistence",
                        )
                        continue
                    cleanup_status = _best_effort_remote_worker_cleanup_status(serial)
                    if cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
                        enter_device_wait(serial, result)
                    elif cleanup_status == _REMOTE_CLEANUP_FAILED:
                        mark_manual_resume(
                            serial,
                            result,
                            "device is online but previous Fastbot/uiautomator2 "
                            "control could not be verified released; manual resume required",
                            "cleanup_gate",
                        )
                    else:
                        pending.append(serial)
            poll_waiting_devices()
            if active:
                time.sleep(_SCHEDULER_POLL_SECONDS)
            elif pending or waiting_for_device:
                now = time.monotonic()
                due_values = [retry_due.get(serial, now) for serial in pending]
                due_values.extend(
                    float(item.get("next_poll", now))
                    for item in waiting_for_device.values()
                )
                due_values.extend(
                    float(item.get("deadline", now))
                    for item in waiting_for_device.values()
                )
                wait_seconds = max(0.05, min(1.0, min(due_values) - now))
                time.sleep(wait_seconds)
    except KeyboardInterrupt:
        interrupted = True
        scheduler_error = "cancelled by user"
    except BaseException as exc:
        # Keep the scheduler from taking the whole web job down on a host-side
        # resource/IPC error.  Device reports are finalized below with an
        # explicit failure reason, and all active workers are still reclaimed.
        scheduler_error = (
            "device worker scheduler failed: %s"
            % (str(exc).strip() or type(exc).__name__)
        )
        logger.exception("Device worker scheduler failed")
    finally:
        if active:
            _shutdown_device_workers(
                active,
                cancel_event,
                results,
                drain,
                status="interrupted" if interrupted else "failed",
                reason=scheduler_error
                or "device worker scheduler stopped before workers completed",
                config=config,
                run_id=run_id,
            )
        try:
            cancel_event.set()
        except Exception:
            pass
        drain()
        try:
            cancel_join = getattr(result_queue, "cancel_join_thread", None)
            if callable(cancel_join):
                # All workers have been joined and their messages drained;
                # never let a broken feeder thread hold the web job open.
                cancel_join()
            result_queue.close()
            result_queue.join_thread()
        except (AttributeError, OSError, ValueError):
            pass
        if scheduler_error:
            # A host/IPC failure can occur after one or more worker rows have
            # already reached ``results``.  Normalize every unfinished row,
            # not only the still-pending queue entries, so a stale ``paused``
            # or ``running`` row cannot be mistaken for a terminal failure on
            # the next host restart.  KeyboardInterrupt remains the explicit
            # user-stop path and is handled as a manual pause below.
            if not interrupted:
                for serial in devices:
                    existing = results.get(serial)
                    row = dict(existing) if isinstance(existing, Mapping) else {}
                    state_value = str(
                        row.get("resume_state") or row.get("status") or ""
                    ).strip().lower()
                    if state_value in TERMINAL_RESUME_STATES or state_value == "needs_manual_resume":
                        continue
                    target_state = (
                        "waiting_for_device"
                        if state_value == "waiting_for_device"
                        else "recovering"
                    )
                    row.setdefault("device_serial", serial)
                    row.update(
                        {
                            "status": target_state,
                            "execution_status": target_state,
                            "coverage_status": target_state,
                            "resume_state": target_state,
                            "recoverable": True,
                            "automatic_resume": True,
                            "waiting_for_device": target_state == "waiting_for_device",
                            "recovery_category": "scheduler_error",
                            "recovery_reason": scheduler_error,
                            "error": scheduler_error,
                        }
                    )
                    results[serial] = row
                    persisted_ok = _persist_scheduler_resume_state(
                        config,
                        run_id,
                        serial,
                        state=target_state,
                        attempts=_report_nonnegative_int(
                            row.get("resume_attempts"), 0
                        ),
                        error=scheduler_error,
                    )
                    if persisted_ok is False:
                        persistence_failed_serials.add(serial)
                        _mark_cleanup_gate_failure(
                            config,
                            run_id,
                            serial,
                            "resume state could not be persisted; manual resume required",
                        )
            for serial in pending:
                if serial in persistence_failed_serials:
                    continue
                # A host/IPC failure must leave an unfinished device
                # resumable.  Preserve any durable row first; only a fresh
                # device with no report can use the minimal scheduler result.
                existing = results.get(serial)
                if existing is None:
                    existing = _worker_result_from_report(serial, config, run_id)
                if isinstance(existing, Mapping) and existing:
                    row = dict(existing)
                else:
                    row = _worker_result_row(
                        serial,
                        "paused" if interrupted else "recovering",
                        scheduler_error,
                    )
                row.setdefault("device_serial", serial)
                if interrupted:
                    row["status"] = "paused"
                    row["execution_status"] = "paused"
                    row["coverage_status"] = "paused"
                    row["resume_state"] = "paused"
                    row["recoverable"] = False
                    row["automatic_resume"] = False
                else:
                    row["status"] = "recovering"
                    row["execution_status"] = "recovering"
                    row["coverage_status"] = "recovering"
                    row["resume_state"] = "recovering"
                    row["recoverable"] = True
                    row["automatic_resume"] = True
                row["error"] = scheduler_error
                results[serial] = row
                if interrupted:
                    persisted_ok = _persist_scheduler_resume_state(
                        config,
                        run_id,
                        serial,
                        state="paused",
                        attempts=_report_nonnegative_int(
                            row.get("resume_attempts", 0)
                        ),
                        error=scheduler_error,
                    )
                else:
                    persisted_ok = _persist_scheduler_resume_state(
                        config,
                        run_id,
                        serial,
                        state="recovering",
                        attempts=_report_nonnegative_int(
                            row.get("resume_attempts", 0)
                        ),
                        error=scheduler_error,
                    )
                if persisted_ok is False:
                    persistence_failed_serials.add(serial)
                    _mark_cleanup_gate_failure(
                        config,
                        run_id,
                        serial,
                        "resume state could not be persisted; manual resume required",
                    )
            for serial in list(waiting_for_device):
                if serial in persistence_failed_serials:
                    waiting_for_device.pop(serial, None)
                    continue
                result = dict(results.get(serial, {}))
                result.setdefault("device_serial", serial)
                if interrupted:
                    # Explicit user cancellation is a manual gate.  Keep the
                    # checkpoint intact, but do not let the reconnect poller
                    # automatically relaunch it later.
                    result["status"] = "paused"
                    result["execution_status"] = "paused"
                    result["coverage_status"] = "paused"
                    result["resume_state"] = "paused"
                    result["recoverable"] = False
                    result["automatic_resume"] = False
                    result["waiting_for_device"] = False
                else:
                    # A scheduler/host failure is recoverable.  The waiting
                    # state is durable and will be picked up by the service
                    # restart dispatcher once the transport returns.
                    result["status"] = "waiting_for_device"
                    result["execution_status"] = "waiting_for_device"
                    result["coverage_status"] = "waiting_for_device"
                    result["resume_state"] = "waiting_for_device"
                    result["recoverable"] = True
                    result["automatic_resume"] = True
                    result["waiting_for_device"] = True
                result["error"] = scheduler_error
                results[serial] = result
                persisted_ok = _persist_scheduler_resume_state(
                    config,
                    run_id,
                    serial,
                    state="paused" if interrupted else "waiting_for_device",
                    attempts=_report_nonnegative_int(
                        result.get("resume_attempts", 0)
                    ),
                    error=scheduler_error,
                )
                if persisted_ok is False:
                    persistence_failed_serials.add(serial)
                    _mark_cleanup_gate_failure(
                        config,
                        run_id,
                        serial,
                        "resume state could not be persisted; manual resume required",
                    )
                waiting_for_device.pop(serial, None)

    # The queue may have delivered an older heartbeat after the worker's
    # atomic final report.  Reconcile every device one last time before adding
    # defaults or building the run summary.
    _refresh_worker_results_from_disk(results, devices, config, run_id)
    for serial in devices:
        results.setdefault(
            serial,
            {
                "device_serial": serial,
                "status": "interrupted" if interrupted else "failed",
                "execution_status": "interrupted" if interrupted else "failed",
                "coverage_status": "interrupted" if interrupted else "failed",
                "error": "no worker result",
            },
        )

    for serial in devices:
        result = results[serial]
        state = listed_states.get(serial, {})
        result.setdefault("adb_state", state.get("adb_state", "missing"))
        result.setdefault("ready", serial in runnable)
        result.setdefault("execution_status", result.get("status", "failed"))
        result.setdefault("coverage_status", "failed")
        result.setdefault("operation_count", 0)
        result.setdefault("pressure_elapsed_seconds", 0.0)
        result.setdefault("duration_ratio", 0.0)
        device_info = result.get("device_info", {})
        model = ""
        if isinstance(device_info, Mapping):
            model = " ".join(
                str(device_info.get(key) or "").strip()
                for key in ("manufacturer", "model")
                if str(device_info.get(key) or "").strip()
            )
        result["model"] = model or str(state.get("model") or "")

    coverage_counts: Dict[str, int] = {}
    matrix: List[Dict[str, Any]] = []
    for serial in devices:
        item = dict(results[serial])
        coverage_status = str(item.get("coverage_status") or "unknown")
        coverage_counts[coverage_status] = coverage_counts.get(coverage_status, 0) + 1
        matrix.append(item)
    all_devices_completed = bool(matrix) and all(
        item.get("coverage_status") == "complete"
        and _worker_result_terminal_state(item)
        in {"completed", "passed", "completed_with_crashes"}
        for item in matrix
    )
    summary_status = (
        "interrupted"
        if interrupted
        else "completed"
        if all_devices_completed
        else "completed_with_gaps"
    )
    summary: Dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "package_name": config.get("app", {}).get("package_name", "")
        if isinstance(config.get("app", {}), Mapping)
        else "",
        "started_at": run_started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": summary_status,
        "expected_device_count": len(devices),
        "runnable_device_count": len(runnable),
        "coverage_counts": coverage_counts,
        "devices": matrix,
    }
    paths_config = config.get("paths", {})
    reports_root = resolve_project_path(
        str(paths_config.get("reports_dir", "./reports"))
        if isinstance(paths_config, Mapping)
        else "./reports"
    )
    summary_dir = reports_root / ("%s_run_summary" % run_id)
    summary_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary_paths = ReportGenerator.write_run_summary(summary, summary_dir)
        summary["artifacts"] = summary_paths
        ReportGenerator.write_json(summary, summary_dir / "run_summary.json")
        results["__run_summary__"] = {
            "device_serial": "__run_summary__",
            "status": summary_status,
            "report_dir": str(summary_dir),
            "artifacts": summary_paths,
            "coverage_counts": coverage_counts,
            "expected_device_count": len(devices),
        }
    except OSError as exc:
        logger.exception("Unable to write run summary in %s", summary_dir)
        results["__run_summary__"] = {
            "device_serial": "__run_summary__",
            "status": "failed",
            "report_dir": str(summary_dir),
            "error": str(exc),
        }
    return results, interrupted


def run_from_config(
    config_path: Optional[str] = None,
    requested_devices: Optional[Iterable[str]] = None,
    run_id: Optional[str] = None,
) -> Tuple[int, Dict[str, Dict[str, Any]]]:
    """Load settings, require a local APK, and run selected attached devices."""
    try:
        config = load_config(config_path)
        require_local_apk(config)
        device_states = DeviceManager.get_device_states()
        devices = _resolve_device_targets(config, requested_devices, device_states)
    except (ConfigurationError, RuntimeError, OSError) as exc:
        logging.getLogger(__name__).error("Cannot start test: %s", exc)
        return 2, {}
    results, interrupted = run_devices(
        devices, config, run_id=run_id, device_states=device_states
    )
    failed = any(
        (
            result.get("status") in {"failed", "completed_with_errors", "not_run"}
            or result.get("coverage_status") not in {None, "complete"}
        )
        for serial, result in results.items()
        if serial != "__run_summary__"
    )
    return (130 if interrupted else 1 if failed else 0), results


def main(
    config_path: Optional[str] = None,
    requested_devices: Optional[Iterable[str]] = None,
) -> int:
    """Public script-style entry point returning a shell exit code."""
    code, _results = run_from_config(config_path, requested_devices)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised manually
    raise SystemExit(main())
