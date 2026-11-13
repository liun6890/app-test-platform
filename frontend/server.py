"""Local HTTP API and static server for the Fastbot web console.

This module is intentionally kept under ``frontend``. It delegates device and
test operations to the existing project modules and CLI without changing them.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
from collections import OrderedDict
import json
import math
import mimetypes
import os
import re
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from console_utils import (  # noqa: F401 - re-exported for tests and patching
    _draft_integer, _path_is_within, _resume_mode_from_source, _ui_bool, safe_name,
)
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


FRONTEND_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = FRONTEND_ROOT.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.configuration import (  # noqa: E402
    MAX_SAFE_WORKERS,
    load_config,
    resolve_project_path,
)
from core.file_io import atomic_write_text  # noqa: E402
from console_apk import PACKAGE_PATTERN, _aapt_path, apk_package_name  # noqa: F401 - re-exported
from console_scenario import (  # noqa: F401 - re-exported for tests and patching
    _audit_gate_step, _is_audit_gate_step, _is_gate_confirmation_step, _step_locator_text,
    _without_audit_gate_steps, apply_audit_gate_policy, scenario_start_pages,
)
from console_store import (  # noqa: F401 - re-exported for tests and patching
    _CHECKPOINT_CACHE, _CHECKPOINT_CACHE_LOCK, _CHECKPOINT_CACHE_MAX_ENTRIES,
    _CHECKPOINT_FILE_NAME, _DEVICE_PROBE_CACHE, _DEVICE_PROBE_LOCK,
    _DEVICE_PROBE_TTL_SECONDS, _JOB_EVIDENCE_CACHE, _JOB_EVIDENCE_LOCK,
    _JSON_CACHE, _REPORT_PATHS_CACHE, _REPORT_PATHS_LOCK, _REPORT_SUMMARY_CACHE,
    _REPORT_SUMMARY_LOCK, _cached_json_document, read_json_file, write_json_file,
)
from core.checkpoint import (  # noqa: E402
    AUTOMATIC_RECOVERY_STATES,
    MANUAL_RECOVERY_STATES,
    MANUALLY_ENDED_STATE,
    RESUME_CLAIM_NAME,
    TERMINAL_RESUME_STATES,
    acquire_resume_claim,
    classify_recovery,
    completed_cycle_numbers,
    effective_resume_state,
    is_completed_cycle,
    load_checkpoint,
    recovery_request_path,
    request_manual_resume,
    release_resume_claim,
    resume_claim_active,
    sha256_file,
    terminal_resume_state,
    unique_cycle_rows,
    update_resume_claim,
)
from core.fastbot_trace import parse_fastbot_log  # noqa: E402
from core.replay_plan import build_replay_trajectory  # noqa: E402
from core.reporting import describe_permission_failure  # noqa: E402
from core.fastbot_assets import (  # noqa: E402
    bundled_native_remote_path,
    first_supported_abi,
    normalize_runtime_options,
    runtime_bundle_has_native,
    runtime_asset_md5,
    runtime_asset_path,
    runtime_remote_path,
)


def runtime_storage_root() -> Path:
    """Return a writable root for mutable web-console state.

    Development workspaces may expose ``frontend/runtime`` as a Windows
    junction to a secondary drive.  That drive can be readable but deny
    writes to the service account, which used to make ``POST /api/runs`` fail
    before the CLI was started.  Keep explicit output-root overrides intact;
    only the implicit junction-backed location gets a local fallback.
    """
    configured_root = str(os.environ.get("APP_PRESS_TEST_OUTPUT_ROOT") or "").strip()
    if not configured_root:
        linked_root = FRONTEND_ROOT / "runtime"
        try:
            # ``stat`` follows a junction and therefore hides the reparse
            # attribute; ``lstat`` keeps the link metadata itself.
            attributes = int(getattr(linked_root.lstat(), "st_file_attributes", 0) or 0)
        except OSError:
            attributes = 0
        # FILE_ATTRIBUTE_REPARSE_POINT (0x400) covers directory junctions on
        # Windows.  A regular runtime directory continues to use the shared
        # output-root policy, including the existing E:/D: fallback.
        if attributes & 0x400:
            fallback = resolve_project_path("./frontend/runtime-local")
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback.resolve(strict=False)
    return resolve_project_path("./frontend/runtime")


DIST_ROOT = FRONTEND_ROOT / "dist"
RUNTIME_ROOT = runtime_storage_root()
UPLOAD_ROOT = RUNTIME_ROOT / "uploads"
RUN_ROOT = RUNTIME_ROOT / "runs"
DRAFT_PATH = RUNTIME_ROOT / "draft.json"
# ``runtime`` may be a junction that is not writable by the account running
# the web server.  New mutable state uses ``RUNTIME_ROOT`` above, but drafts
# and APKs created by an older server must remain readable during migration.
LEGACY_RUNTIME_ROOT = resolve_project_path("./frontend/runtime")
LEGACY_UPLOAD_ROOT = LEGACY_RUNTIME_ROOT / "uploads"
LEGACY_DRAFT_PATH = LEGACY_RUNTIME_ROOT / "draft.json"
CLI_PATH = PROJECT_ROOT / "cli.py"
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.json"
CONFIG_ROOT = PROJECT_ROOT / "config"
VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
# The project virtualenv is the default interpreter, but endpoint security
# software can deny file writes per executable path (this host blocks the venv
# python from writing outside the workspace while the system python is fine).
# An explicit override lets the console run its workers with a writable
# interpreter instead of failing every report write.
_RUNTIME_PYTHON_OVERRIDE = str(
    os.environ.get("APP_PRESS_TEST_RUNTIME_PYTHON") or ""
).strip()
RUNTIME_PYTHON = (
    Path(_RUNTIME_PYTHON_OVERRIDE)
    if _RUNTIME_PYTHON_OVERRIDE
    else (VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))
)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_APK_BYTES = 4 * 1024 * 1024 * 1024
MAX_REPLAY_EVENTS = 20000
LOCAL_STOP_GRACE_SECONDS = 20
ACTIVE_JOB_STATUSES = {
    "queued",
    "awaiting_install",
    "preparing",
    "installing",
    "running",
    "stopping",
    "recovering",
    "waiting_for_device",
    "resuming",
    "paused",
    "needs_manual_resume",
}
_REMOTE_CLEANUP_RELEASED = "released"
_REMOTE_CLEANUP_DEVICE_UNAVAILABLE = "device_unavailable"
_REMOTE_CLEANUP_FAILED = "cleanup_failed"
_REMOTE_CLEANUP_TIMEOUT_SECONDS = 30.0
_REMOTE_TRANSPORT_PROBE_TIMEOUT_SECONDS = 2.0


def _is_process_handle(value: Any) -> bool:
    """Return whether *value* behaves like a child process.

    Tests often replace ``subprocess.Popen`` with a mock object. In that case
    ``isinstance(value, subprocess.Popen)`` raises ``TypeError`` because the
    replacement is no longer a class, so use the small process interface as a
    compatibility fallback.
    """
    if value is None:
        return False
    process_type = getattr(subprocess, "Popen", None)
    if isinstance(process_type, type):
        return isinstance(value, process_type)
    return callable(getattr(value, "poll", None))

for directory in (RUNTIME_ROOT, UPLOAD_ROOT, RUN_ROOT):
    directory.mkdir(parents=True, exist_ok=True)

from core.crash_handler import CrashHandler  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402
from core.vendor_install import InstallPromptClicker  # noqa: E402
from core.mutex_controller import MutexController  # noqa: E402
from core.reporting import ReportGenerator  # noqa: E402


JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.RLock()
# The key is the original job id and device serial.  A job can have more than
# one CLI invocation while other devices continue, so a job-wide boolean is not
# sufficient to prevent duplicate workers.
RESUME_IN_FLIGHT: set[Tuple[str, str]] = set()
# Serialize resume requests for the same job/device while the durable claim is
# being acquired and the old controller is being cleaned up.  The lock is
# intentionally per device so a slow/offline resume cannot block unrelated
# devices in the same job.
_RESUME_REQUEST_LOCKS_GUARD = threading.Lock()
_RESUME_REQUEST_LOCKS: Dict[Tuple[str, str], threading.RLock] = {}
REPLAY_DEVICES: set[str] = set()
REPLAY_LOCK = threading.RLock()


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status = int(status)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


AUDIT_LOG = RUNTIME_ROOT / "audit.jsonl"
_AUDIT_LOCK = threading.Lock()

# A console log is append-only for the lifetime of a job; rotate at a fixed size
# so a long campaign cannot grow one file without bound.
LOG_ROTATE_BYTES = 20 * 1024 * 1024


def open_job_log(path: Path) -> Any:
    """Open a job console log for appending, rotating a large one first."""
    target = Path(path)
    try:
        if target.is_file() and target.stat().st_size >= LOG_ROTATE_BYTES:
            backup = target.with_suffix(target.suffix + ".1")
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            try:
                target.rename(backup)
            except OSError:
                pass
    except OSError:
        pass
    return target.open("ab", buffering=0)


def audit_event(action: str, **fields: Any) -> None:
    """Append one operator action to the local audit trail.

    The console has no authentication because it only listens on loopback; an
    append-only record of who started/stopped what is the compensating control
    that keeps the history reviewable after the fact.
    """
    record: Dict[str, Any] = {"time": utc_now(), "action": action}
    for key, value in fields.items():
        if value is None:
            continue
        record[key] = value
    try:
        with _AUDIT_LOCK:
            with AUDIT_LOG.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        sys.stderr.write("audit: unable to append %s\n" % action)


def runtime_health() -> Dict[str, Any]:
    try:
        import uiautomator2  # noqa: F401
        uiautomator_available = True
    except ImportError:
        uiautomator_available = False
    return {
        "ok": uiautomator_available,
        "mode": "local",
        "engine": "fastbot",
        "python": str(RUNTIME_PYTHON),
        "uiautomator2": uiautomator_available,
        "time": utc_now(),
    }


def _cached_checkpoint(
    report_dir: Path,
    *,
    run_id: Optional[str] = None,
    device_serial: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return a validated checkpoint, reusing it while the file is unchanged."""
    directory = Path(report_dir)
    path = directory / _CHECKPOINT_FILE_NAME
    try:
        stat = path.stat()
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        stamp = (0, 0)
    key = (str(path), stamp, str(run_id or ""), str(device_serial or ""))
    with _CHECKPOINT_CACHE_LOCK:
        if key in _CHECKPOINT_CACHE:
            _CHECKPOINT_CACHE.move_to_end(key)
            return _CHECKPOINT_CACHE[key]
    value = load_checkpoint(
        directory, run_id=run_id, device_serial=device_serial
    )
    with _CHECKPOINT_CACHE_LOCK:
        _CHECKPOINT_CACHE[key] = value
        _CHECKPOINT_CACHE.move_to_end(key)
        while len(_CHECKPOINT_CACHE) > _CHECKPOINT_CACHE_MAX_ENTRIES:
            _CHECKPOINT_CACHE.popitem(last=False)
    return value


def _cached_device_probe(
    key: str, factory: Any, ttl: Optional[float] = None
) -> Any:
    """Memoize a short ADB probe so one poll does not repeat it per device."""
    now = time.monotonic()
    lifetime = _DEVICE_PROBE_TTL_SECONDS if ttl is None else max(0.0, float(ttl))
    with _DEVICE_PROBE_LOCK:
        entry = _DEVICE_PROBE_CACHE.get(key)
        if entry is not None and now - entry[0] < lifetime:
            return entry[1]
    value = factory()
    with _DEVICE_PROBE_LOCK:
        _DEVICE_PROBE_CACHE[key] = (time.monotonic(), value)
        if len(_DEVICE_PROBE_CACHE) > 128:
            oldest = min(_DEVICE_PROBE_CACHE.items(), key=lambda item: item[1][0])[0]
            _DEVICE_PROBE_CACHE.pop(oldest, None)
    return value


# Model, Android version and screen size do not change while the console keeps
# running; probing them on every poll cost five adb calls per device.
_DEVICE_INFO_CACHE: Dict[str, Dict[str, Any]] = {}
_DEVICE_INFO_LOCK = threading.Lock()
_DEVICE_READY_TTL_SECONDS = 30.0


def _cached_device_info(serial: str) -> Dict[str, Any]:
    with _DEVICE_INFO_LOCK:
        cached = _DEVICE_INFO_CACHE.get(serial)
    if cached is not None:
        return dict(cached)
    info = DeviceManager.get_device_info(serial)
    if not isinstance(info, Mapping):
        return {}
    with _DEVICE_INFO_LOCK:
        _DEVICE_INFO_CACHE[serial] = dict(info)
    return dict(info)


def _job_resume_mode(
    job: Optional[Mapping[str, Any]],
    report: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return the durable resume mode for one job/report pair."""
    sources: List[Any] = []
    if isinstance(job, Mapping):
        sources.extend(
            (
                job.get("resume"),
                job.get("resumeMode"),
                job.get("config"),
            )
        )
        config_path = str(job.get("configPath") or "").strip()
        if config_path:
            value = read_json_file(Path(config_path), None)
            if isinstance(value, Mapping):
                sources.append(value)
    if isinstance(report, Mapping):
        sources.extend(
            (
                report.get("resume"),
                report.get("resume_mode"),
                report.get("config"),
            )
        )
    for source in sources:
        mode = _resume_mode_from_source(source)
        if mode:
            return mode
    return "auto"


def _can_resume_device_state(state: Any, *, resume_mode: str = "auto") -> bool:
    """Whether the UI should expose a device-level resume action.

    Both automatic and manual recovery states are actionable from the device
    card.  The automatic scheduler may already be polling a transport failure,
    but an explicit click is still useful (and is guarded by the durable claim
    below) when an operator knows the device has returned.  ``resuming`` is
    intentionally excluded because it denotes an already claimed worker.
    """
    normalized = str(state or "").strip().lower()
    # Keep the argument for callers that read old job/config snapshots.  The
    # mode controls whether the background dispatcher acts, not whether the
    # operator may explicitly request a resume.
    _ = resume_mode
    return normalized in (AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES)


def _public_device_resume_status(state: Any) -> str:
    """Translate internal device states to the stable API vocabulary.

    ``manually_ended`` is intentionally an internal terminal marker.  The
    console and older API consumers should continue to see the public
    ``stopped`` status while ``resumeState`` retains the precise marker.
    """
    normalized = str(state or "").strip().lower()
    return "stopped" if normalized == MANUALLY_ENDED_STATE else normalized


def default_ui_config() -> Dict[str, Any]:
    raw = read_json_file(DEFAULT_SETTINGS, {})
    app = raw.get("app", {}) if isinstance(raw, dict) else {}
    test = raw.get("test", {}) if isinstance(raw, dict) else {}
    parallel = raw.get("parallel", {}) if isinstance(raw, dict) else {}
    screenshot = raw.get("screenshot", {}) if isinstance(raw, dict) else {}
    watchdog = raw.get("stall_watchdog", {}) if isinstance(raw, dict) else {}
    fastbot = raw.get("fastbot", {}) if isinstance(raw, dict) else {}
    scenario = raw.get("scenario", {}) if isinstance(raw, dict) else {}
    # Keep the settings file usable when no selectable Fastbot template is
    # available (for example, immediately after a fresh checkout).  The
    # uploaded APK will replace this placeholder package in generic mode.
    package_name = str(app.get("package_name") or "") if isinstance(app, Mapping) else ""
    selected_template = None
    if str(fastbot.get("engine") or "").lower() != "fastbot":
        selected_template = next(iter(available_configs()), None)
    if selected_template:
        config_file = str(selected_template["name"])
        package_name = str(selected_template["packageName"])
        duration = int(selected_template["duration"])
        cycles = int(selected_template["cycles"])
        throttle = int(selected_template["throttle"])
        workers = min(MAX_SAFE_WORKERS, int(selected_template["workers"]))
        screenshot_enabled = bool(selected_template["screenshot"])
        watchdog_enabled = bool(selected_template["stallWatchdog"])
        watchdog_mode = str(selected_template["stallMode"])
        clear_app_data = bool(selected_template["clearAppData"])
        stop_on_crash = bool(selected_template["stopOnCrash"])
        classpath = str(selected_template["classpath"])
        expected_md5 = str(selected_template["expectedMd5"])
        extra_args = list(selected_template["extraArgs"])
        scenario_target = str(selected_template["scenarioTarget"])
        navigation_steps = int(selected_template["navigationSteps"])
        startup_page = str((selected_template.get("startupPages") or [{}])[0].get("id") or "")
    else:
        config_file = DEFAULT_SETTINGS.name
        duration = max(1, int(test.get("fastbot_duration_minutes", 10) or 10))
        cycles = max(1, int(test.get("max_cycles", 1) or 1))
        throttle = max(0, int(test.get("fastbot_throttle_ms", 250) or 250))
        workers = min(
            MAX_SAFE_WORKERS, max(1, int(parallel.get("max_workers", 1) or 1))
        )
        screenshot_enabled = bool(screenshot.get("enabled", True))
        watchdog_enabled = bool(watchdog.get("enabled", True))
        watchdog_mode = str(
            watchdog.get(
                "mode", "recover_once" if watchdog_enabled else "observe"
            )
        )
        if watchdog_mode not in {"observe", "recover_once", "recover"}:
            watchdog_mode = "recover_once" if watchdog_enabled else "observe"
        clear_app_data = bool(test.get("clear_app_data_each_cycle", True))
        stop_on_crash = bool(test.get("stop_on_crash", False))
        classpath = str(fastbot.get("classpath") or "")
        expected_md5 = str(fastbot.get("expected_monkeyq_md5") or "")
        extra_args = list(fastbot.get("extra_args") or [])
        scenario_target = str(scenario.get("target_page") or "")
        navigation_steps = len(scenario.get("navigation_steps") or []) if isinstance(scenario.get("navigation_steps"), list) else 0
        startup_page = str((scenario_start_pages(scenario) or [{}])[0].get("id") or "")
    return {
        "configFile": config_file,
        "workflowMode": "matched",
        "packageName": package_name,
        "apkPackageName": "",
        "apkName": "",
        "apkPath": "",
        "duration": duration,
        "cycles": cycles,
        "throttle": throttle,
        "workers": workers,
        "screenshot": screenshot_enabled,
        "stallWatchdog": watchdog_enabled,
        "stallMode": watchdog_mode,
        "clearAppData": clear_app_data,
        "stopOnCrash": stop_on_crash,
        "fastbotClasspath": classpath,
        "fastbotMd5": expected_md5,
        "fastbotExtraArgs": extra_args,
        "scenarioTarget": scenario_target,
        "navigationSteps": navigation_steps,
        "startupPage": startup_page,
        "auditGateEveryLaunch": True,
    }


def _config_path(name: str) -> Path:
    """Resolve a selectable project config without allowing path traversal."""
    value = Path(str(name or "").strip())
    if value.is_absolute() or value.suffix.lower() != ".json":
        raise ApiError("配置文件必须是 config 目录下的 JSON 文件")
    candidate = (CONFIG_ROOT / value.name).resolve(strict=False)
    try:
        candidate.relative_to(CONFIG_ROOT.resolve())
    except ValueError as exc:
        raise ApiError("配置文件路径无效") from exc
    if not candidate.is_file():
        raise ApiError("配置文件不存在: %s" % value.name, HTTPStatus.NOT_FOUND)
    return candidate


def available_configs() -> List[Dict[str, Any]]:
    """Return valid Fastbot project configs for the web console selector."""
    rows: List[Dict[str, Any]] = []
    for path in sorted(CONFIG_ROOT.glob("*.json"), key=lambda item: item.name.lower()):
        if path.name.startswith(".tmp"):
            continue
        try:
            raw = load_config(str(path))
        except Exception:
            continue
        app = raw.get("app", {})
        test = raw.get("test", {})
        fastbot = raw.get("fastbot", {})
        scenario = raw.get("scenario", {})
        if not isinstance(app, Mapping) or not isinstance(fastbot, Mapping):
            continue
        if str(fastbot.get("engine") or "").lower() != "fastbot":
            continue
        navigation = scenario.get("navigation_steps", []) if isinstance(scenario, Mapping) else []
        rows.append(
            {
                "name": path.name,
                "packageName": str(app.get("package_name") or ""),
                "duration": int(test.get("fastbot_duration_minutes", 10) or 10),
                "cycles": int(test.get("max_cycles", 1) or 1),
                "throttle": int(test.get("fastbot_throttle_ms", 250) or 250),
                "workers": min(
                    MAX_SAFE_WORKERS,
                    int(raw.get("parallel", {}).get("max_workers", 1) or 1),
                )
                if isinstance(raw.get("parallel"), Mapping)
                else 1,
                "screenshot": bool(raw.get("screenshot", {}).get("enabled", True))
                if isinstance(raw.get("screenshot"), Mapping)
                else True,
                "stallWatchdog": bool(raw.get("stall_watchdog", {}).get("enabled", True))
                if isinstance(raw.get("stall_watchdog"), Mapping)
                else True,
                "stallMode": str(raw.get("stall_watchdog", {}).get("mode", "recover_once"))
                if isinstance(raw.get("stall_watchdog"), Mapping)
                else "recover_once",
                "clearAppData": bool(test.get("clear_app_data_each_cycle", True)),
                "stopOnCrash": bool(test.get("stop_on_crash", False)),
                "classpath": str(fastbot.get("classpath") or ""),
                "expectedMd5": str(fastbot.get("expected_monkeyq_md5") or ""),
                "extraArgs": list(fastbot.get("extra_args") or []),
                "scenarioTarget": str(scenario.get("target_page") or "")
                if isinstance(scenario, Mapping)
                else "",
                "navigationSteps": len(navigation) if isinstance(navigation, list) else 0,
                "startupPages": scenario_start_pages(scenario),
            }
        )
    return rows


def _valid_uploaded_apk(value: Any) -> Optional[Path]:
    """Keep drafts constrained to the current or previous upload directory."""
    candidate = Path(str(value or "")).resolve(strict=False)
    allowed_roots = (UPLOAD_ROOT, LEGACY_UPLOAD_ROOT)
    if not any(_path_is_within(candidate, root) for root in allowed_roots):
        return None
    return candidate if candidate.suffix.lower() == ".apk" and candidate.is_file() else None


def sanitize_draft(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Persist UI choices without accepting arbitrary paths or config fields."""
    raw_devices = payload.get("selectedDevices", [])
    if not isinstance(raw_devices, list):
        raw_devices = []
    selected_devices = list(dict.fromkeys(
        str(value).strip() for value in raw_devices if str(value).strip()
    ))[:64]
    raw_config = payload.get("config", {})
    if not isinstance(raw_config, Mapping):
        raw_config = {}
    config: Dict[str, Any] = {}
    config_file = str(raw_config.get("configFile") or "").strip()
    if config_file:
        try:
            config["configFile"] = _config_path(config_file).name
        except ApiError:
            pass
    package_name = str(raw_config.get("packageName") or "").strip()
    if PACKAGE_PATTERN.fullmatch(package_name):
        config["packageName"] = package_name
    for key, minimum, maximum in (
        ("duration", 1, 1440),
        ("cycles", 1, 100),
        ("throttle", 0, 60000),
        ("workers", 1, 32),
    ):
        number = _draft_integer(raw_config.get(key), minimum, maximum)
        if number is not None:
            if key == "workers":
                selected_limit = len(selected_devices) if selected_devices else MAX_SAFE_WORKERS
                config[key] = min(MAX_SAFE_WORKERS, selected_limit, number)
            else:
                config[key] = number
    for key in ("screenshot", "stallWatchdog", "clearAppData", "stopOnCrash", "auditGateEveryLaunch"):
        if isinstance(raw_config.get(key), bool):
            config[key] = raw_config[key]
    if raw_config.get("stallMode") in {"observe", "recover_once"}:
        config["stallMode"] = raw_config["stallMode"]
    if raw_config.get("workflowMode") in {"matched", "generic"}:
        config["workflowMode"] = raw_config["workflowMode"]
    if raw_config.get("startupPage") in {"audit_gate", "app_home"}:
        config["startupPage"] = raw_config["startupPage"]
    apk_path = _valid_uploaded_apk(raw_config.get("apkPath"))
    apk_package = str(raw_config.get("apkPackageName") or "").strip()
    if apk_path and PACKAGE_PATTERN.fullmatch(apk_package):
        config.update({
            "apkPath": str(apk_path),
            "apkName": safe_name(str(raw_config.get("apkName") or apk_path.name), apk_path.name),
            "apkPackageName": apk_package,
            "packageName": apk_package,
        })
    return {"selectedDevices": selected_devices, "config": config, "savedAt": utc_now()}


def read_draft() -> Dict[str, Any]:
    raw = read_json_file(DRAFT_PATH, None)
    if not isinstance(raw, Mapping) and LEGACY_DRAFT_PATH.resolve(strict=False) != DRAFT_PATH.resolve(strict=False):
        raw = read_json_file(LEGACY_DRAFT_PATH, {})
    return sanitize_draft(raw) if isinstance(raw, Mapping) else {"selectedDevices": [], "config": {}}


def save_draft(payload: Mapping[str, Any]) -> Dict[str, Any]:
    draft = sanitize_draft(payload)
    write_json_file(DRAFT_PATH, draft)
    return draft


def _package_installed(serial: str, package_name: str) -> bool:
    """Return whether Android Package Manager exposes the target package.

    Android builds do not all expose the same package-manager shell alias.
    Check both supported forms and accept output written to either stream.
    """
    for command in (
        ["shell", "pm", "path", package_name],
        ["shell", "cmd", "package", "path", package_name],
    ):
        try:
            result = DeviceManager._run_adb(  # type: ignore[attr-defined]
                command,
                serial=serial,
                timeout=20,
                retries=1,
            )
        except Exception:
            continue
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if any(line.strip().lower().startswith("package:") for line in output.splitlines()):
            return True
    return False


def _installed_package_apk_md5(serial: str, package_name: str) -> Optional[str]:
    """Return the installed base APK fingerprint, or ``None`` when unknown."""
    package_paths: List[str] = []
    for command in (
        ["shell", "pm", "path", package_name],
        ["shell", "cmd", "package", "path", package_name],
    ):
        try:
            result = DeviceManager._run_adb(  # type: ignore[attr-defined]
                command,
                serial=serial,
                timeout=20,
                retries=1,
            )
        except Exception:
            continue
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        package_paths = [
            line.strip()[len("package:") :].strip()
            for line in output.splitlines()
            if line.strip().lower().startswith("package:")
        ]
        if package_paths:
            break
    if not package_paths:
        return None

    base_path = next(
        (path for path in package_paths if path.lower().endswith("/base.apk")),
        package_paths[0],
    )
    for command in (
        ["shell", "md5sum", base_path],
        ["shell", "toybox", "md5sum", base_path],
    ):
        try:
            result = DeviceManager._run_adb(  # type: ignore[attr-defined]
                command,
                serial=serial,
                timeout=30,
                retries=1,
            )
        except Exception:
            continue
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        match = re.search(r"(?i)\b([a-f0-9]{32})\b", output)
        if result.returncode == 0 and match:
            return match.group(1).lower()
    return None


def _local_apk_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_matches_apk(
    serial: str, package_name: str, expected_md5: str
) -> bool:
    """Require the selected APK bytes, not merely the same package name."""
    expected = str(expected_md5 or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", expected):
        return False
    return _installed_package_apk_md5(serial, package_name) == expected


def _active_jobs() -> List[Dict[str, Any]]:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        refresh_job(job)
    return [job for job in jobs if job.get("status") in ACTIVE_JOB_STATUSES]


def _battery_level(serial: str) -> Optional[int]:
    try:
        result = DeviceManager._run_adb(  # type: ignore[attr-defined]
            ["shell", "dumpsys", "battery"], serial=serial, timeout=10, retries=1
        )
        match = re.search(r"^\s*level:\s*(\d+)", result.stdout, re.MULTILINE)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _cached_job_report_paths(job: Mapping[str, Any]) -> List[Path]:
    """Return a job's report paths, re-walking only when the tree changes.

    ``rglob`` plus a ``resolve`` per candidate runs for every job on every
    poll; caching it on the report root's identity keeps the listing cheap
    while a new cycle (which creates a directory) still invalidates it.
    """
    root = str(job.get("reportRoot") or "").strip()
    if not root:
        return []
    try:
        root_path = Path(root)
        stat = root_path.stat()
        # report.json files appear after their device directory already exists,
        # so the root's own mtime is not enough: one early walk cached an empty
        # list and hid every device for the whole run.
        children = tuple(
            sorted(
                (child.name, int(child.stat().st_mtime_ns))
                for child in root_path.iterdir()
                if child.is_dir()
            )
        )
    except OSError:
        return []
    key = (int(stat.st_mtime_ns), children)
    with _REPORT_PATHS_LOCK:
        cached = _REPORT_PATHS_CACHE.get(root)
        if cached is not None and cached[0] == key:
            return list(cached[1])
    paths = _job_report_paths(job)
    with _REPORT_PATHS_LOCK:
        _REPORT_PATHS_CACHE[root] = (key, paths)
        if len(_REPORT_PATHS_CACHE) > 64:
            _REPORT_PATHS_CACHE.pop(next(iter(_REPORT_PATHS_CACHE)))
    return list(paths)


def _job_evidence_signature(job: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Cheap identity of everything ``_job_evidence`` reads from disk."""
    # In-memory job fields also feed the evidence (device list, manual-end
    # markers, cycle budget).  Hashing them keeps an edited job from serving a
    # stale cached evidence after an operator action.
    mutable_fields = (
        "id",
        "runId",
        "status",
        "phase",
        "devices",
        "cycles",
        "duration",
        "startedAt",
        "finishedAt",
        "installation",
        "manualStopRequested",
        "manualStopDevices",
        "manualEndDevices",
    )
    state = {
        key: job.get(key)
        for key in mutable_fields
        if job.get(key) is not None
    }
    parts: List[Any] = [
        hashlib.sha256(
            json.dumps(state, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    ]
    for path in _cached_job_report_paths(job):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
        log_path = path.parent / "fastbot.log"
        try:
            log_stat = log_path.stat()
        except OSError:
            continue
        parts.append(
            (str(log_path), int(log_stat.st_mtime_ns), int(log_stat.st_size))
        )
    return tuple(parts)


def _cached_job_evidence(job: Mapping[str, Any]) -> Dict[str, Any]:
    """Memoize ``_job_evidence`` until one of its input files changes."""
    signature = _job_evidence_signature(job)
    identifier = str(job.get("id") or "")
    with _JOB_EVIDENCE_LOCK:
        cached = _JOB_EVIDENCE_CACHE.get(identifier)
        if cached is not None and cached[0] == signature:
            # Callers may stamp phase/finishedAt onto the evidence, so hand out
            # a shallow copy instead of the cached instance.
            return dict(cached[1])
    value = _job_evidence(job)
    with _JOB_EVIDENCE_LOCK:
        _JOB_EVIDENCE_CACHE[identifier] = (signature, value)
        if len(_JOB_EVIDENCE_CACHE) > 128:
            _JOB_EVIDENCE_CACHE.pop(next(iter(_JOB_EVIDENCE_CACHE)))
    return dict(value)


def _job_report_paths(job: Mapping[str, Any]) -> List[Path]:
    """Return report files strictly below a job's report root.

    ``reportRoot`` is persisted in ``job.json`` and is therefore untrusted
    input after a host restart.  Resolving both sides before the containment
    check also prevents a symlinked report from making another job visible.
    Checkpoint discovery uses the same root policy, but is deliberately kept
    separate because a checkpoint may exist before ``report.json``.
    """
    raw_root = str(job.get("reportRoot") or "").strip()
    if not raw_root:
        return []
    try:
        report_root = Path(raw_root).resolve(strict=False)
    except (OSError, ValueError, TypeError):
        return []
    if not report_root.is_dir():
        return []
    paths: List[Path] = []
    try:
        candidates = report_root.rglob("report.json")
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(report_root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                paths.append(resolved)
    except OSError:
        return []
    return list(dict.fromkeys(paths))


def _job_report_root(job: Mapping[str, Any]) -> Optional[Path]:
    """Resolve a job report root, returning ``None`` for malformed paths."""
    raw_root = str(job.get("reportRoot") or "").strip()
    if not raw_root:
        return None
    try:
        root = Path(raw_root).resolve(strict=False)
    except (OSError, ValueError, TypeError):
        return None
    return root


def _job_execution_run_id(job: Mapping[str, Any]) -> str:
    """Return the CLI execution id associated with a web job.

    A web job id is the stable API identity.  Older jobs created the device
    reports with a separate timestamp-based run id, so blindly using ``id``
    makes an otherwise valid checkpoint invisible after a service restart.
    Prefer an explicit persisted mapping, then infer one from report summaries
    or the strictly contained report files.  New jobs persist ``runId`` and
    therefore never need the discovery fallback.
    """
    for key in ("runId", "run_id"):
        value = str(job.get(key) or "").strip()
        if value:
            return value

    candidates: Dict[str, int] = {}
    summary_values = job.get("reports")
    if isinstance(summary_values, Mapping):
        summary_iterable = summary_values.values()
    elif isinstance(summary_values, list):
        summary_iterable = summary_values
    else:
        summary_iterable = []
    for item in summary_iterable:
        if not isinstance(item, Mapping):
            continue
        value = str(item.get("runId") or item.get("run_id") or "").strip()
        if value:
            candidates[value] = candidates.get(value, 0) + 1

    # The report root is untrusted persisted state; _job_report_paths performs
    # the containment and symlink checks before we inspect any file below it.
    configured_serials = {
        str(value).strip()
        for value in (job.get("devices", []) or [])
        if str(value).strip()
    }
    package_name = str(job.get("packageName") or job.get("package_name") or "").strip()
    for path in _cached_job_report_paths(job):
        report = _cached_json_document(path, None)
        if not isinstance(report, Mapping):
            continue
        serial = str(report.get("device_serial") or "").strip()
        if configured_serials and serial not in configured_serials:
            continue
        report_package = str(report.get("package_name") or report.get("packageName") or "").strip()
        if package_name and report_package and report_package != package_name:
            continue
        value = str(report.get("run_id") or report.get("job_id") or "").strip()
        if value:
            candidates[value] = candidates.get(value, 0) + 1

    if candidates:
        # A single execution id is expected.  Count wins when a partially
        # migrated job contains both old and newly written summaries.
        return max(candidates, key=lambda value: (candidates[value], value))
    return str(job.get("id") or "").strip()


def _job_known_execution_ids(job: Mapping[str, Any]) -> set[str]:
    """Return web and execution ids accepted for durable artifact identity."""
    values: set[str] = set()
    for key in ("id", "runId", "run_id"):
        value = str(job.get(key) or "").strip()
        if value:
            values.add(value)
    summary_values = job.get("reports")
    if isinstance(summary_values, Mapping):
        summary_iterable = summary_values.values()
    elif isinstance(summary_values, list):
        summary_iterable = summary_values
    else:
        summary_iterable = []
    for item in summary_iterable:
        if isinstance(item, Mapping):
            for key in ("runId", "run_id", "testId", "job_id"):
                value = str(item.get(key) or "").strip()
                if value:
                    values.add(value)
    execution_id = _job_execution_run_id(job)
    if execution_id:
        values.add(execution_id)
    return values


def _expected_device_report_path(
    job: Mapping[str, Any], serial: str
) -> Optional[Path]:
    """Return the canonical report path for one job/device pair.

    The executor uses ``<execution run id>_<safe serial>/report.json``.  Keeping this
    calculation in one place lets the web service recognise a checkpoint-only
    device without accepting an arbitrary ``checkpoint.json`` planted below a
    report root.
    """
    root = _job_report_root(job)
    job_id = _job_execution_run_id(job)
    serial_value = str(serial or "").strip()
    if root is None or not job_id or not serial_value:
        return None
    try:
        candidate = (
            root
            / ("%s_%s" % (job_id, re.sub(r"[^A-Za-z0-9_.-]", "_", serial_value)))
            / "report.json"
        ).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate


def _strict_job_checkpoint(
    path: Path,
    job: Mapping[str, Any],
    serial: str,
    *,
    allowed_parent: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Load a checkpoint only when its identity and location are exact.

    ``core.checkpoint.load_checkpoint`` intentionally accepts blank identity
    fields for compatibility with old files.  The web-facing discovery path
    must be stricter: a blank/foreign job or device id could otherwise expose
    another task's progress or allow resuming the wrong device.
    """
    root = _job_report_root(job)
    job_id = str(job.get("id") or "").strip()
    known_ids = _job_known_execution_ids(job)
    serial_value = str(serial or "").strip()
    if root is None or not job_id or not serial_value:
        return None
    expected_report = _expected_device_report_path(job, serial_value)
    if expected_report is None and allowed_parent is None:
        return None
    try:
        checkpoint_path = Path(path).resolve(strict=False)
        checkpoint_path.relative_to(root)
        # A checkpoint is accepted only beside the canonical report path.  A
        # matching id in an arbitrary child directory is not enough to prove
        # that it belongs to this device.
        expected_parent = (
            Path(allowed_parent).resolve(strict=False)
            if allowed_parent is not None
            else expected_report.parent
        )
        if checkpoint_path.parent != expected_parent:
            return None
    except (OSError, ValueError):
        return None
    value = read_json_file(checkpoint_path, None)
    if not isinstance(value, Mapping):
        return None
    checkpoint_job_id = str(value.get("job_id") or "").strip()
    if not checkpoint_job_id or checkpoint_job_id not in known_ids:
        return None
    if str(value.get("device_serial") or "").strip() != serial_value:
        return None
    # Reuse the defensive numeric validation from the checkpoint module, then
    # retain the exact identity checks above (which that helper relaxes for
    # historical files).
    loaded = load_checkpoint(
        checkpoint_path.parent,
        run_id=checkpoint_job_id,
        device_serial=serial_value,
    )
    return loaded if isinstance(loaded, Mapping) else None


def _job_checkpoint_paths(job: Mapping[str, Any]) -> List[Path]:
    """Enumerate checkpoint files below ``reportRoot`` without trusting links."""
    root = _job_report_root(job)
    if root is None or not root.is_dir():
        return []
    values: List[Path] = []
    try:
        for candidate in root.rglob("checkpoint.json"):
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                values.append(resolved)
    except OSError:
        return []
    return list(dict.fromkeys(values))


def _job_device_artifacts(
    job: Mapping[str, Any], serial: str
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Find a device report and its strictly belonging checkpoint.

    Existing reports may use a historical directory name (the tests and old
    CLI did so), so a matching report is accepted anywhere under the root.  A
    checkpoint without a report is accepted only in the canonical device
    directory.  This distinction gives us checkpoint-only recovery while
    still rejecting foreign/malformed files.
    """
    serial_value = str(serial or "").strip()
    if not serial_value:
        return None, None
    expected = _expected_device_report_path(job, serial_value)
    known_ids = _job_known_execution_ids(job)
    report_path: Optional[Path] = None
    # Prefer the canonical report when it exists and identifies this device;
    # otherwise retain compatibility with legacy arbitrary child directories.
    report_candidates: List[Path] = []
    if expected is not None and expected.is_file():
        report_candidates.append(expected)
    report_candidates.extend(
        path for path in _cached_job_report_paths(job) if path not in report_candidates
    )
    for path in report_candidates:
        value = read_json_file(path, None)
        if not isinstance(value, Mapping):
            continue
        if str(value.get("device_serial") or "").strip() != serial_value:
            continue
        report_ids = {
            str(value.get(key) or "").strip()
            for key in ("run_id", "job_id", "test_id")
            if str(value.get(key) or "").strip()
        }
        if report_ids and known_ids.isdisjoint(report_ids):
            continue
        expected_package = str(
            job.get("packageName") or job.get("package_name") or ""
        ).strip()
        report_package = str(
            value.get("package_name") or value.get("packageName") or ""
        ).strip()
        if expected_package and report_package and expected_package != report_package:
            continue
        report_path = path
        break

    checkpoint: Optional[Dict[str, Any]] = None
    checkpoint_candidates: List[Path] = []
    # A current worker writes beside the canonical report directory. Older
    # workers may have used a legacy directory name; once the matching report
    # has been identified, its sibling checkpoint is equally trustworthy.
    if report_path is not None:
        checkpoint_candidates.append(report_path.parent / "checkpoint.json")
    if expected is not None:
        canonical_checkpoint = expected.parent / "checkpoint.json"
        if canonical_checkpoint not in checkpoint_candidates:
            checkpoint_candidates.append(canonical_checkpoint)
    for path in checkpoint_candidates:
        value = _strict_job_checkpoint(
            path,
            job,
            serial_value,
            allowed_parent=report_path.parent if report_path is not None else None,
        )
        if value is None:
            continue
        # If a checkpoint-only file is found, its parent must be the canonical
        # directory. A report-backed checkpoint may use a legacy directory.
        if report_path is None and (expected is None or path.parent != expected.parent):
            continue
        checkpoint = value
        break
    return report_path, checkpoint


def _nonnegative_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed == parsed and parsed >= 0 else default


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(default, parsed)


def _effective_resume_state(
    report: Optional[Mapping[str, Any]] = None,
    checkpoint: Optional[Mapping[str, Any]] = None,
    default: str = "running",
) -> str:
    """Compatibility wrapper around the shared durable-state resolver."""
    return effective_resume_state(report, checkpoint, default)


def _stall_watchdog_options(
    job: Optional[Mapping[str, Any]] = None,
    report: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, float, float]:
    """Read the thresholds used to judge a live-but-idle Fastbot heartbeat."""
    sources: List[Any] = []
    if isinstance(job, Mapping):
        sources.extend((job.get("config"),))
        config_path = str(job.get("configPath") or "").strip()
        if config_path:
            value = read_json_file(Path(config_path), None)
            if isinstance(value, Mapping):
                sources.append(value)
    if isinstance(report, Mapping):
        sources.append(report.get("config"))
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        watchdog = source.get("stall_watchdog")
        if not isinstance(watchdog, Mapping):
            continue
        enabled = bool(watchdog.get("enabled", True))
        try:
            unchanged = max(5.0, float(watchdog.get("unchanged_seconds", 150)))
        except (TypeError, ValueError, OverflowError):
            unchanged = 150.0
        try:
            grace = max(0.0, float(watchdog.get("startup_grace_seconds", 30)))
        except (TypeError, ValueError, OverflowError):
            grace = 30.0
        return enabled, unchanged, grace
    return False, 150.0, 30.0


def _runtime_heartbeat_state(
    state: str,
    report: Optional[Mapping[str, Any]],
    checkpoint: Optional[Mapping[str, Any]],
    *,
    job: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, bool, Optional[float]]:
    """Reconcile a logical running state with the Fastbot heartbeat.

    A worker can leave ``resume_state: running`` on disk while its detached
    Fastbot process has exited or while its operation counter has stopped
    advancing.  Treat those two durable signals as recovery evidence.  Older
    checkpoints do not contain the new fields and keep their historical
    behavior.
    """
    normalized = str(state or "running").strip().lower() or "running"
    if terminal_resume_state(report) or terminal_resume_state(checkpoint):
        return normalized, False, None
    value = checkpoint if isinstance(checkpoint, Mapping) else {}
    phase_name = str(value.get("phase_name") or "").strip().lower()
    if phase_name not in {"fastbot", "starting_fastbot"}:
        return normalized, False, None
    alive = value.get("fastbot_alive")
    if alive is False:
        return "recovering", False, None
    if alive is not True:
        return normalized, False, None

    enabled, unchanged_seconds, startup_grace = _stall_watchdog_options(job, report)
    if not enabled:
        return normalized, False, None
    # Only a real operation timestamp proves the engine was alive and then
    # went quiet.  Falling back to ``fastbot_started_at`` marked every healthy
    # device stalled once the engine ran longer than ``unchanged_seconds``,
    # because some Fastbot builds never print the "Event id" marker this
    # console used to parse.
    baseline = value.get("last_operation_at")
    if not baseline:
        return normalized, False, None
    try:
        baseline_epoch = datetime.fromisoformat(
            str(baseline).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return normalized, False, None
    age = max(0.0, time.time() - baseline_epoch)
    elapsed = _nonnegative_float(value.get("elapsed_seconds"), 0.0)
    if age >= unchanged_seconds and elapsed >= startup_grace:
        return "recovering", True, age
    return normalized, False, age


def _format_elapsed_seconds(value: Any) -> str:
    seconds = max(0, int(_nonnegative_float(value)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def _device_resume_snapshot(serial: str, job: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one device's durable report and checkpoint as a single view.

    The report contains immutable completed cycles, while the checkpoint is the
    authoritative source for an in-flight cycle.  Combining both here keeps the
    API useful while a worker is recovering or the device is disconnected.
    """
    cycles_target = max(1, _nonnegative_int(job.get("cycles"), 1))
    seconds_per_cycle = max(1.0, _nonnegative_float(job.get("duration"), 1.0) * 60.0)
    target_seconds = seconds_per_cycle * cycles_target
    report: Mapping[str, Any] = {}
    report_path, strict_checkpoint = _job_device_artifacts(job, serial)
    if report_path is not None and report_path.is_file():
        value = _cached_json_document(report_path, {})
        if isinstance(value, Mapping):
            report = value
        else:
            report_path = None
    checkpoint: Optional[Mapping[str, Any]] = strict_checkpoint
    report_terminal = terminal_resume_state(report)
    checkpoint_terminal = terminal_resume_state(checkpoint)
    cycles = unique_cycle_rows(report)
    completed_numbers = completed_cycle_numbers(report)
    completed_elapsed = sum(
        _nonnegative_float(item.get("pressure_elapsed_seconds"), 0.0)
        for item in cycles
        if isinstance(item, Mapping) and is_completed_cycle(item)
    )
    completed_operations = sum(
        _nonnegative_int(item.get("operation_count"), 0)
        for item in cycles
        if isinstance(item, Mapping) and is_completed_cycle(item)
    )
    terminal_state = bool(report_terminal or checkpoint_terminal)
    current_cycle: Mapping[str, Any] = {}
    if (
        isinstance(report.get("current_cycle"), Mapping)
        and not terminal_state
    ):
        current_cycle = report["current_cycle"]
    # Reports from the pre-checkpoint runner used bare cycle dictionaries. They
    # are still useful evidence, so treat those rows as accumulated completed
    # pressure only when there is no newer in-flight checkpoint/current cycle.
    legacy_cycles = bool(cycles) and not any(
        isinstance(item, Mapping)
        and any(key in item for key in ("cycle", "status", "completed"))
        for item in cycles
    )
    legacy_completed = legacy_cycles and not checkpoint and not current_cycle
    if legacy_completed:
        completed_elapsed = sum(
            _nonnegative_float(item.get("pressure_elapsed_seconds"), 0.0)
            for item in cycles
            if isinstance(item, Mapping)
        )
    checkpoint_cycle = _nonnegative_int((checkpoint or {}).get("cycle"), 0)
    if terminal_state:
        checkpoint_cycle = 0
    if checkpoint_cycle and checkpoint_cycle not in completed_numbers:
        current_cycle = checkpoint or current_cycle
    current_elapsed = _nonnegative_float(
        (checkpoint or {}).get("elapsed_seconds")
        if checkpoint_cycle and checkpoint_cycle not in completed_numbers
        else current_cycle.get("pressure_elapsed_seconds"),
        0.0,
    )
    current_operations = _nonnegative_int(
        (checkpoint or {}).get("operation_count")
        if checkpoint_cycle and checkpoint_cycle not in completed_numbers
        else current_cycle.get("operation_count"),
        0,
    )
    statistics = report.get("statistics", {})
    aggregate_elapsed = 0.0
    aggregate_operations = 0
    if isinstance(statistics, Mapping):
        aggregate_elapsed = _nonnegative_float(
            statistics.get("pressure_elapsed_seconds"), 0.0
        )
        aggregate_operations = _nonnegative_int(
            statistics.get("total_operations"), 0
        )
    # A legacy report may have only aggregate statistics. Use them only when
    # there is no cycle/checkpoint detail, never on top of completed cycles.
    if not cycles and not current_cycle:
        current_elapsed = aggregate_elapsed
        current_operations = aggregate_operations
    # Some legacy writers included the in-flight cycle in aggregate statistics,
    # while the checkpoint writer stores it separately.  Use the larger of the
    # two representations instead of summing both and double-counting.
    elapsed = min(target_seconds, max(aggregate_elapsed, completed_elapsed + current_elapsed))
    operations = max(aggregate_operations, completed_operations + current_operations)
    remaining = 0.0 if terminal_state else max(0.0, target_seconds - elapsed)
    status = _effective_resume_state(report, checkpoint)
    status, operation_stalled, stalled_for_seconds = _runtime_heartbeat_state(
        status,
        report,
        checkpoint,
        job=job,
    )
    if terminal_state:
        # A terminal report has no resumable current round.  Keep the last
        # completed round visible, but never surface a stale heartbeat cycle.
        cycle = max(completed_numbers, default=cycles_target)
    elif current_cycle:
        cycle = _nonnegative_int(
            current_cycle.get("cycle") or current_cycle.get("cycle_number"), 0
        )
    elif legacy_completed:
        cycle = min(cycles_target, len(cycles))
    else:
        cycle = next(
            (number for number in range(1, cycles_target + 1) if number not in completed_numbers),
            cycles_target,
        )
    phase = (
        0
        if terminal_state
        else _nonnegative_int(
            (checkpoint or {}).get("phase", current_cycle.get("phase", 0)), 0
        )
    )
    ratio = min(1.0, elapsed / target_seconds) if target_seconds else 0.0
    progress = int(ratio * 100)
    completed_count = len(cycles) if legacy_completed else len(completed_numbers)
    if status in {"completed", "passed", "completed_with_crashes"} and (
        completed_count >= cycles_target or elapsed >= target_seconds
    ):
        progress = 100
    else:
        progress = min(99, progress)
    last_error = (
        report.get("last_error")
        if terminal_state
        else (
            report.get("last_error")
            or (checkpoint or {}).get("last_error")
            or current_cycle.get("last_error")
        )
    )
    heartbeat_checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    fastbot_alive = heartbeat_checkpoint.get("fastbot_alive")
    if status == "recovering" and not last_error:
        if fastbot_alive is False:
            last_error = "Fastbot 进程已退出，正在恢复设备控制"
        elif operation_stalled:
            last_error = "Fastbot 操作心跳已停止，正在恢复设备控制"
    info = report.get("device_info", {})
    checkpoint_path: Optional[Path] = None
    if checkpoint is not None:
        if report_path is not None:
            checkpoint_path = report_path.parent / "checkpoint.json"
        else:
            expected_path = _expected_device_report_path(job, serial)
            if expected_path is not None:
                checkpoint_path = expected_path.parent / "checkpoint.json"
    return {
        "report": report,
        "checkpoint": checkpoint,
        "report_path": report_path,
        "report_available": bool(report and report_path is not None and report_path.is_file()),
        "checkpoint_available": bool(checkpoint),
        "checkpoint_path": checkpoint_path,
        "completed_cycles": completed_count,
        "cycle": cycle,
        "phase": phase,
        "elapsed_seconds": elapsed,
        "operation_count": operations,
        "completed_operations": completed_operations,
        "remaining_seconds": remaining,
        "progress": progress,
        "status": status,
        "resume_state": status,
        "fastbot_alive": fastbot_alive,
        "fastbot_started_at": heartbeat_checkpoint.get("fastbot_started_at"),
        "last_operation_at": heartbeat_checkpoint.get("last_operation_at"),
        "operation_stalled": operation_stalled,
        "stalled_for_seconds": stalled_for_seconds,
        "resume_attempts": max(
            _nonnegative_int(report.get("resume_attempts"), 0),
            _nonnegative_int((checkpoint or {}).get("resume_attempts"), 0),
        ),
        "last_error": str(last_error) if last_error else None,
        "last_heartbeat": report.get("last_heartbeat")
        or (checkpoint or {}).get("last_heartbeat"),
        "current_cycle": dict(current_cycle) if current_cycle else None,
        "apk_fingerprint": (checkpoint or {}).get("apk_fingerprint")
        or report.get("apk_fingerprint"),
        "remote_log_path": (checkpoint or {}).get("remote_log_path"),
        "remote_log_offset": _nonnegative_int(
            (checkpoint or {}).get("remote_log_offset"), 0
        ),
        "foreground_component": (checkpoint or {}).get("foreground_component")
        or report.get("foreground_component"),
        "device_info": dict(info) if isinstance(info, Mapping) else {},
    }


def _device_progress(serial: str, job: Mapping[str, Any]) -> Tuple[int, int, str]:
    snapshot = _device_resume_snapshot(serial, job)
    if snapshot.get("report") or snapshot.get("checkpoint"):
        return (
            int(snapshot["progress"]),
            int(snapshot["cycle"]),
            str(snapshot["status"] or "running"),
        )
    cycles_target = max(1, _nonnegative_int(job.get("cycles"), 1))
    target_seconds = max(1.0, _nonnegative_float(job.get("duration"), 1.0) * 60 * cycles_target)
    started = _nonnegative_float(job.get("startedEpoch"), time.time())
    elapsed = max(0.0, time.time() - started)
    return min(15, int(elapsed * 100 / target_seconds)), 1, "running"


def _device_job_is_occupied(
    job: Optional[Mapping[str, Any]],
    serial: str,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether an active batch still owns one particular device.

    A web job remains active while any sibling is recovering, but completed
    device reports must release their devices independently. A terminal
    report/checkpoint is free for a later test; non-terminal devices stay
    unavailable while their worker, claim, or recovery state is active.
    """
    if not isinstance(job, Mapping):
        return False
    serial_value = str(serial or "").strip()
    if not serial_value:
        return False

    value = snapshot if isinstance(snapshot, Mapping) else {}
    report = value.get("report")
    checkpoint = value.get("checkpoint")
    # A terminal per-device artifact is stronger than the job-wide process
    # map. One multi-device CLI process can remain alive for a recovering
    # sibling after this device's worker and report are already complete.
    if terminal_resume_state(report) or terminal_resume_state(checkpoint):
        return False

    # ``refresh_job`` hydrates these maps before the device list is built. For
    # a non-terminal device, either owner is enough to keep it unavailable.
    if _live_process_owners(job, serial_value):
        return True
    claims = job.get("_resumeClaims")
    if isinstance(claims, Mapping) and serial_value in {
        str(value) for value in claims
    }:
        parent = _resume_claim_parent(job, serial_value)
        if parent is not None and resume_claim_active(
            parent,
            job_id=str(job.get("id") or ""),
            device_serial=serial_value,
        ):
            return True

    state = str(
        value.get("resume_state") or value.get("status") or ""
    ).strip().lower()
    return state not in TERMINAL_RESUME_STATES


def list_devices() -> List[Dict[str, Any]]:
    states = DeviceManager.get_device_states()
    active = _active_jobs()
    # Keep interrupted devices visible after ADB removes them from the live
    # listing.  Their report/checkpoint supplies the stable identity fields.
    jobs_by_serial: Dict[str, List[Dict[str, Any]]] = {}
    for job in active:
        for value in job.get("devices", []) if isinstance(job.get("devices"), list) else []:
            jobs_by_serial.setdefault(str(value), []).append(job)
    serials = list(dict.fromkeys([str(value) for value in states] + list(jobs_by_serial)))
    # Probe live device info concurrently: one device needs several ADB round
    # trips, and doing seven of them serially made /api/devices take ~6s.
    online_serials = [
        serial
        for serial in serials
        if str(
            (states.get(serial) or {}).get("adb_state")
            if isinstance(states.get(serial), Mapping)
            else ""
        )
        == "device"
    ]
    if online_serials:
        from concurrent.futures import ThreadPoolExecutor

        def _probe(serial: str) -> None:
            _cached_device_info(serial)
            _cached_device_probe(
                  "ready:" + serial,
                  lambda: DeviceManager.is_device_ready(serial),
                  ttl=_DEVICE_READY_TTL_SECONDS,
              )

        with ThreadPoolExecutor(max_workers=min(8, len(online_serials))) as pool:
            list(pool.map(_probe, online_serials))
    rows: List[Dict[str, Any]] = []
    for serial in serials:
        state = states.get(serial, {})
        if not isinstance(state, Mapping):
            state = {}
        adb_state = str(state.get("adb_state") or "offline")
        # A finished device can appear in an active batch and a new job. Use
        # the job that still owns the device, keeping the old report only as
        # context when no job occupies it.
        job_context = active_job = None
        snapshot: Dict[str, Any] = {}
        for candidate in jobs_by_serial.get(serial, []):
            candidate_snapshot = _device_resume_snapshot(serial, candidate)
            if _device_job_is_occupied(candidate, serial, candidate_snapshot):
                job_context = active_job = candidate
                snapshot = candidate_snapshot
                break
            if job_context is None:
                job_context, snapshot = candidate, candidate_snapshot
        info: Dict[str, Any] = dict(snapshot.get("device_info") or {})
        ready = False
        if adb_state == "device":
            try:
                live_info = _cached_device_info(serial)
                if isinstance(live_info, Mapping):
                    info.update(dict(live_info))
            except Exception as exc:
                info.setdefault("error", str(exc))
            try:
                ready = bool(
                    _cached_device_probe(
                  "ready:" + serial,
                  lambda: DeviceManager.is_device_ready(serial),
                  ttl=_DEVICE_READY_TTL_SECONDS,
              )
                )
            except Exception:
                ready = False
        resume_state = str(snapshot.get("resume_state") or "")
        resume_mode = _job_resume_mode(
            job_context,
            snapshot.get("report") if isinstance(snapshot, Mapping) else None,
        )
        if active_job:
            job_status = str(active_job.get("status") or "preparing")
            if resume_state in {
                "recovering",
                "waiting_for_device",
                "resuming",
                "paused",
                "needs_manual_resume",
            }:
                status = resume_state
            elif job_status == "stopping":
                status = "stopping"
            elif adb_state != "device" and job_status not in {"stopping", "paused"}:
                status = "waiting_for_device"
            elif resume_state == "running" and (
                snapshot.get("report") or snapshot.get("checkpoint")
            ):
                status = "running"
            elif job_status in ACTIVE_JOB_STATUSES:
                status = job_status
            else:
                status = "ready" if ready else "not_ready"
        elif adb_state == "unauthorized":
            status = "unauthorized"
        elif adb_state != "device":
            status = "offline"
        elif ready:
            status = "ready"
        else:
            status = "not_ready"
        model = str(info.get("model") or state.get("model") or "Android Device")
        manufacturer = str(info.get("manufacturer") or state.get("device") or "Android")
        rows.append(
            {
                "serial": serial,
                "alias": model,
                "model": model,
                "brand": manufacturer,
                "android": str(info.get("android_version") or "--"),
                "sdk": str(info.get("sdk") or "--"),
                "screenSize": str(info.get("screen_size") or "--"),
                "battery": _battery_level(serial) if adb_state == "device" else None,
                "status": status,
                "adbState": adb_state,
                "ready": ready,
                "progress": int(snapshot.get("progress", 0) or 0),
                "cycle": int(snapshot.get("cycle", 0) or 0),
                "phase": int(snapshot.get("phase", 0) or 0),
                "elapsed": _format_elapsed_seconds(snapshot.get("elapsed_seconds", 0))
                if active_job
                else "--:--",
                "elapsedSeconds": float(snapshot.get("elapsed_seconds", 0) or 0),
                "remainingSeconds": float(snapshot.get("remaining_seconds", 0) or 0),
                "currentCycle": snapshot.get("current_cycle")
                or ({"cycle": snapshot.get("cycle"), "phase": snapshot.get("phase")} if active_job else None),
                "resumeState": resume_state or (status if active_job else None),
                "resumeAttempts": int(snapshot.get("resume_attempts", 0) or 0),
                "lastError": snapshot.get("last_error"),
                "lastHeartbeat": snapshot.get("last_heartbeat"),
                "operations": int(snapshot.get("operation_count", 0) or 0),
                "fastbotAlive": snapshot.get("fastbot_alive"),
                "lastOperationAt": snapshot.get("last_operation_at"),
                "operationStalled": bool(snapshot.get("operation_stalled")),
                "stalledForSeconds": snapshot.get("stalled_for_seconds"),
                "waitingForDevice": status == "waiting_for_device",
                "canResume": bool(
                    active_job
                    and (snapshot.get("report") or snapshot.get("checkpoint"))
                    and _can_resume_device_state(status, resume_mode=resume_mode)
                ),
                "jobId": active_job.get("id") if active_job else None,
                # A device-level end is available while this job still owns
                # the device.  Once the durable terminal marker is written,
                # ``active_job`` becomes false and the device is selectable
                # for a new task again.
                "canEnd": bool(active_job),
                "manualEnd": resume_state == MANUALLY_ENDED_STATE,
                "reportStatus": str(snapshot.get("report", {}).get("status") or "") or None
                if isinstance(snapshot.get("report"), Mapping)
                else None,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["status"] in {"offline", "waiting_for_device"},
            row["serial"],
        ),
    )


def elapsed_text(job: Optional[Mapping[str, Any]]) -> str:
    if not job:
        return "00:00"
    seconds = max(0, int(time.time() - float(job.get("startedEpoch") or time.time())))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def _legacy_build_run_config(payload: Mapping[str, Any], job_dir: Path) -> Dict[str, Any]:
    package_name = str(payload.get("packageName") or "").strip()
    if not PACKAGE_PATTERN.fullmatch(package_name):
        raise ApiError("请输入有效的 Android 包名")
    apk_path = Path(str(payload.get("apkPath") or "")).resolve(strict=False)
    if not any(_path_is_within(apk_path, root) for root in (UPLOAD_ROOT, LEGACY_UPLOAD_ROOT)):
        exc = ValueError(str(apk_path))
        raise ApiError("APK 必须先通过网页上传") from exc
    if apk_path.suffix.lower() != ".apk" or not apk_path.is_file():
        raise ApiError("上传的 APK 文件不存在")
    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ApiError("请至少选择一台设备")
    serials = list(dict.fromkeys(str(value).strip() for value in devices if str(value).strip()))
    if not serials:
        raise ApiError("请至少选择一台设备")
    known = DeviceManager.get_device_states()
    missing = [serial for serial in serials if known.get(serial, {}).get("adb_state") != "device"]
    if missing:
        raise ApiError("以下设备当前不可用：%s" % ", ".join(missing))

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = payload.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError("%s 必须是整数" % name)
        if value < minimum or value > maximum:
            raise ApiError("%s 必须在 %s 到 %s 之间" % (name, minimum, maximum))
        return value

    duration = integer("duration", 10, 1, 1440)
    cycles = integer("cycles", 1, 1, 100)
    throttle = integer("throttle", 250, 0, 60000)
    workers = min(MAX_SAFE_WORKERS, integer("workers", 1, 1, 32))
    reports_dir = job_dir / "reports"
    logs_dir = job_dir / "logs"
    return {
        "app": {"package_name": package_name, "apk_path": str(apk_path)},
        "test": {
            "fastbot_duration_minutes": duration,
            "fastbot_throttle_ms": throttle,
            "max_cycles": cycles,
            "crash_replay_attempts": 3,
            "fastbot_timeout_grace_seconds": 60,
            "permission_dialog_settle_seconds": 3,
            "clear_app_data_each_cycle": bool(payload.get("clearAppData", True)),
            "continue_without_data_clear_on_failure": False,
            "stop_on_crash": bool(payload.get("stopOnCrash", False)),
        },
        "crash_replay": {
            "window_seconds": 120,
            "max_events": 400,
            "activity_restore_timeout_seconds": 8,
            "require_page_anchor": True,
            "allow_cold_activity_start": False,
        },
        "devices": [{"serial": serial, "alias": serial} for serial in serials],
        "logging": {"level": "INFO", "log_dir": str(logs_dir)},
        "paths": {"reports_dir": str(reports_dir)},
        "parallel": {"max_workers": min(workers, len(serials), MAX_SAFE_WORKERS)},
        "screenshot": {
            "enabled": bool(payload.get("screenshot", True)),
            "interval_seconds": 5,
        },
        "stall_watchdog": {
            "enabled": _ui_bool(payload.get("stallWatchdog"), True),
            "mode": (
                str(payload.get("stallMode"))
                if str(payload.get("stallMode")) in {"observe", "recover_once"}
                else (
                    "recover_once"
                    if _ui_bool(payload.get("stallWatchdog"), True)
                    else "observe"
                )
            ),
            "sample_interval_seconds": 10,
            "unchanged_seconds": 150,
            "startup_grace_seconds": 30,
            "max_hash_distance": 6,
            "max_probe_failures": 3,
        },
        "fastbot": {
            "engine": "fastbot",
            "classpath": runtime_remote_path(),
            "main_class": "com.android.commands.monkey.Monkey",
            "expected_monkeyq_md5": runtime_asset_md5(),
            "extra_args": [],
            "event_percentages": {"touch": 70, "motion": 25, "pinchzoom": 5},
        },
        "scenario": {
            "login_enabled": False,
            "target_page": "HomePage",
            "pre_login_steps": [],
            "pre_login_steps_by_device": {},
            "navigation_steps": [
                {"action": "launch_app", "stop": False, "timeout": 30, "after": 2},
                {
                    "action": "complete_audit_gate",
                    "audit_locator": {
                        "resourceId": "%s:id/auditRadioButton" % package_name,
                    },
                    "confirm_locator": {"text": "确定"},
                    "timeout": 30,
                    "after": 3,
                },
                {
                    "action": "verify_foreground_stable",
                    "restore": True,
                    "stable_seconds": 2,
                    "timeout": 30,
                    "after": 0,
                },
            ],
            "navigation_steps_by_device": {},
            "stop_packages": [],
            # The audit gate is not only a startup step.  The executor uses
            # this global recovery rule while Fastbot is running as well, so
            # a clear-data/relaunch/privacy-policy cycle cannot leave the
            # pressure run stuck on the same page.
            "audit_recovery": {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "%s:id/auditRadioButton" % package_name,
                "confirm_text": "确定",
            },
        },
    }


def build_run_config(payload: Mapping[str, Any], job_dir: Path) -> Dict[str, Any]:
    """Create a run config by preserving the selected project workflow."""
    # Keep the small programmatic API used by existing callers/tests. The web
    # console always sends configFile and workflowMode, so this branch is only
    # for older integrations that still request the standalone Fastbot config.
    if not str(payload.get("configFile") or "").strip():
        return _legacy_build_run_config(payload, job_dir)
    template_path = _config_path(str(payload.get("configFile") or "settings.json"))
    raw = read_json_file(template_path, None)
    if not isinstance(raw, dict):
        raise ApiError("配置文件无法读取或不是 JSON 对象")
    config = copy.deepcopy(raw)
    config.pop("tab_rotation", None)
    workflow_mode = str(payload.get("workflowMode") or "matched").strip().lower()
    if workflow_mode not in {"matched", "generic"}:
        raise ApiError("workflowMode 必须是 matched 或 generic")
    app = config.setdefault("app", {})
    if not isinstance(app, dict):
        raise ApiError("配置中的 app 必须是对象")
    package_name = str(payload.get("packageName") or app.get("package_name") or "").strip()
    if not PACKAGE_PATTERN.fullmatch(package_name):
        raise ApiError("请输入有效的 Android 包名")

    apk_path = Path(str(payload.get("apkPath") or "")).resolve(strict=False)
    if not any(_path_is_within(apk_path, root) for root in (UPLOAD_ROOT, LEGACY_UPLOAD_ROOT)):
        exc = ValueError(str(apk_path))
        raise ApiError("APK 必须先通过网页上传") from exc
    if apk_path.suffix.lower() != ".apk" or not apk_path.is_file():
        raise ApiError("上传的 APK 文件不存在")
    uploaded_package = str(payload.get("apkPackageName") or "").strip()
    if uploaded_package:
        detected_package, package_error = apk_package_name(apk_path)
        if not detected_package:
            raise ApiError(package_error, HTTPStatus.UNPROCESSABLE_ENTITY)
        if uploaded_package != detected_package:
            raise ApiError("上传 APK 的包名校验结果无效", HTTPStatus.UNPROCESSABLE_ENTITY)
        if detected_package != package_name:
            raise ApiError(
                "APK 包名与目标包名不一致：APK 为 %s，目标为 %s" % (detected_package, package_name),
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    template_app = raw.get("app", {}) if isinstance(raw.get("app"), Mapping) else {}
    template_package = str(template_app.get("package_name") or "").strip()
    configured_start_pages = scenario_start_pages(config.get("scenario"))
    requested_start_page = str(payload.get("startupPage") or "").strip()
    if workflow_mode == "matched":
        allowed_start_pages = {item["id"] for item in configured_start_pages}
        if allowed_start_pages:
            requested_start_page = requested_start_page or configured_start_pages[0]["id"]
            if requested_start_page not in allowed_start_pages:
                raise ApiError(
                    "Selected startup page does not belong to this framework configuration",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
        elif requested_start_page:
            raise ApiError("This framework configuration has no selectable startup page", HTTPStatus.UNPROCESSABLE_ENTITY)
    elif requested_start_page and requested_start_page not in {"audit_gate", "app_home"}:
        raise ApiError("Generic Fastbot mode can only start from the audit gate or app home page", HTTPStatus.UNPROCESSABLE_ENTITY)
    if workflow_mode == "matched" and template_package != package_name:
        raise ApiError(
            "完整流程只适用于配置中的原应用；新 APK 请使用通用 Fastbot 模式",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ApiError("请至少选择一台设备")
    serials = list(dict.fromkeys(str(value).strip() for value in devices if str(value).strip()))
    if not serials:
        raise ApiError("请至少选择一台设备")
    known = DeviceManager.get_device_states()
    missing = [serial for serial in serials if known.get(serial, {}).get("adb_state") != "device"]
    if missing:
        raise ApiError("以下设备当前不可用：%s" % ", ".join(missing))

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = payload.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError("%s 必须是整数" % name)
        if value < minimum or value > maximum:
            raise ApiError("%s 必须在 %s 到 %s 之间" % (name, minimum, maximum))
        return value

    test = config.setdefault("test", {})
    fastbot = config.setdefault("fastbot", {})
    # Migrate selectable configs that still reference the historical split
    # runtime before validating the classpath used by this job.
    if isinstance(fastbot, dict):
        normalize_runtime_options(fastbot)
        fastbot.setdefault("expected_monkeyq_md5", runtime_asset_md5())
    if not isinstance(test, dict) or not isinstance(fastbot, dict):
        raise ApiError("配置中的 test 和 fastbot 必须是对象")
    if not fastbot.get("classpath") or not fastbot.get("main_class"):
        raise ApiError("所选配置缺少 Fastbot classpath 或 main_class")
    fastbot["engine"] = "fastbot"
    app.update({"package_name": package_name, "apk_path": str(apk_path)})
    audit_gate_every_launch = _ui_bool(payload.get("auditGateEveryLaunch"), True)
    if workflow_mode == "generic":
        # A template may contain package-specific locators, activities and
        # stop lists. None of those are safe for an unknown APK. Keep the
        # verified Fastbot/runtime settings; the shared audit policy is added
        # below with package-relative controls.
        generic_steps: List[Dict[str, Any]] = [
            {
                "action": "launch_app",
                "stop": False,
                "timeout": 30,
                "after": 2,
            },
        ]
        generic_steps.append(
            {
                "action": "verify_foreground_stable",
                "restore": True,
                "stable_seconds": 2,
                "timeout": 30,
                "after": 0,
            }
        )
        config["scenario"] = {
            "login_enabled": False,
            "target_page": "AppHome",
            "pre_login_steps": [],
            "pre_login_steps_by_device": {},
            "navigation_steps": generic_steps,
            "navigation_steps_by_device": {},
            "stop_packages": [package_name],
        }
    apply_audit_gate_policy(config, package_name, audit_gate_every_launch)
    test.update(
        {
            "fastbot_duration_minutes": integer("duration", int(test.get("fastbot_duration_minutes", 10) or 10), 1, 1440),
            "fastbot_throttle_ms": integer("throttle", int(test.get("fastbot_throttle_ms", 250) or 250), 0, 60000),
            "max_cycles": integer("cycles", int(test.get("max_cycles", 1) or 1), 1, 100),
            "clear_app_data_each_cycle": bool(payload.get("clearAppData", test.get("clear_app_data_each_cycle", True))),
            "stop_on_crash": bool(payload.get("stopOnCrash", test.get("stop_on_crash", False))),
        }
    )
    workers = min(MAX_SAFE_WORKERS, integer("workers", 1, 1, 32))
    config["devices"] = [{"serial": serial, "alias": serial} for serial in serials]
    logging_config = config.get("logging") if isinstance(config.get("logging"), dict) else {}
    config["logging"] = {**logging_config, "level": "INFO", "log_dir": str(job_dir / "logs")}
    paths_config = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    config["paths"] = {**paths_config, "reports_dir": str(job_dir / "reports")}
    parallel = config.get("parallel") if isinstance(config.get("parallel"), dict) else {}
    config["parallel"] = {
        **parallel,
        "max_workers": min(workers, len(serials), MAX_SAFE_WORKERS),
    }
    screenshot = config.get("screenshot") if isinstance(config.get("screenshot"), dict) else {}
    screenshot["enabled"] = bool(payload.get("screenshot", screenshot.get("enabled", True)))
    config["screenshot"] = screenshot
    watchdog = config.get("stall_watchdog") if isinstance(config.get("stall_watchdog"), dict) else {}
    # Generic APK runs use the shared recovery guard by default. A caller can
    # still explicitly turn it off or select observe-only mode.
    # Missing watchdog blocks are normalized to the shared recovery default;
    # an explicit enabled:false in a selected framework remains honored even
    # when that framework is reused in generic APK mode.
    watchdog_default = bool(watchdog.get("enabled", True))
    watchdog_enabled = _ui_bool(payload.get("stallWatchdog"), watchdog_default)
    watchdog["enabled"] = watchdog_enabled
    if str(payload.get("stallMode")) in {"observe", "recover_once", "recover"}:
        watchdog["mode"] = str(payload["stallMode"])
    elif "mode" not in watchdog:
        watchdog["mode"] = "recover_once" if watchdog_enabled else "observe"
    config["stall_watchdog"] = watchdog
    return config


def _job_log(job: Mapping[str, Any], message: str) -> None:
    stream = job.get("logStream")
    if stream is None:
        return
    try:
        stream.write(("[平台] %s\r\n" % message).encode("utf-8", errors="replace"))
    except OSError:
        pass


def _finish_install_processes(job: Dict[str, Any]) -> None:
    clickers = job.pop("installClickers", {})
    if isinstance(clickers, Mapping):
        for serial, clicker in clickers.items():
            try:
                clicker.stop()
                if clicker.clicks:
                    _job_log(
                        job,
                        "%s 自动确认安装 %d 次：%s"
                        % (serial, len(clicker.clicks), ", ".join(clicker.clicks)),
                    )
            except Exception:
                logger.debug("Unable to stop install clicker for %s", serial, exc_info=True)
    processes = job.pop("installProcesses", {})
    if not isinstance(processes, Mapping):
        return
    for process in processes.values():
        if not _is_process_handle(process) or process.poll() is not None:
            continue
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except (OSError, ValueError):
                pass


def _job_process_map(job: Dict[str, Any]) -> Dict[str, subprocess.Popen]:
    """Return all live CLI invocations belonging to one persisted job.

    The normal path has one invocation, but a device-level manual resume may
    run beside other devices that are still completing.  Keeping the map
    in-memory prevents the second invocation from hiding the first one.
    """
    processes = job.get("_processes")
    if not isinstance(processes, dict):
        processes = {}
        job["_processes"] = processes
    legacy = job.get("process")
    if _is_process_handle(legacy):
        key = str(job.get("_primaryProcessKey") or "primary")
        processes.setdefault(key, legacy)
    return processes


def _live_job_processes(job: Mapping[str, Any]) -> Dict[str, subprocess.Popen]:
    value = job.get("_processes")
    result: Dict[str, subprocess.Popen] = {}
    if isinstance(value, Mapping):
        for key, process in value.items():
            if _is_process_handle(process):
                try:
                    if process.poll() is None:
                        result[str(key)] = process
                except (OSError, ValueError):
                    continue
    process = job.get("process")
    if _is_process_handle(process):
        try:
            if process.poll() is None:
                result.setdefault(str(job.get("_primaryProcessKey") or "primary"), process)
        except (OSError, ValueError):
            pass
    return result


def _live_process_owners(job: Mapping[str, Any], serial: str) -> List[str]:
    """Return live invocation keys that currently claim ``serial``."""
    serial_value = str(serial)
    process_devices = job.get("_processDevices")
    primary_key = str(job.get("_primaryProcessKey") or "primary")
    owners: List[str] = []
    for key in _live_job_processes(job):
        values = process_devices.get(key) if isinstance(process_devices, Mapping) else None
        if not isinstance(values, list):
            # Legacy snapshots only persisted the primary invocation.  It owns
            # the configured devices until that process exits.
            values = job.get("devices", []) if str(key) == primary_key else []
        if serial_value in {str(value) for value in values}:
            owners.append(str(key))
    return owners


def _resume_claim_parent(job: Mapping[str, Any], serial: str) -> Optional[Path]:
    """Resolve the trusted report directory used for a device claim."""
    report_path, _checkpoint = _job_device_artifacts(job, serial)
    if report_path is not None:
        return report_path.parent
    expected = _expected_device_report_path(job, serial)
    return expected.parent if expected is not None else None


def _pending_manual_resume_request(
    job: Mapping[str, Any], serial: str
) -> Optional[Dict[str, Any]]:
    """Return an unconsumed explicit resume request for one job/device.

    The request is deliberately read from the same trusted report directory
    used by durable claims.  A request file is an operator action, so it is
    allowed to wake a manual recovery state even when automatic recovery is
    disabled; the CLI remains responsible for consuming it exactly once.
    """
    parent = _resume_claim_parent(job, serial)
    if parent is None:
        return None
    value = read_json_file(recovery_request_path(parent), None)
    return dict(value) if isinstance(value, Mapping) else None


def _read_resume_claim(
    job: Mapping[str, Any], serial: str
) -> Optional[Dict[str, Any]]:
    """Read and validate the durable claim for one job/device pair.

    The in-memory ``_resumeClaims`` map is intentionally not persisted in
    ``job.json``.  This helper is the single disk-backed source used after a
    service restart and rejects a claim that was copied from another job or
    device.
    """
    serial_value = str(serial).strip()
    job_id = str(job.get("id") or "").strip()
    parent = _resume_claim_parent(job, serial_value)
    if not serial_value or not job_id or parent is None:
        return None
    value = read_json_file(parent / RESUME_CLAIM_NAME, None)
    if not isinstance(value, Mapping):
        return None
    if str(value.get("job_id") or "").strip() != job_id:
        return None
    if str(value.get("device_serial") or "").strip() != serial_value:
        return None
    claim_id = str(value.get("claim_id") or "").strip()
    if not claim_id:
        return None
    return dict(value)


def _remember_resume_claim(job: Dict[str, Any], serial: str, claim: Mapping[str, Any]) -> None:
    """Mirror a valid durable claim into the current service snapshot."""
    serial_value = str(serial)
    claim_id = str(claim.get("claim_id") or "").strip()
    if not claim_id:
        return
    claims = job.get("_resumeClaims")
    if not isinstance(claims, dict):
        claims = {}
        job["_resumeClaims"] = claims
    claims[serial_value] = claim_id
    devices = job.get("resumeInFlightDevices")
    if not isinstance(devices, list):
        devices = []
        job["resumeInFlightDevices"] = devices
    if serial_value not in {str(item) for item in devices}:
        devices.append(serial_value)
    RESUME_IN_FLIGHT.add((str(job.get("id") or ""), serial_value))
    job["resumeInFlight"] = True


def _forget_resume_claim(job: Dict[str, Any], serial: str) -> None:
    """Remove only the service's in-memory bookkeeping for one claim."""
    serial_value = str(serial)
    key = (str(job.get("id") or ""), serial_value)
    RESUME_IN_FLIGHT.discard(key)
    devices = job.get("resumeInFlightDevices")
    if isinstance(devices, list):
        job["resumeInFlightDevices"] = [
            item for item in devices if str(item) != serial_value
        ]
    claims = job.get("_resumeClaims")
    if isinstance(claims, dict):
        claims.pop(serial_value, None)
    job["resumeInFlight"] = bool(job.get("resumeInFlightDevices"))


def _hydrate_resume_claims(job: Dict[str, Any]) -> Dict[str, str]:
    """Restore live device claims after a web-service restart.

    A claim whose attached worker is still alive is retained so a second CLI
    cannot be started.  Dead claims are removed only after the identity checks
    in :func:`_read_resume_claim` pass; malformed/foreign files are left alone
    for forensic inspection.
    """
    claims: Dict[str, str] = {}
    serials = job.get("devices", [])
    values = serials if isinstance(serials, list) else []
    for serial_value in values:
        serial = str(serial_value).strip()
        if not serial:
            continue
        claim = _read_resume_claim(job, serial)
        if claim is None:
            continue
        parent = _resume_claim_parent(job, serial)
        claim_id = str(claim.get("claim_id") or "").strip()
        if parent is None or not claim_id:
            continue
        # Prefer the local process handle when this service still owns the
        # invocation.  The durable worker PID probe is necessarily best
        # effort (and can briefly fail for a freshly spawned/mock process),
        # but dropping a claim while its local owner is alive would allow a
        # second resume request to start another controller on the device.
        local_owner = bool(_live_process_owners(job, serial))
        if resume_claim_active(
            parent,
            job_id=str(job.get("id") or ""),
            device_serial=serial,
        ) or local_owner:
            claims[serial] = claim_id
            _remember_resume_claim(job, serial, claim)
            continue
        # A dead claim is safe to retire, but only with its exact id.  This
        # preserves another invocation's replacement claim if a race occurs.
        release_resume_claim(parent, claim_id)
        _forget_resume_claim(job, serial)
    return claims


def _live_resume_claim_serials(job: Dict[str, Any]) -> set[str]:
    """Return serials guarded by a currently live durable worker claim."""
    _hydrate_resume_claims(job)
    live: set[str] = set()
    claims = job.get("_resumeClaims")
    candidates = (
        [str(item) for item in claims]
        if isinstance(claims, Mapping)
        else []
    )
    for serial in candidates:
        claim = _read_resume_claim(job, serial)
        parent = _resume_claim_parent(job, serial)
        local_owner = bool(_live_process_owners(job, serial))
        durable_owner = bool(
            claim is not None
            and parent is not None
            and resume_claim_active(
                parent,
                job_id=str(job.get("id") or ""),
                device_serial=serial,
            )
        )
        if claim is not None and parent is not None and (durable_owner or local_owner):
            live.add(serial)
        else:
            _release_resume_claim_if_unowned(job, serial)
    return live


def _release_resume_claim_if_unowned(
    job: Mapping[str, Any], serial: str, *, force: bool = False
) -> None:
    """Drop a resume lock only after no live process still owns the device.

    ``force`` is reserved for a caller that has just observed the exact local
    owner process exit.  Windows can briefly report an exited PID as alive;
    allowing that stale PID to retain the claim would block a valid resume.
    """
    if _live_process_owners(job, serial):
        return
    serial_value = str(serial)
    claim_parent = _resume_claim_parent(job, serial_value)
    durable = _read_resume_claim(job, serial_value)
    claims = job.get("_resumeClaims")
    claim_id = claims.get(serial_value) if isinstance(claims, Mapping) else None
    if not claim_id and durable is not None:
        claim_id = durable.get("claim_id")
    # A service restart loses the local process map.  Never remove a claim
    # merely because that map is empty while the recorded CLI worker is alive.
    if (
        not force
        and durable is not None
        and claim_parent is not None
        and resume_claim_active(
            claim_parent,
            job_id=str(job.get("id") or ""),
            device_serial=serial_value,
        )
    ):
        if isinstance(job, dict):
            _remember_resume_claim(job, serial_value, durable)
        return
    released = False
    if claim_parent is not None and claim_id:
        released = release_resume_claim(claim_parent, str(claim_id))
    # Do not clear bookkeeping for a replacement claim that won a race after
    # the caller captured the old id.
    current = _read_resume_claim(job, serial_value)
    current_id = str(current.get("claim_id") or "") if current else ""
    if isinstance(job, dict):
        if released or not current_id or current_id == str(claim_id or ""):
            _forget_resume_claim(job, serial_value)
        elif current:
            _remember_resume_claim(job, serial_value, current)
    else:
        if released or not current_id or current_id == str(claim_id or ""):
            RESUME_IN_FLIGHT.discard((str(job.get("id") or ""), serial_value))


def _launch_job_cli_legacy(
    job: Dict[str, Any], devices: Optional[Iterable[str]] = None
) -> Optional[subprocess.Popen]:
    config_path = Path(str(job["configPath"]))
    log_stream = job.get("logStream")
    if log_stream is None:
        log_stream = open_job_log(Path(str(job["logPath"])))
        job["logStream"] = log_stream
    command = [
        str(RUNTIME_PYTHON),
        "-u",
        str(CLI_PATH),
        "run",
        "--config",
        str(config_path),
        "--devices",
        ",".join(
            str(value)
            for value in (
                list(devices)
                if devices is not None
                else job["devices"]
            )
        ),
        "--run-id",
        _job_execution_run_id(job),
    ]
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process_key = uuid.uuid4().hex
    had_live_process = bool(_live_job_processes(job))
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise ApiError("无法启动测试进程：%s" % exc, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
    processes = _job_process_map(job)
    processes[process_key] = process
    process_devices = job.get("_processDevices")
    if not isinstance(process_devices, dict):
        process_devices = {}
        job["_processDevices"] = process_devices
    process_devices[process_key] = [
        str(value)
        for value in (list(devices) if devices is not None else job.get("devices", []))
    ]
    # Keep the original process as the public/primary handle.  A secondary
    # device resume is tracked in the private map and must not detach the
    # primary process from refresh/stop handling.
    if not _live_job_processes(job) or not had_live_process:
        job["process"] = process
        job["pid"] = process.pid
        job["_primaryProcessKey"] = process_key
    if devices is not None:
        # A device-list launch is always a recovery/re-dispatch invocation.
        # It may be the only surviving worker after a service restart, so
        # ``had_live_process`` cannot be used to infer the state.
        job["status"] = "resuming"
        job["phase"] = "resuming"
    else:
        job["status"] = "preparing"
        job["phase"] = "preparing"
    installation = job.get("installation")
    if isinstance(installation, dict) and installation.get("required"):
        installation["status"] = "confirmed"
        installation["confirmedAt"] = utc_now()
        installation["pendingDevices"] = []
    _job_log(job, "安装确认通过，开始执行清数据、场景准备和 Fastbot 流程")
    persist_job(job)
    return process


def _launch_job_cli(
    job: Dict[str, Any], devices: Optional[Iterable[str]] = None
) -> Optional[subprocess.Popen]:
    """Start one CLI invocation after claiming every target device durably."""
    target_devices = list(
        dict.fromkeys(
            str(value).strip()
            for value in (
                list(devices) if devices is not None else job.get("devices", [])
            )
            if str(value).strip()
        )
    )
    if not target_devices:
        raise ApiError("device is required", HTTPStatus.UNPROCESSABLE_ENTITY)

    claim_records: List[Tuple[str, Path, str]] = []
    preacquired = job.get("_resumeClaimPreacquired")
    if isinstance(preacquired, (list, set, tuple)):
        preacquired_serials = {str(value) for value in preacquired}
    else:
        preacquired_serials = set()

    # Claims are acquired before Popen so a second HTTP request or a newly
    # restarted service cannot race this invocation into the same device.
    try:
        with JOBS_LOCK:
            claims = job.get("_resumeClaims")
            if not isinstance(claims, dict):
                claims = {}
                job["_resumeClaims"] = claims
            for serial in target_devices:
                if _live_process_owners(job, serial):
                    raise ApiError(
                        "device already has a live worker", HTTPStatus.CONFLICT
                    )
                parent = _resume_claim_parent(job, serial)
                if parent is None:
                    raise ApiError(
                        "device report directory is unavailable", HTTPStatus.CONFLICT
                    )
                durable = _read_resume_claim(job, serial)
                claim: Optional[Mapping[str, Any]] = None
                if durable is not None:
                    claim_id = str(durable.get("claim_id") or "").strip()
                    active = resume_claim_active(
                        parent,
                        job_id=str(job.get("id") or ""),
                        device_serial=serial,
                    )
                    if active:
                        # An attached worker is always a conflict.  An
                        # owner-only claim is reusable only when this job
                        # explicitly created it immediately before launch.
                        if durable.get("worker_pid") not in (None, "", 0):
                            raise ApiError(
                                "device already has a live resume claim",
                                HTTPStatus.CONFLICT,
                            )
                        if serial not in preacquired_serials:
                            raise ApiError(
                                "device resume is already being launched",
                                HTTPStatus.CONFLICT,
                            )
                        claim = durable
                    else:
                        release_resume_claim(parent, claim_id)
                        _forget_resume_claim(job, serial)
                if claim is None:
                    claim = acquire_resume_claim(
                        parent,
                        job_id=str(job.get("id") or ""),
                        device_serial=serial,
                        requested_by="web",
                    )
                    if claim is None:
                        raise ApiError(
                            "device already has a live resume claim",
                            HTTPStatus.CONFLICT,
                        )
                claim_id = str(claim.get("claim_id") or "").strip()
                if not claim_id:
                    raise ApiError("invalid device resume claim", HTTPStatus.CONFLICT)
                claims[serial] = claim_id
                _remember_resume_claim(job, serial, claim)
                claim_records.append((serial, parent, claim_id))
    except Exception:
        for serial, parent, claim_id in claim_records:
            release_resume_claim(parent, claim_id)
            _forget_resume_claim(job, serial)
        raise

    process: Optional[subprocess.Popen] = None
    log_stream = job.get("logStream")
    try:
        if log_stream is None:
            log_stream = open_job_log(Path(str(job["logPath"])))
            job["logStream"] = log_stream
        command = [
            str(RUNTIME_PYTHON),
            "-u",
            str(CLI_PATH),
            "run",
            "--config",
            str(Path(str(job["configPath"]))),
            "--devices",
            ",".join(target_devices),
            "--run-id",
            _job_execution_run_id(job),
        ]
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process_key = uuid.uuid4().hex
        had_live_process = bool(_live_job_processes(job))
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
    except OSError as exc:
        for serial, parent, claim_id in claim_records:
            release_resume_claim(parent, claim_id)
            _forget_resume_claim(job, serial)
        raise ApiError(
            "无法启动测试进程：%s" % exc,
            HTTPStatus.INTERNAL_SERVER_ERROR,
        ) from exc
    except Exception:
        for serial, parent, claim_id in claim_records:
            release_resume_claim(parent, claim_id)
            _forget_resume_claim(job, serial)
        raise

    processes = _job_process_map(job)
    processes[process_key] = process
    process_devices = job.get("_processDevices")
    if not isinstance(process_devices, dict):
        process_devices = {}
        job["_processDevices"] = process_devices
    process_devices[process_key] = list(target_devices)
    # Keep the original process as the public/primary handle. A secondary
    # device resume remains in the private map.
    if not _live_job_processes(job) or not had_live_process:
        job["process"] = process
        job["pid"] = getattr(process, "pid", None)
        job["_primaryProcessKey"] = process_key
    # A device-scoped launch is always a continuation, including the case
    # where it is the first surviving process after a service restart.  Do
    # not expose a transient ``preparing`` state: the UI and a concurrent
    # resume request need to see that this device is already claimed.
    if devices is not None:
        job["status"] = "resuming"
        job["phase"] = "resuming"
    else:
        job["status"] = "preparing"
        job["phase"] = "preparing"
    installation = job.get("installation")
    if isinstance(installation, dict) and installation.get("required"):
        installation["status"] = "confirmed"
        installation["confirmedAt"] = utc_now()
        installation["pendingDevices"] = []
    worker_pid = getattr(process, "pid", None)
    for serial, parent, claim_id in claim_records:
        update_resume_claim(
            parent,
            claim_id,
            worker_pid=worker_pid,
            launched_at=utc_now(),
        )
    if isinstance(preacquired, list):
        target_set = set(target_devices)
        job["_resumeClaimPreacquired"] = [
            value for value in preacquired if str(value) not in target_set
        ]
    elif isinstance(preacquired, set):
        job["_resumeClaimPreacquired"] = preacquired.difference(target_devices)
    _job_log(
        job,
        "安装确认通过，开始执行清数据、场景准备和压测流程",
    )
    persist_job(job)
    return process


def _start_manual_install(job: Dict[str, Any], config: Mapping[str, Any], serials: List[str]) -> None:
    apk_path = str(config["app"]["apk_path"])
    log_stream = job["logStream"]
    processes: Dict[str, subprocess.Popen] = {}
    try:
        for serial in serials:
            command = [
                DeviceManager.adb_path,
                "-s",
                serial,
                "install",
                "-r",
                "-t",
                "-g",
                apk_path,
            ]
            processes[serial] = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        job["installProcesses"] = processes
        _finish_install_processes(job)
        raise ApiError("无法拉起设备安装页：%s" % exc, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
    job["installProcesses"] = processes
    # Answer vendor install sheets automatically instead of waiting for an
    # operator: MIUI's reject button counts down and cancels the install.
    clickers: Dict[str, InstallPromptClicker] = {}
    for serial in serials:
        try:
            clickers[serial] = InstallPromptClicker(serial, timeout_seconds=900).start()
        except Exception:
            logger.warning(
                "Unable to start the install prompt clicker for %s", serial,
                exc_info=True,
            )
    job["installClickers"] = clickers
    package_name = str(config["app"]["package_name"])
    expected_md5 = _local_apk_md5(Path(str(config["app"]["apk_path"]))) or ""
    if expected_md5:
        threading.Thread(
            target=_auto_confirm_install_when_ready,
            args=(str(job["id"]), list(serials), package_name, expected_md5),
            name="auto-confirm-%s" % job["id"],
            daemon=True,
        ).start()
    _job_log(
        job,
        "已自动点击设备安装确认；若某台设备未弹出安装页，请在设备上点击安装后回平台确认继续：%s"
        % ", ".join(serials),
    )


def _installer_window_visible(serial: str) -> bool:
    """True while a vendor install page still owns the device's foreground."""
    try:
        result = DeviceManager._run_adb(
            ["shell", "dumpsys", "window", "windows"],
            serial=serial,
            timeout=8,
            retries=1,
        )
    except Exception:
        return False
    for line in str(result.stdout or "").splitlines():
        if "mCurrentFocus" not in line:
            continue
        lowered = line.lower()
        return any(
            hint in lowered
            for hint in (
                "packageinstaller",
                "permissioncontroller",
                "securitycenter",
                "appmarket",
                "appstore",
                "installer",
            )
        )
    return False


def _auto_confirm_install_when_ready(
    job_id: str,
    serials: List[str],
    package_name: str,
    expected_md5: str,
    *,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 2.0,
) -> None:
    """Confirm the install stage as soon as every device really has the APK.

    The clicker answers the vendor prompts; this watcher removes the last manual
    click (the console's "确认安装") when everything installed by itself.  If it
    times out the operator still has the manual button, so a failure here never
    blocks the job.
    """
    deadline = time.time() + max(30.0, float(timeout_seconds))
    while time.time() < deadline:
        job = JOBS.get(job_id)
        if not isinstance(job, Mapping) or job.get("status") != "awaiting_install":
            return
        try:
            ready = all(
                _package_matches_apk(serial, package_name, expected_md5)
                for serial in serials
            )
        except Exception:
            ready = False
        if ready and any(_installer_window_visible(serial) for serial in serials):
            # The installer page can still cover the device after the package is
            # readable; starting the worker now races the page and fails prep.
            time.sleep(max(0.5, float(poll_seconds)))
            continue
        if ready:
            try:
                confirm_job_install(job_id)
                _job_log(job, "检测到所有设备安装完成，已自动确认并开始测试")
            except Exception as exc:
                logger.warning("Auto install confirmation failed: %s", exc)
            return
        time.sleep(max(0.5, float(poll_seconds)))
    job = JOBS.get(job_id)
    if isinstance(job, Mapping) and job.get("status") == "awaiting_install":
        _job_log(job, "自动安装确认超时，请在控制台手动确认安装")


def start_run(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not str(payload.get("apkPackageName") or "").strip():
        raise ApiError("请重新上传 APK，启动前必须完成 manifest 包名校验", HTTPStatus.UNPROCESSABLE_ENTITY)
    job_id = "WEB-%s-%s" % (
        datetime.now().strftime("%Y%m%d-%H%M%S"),
        uuid.uuid4().hex[:5].upper(),
    )
    job_dir = RUN_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        config = build_run_config(payload, job_dir)
        preflight = preflight_config(config)
        if not preflight["success"]:
            details: List[str] = []
            for check in preflight["checks"]:
                if check.get("name") == "devices":
                    for item in check.get("devices", []):
                        if not item.get("ok"):
                            fastbot = item.get("fastbot") or {}
                            details.append("%s: %s" % (item.get("serial"), item.get("error") or fastbot.get("error") or "预检失败"))
                elif not check.get("ok"):
                    details.append(str(check.get("detail") or check.get("name")))
            raise ApiError("启动前预检失败，未启动 Fastbot：%s" % "; ".join(details), HTTPStatus.UNPROCESSABLE_ENTITY)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    config_path = job_dir / "config.json"
    log_path = job_dir / "console.log"
    write_json_file(config_path, config)
    log_stream = open_job_log(log_path)
    package_name = str(config["app"]["package_name"])
    serials = [str(device["serial"]) for device in config["devices"]]
    expected_apk_md5 = _local_apk_md5(Path(config["app"]["apk_path"]))
    missing = [
        serial
        for serial in serials
        if not _package_matches_apk(serial, package_name, expected_apk_md5)
    ]
    installation_devices = {
        serial: {
            "serial": serial,
            "required": serial in missing,
            "installed": serial not in missing,
            "status": "waiting" if serial in missing else "already_installed",
        }
        for serial in serials
    }
    job = {
        "id": job_id,
        # New jobs use one stable id for the web task and CLI report tree.
        # Keeping this explicit also makes the mapping unambiguous after a
        # service restart or a later schema migration.
        "runId": job_id,
        "status": "awaiting_install" if missing else "preparing",
        "phase": "awaiting_install" if missing else "preparing",
        "startedAt": utc_now(),
        "startedEpoch": time.time(),
        "finishedAt": None,
        "packageName": package_name,
        "apkName": Path(config["app"]["apk_path"]).name,
        "devices": serials,
        "duration": config["test"]["fastbot_duration_minutes"],
        "cycles": config["test"]["max_cycles"],
        "workers": config["parallel"]["max_workers"],
        "pid": None,
        "exitCode": None,
        "jobDir": str(job_dir),
        "reportRoot": str(job_dir / "reports"),
        "logPath": str(log_path),
        "configPath": str(config_path),
        "stopRequestedAt": None,
        "stopDeadlineEpoch": None,
        "manualStopRequested": False,
        "manualStopDevices": [],
        "process": None,
        "logStream": log_stream,
        "installation": {
            "required": bool(missing),
            "status": "waiting" if missing else "not_required",
            "pendingDevices": missing,
            "confirmedAt": None,
            "expectedApkMd5": expected_apk_md5,
            "devices": installation_devices,
        },
    }
    # Register before starting the install helper: its auto-confirm thread
    # looks the job up in JOBS and would exit immediately otherwise.
    with JOBS_LOCK:
        JOBS[job_id] = job
    try:
        if missing:
            _start_manual_install(job, config, missing)
        else:
            _job_log(job, "目标应用已安装，无需人工确认")
            _launch_job_cli(job)
    except Exception:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        _finish_install_processes(job)
        log_stream.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    persist_job(job)
    return public_job(job, include_log=True)


def _legacy_fastbot_device_check(serial: str, fastbot: Mapping[str, Any]) -> Dict[str, Any]:
    """Compatibility alias; the implementation below is the bundled-runtime check."""
    return _fastbot_device_check(serial, fastbot)
    """

    options = dict(fastbot)
    normalize_runtime_options(options)
    classpath = str(options.get("classpath") or "")
    expected_md5 = str(options.get("expected_monkeyq_md5") or "").strip().lower()
    missing: List[str] = []
    errors: List[str] = []
    native_abi: Optional[str] = None
    native_path: Optional[str] = None
    expected_native_md5 = ""
    actual_native_md5 = ""
    for remote_file in [item for item in classpath.split(":") if item]:
        try:
            result = DeviceManager._run_adb(["shell", "ls", remote_file], serial=serial, timeout=10, retries=1)  # type: ignore[attr-defined]
            if result.returncode != 0:
                missing.append(remote_file)
        except Exception as exc:
            errors.append(str(exc))
    actual_md5 = ""
    if expected_md5:
        try:
            result = DeviceManager._run_adb(["shell", "md5sum", runtime_remote_path()], serial=serial, timeout=10, retries=1)  # type: ignore[attr-defined]
            match = re.search(r"\b([A-Fa-f0-9]{32})\b", result.stdout or "")
            actual_md5 = match.group(1).lower() if match else ""
            if actual_md5 != expected_md5: pass
            errors.append("Fastbot 运行包 MD5 不匹配：期望 %s，实际 %s" % (expected_md5, actual_md5 or "不可用"))
        except Exception as exc:
            errors.append("无法校验 Fastbot 运行包：%s" % exc)

    abi_values: List[str] = []
    for property_name in ("ro.product.cpu.abilist", "ro.product.cpu.abi"):
        try:
            result = DeviceManager._run_adb(  # type: ignore[attr-defined]
                ["shell", "getprop", property_name],
                serial=serial,
                timeout=10,
                retries=1,
            )
            if result.returncode == 0:
                abi_values.append(result.stdout or "")
                native_abi = first_supported_abi(abi_values)
                if native_abi:
                    break
        except Exception as exc:
            errors.append("无法读取设备 ABI：%s" % exc)
    if not native_abi:
        errors.append("无法确定 Fastbot native 库 ABI")
    else:
        native_path = native_remote_path(native_abi)
        local_native = native_asset_path(native_abi)
        if not local_native.is_file():
            errors.append(
                "本机缺少 ABI %s 的 Fastbot native 库：%s"
                % (native_abi, local_native)
            )
        else:
            expected_native_md5 = str(
                options.get("expected_fastbot_native_md5")
                or fastbot_asset_md5(local_native)
            ).strip().lower()
        try:
            result = DeviceManager._run_adb(  # type: ignore[attr-defined]
                ["shell", "ls", native_path],
                serial=serial,
                timeout=10,
                retries=1,
            )
            if result.returncode != 0:
                missing.append(native_path)
            elif expected_native_md5:
                checksum = DeviceManager._run_adb(  # type: ignore[attr-defined]
                    ["shell", "md5sum", native_path],
                    serial=serial,
                    timeout=10,
                    retries=1,
                )
                match = re.search(r"\b([A-Fa-f0-9]{32})\b", checksum.stdout or "")
                actual_native_md5 = match.group(1).lower() if match else ""
                if checksum.returncode != 0 or actual_native_md5 != expected_native_md5:
                    errors.append(
                        "libfastbot_native.so MD5 不匹配：期望 %s，实际 %s"
                        % (expected_native_md5, actual_native_md5 or "不可用")
                    )
        except Exception as exc:
            errors.append("无法校验 Fastbot native 库：%s" % exc)
    if missing:
        errors.append("设备缺少 Fastbot 文件：%s" % ", ".join(missing))
    return {
        "serial": serial,
        "ok": not errors,
        "classpath": classpath,
        "expectedMd5": expected_md5 or None,
        "actualMd5": actual_md5 or None,
        "nativeAbi": native_abi,
        "nativePath": native_path,
        "expectedNativeMd5": expected_native_md5 or None,
        "actualNativeMd5": actual_native_md5 or None,
        "error": "; ".join(errors) if errors else None,
    }
    """


def _fastbot_device_check(serial: str, fastbot: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the one-file Fastbot runtime used by new and migrated jobs."""
    options = dict(fastbot)
    normalize_runtime_options(options)
    classpath = str(options.get("classpath") or runtime_remote_path()).strip()
    expected_md5 = str(options.get("expected_monkeyq_md5") or "").strip().lower()
    missing: List[str] = []
    errors: List[str] = []
    for remote_file in [item for item in classpath.split(":") if item]:
        try:
            result = DeviceManager._run_adb(
                ["shell", "ls", remote_file], serial=serial, timeout=10, retries=1
            )  # type: ignore[attr-defined]
            if result.returncode != 0:
                missing.append(remote_file)
        except Exception as exc:
            errors.append(str(exc))

    actual_md5 = ""
    if expected_md5:
        try:
            result = DeviceManager._run_adb(
                ["shell", "md5sum", runtime_remote_path()],
                serial=serial,
                timeout=10,
                retries=1,
            )  # type: ignore[attr-defined]
            match = re.search(r"\b([A-Fa-f0-9]{32})\b", result.stdout or "")
            actual_md5 = match.group(1).lower() if match else ""
            if result.returncode != 0 or actual_md5 != expected_md5:
                errors.append(
                    "Fastbot runtime APK MD5 mismatch: expected %s, got %s"
                    % (expected_md5, actual_md5 or "unavailable")
                )
        except Exception as exc:
            errors.append("Unable to verify Fastbot runtime APK: %s" % exc)

    abi_values: List[str] = []
    native_abi: Optional[str] = None
    native_path: Optional[str] = None
    for property_name in ("ro.product.cpu.abilist", "ro.product.cpu.abi"):
        try:
            result = DeviceManager._run_adb(
                ["shell", "getprop", property_name],
                serial=serial,
                timeout=10,
                retries=1,
            )  # type: ignore[attr-defined]
            if result.returncode == 0:
                abi_values.append(result.stdout or "")
                native_abi = first_supported_abi(abi_values)
                if native_abi:
                    break
        except Exception as exc:
            errors.append("Unable to read device ABI: %s" % exc)
    if not native_abi:
        errors.append("Unable to determine Fastbot native ABI")
    else:
        native_path = bundled_native_remote_path(native_abi)
        if not runtime_bundle_has_native(native_abi, runtime_asset_path()):
            errors.append(
                "Local Fastbot runtime APK has no native library for ABI %s" % native_abi
            )
    if missing:
        errors.append("Device is missing Fastbot runtime: %s" % ", ".join(missing))
    return {
        "serial": serial,
        "ok": not errors,
        "classpath": classpath,
        "expectedMd5": expected_md5 or None,
        "actualMd5": actual_md5 or None,
        "nativeAbi": native_abi,
        "nativePath": native_path,
        "expectedNativeMd5": None,
        "actualNativeMd5": None,
        "error": "; ".join(errors) if errors else None,
    }


def preflight_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Run the same host/device checks that must pass before CLI workers start."""
    checks: List[Dict[str, Any]] = []
    try:
        import uiautomator2  # noqa: F401
        checks.append({"name": "uiautomator2", "ok": True, "detail": "已安装"})
    except ImportError:
        checks.append({"name": "uiautomator2", "ok": False, "detail": "当前 Python 环境未安装 uiautomator2"})
    app = config.get("app", {})
    apk_path = Path(str(app.get("apk_path") or "")) if isinstance(app, Mapping) else Path()
    detected, error = apk_package_name(apk_path) if apk_path.is_file() else (None, "APK 文件不存在")
    expected = str(app.get("package_name") or "") if isinstance(app, Mapping) else ""
    checks.append({"name": "apk", "ok": bool(detected and detected == expected), "detail": error or "包名 %s" % detected})
    fastbot = config.get("fastbot", {}) if isinstance(config.get("fastbot"), Mapping) else {}
    device_checks: List[Dict[str, Any]] = []
    for device in config.get("devices", []):
        serial = str(device.get("serial") if isinstance(device, Mapping) else "").strip()
        ready = False
        try:
            ready = DeviceManager.is_device_ready(serial)
        except Exception:
            ready = False
        device_check = {"serial": serial, "ready": ready, "fastbot": None, "ok": ready}
        if ready:
            device_check["fastbot"] = _fastbot_device_check(serial, fastbot)
            device_check["ok"] = bool(device_check["ok"] and device_check["fastbot"].get("ok"))
        else:
            try:
                input_issue = DeviceManager.input_injection_block_reason(serial)
            except Exception:
                input_issue = None
            device_check["error"] = input_issue or "设备未连接、未授权、未开机完成或处于锁屏"
        device_checks.append(device_check)
    checks.append({"name": "devices", "ok": bool(device_checks) and all(item["ok"] for item in device_checks), "devices": device_checks})
    return {"success": all(bool(item.get("ok")) for item in checks), "checks": checks, "engine": "fastbot"}


def preflight_run(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not str(payload.get("apkPackageName") or "").strip():
        raise ApiError("请重新上传 APK，启动前必须完成 manifest 包名校验", HTTPStatus.UNPROCESSABLE_ENTITY)
    preflight_dir = RUN_ROOT / ("preflight-%s" % uuid.uuid4().hex)
    try:
        config = build_run_config(payload, preflight_dir)
        result = preflight_config(config)
    finally:
        # A preflight is read-only from the user's perspective. Do not leave
        # an empty run directory that later appears as a historical report.
        shutil.rmtree(preflight_dir, ignore_errors=True)
    result["config"] = {
        "file": str(payload.get("configFile") or "settings.json"),
        "packageName": config["app"]["package_name"],
        "classpath": config["fastbot"].get("classpath"),
        "expectedMd5": config["fastbot"].get("expected_monkeyq_md5"),
        "expectedNativeMd5": config["fastbot"].get("expected_fastbot_native_md5"),
    }
    return result


def _job_evidence(job: Mapping[str, Any]) -> Dict[str, Any]:
    reports = _cached_job_report_paths(job)
    known_ids = _job_known_execution_ids(job)
    pressure_started = False
    installation_started = False
    errors: List[str] = []
    devices: Dict[str, Dict[str, Any]] = {}
    for path in reports:
        report = _cached_json_document(path, {})
        if not isinstance(report, dict):
            continue
        report_ids = {
            str(report.get(key) or "").strip()
            for key in ("run_id", "job_id", "test_id")
            if str(report.get(key) or "").strip()
        }
        if report_ids and known_ids.isdisjoint(report_ids):
            continue
        serial = str(report.get("device_serial") or path.parent.name)
        preflight = report.get("preflight", {})
        if isinstance(preflight, Mapping) and preflight.get("success"):
            installation_started = True
        cycles = unique_cycle_rows(report)
        _resolved_report, checkpoint = _job_device_artifacts(job, serial)
        snapshot = _device_resume_snapshot(serial, job)
        log_path = path.parent / "fastbot.log"
        log_text = log_tail(log_path, 4000) if log_path.is_file() else ""
        has_marker = "=== Fastbot started " in log_text
        has_pressure = has_marker or any(
            isinstance(item, Mapping)
            and (
                int(item.get("operation_count", 0) or 0) > 0
                or float(item.get("pressure_elapsed_seconds", 0) or 0) > 0
            )
            for item in cycles
        ) if isinstance(cycles, list) else has_marker
        pressure_started = pressure_started or has_pressure
        report_errors = [str(item) for item in report.get("errors", []) if str(item).strip()] if isinstance(report.get("errors"), list) else []
        errors.extend(report_errors)
        # The snapshot reconciles the persisted resume marker with Fastbot's
        # liveness/operation heartbeat. Use it here so a dead or stalled engine
        # cannot keep the whole job classified as ordinary ``running``.
        effective_state = str(
            snapshot.get("resume_state") or _effective_resume_state(report, checkpoint)
        )
        devices[serial] = {
            "serial": serial,
            "preflight": bool(isinstance(preflight, Mapping) and preflight.get("success")),
            "fastbotStarted": has_pressure,
            "cycles": int(snapshot.get("completed_cycles", 0) or 0)
            if snapshot
            else (len(cycles) if isinstance(cycles, list) else 0),
            "operations": int(snapshot.get("operation_count", 0) or 0)
            if snapshot
            else max(
                sum(int(item.get("operation_count", 0) or 0) for item in cycles if isinstance(item, Mapping))
                if isinstance(cycles, list) else 0,
                int((checkpoint or {}).get("operation_count", 0) or 0),
            ),
            "status": _public_device_resume_status(effective_state),
            "errors": report_errors,
            "resumeState": effective_state,
            "resumeAttempts": max(
                int(report.get("resume_attempts", 0) or 0),
                int((checkpoint or {}).get("resume_attempts", 0) or 0),
            ),
            "remainingSeconds": float(
                report.get("remaining_seconds", (checkpoint or {}).get("remaining_seconds", 0)) or 0
            ),
            "lastHeartbeat": report.get("last_heartbeat")
            or (checkpoint or {}).get("last_heartbeat"),
            "fastbotAlive": snapshot.get("fastbot_alive"),
            "lastOperationAt": snapshot.get("last_operation_at"),
            "operationStalled": bool(snapshot.get("operation_stalled")),
            "currentCycle": report.get("current_cycle")
            or ({
                "cycle": (checkpoint or {}).get("cycle"),
                "phase": (checkpoint or {}).get("phase"),
                "remaining_seconds": (checkpoint or {}).get("remaining_seconds"),
            } if checkpoint else None),
            "lastError": report.get("last_error")
            or (checkpoint or {}).get("last_error"),
        }
    # A checkpoint can be flushed just before report.json is created (or while
    # the latter is being atomically replaced).  Include that serial in job
    # evidence so a transient host restart does not erase its state.
    configured_serials = job.get("devices", [])
    if isinstance(configured_serials, list):
        for serial_value in configured_serials:
            serial = str(serial_value)
            if serial in devices:
                continue
            snapshot = _device_resume_snapshot(serial, job)
            checkpoint = snapshot.get("checkpoint")
            report = snapshot.get("report")
            if not checkpoint and not report:
                continue
            report_mapping = report if isinstance(report, Mapping) else {}
            effective_state = str(snapshot.get("resume_state") or report_mapping.get("status") or "running")
            devices[serial] = {
                "serial": serial,
                "preflight": False,
                "fastbotStarted": bool(
                    snapshot.get("elapsed_seconds", 0)
                    or (checkpoint or {}).get("operation_count", 0)
                ),
                "cycles": int(snapshot.get("completed_cycles", 0) or 0),
                "operations": int(snapshot.get("operation_count", 0) or 0),
                "status": _public_device_resume_status(effective_state),
                "errors": [],
                "resumeState": effective_state,
                "resumeAttempts": int(snapshot.get("resume_attempts", 0) or 0),
                "remainingSeconds": float(snapshot.get("remaining_seconds", 0) or 0),
                "lastHeartbeat": snapshot.get("last_heartbeat"),
                "fastbotAlive": snapshot.get("fastbot_alive"),
                "lastOperationAt": snapshot.get("last_operation_at"),
                "operationStalled": bool(snapshot.get("operation_stalled")),
                "currentCycle": snapshot.get("current_cycle"),
                "lastError": snapshot.get("last_error"),
            }
            if snapshot.get("elapsed_seconds", 0):
                pressure_started = True
    installation = copy.deepcopy(job.get("installation"))
    waiting_install = bool(
        isinstance(installation, Mapping)
        and installation.get("required")
        and installation.get("status") == "waiting"
    )
    device_resume_states = {
        str(item.get("resumeState") or "") for item in devices.values()
    }
    if waiting_install:
        phase = "awaiting_install"
    else:
        # Aggregate state is only a job-level hint. Prefer a live sibling over
        # a manually paused device so one device can stop while the remaining
        # workers continue in the same job.
        if "running" in device_resume_states:
            phase = "running"
        elif "resuming" in device_resume_states:
            phase = "resuming"
        elif "recovering" in device_resume_states:
            phase = "recovering"
        elif "waiting_for_device" in device_resume_states:
            phase = "waiting_for_device"
        elif "needs_manual_resume" in device_resume_states:
            phase = "needs_manual_resume"
        elif "paused" in device_resume_states:
            phase = "paused"
        elif MANUALLY_ENDED_STATE in device_resume_states:
            phase = "stopped"
        else:
            phase = "running" if pressure_started else "installing" if installation_started else "preparing"
    return {
        "phase": phase,
        "pressureStarted": pressure_started,
        "reports": devices,
        "errors": list(dict.fromkeys(errors)),
        "installation": installation,
    }


def _reap_finished_job_processes(job: Dict[str, Any]) -> None:
    processes = job.get("_processes")
    if not isinstance(processes, dict):
        return
    process_devices = job.get("_processDevices")
    primary_key = str(job.get("_primaryProcessKey") or "")
    for key, process in list(processes.items()):
        try:
            finished = process.poll() is not None
        except (OSError, ValueError, AttributeError):
            finished = True
        if not finished or str(key) == primary_key:
            continue
        processes.pop(key, None)
        serial_values = process_devices.pop(key, []) if isinstance(process_devices, dict) else []
        with JOBS_LOCK:
            for serial in serial_values if isinstance(serial_values, list) else []:
                value = str(serial)
                _release_resume_claim_if_unowned(job, value, force=True)
        try:
            process.wait(timeout=0)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass


def _retry_due_epoch(value: Any) -> Optional[float]:
    """Parse an ISO retry timestamp without allowing malformed state to loop."""
    if value in (None, ""):
        return None
    # Accept both the persisted ISO form and a numeric epoch for compatibility
    # with older scheduler snapshots.  Malformed values are treated as due so
    # a bad timestamp cannot permanently strand a recoverable device.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _job_run_duration_elapsed(job: Mapping[str, Any], grace_seconds: float = 600.0) -> bool:
    """Return whether the planned run window (plus grace) is over.

    A device left in ``needs_manual_resume`` keeps the console job open forever
    because it waits for an operator.  Once the planned duration has passed the
    run is over in practice, so callers finalize it as a run with gaps instead of
    letting it linger for hours.
    """
    started = _retry_due_epoch(job.get("startedAt"))
    try:
        duration_minutes = int(job.get("duration") or 0)
    except (TypeError, ValueError):
        return False
    if started is None or duration_minutes <= 0:
        return False
    return time.time() >= started + duration_minutes * 60 + grace_seconds


def _remote_transport_available(serial: str) -> bool:
    """Probe only the ADB transport, without requiring the app to be ready."""
    runner = getattr(DeviceManager, "_run_adb", None)
    if not callable(runner):
        return False
    try:
        result = runner(
            ["get-state"],
            serial=str(serial),
            timeout=max(1, int(_REMOTE_TRANSPORT_PROBE_TIMEOUT_SECONDS)),
            retries=1,
        )
    except Exception:
        return False
    return str(getattr(result, "stdout", "") or "").strip().lower() == "device"


def _best_effort_remote_worker_cleanup_status(serial: str) -> str:
    """Verify and release remote Fastbot/uiautomator2 owners before relaunch."""
    if not _remote_transport_available(serial):
        return _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
    finished = threading.Event()
    outcome = {"status": _REMOTE_CLEANUP_FAILED}

    def cleanup() -> None:
        try:
            controller = MutexController(str(serial))
            for method_name in (
                "_kill_fastbot_if_running",
                "_stop_remote_uiautomator_processes",
                "_wait_for_uiautomation_release",
            ):
                probe = getattr(controller, method_name, None)
                if not callable(probe):
                    continue
                try:
                    if not bool(probe()):
                        outcome["status"] = (
                            _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
                            if not _remote_transport_available(serial)
                            else _REMOTE_CLEANUP_FAILED
                        )
                        return
                except Exception:
                    outcome["status"] = (
                        _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
                        if not _remote_transport_available(serial)
                        else _REMOTE_CLEANUP_FAILED
                    )
                    return
            outcome["status"] = _REMOTE_CLEANUP_RELEASED
        finally:
            finished.set()

    thread = threading.Thread(
        target=cleanup,
        name="web-worker-cleanup-%s" % re.sub(r"[^A-Za-z0-9_.-]", "_", str(serial)),
        daemon=True,
    )
    thread.start()
    if not finished.wait(_REMOTE_CLEANUP_TIMEOUT_SECONDS):
        return (
            _REMOTE_CLEANUP_DEVICE_UNAVAILABLE
            if not _remote_transport_available(serial)
            else _REMOTE_CLEANUP_FAILED
        )
    return str(outcome.get("status") or _REMOTE_CLEANUP_FAILED)


def _persist_job_device_resume_state(
    job: Mapping[str, Any],
    serial: str,
    state: str,
    reason: Optional[str] = None,
    *,
    manual_stop: bool = False,
    renew_wait_window: bool = False,
) -> None:
    """Persist a scheduler transition for report and checkpoint-only runs."""
    report_path, checkpoint = _job_device_artifacts(job, serial)
    if report_path is None:
        expected = _expected_device_report_path(job, serial)
        report_path = expected if expected is not None and checkpoint is not None else None
    report: Optional[Dict[str, Any]] = None
    if report_path is not None and report_path.is_file():
        value = read_json_file(report_path, None)
        if isinstance(value, dict):
            report = value
    normalized = str(state or "recovering").strip().lower()
    if normalized in {"completed", "passed", "completed_with_crashes", "completed_with_errors", "failed"}:
        return
    if report is not None and terminal_resume_state(report):
        return
    now = utc_now()

    # A service restart can discover a transport failure before the worker has
    # had a chance to write its reconnect window.  Make that window durable at
    # the first scheduler transition so subsequent refreshes neither reset the
    # timeout nor wait forever.  Existing valid timestamps are retained,
    # including an already-expired deadline, so a restart cannot extend the
    # configured wait budget accidentally.
    wait_started_at: Optional[str] = None
    wait_deadline_at: Optional[str] = None
    if normalized == "waiting_for_device":
        sources = [report, checkpoint]
        raw_started = None if renew_wait_window else next(
            (
                source.get("wait_started_at")
                for source in sources
                if isinstance(source, Mapping)
                and source.get("wait_started_at") not in (None, "")
            ),
            None,
        )
        raw_deadline = None if renew_wait_window else next(
            (
                source.get("wait_deadline_at")
                for source in sources
                if isinstance(source, Mapping)
                and source.get("wait_deadline_at") not in (None, "")
            ),
            None,
        )
        started_epoch = _retry_due_epoch(raw_started)
        if started_epoch is None:
            started_epoch = time.time()
            wait_started_at = now
        else:
            wait_started_at = str(raw_started)
        deadline_epoch = _retry_due_epoch(raw_deadline)
        if deadline_epoch is None:
            config_path = Path(str(job.get("configPath") or ""))
            resume = _persisted_resume_settings(config_path)
            try:
                wait_seconds = max(
                    1.0,
                    float(resume.get("wait_for_device_seconds", 1800) or 1800),
                )
            except (TypeError, ValueError, OverflowError):
                wait_seconds = 1800.0
            deadline_epoch = started_epoch + wait_seconds
            wait_deadline_at = datetime.fromtimestamp(
                deadline_epoch, timezone.utc
            ).isoformat()
        else:
            wait_deadline_at = str(raw_deadline)
        if wait_started_at is None:
            wait_started_at = str(raw_started)
        if wait_deadline_at is None:
            wait_deadline_at = str(raw_deadline)
    if report is not None:
        report["status"] = normalized
        report["resume_state"] = normalized
        report["finished_at"] = None
        report["scheduler_updated_at"] = now
        if normalized == "waiting_for_device":
            report["waiting_for_device"] = True
            report["wait_started_at"] = wait_started_at
            report["wait_deadline_at"] = wait_deadline_at
        if reason:
            report["last_error"] = str(reason)
            errors = report.setdefault("errors", [])
            if isinstance(errors, list) and str(reason) not in errors:
                errors.append(str(reason))
        if manual_stop:
            # Keep the operator's stop durable even when the worker was
            # already gone (for example, an offline device in a reconnect
            # window).  The scheduler treats this marker as a manual gate.
            report["manual_stop"] = True
            report["recovery_category"] = "manual_stop"
            report["recovery_reason"] = str(reason or "pressure stopped by web console")
        current = report.get("current_cycle")
        if isinstance(current, dict):
            current["status"] = normalized
            if reason:
                current["last_error"] = str(reason)
        try:
            write_json_file(report_path, report)
        except OSError:
            pass
    checkpoint_path: Optional[Path] = None
    if checkpoint is not None:
        if report_path is not None:
            checkpoint_path = report_path.parent / "checkpoint.json"
        else:
            expected = _expected_device_report_path(job, serial)
            checkpoint_path = expected.parent / "checkpoint.json" if expected is not None else None
    if checkpoint_path is not None and checkpoint_path.is_file():
        value = read_json_file(checkpoint_path, None)
        if isinstance(value, dict) and not terminal_resume_state(value):
            value["resume_state"] = normalized
            if normalized == "waiting_for_device":
                value["waiting_for_device"] = True
                value["wait_started_at"] = wait_started_at
                value["wait_deadline_at"] = wait_deadline_at
            if reason:
                value["last_error"] = str(reason)
            if manual_stop:
                value["manual_stop"] = True
                value["recovery_category"] = "manual_stop"
                value["recovery_reason"] = str(
                    reason or "pressure stopped by web console"
                )
            value["scheduler_updated_at"] = now
            try:
                write_json_file(checkpoint_path, value)
            except OSError:
                pass


def _persist_manual_end_artifacts(
    job: Mapping[str, Any],
    serial: str,
    reason: str,
    *,
    finished_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Write an immutable, per-device operator-termination marker.

    The legacy ``stopped`` value is also used for resumable web pauses, so a
    separate ``resume_state`` marker is required here.  Both report files are
    updated so either side of an atomic-write race can establish the terminal
    state after a service or worker restart.
    """
    serial_value = str(serial).strip()
    now = str(finished_at or utc_now())
    report_path, strict_checkpoint = _job_device_artifacts(job, serial_value)
    if report_path is None:
        expected = _expected_device_report_path(job, serial_value)
        if expected is not None:
            report_path = expected
    if report_path is None:
        return {
            "state": MANUALLY_ENDED_STATE,
            "report_path": None,
            "checkpoint_path": None,
            "written": False,
        }

    parent = report_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # The caller still releases the logical claim.  The return value makes
        # the persistence gap visible to the UI/log without blocking cleanup.
        return {
            "state": MANUALLY_ENDED_STATE,
            "report_path": report_path,
            "checkpoint_path": parent / "checkpoint.json",
            "written": False,
        }

    raw_report = read_json_file(report_path, {}) if report_path.is_file() else {}
    report = dict(raw_report) if isinstance(raw_report, Mapping) else {}
    checkpoint_path = parent / "checkpoint.json"
    raw_checkpoint = (
        strict_checkpoint
        if isinstance(strict_checkpoint, Mapping)
        else read_json_file(checkpoint_path, {})
        if checkpoint_path.is_file()
        else {}
    )
    checkpoint = (
        dict(raw_checkpoint) if isinstance(raw_checkpoint, Mapping) else {}
    )

    existing_terminal = terminal_resume_state(report) or terminal_resume_state(
        checkpoint
    )
    if existing_terminal and existing_terminal != MANUALLY_ENDED_STATE:
        return {
            "state": existing_terminal,
            "report_path": report_path,
            "checkpoint_path": checkpoint_path,
            "written": False,
        }

    execution_id = _job_execution_run_id(job)
    report.setdefault("run_id", execution_id)
    report.setdefault("job_id", str(job.get("id") or execution_id))
    report.setdefault("device_serial", serial_value)
    report.setdefault("package_name", str(job.get("packageName") or ""))
    report.setdefault("started_at", job.get("startedAt"))
    if not isinstance(report.get("cycles"), list):
        report["cycles"] = []
    report.update(
        {
            "status": "stopped",
            "resume_state": MANUALLY_ENDED_STATE,
            "manual_end": True,
            "manual_stop": True,
            "finished_at": now,
            "remaining_seconds": 0.0,
            "waiting_for_device": False,
            "resume_attempt_reserved": False,
            "next_retry_at": None,
            "recovery_category": "manual_end",
            "recovery_reason": str(reason),
            "last_error": str(reason),
            "scheduler_updated_at": now,
            "manual_end_at": now,
        }
    )
    current = report.get("current_cycle")
    if not isinstance(current, dict):
        cp_cycle = _nonnegative_int(checkpoint.get("cycle"), 0)
        if cp_cycle:
            current = {
                "cycle": cp_cycle,
                "phase": _nonnegative_int(checkpoint.get("phase"), 0),
                "pressure_elapsed_seconds": _nonnegative_float(
                    checkpoint.get("elapsed_seconds"), 0.0
                ),
                "operation_count": _nonnegative_int(
                    checkpoint.get("operation_count"), 0
                ),
            }
            report["current_cycle"] = current
    if isinstance(current, dict):
        current.update(
            {
                "status": "stopped",
                "resume_state": MANUALLY_ENDED_STATE,
                "completed": False,
                "remaining_seconds": 0.0,
                "stop_reason": "manual_end",
                "last_error": str(reason),
            }
        )
    errors = report.setdefault("errors", [])
    if isinstance(errors, list) and str(reason) not in errors:
        errors.append(str(reason))
    coverage = report.get("coverage")
    if isinstance(coverage, dict):
        coverage["status"] = "stopped"

    checkpoint.setdefault("schema_version", 1)
    checkpoint.setdefault("job_id", execution_id)
    checkpoint.setdefault("device_serial", serial_value)
    checkpoint.update(
        {
            "status": "stopped",
            "resume_state": MANUALLY_ENDED_STATE,
            "manual_end": True,
            "manual_stop": True,
            "finished_at": now,
            "saved_at": now,
            "last_heartbeat": now,
            "scheduler_updated_at": now,
            "phase_name": "stopped",
            "running": False,
            "remaining_seconds": 0.0,
            "resume_attempt_reserved": False,
            "recovery_category": "manual_end",
            "recovery_reason": str(reason),
            "last_error": str(reason),
        }
    )
    try:
        # Generate the normal HTML/artifact index when possible, but keep the
        # JSON marker authoritative if an incomplete report lacks optional
        # fields required by the renderer.
        try:
            paths = ReportGenerator.write_all(report, parent)
            if isinstance(paths, Mapping):
                artifacts = report.setdefault("artifacts", {})
                if isinstance(artifacts, dict):
                    artifacts.update(paths)
        except Exception:
            pass
        write_json_file(report_path, report)
        write_json_file(checkpoint_path, checkpoint)
        return {
            "state": MANUALLY_ENDED_STATE,
            "report_path": report_path,
            "checkpoint_path": checkpoint_path,
            "written": True,
        }
    except OSError:
        return {
            "state": MANUALLY_ENDED_STATE,
            "report_path": report_path,
            "checkpoint_path": checkpoint_path,
            "written": False,
        }


def _detach_device_from_local_processes(
    job: Dict[str, Any], serial: str
) -> List[subprocess.Popen]:
    """Remove one serial from local owner maps and return solo processes.

    A shared CLI invocation must keep running for its sibling devices.  A
    process that owns only the ended serial can be signalled after the map is
    detached; callers decide whether a bounded force-stop is necessary.
    """
    serial_value = str(serial)
    processes = job.get("_processes")
    if not isinstance(processes, dict):
        processes = {}
        job["_processes"] = processes
    legacy_process = job.get("process")
    if _is_process_handle(legacy_process):
        processes.setdefault(
            str(job.get("_primaryProcessKey") or "primary"), legacy_process
        )
    process_devices = job.get("_processDevices")
    if not isinstance(process_devices, dict):
        process_devices = {}
        job["_processDevices"] = process_devices
    solo: List[subprocess.Popen] = []
    if isinstance(processes, Mapping):
        for key, process in list(processes.items()):
            values = process_devices.get(key)
            if not isinstance(values, list):
                values = [
                    str(value)
                    for value in job.get("devices", [])
                    if str(value) != serial_value
                ] + [serial_value]
            if serial_value not in {str(value) for value in values}:
                continue
            remaining = [str(value) for value in values if str(value) != serial_value]
            process_devices[key] = remaining
            if not remaining and _is_process_handle(process):
                solo.append(process)
    # Keep the legacy public handle aligned with the detached map.
    primary_key = str(job.get("_primaryProcessKey") or "")
    if primary_key and primary_key in process_devices:
        remaining_primary = process_devices.get(primary_key) or []
        if not remaining_primary and not _live_job_processes(job):
            job["process"] = None
            job["pid"] = None
            job["_primaryProcessKey"] = ""
    return solo


def _clear_device_resume_request(job: Mapping[str, Any], serial: str) -> None:
    """Remove a stale explicit-resume request after a device becomes terminal."""
    parent = _resume_claim_parent(job, serial)
    if parent is None:
        return
    try:
        recovery_request_path(parent).unlink(missing_ok=True)
    except OSError:
        pass


def _record_job_device_manual_end(job: Dict[str, Any], serial: str) -> None:
    """Update job bookkeeping without changing any sibling device state."""
    serial_value = str(serial)
    manual_end_devices = job.get("manualEndDevices")
    if not isinstance(manual_end_devices, list):
        manual_end_devices = []
        job["manualEndDevices"] = manual_end_devices
    if serial_value not in {str(value) for value in manual_end_devices}:
        manual_end_devices.append(serial_value)

    manual_stop_devices = job.get("manualStopDevices")
    if isinstance(manual_stop_devices, list):
        job["manualStopDevices"] = [
            value for value in manual_stop_devices if str(value) != serial_value
        ]

    # A still-exiting solo CLI is no longer an owner after its only device has
    # reached an immutable terminal state.  Publishing the stopped job now
    # also prevents a later non-zero child exit from relabelling a deliberate
    # manual end as a failed batch.
    states = _persisted_job_device_states(job)
    all_terminal = bool(states) and all(
        terminal_resume_state(item.get("report"))
        or terminal_resume_state(item.get("checkpoint"))
        for item in states.values()
    )
    if all_terminal:
        job["status"] = "stopped"
        job["phase"] = "stopped"
        job["finishedAt"] = job.get("finishedAt") or utc_now()
    elif job.get("status") in {"paused", "needs_manual_resume", "waiting_for_device"}:
        job["phase"] = job.get("status")
        job["finishedAt"] = None


def end_job_device(identifier: str, serial: str) -> Dict[str, Any]:
    """End one device's portion of a batch while preserving its siblings."""
    serial_value = str(serial).strip()
    if not serial_value:
        raise ApiError("device serial is required")
    with _resume_request_lock(identifier, serial_value):
        with JOBS_LOCK:
            job = JOBS.get(identifier)
        if job is None:
            candidate = RUN_ROOT / identifier / "job.json"
            value = read_json_file(candidate, None)
            if isinstance(value, Mapping):
                job = dict(value)
                job.update(
                    {
                        "jobDir": str(candidate.parent),
                        "reportRoot": str(candidate.parent / "reports"),
                        "logPath": str(candidate.parent / "console.log"),
                        "configPath": str(candidate.parent / "config.json"),
                        "process": None,
                        "_processes": {},
                        "_processDevices": {},
                    }
                )
                execution_run_id = _job_execution_run_id(job)
                if execution_run_id:
                    job["runId"] = execution_run_id
                with JOBS_LOCK:
                    JOBS[identifier] = job
            else:
                raise ApiError("job not found", HTTPStatus.NOT_FOUND)
        configured = {
            str(value).strip()
            for value in (job.get("devices", []) or [])
            if str(value).strip()
        }
        if serial_value not in configured:
            raise ApiError("device is not part of this job", HTTPStatus.NOT_FOUND)

        # Read the durable state before touching ownership.  Repeated calls are
        # intentionally idempotent and do not send another remote stop.
        snapshot = _device_resume_snapshot(serial_value, job)
        durable_state = str(snapshot.get("resume_state") or "").strip().lower()
        if durable_state == MANUALLY_ENDED_STATE:
            with JOBS_LOCK:
                solo_processes = _detach_device_from_local_processes(
                    job, serial_value
                )
                for process in solo_processes:
                    _signal_local_stop(process)
                _release_resume_claim_if_unowned(job, serial_value, force=True)
                _record_job_device_manual_end(job, serial_value)
            _clear_device_resume_request(job, serial_value)
            persist_job(job)
            return {
                "serial": serial_value,
                "ended": True,
                "stopped": True,
                "manualEnd": True,
                "remoteStopped": True,
                "idempotent": True,
            }

        # Best-effort remote stop.  A disconnected device is still allowed to
        # reach the logical terminal state; a reconnect must not resurrect it.
        remote_stopped: Optional[bool] = None
        remote_error: Optional[str] = None
        try:
            device_states = DeviceManager.get_device_states()
            online = (
                isinstance(device_states, Mapping)
                and str((device_states.get(serial_value) or {}).get("adb_state") or "")
                == "device"
            )
            if online:
                remote_stopped = bool(
                    MutexController(serial_value).request_stop_remote_fastbot()
                )
                if not remote_stopped:
                    remote_error = "remote Fastbot stop could not be confirmed"
            else:
                remote_stopped = False
                remote_error = "device transport unavailable; logical end recorded"
        except Exception as exc:
            remote_stopped = False
            remote_error = str(exc) or type(exc).__name__

        reason = "device test ended manually from web console"
        if remote_error:
            reason = "%s (%s)" % (reason, remote_error)

        # Publish the immutable marker before releasing any local or durable
        # owner.  This closes the window in which the old scheduler could see
        # an unclaimed resumable device and enqueue it again.
        persisted = _persist_manual_end_artifacts(job, serial_value, reason)
        persisted_state = str(persisted.get("state") or "").strip().lower()
        if persisted_state != MANUALLY_ENDED_STATE:
            # The device completed (or otherwise reached a stronger terminal
            # result) while this request was in flight. It is already free for
            # a new task, but must not be relabelled as manually ended.
            with JOBS_LOCK:
                _detach_device_from_local_processes(job, serial_value)
                _release_resume_claim_if_unowned(job, serial_value, force=True)
            persist_job(job)
            return {
                "serial": serial_value,
                "ended": False,
                "stopped": True,
                "manualEnd": False,
                "terminalState": persisted_state,
                "remoteStopped": bool(remote_stopped),
                "remoteError": remote_error,
                "idempotent": True,
            }
        if not bool(persisted.get("written")):
            raise ApiError(
                "unable to persist the manual-end state; device ownership was retained",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        _clear_device_resume_request(job, serial_value)

        # Detach only this serial.  A shared primary process remains attached
        # to sibling serials; a solo owner receives a graceful stop signal.
        with JOBS_LOCK:
            solo_processes = _detach_device_from_local_processes(job, serial_value)
            for process in solo_processes:
                # ``solo_processes`` was calculated from the owner map before
                # detaching this serial; other sibling processes may still be
                # live and do not change the fact that this handle is safe to
                # stop.
                _signal_local_stop(process)
            claims = job.get("_resumeClaims")
            durable_claim = _read_resume_claim(job, serial_value)
            claim_parent = _resume_claim_parent(job, serial_value)
            claim_id = (
                claims.get(serial_value)
                if isinstance(claims, Mapping)
                else None
            ) or (durable_claim or {}).get("claim_id")
            if claim_parent is not None and claim_id:
                release_resume_claim(claim_parent, str(claim_id))
            _forget_resume_claim(job, serial_value)
            preacquired = job.get("_resumeClaimPreacquired")
            if isinstance(preacquired, list):
                job["_resumeClaimPreacquired"] = [
                    value for value in preacquired if str(value) != serial_value
                ]
            elif isinstance(preacquired, set):
                job["_resumeClaimPreacquired"] = {
                    value for value in preacquired if str(value) != serial_value
                }

        _record_job_device_manual_end(job, serial_value)
        persist_job(job)
        return {
            "serial": serial_value,
            "ended": True,
            "stopped": True,
            "manualEnd": True,
            "remoteStopped": bool(remote_stopped),
            "remoteError": remote_error,
            "persisted": bool(persisted.get("written")),
            "idempotent": False,
        }


def _maybe_dispatch_persisted_recovery(job: Dict[str, Any]) -> bool:
    """Run at most one persisted-recovery scan per job at a time."""
    with JOBS_LOCK:
        if job.get("_autoResumeDispatching"):
            return False
        job["_autoResumeDispatching"] = True
    try:
        return _dispatch_persisted_recovery_once(job)
    finally:
        with JOBS_LOCK:
            job["_autoResumeDispatching"] = False


def _dispatch_persisted_recovery_once(job: Dict[str, Any]) -> bool:
    """Re-dispatch recoverable devices after an owner process has exited.

    The durable claim is the primary duplicate guard.  This path is mainly for
    a web-service restart where no ``Popen`` handle exists to trigger the
    normal completion callback; the CLI still performs its device-side cleanup
    gate before entering the pressure loop.
    """
    installation = job.get("installation")
    installation_status = (
        str(installation.get("status") or "").strip().lower()
        if isinstance(installation, Mapping)
        else ""
    )
    if (
        job.get("manualStopRequested")
        or job.get("stopRequestedAt")
        or installation_status == "cancelled"
    ):
        # A whole-job stop is a durable manual gate.  A web-service restart or
        # a stale device heartbeat must never reinterpret it as an automatic
        # recovery request.  The explicit per-device resume endpoint clears
        # these markers before asking this scheduler to dispatch again.
        return False
    # A pre-claim/legacy job snapshot only has the CLI PID.  During a service
    # restart that process may still be alive even though no local Popen handle
    # exists; never start a replacement until the recorded owner exits.
    if job.get("_legacyProcessAlive"):
        if _pid_is_alive(job.get("pid")):
            return False
        job["_legacyProcessAlive"] = False
        job["pid"] = None
    elif job.get("pid") and _pid_is_alive(job.get("pid")) and not _live_job_processes(job):
        # Re-check the persisted PID on every dispatch attempt.  This covers a
        # service restart between load and the first refresh call.
        return False
    if job.get("status") in {
        "awaiting_install",
        "stopping",
        "completed",
        "failed",
        "stopped",
    }:
        return False
    config_path = Path(str(job.get("configPath") or ""))
    if not config_path.is_file():
        return False
    resume = _persisted_resume_settings(config_path)
    states = _persisted_job_device_states(job)
    pending_request_serials = {
        serial
        for serial in states
        if _pending_manual_resume_request(job, serial) is not None
    }
    automatic_enabled = bool(
        resume.get("enabled") and resume.get("mode") == "auto"
    )
    # An explicit request is an operator opt-in and must be dispatched even
    # when the persisted configuration uses manual mode (or has automatic
    # recovery disabled).  Without a request, retain the old automatic gate.
    if not automatic_enabled and not pending_request_serials:
        return False
    live_claims = _live_resume_claim_serials(job)
    max_attempts = int(resume.get("max_attempts", 3) or 0)
    candidates: List[str] = []
    waiting_serials: set[str] = set()
    manual_required = False
    expired_wait = False
    now = time.time()
    for serial, item in states.items():
        explicit_request = serial in pending_request_serials
        state = str(item.get("state") or "").strip()
        # An operator-ended device is terminal for this job.  A stale resume
        # request or an automatic scan must never put it back in the queue.
        if state == MANUALLY_ENDED_STATE:
            continue
        if explicit_request:
            if state not in (
                AUTOMATIC_RECOVERY_STATES
                | MANUAL_RECOVERY_STATES
                | {"resuming"}
            ):
                continue
        elif (
            not automatic_enabled
            or (state not in AUTOMATIC_RECOVERY_STATES and state != "recovering")
        ):
            continue
        # A live sibling invocation must not block this device from resuming,
        # but the exact invocation that still owns this serial remains an
        # unconditional duplicate guard.
        if serial in live_claims or _live_process_owners(job, serial):
            continue
        if not item.get("has_artifact"):
            continue
        report = item.get("report") if isinstance(item.get("report"), Mapping) else {}
        checkpoint = item.get("checkpoint") if isinstance(item.get("checkpoint"), Mapping) else {}
        if state == "waiting_for_device":
            waiting_serials.add(serial)
            # ``wait_deadline_at`` is durable across a web-service restart.
            # Enforce it here instead of creating a fresh in-memory timeout on
            # every refresh, which would otherwise wait forever (or relaunch
            # a device after the original deadline has elapsed).
            if not explicit_request:
                wait_deadline = _retry_due_epoch(
                    report.get("wait_deadline_at")
                    or checkpoint.get("wait_deadline_at")
                )
                if wait_deadline is None:
                    wait_started = _retry_due_epoch(
                        report.get("wait_started_at")
                        or checkpoint.get("wait_started_at")
                    )
                    if wait_started is not None:
                        wait_deadline = wait_started + max(
                            1.0,
                            float(resume.get("wait_for_device_seconds", 1800) or 1800),
                        )
                if wait_deadline is not None and wait_deadline <= now:
                    _persist_job_device_resume_state(
                        job,
                        serial,
                        "needs_manual_resume",
                        "wait_for_device_timeout: persisted reconnect deadline elapsed",
                    )
                    waiting_serials.discard(serial)
                    manual_required = True
                    expired_wait = True
                    continue
        if not explicit_request:
            if _nonnegative_int(item.get("attempts"), 0) >= max_attempts:
                manual_required = True
                continue
            retry_at = _retry_due_epoch(
                report.get("next_retry_at") or checkpoint.get("next_retry_at")
            )
            if retry_at is not None and retry_at > now:
                continue
        candidates.append(serial)
    if not candidates:
        if waiting_serials:
            # Keep the batch alive while at least one device is inside its
            # reconnect window.  A later refresh will run the same cleanup gate
            # and dispatch the original run id once that serial is back.
            if _job_run_duration_elapsed(job):
                job["status"] = "completed_with_gaps"
                job["phase"] = "completed"
                job["finishedAt"] = utc_now()
                if job.get("exitCode") is None:
                    job["exitCode"] = 1
                audit_event("job_finalized_with_gaps", job_id=job.get("id"))
            else:
                job["status"] = "waiting_for_device"
                job["phase"] = "waiting_for_device"
                job["finishedAt"] = None
            job["evidence"] = _cached_job_evidence(job)
            persist_job(job)
        elif expired_wait or manual_required:
            if _job_run_duration_elapsed(job):
                # The planned window is over; waiting for an operator would keep
                # the job "running" for hours (one overnight run lingered from
                # 03:40 to 09:35).  Finalize it and keep the partial evidence.
                job["status"] = "completed_with_gaps"
                job["phase"] = "completed"
                job["finishedAt"] = utc_now()
                if job.get("exitCode") is None:
                    job["exitCode"] = 1
                audit_event("job_finalized_with_gaps", job_id=job.get("id"))
            else:
                job["status"] = "needs_manual_resume"
                job["phase"] = "needs_manual_resume"
                job["finishedAt"] = None
            job["evidence"] = _cached_job_evidence(job)
            persist_job(job)
        return False

    # Never launch a replacement while a previous Fastbot/uiautomator2 owner
    # might still be attached to the device. This explicit gate is required
    # after a web-service restart, where the local Popen handle is gone.
    cleaned: List[str] = []
    for serial in candidates:
        cleanup_status = _best_effort_remote_worker_cleanup_status(serial)
        if cleanup_status == _REMOTE_CLEANUP_RELEASED:
            cleaned.append(serial)
            continue
        if cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
            unavailable_reason = (
                "device transport is unavailable before requested resume"
                if serial in pending_request_serials
                else "device transport is unavailable before automatic resume"
            )
            _persist_job_device_resume_state(
                job,
                serial,
                "waiting_for_device",
                unavailable_reason,
            )
            waiting_serials.add(serial)
            continue
        _persist_job_device_resume_state(
            job,
            serial,
            "needs_manual_resume",
            "previous Fastbot/uiautomator2 control could not be verified released",
        )
        waiting_serials.discard(serial)
        manual_required = True
    candidates = cleaned
    if not candidates:
        job["evidence"] = _cached_job_evidence(job)
        evidence_states = {
            str(item.get("resumeState") or "")
            for item in job["evidence"].get("reports", {}).values()
            if isinstance(item, Mapping)
        }
        if waiting_serials or "waiting_for_device" in evidence_states:
            state = "waiting_for_device"
        elif manual_required or expired_wait or "needs_manual_resume" in evidence_states:
            state = "needs_manual_resume"
        elif "recovering" in evidence_states:
            state = "recovering"
        elif "resuming" in evidence_states:
            state = "resuming"
        else:
            # All candidates may simply be held by a future backoff timestamp;
            # leave the current state untouched and let the next refresh retry.
            return False
        job["status"] = state
        job["phase"] = state
        job["finishedAt"] = None
        persist_job(job)
        return False
    try:
        for serial in candidates:
            if serial in pending_request_serials:
                # An offline click leaves the durable report in a paused/waiting
                # state. Clear that file-level manual marker before the new
                # worker starts, otherwise the first heartbeat could preserve
                # the stale pause over the explicit request.
                _mark_device_resume_requested(job, serial, requested_by="web")
                _clear_device_manual_stop_markers(job, serial)
        _launch_job_cli(job, devices=candidates)
        return True
    except ApiError as exc:
        # A concurrent service/worker may have won the file claim. Keep the
        # durable state observable and let that owner finish rather than
        # converting a race into a manual failure.
        if exc.status == HTTPStatus.CONFLICT:
            return False
        evidence = _cached_job_evidence(job)
        evidence_states = {
            str(item.get("resumeState") or "")
            for item in evidence.get("reports", {}).values()
            if isinstance(item, Mapping)
        }
        state = "waiting_for_device" if waiting_serials else "needs_manual_resume"
        if not waiting_serials and "recovering" in evidence_states:
            state = "recovering"
        job["status"] = state
        job["phase"] = state
        job["finishedAt"] = None
        job["lastError"] = exc.message
        job["evidence"] = evidence
        return False
    except Exception as exc:
        evidence = _cached_job_evidence(job)
        evidence_states = {
            str(item.get("resumeState") or "")
            for item in evidence.get("reports", {}).values()
            if isinstance(item, Mapping)
        }
        state = "waiting_for_device" if waiting_serials else "needs_manual_resume"
        if not waiting_serials and "recovering" in evidence_states:
            state = "recovering"
        job["status"] = state
        job["phase"] = state
        job["finishedAt"] = None
        job["lastError"] = str(exc)
        job["evidence"] = evidence
        return False


def refresh_job(job: Dict[str, Any]) -> None:
    _hydrate_resume_claims(job)
    _reap_finished_job_processes(job)
    process = job.get("process")
    dead_primary_process = None
    dead_primary_serials: List[str] = []
    dead_primary_key = str(job.get("_primaryProcessKey") or "")
    # If the primary invocation has exited while a device-level resume is still
    # active, promote the live invocation so the job remains observable.
    try:
        primary_live = process is not None and process.poll() is None
    except (OSError, ValueError, AttributeError):
        primary_live = False
    if process is not None and not primary_live:
        # Remove the dead primary from the owner map before checking whether a
        # persisted recovery may be dispatched.  Leaving a stale handle here
        # makes ``_live_job_processes`` report a phantom owner after a service
        # restart and strands the device indefinitely.
        dead_primary_process = process
        process_devices = job.get("_processDevices")
        if isinstance(process_devices, Mapping) and dead_primary_key:
            values = process_devices.get(dead_primary_key, [])
            if isinstance(values, (list, tuple, set)):
                dead_primary_serials = [str(value) for value in values]
        if not dead_primary_serials:
            values = job.get("devices", [])
            if isinstance(values, list):
                dead_primary_serials = [str(value) for value in values]
        processes = job.get("_processes")
        if isinstance(processes, dict) and dead_primary_key:
            processes.pop(dead_primary_key, None)
        if isinstance(process_devices, dict) and dead_primary_key:
            process_devices.pop(dead_primary_key, None)
        job["process"] = None
        job["pid"] = None
        job["_primaryProcessKey"] = ""
        with JOBS_LOCK:
            for serial in dead_primary_serials:
                _release_resume_claim_if_unowned(job, serial, force=True)
    if not primary_live:
        live = _live_job_processes(job)
        if live:
            key, process = next(iter(live.items()))
            job["process"] = process
            job["pid"] = getattr(process, "pid", None)
            job["_primaryProcessKey"] = key
        elif dead_primary_process is not None:
            # Keep the exited handle locally long enough to preserve its exit
            # code in the job snapshot; it is no longer considered an owner.
            process = dead_primary_process
    if job.get("status") == "awaiting_install":
        job["phase"] = "awaiting_install"
        job["evidence"] = _cached_job_evidence(job)
        persist_job(job)
        return
    if process is None:
        # A restarted web service has no local Popen handle. Keep the job alive
        # while the durable worker claim is active; once it disappears, the
        # bounded recovery dispatcher below can create exactly one replacement.
        live_claims = _live_resume_claim_serials(job)
        if live_claims:
            evidence = _cached_job_evidence(job)
            job["phase"] = evidence["phase"]
            if job.get("status") not in {"stopping", "paused", "needs_manual_resume"}:
                job["status"] = evidence["phase"]
            job["evidence"] = evidence
            persist_job(job)
            return
        if job.get("status") in ACTIVE_JOB_STATUSES:
            if _maybe_dispatch_persisted_recovery(job):
                return
        return
    if job.get("status") not in ACTIVE_JOB_STATUSES:
        return
    if job.get("status") == "stopping":
        deadline = float(job.get("stopDeadlineEpoch") or 0)
        if deadline and time.time() >= deadline and process.poll() is None:
            _terminate_local_process(process)
    return_code = process.poll()
    if return_code is None:
        # One device can leave a multi-device CLI after its reconnect window
        # expires while sibling workers keep the parent invocation alive.  A
        # later explicit reconnect request detaches only that serial from the
        # primary owner map; dispatch its persisted continuation without
        # waiting for every sibling to finish.
        if _maybe_dispatch_persisted_recovery(job):
            return
        evidence = _cached_job_evidence(job)
        job["phase"] = evidence["phase"]
        if job.get("status") != "stopping":
            job["status"] = evidence["phase"]
        job["evidence"] = evidence
        persist_job(job)
        return
    job["exitCode"] = return_code
    evidence = _cached_job_evidence(job)
    device_states = {
        str(item.get("resumeState") or "")
        for item in evidence.get("reports", {}).values()
        if isinstance(item, Mapping)
    }
    manual_job_stop = bool(job.get("manualStopRequested"))
    resumable_state = None if manual_job_stop else next(
        (
            value
            for value in (
                "needs_manual_resume",
                "paused",
                "waiting_for_device",
                "recovering",
                "resuming",
            )
            if value in device_states
        ),
        None,
    )
    if manual_job_stop:
        job["status"] = "stopped"
        job["phase"] = "stopped"
        job["finishedAt"] = utc_now()
        job["stopDeadlineEpoch"] = None
    elif resumable_state:
        # The CLI process may exit after exhausting its bounded automatic
        # attempts. Keep the job resumable and retain its checkpoint instead
        # of converting the interruption into a terminal failure.
        job["status"] = resumable_state
        job["phase"] = resumable_state
        job["finishedAt"] = None
    else:
        job["finishedAt"] = utc_now()
        job["status"] = "completed" if return_code == 0 and evidence["pressureStarted"] else (
            "stopped" if job.get("status") == "stopping" else "failed"
        )
    job["phase"] = job["status"]
    # ``_job_evidence`` is derived from device reports and can still describe
    # the last live phase (usually ``running``) when the host process has just
    # exited. Keep the persisted evidence in sync with the authoritative
    # terminal job status so a refresh cannot resurrect a finished run in the
    # UI.
    evidence["phase"] = job["status"]
    evidence["finishedAt"] = job["finishedAt"]
    if return_code == 0 and not evidence["pressureStarted"] and not evidence["errors"]:
        evidence["errors"] = ["进程已结束，但未检测到 Fastbot 启动标志或操作记录"]
    job["evidence"] = evidence
    if job["status"] == "stopped":
        _finalize_interrupted_reports(job, terminal_stop=True)
    # Release every claim owned by the finished invocation. ``resumeDevice``
    # only identifies the last manual request and is insufficient for a normal
    # multi-device launch. Remove the exited process from the owner map first;
    # otherwise the liveness guard quite correctly treats the dead handle as a
    # live owner and keeps the claim forever on Windows.
    process_devices = job.get("_processDevices")
    primary_key = str(job.get("_primaryProcessKey") or "")
    released_serials: set[str] = set()
    if isinstance(process_devices, Mapping) and primary_key:
        values = process_devices.get(primary_key, [])
        if isinstance(values, (list, tuple, set)):
            released_serials.update(str(value) for value in values)
    if not released_serials:
        values = job.get("devices", [])
        if isinstance(values, list):
            released_serials.update(str(value) for value in values)
    claims = job.get("_resumeClaims")
    if isinstance(claims, Mapping):
        released_serials.update(str(value) for value in claims)
    processes = job.get("_processes")
    if isinstance(processes, dict) and primary_key:
        processes.pop(primary_key, None)
    if isinstance(process_devices, dict) and primary_key:
        process_devices.pop(primary_key, None)
    if primary_key == str(job.get("_primaryProcessKey") or ""):
        job["process"] = None
    with JOBS_LOCK:
        for serial in released_serials:
            _release_resume_claim_if_unowned(job, serial, force=True)

    # If another CLI survived a service restart, its durable claim keeps the
    # job open even though this local process has just exited.
    if _live_resume_claim_serials(job):
        evidence = _cached_job_evidence(job)
        job["phase"] = evidence["phase"]
        job["status"] = evidence["phase"]
        job["finishedAt"] = None
        job["evidence"] = evidence
        persist_job(job)
        return

    if resumable_state and _maybe_dispatch_persisted_recovery(job):
        # The newly launched process owns the claim and the log stream remains
        # open for its output.
        return
    stream = job.pop("logStream", None)
    if stream is not None:
        stream.close()
    persist_job(job)


def confirm_job_install(identifier: str) -> Dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(identifier)
    if job is None:
        raise ApiError("任务不存在", HTTPStatus.NOT_FOUND)
    if job.get("status") != "awaiting_install":
        if job.get("status") in ACTIVE_JOB_STATUSES:
            return public_job(job, include_log=True)
        raise ApiError("任务已结束，不能继续安装确认", HTTPStatus.CONFLICT)

    installation = job.get("installation")
    pending = (
        list(installation.get("pendingDevices", []))
        if isinstance(installation, Mapping)
        else list(job.get("devices", []))
    )
    package_name = str(job.get("packageName") or "")
    missing: List[str] = []
    installation_devices = (
        installation.get("devices", {})
        if isinstance(installation, Mapping)
        and isinstance(installation.get("devices"), Mapping)
        else {}
    )
    expected_apk_md5 = (
        str(installation.get("expectedApkMd5") or "").strip().lower()
        if isinstance(installation, Mapping)
        else ""
    )
    for serial_value in pending:
        serial = str(serial_value)
        installed = (
            _package_matches_apk(serial, package_name, expected_apk_md5)
            if expected_apk_md5
            else _package_installed(serial, package_name)
        )
        if installed:
            item = installation_devices.get(serial)
            if isinstance(item, dict):
                item["installed"] = True
                item["status"] = "confirmed"
        else:
            missing.append(serial)
    if isinstance(installation, dict):
        installation["pendingDevices"] = missing
    if missing:
        # Keep partial progress visible after a failed confirmation so the UI
        # can tell the operator which devices still need manual installation.
        persist_job(job)
        raise ApiError(
            "尚未检测到 APK 安装完成，请先在这些设备上点击安装：%s"
            % ", ".join(str(item) for item in missing),
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    _finish_install_processes(job)
    try:
        _launch_job_cli(job)
    except Exception:
        job["status"] = "failed"
        job["phase"] = "failed"
        job["finishedAt"] = utc_now()
        persist_job(job)
        raise
    return public_job(job, include_log=True)


def _job_report_for_serial(job: Mapping[str, Any], serial: str) -> Optional[Path]:
    report_path, checkpoint = _job_device_artifacts(job, serial)
    if report_path is not None:
        return report_path
    # A heartbeat may exist before the first report replacement.  Returning the
    # canonical path lets callers place a manual-resume request beside it.
    if checkpoint is not None:
        return _expected_device_report_path(job, serial)
    return None


def _mark_device_resume_requested(
    job: Mapping[str, Any], serial: str, *, requested_by: str = "web"
) -> None:
    """Publish the explicit resume transition before starting a child worker.

    The worker may take a moment to emit its first heartbeat.  Updating both
    durable files here closes that window and removes the old manual-stop
    marker, while recovery evidence remains immutable in its own directory.
    """
    report_path, checkpoint = _job_device_artifacts(job, serial)
    if report_path is None and checkpoint is not None:
        report_path = _expected_device_report_path(job, serial)
    paths: List[Tuple[Path, bool]] = []
    if report_path is not None and report_path.is_file():
        paths.append((report_path, True))
    checkpoint_path = (
        report_path.parent / "checkpoint.json"
        if report_path is not None
        else None
    )
    if checkpoint_path is not None and checkpoint_path.is_file():
        if all(path != checkpoint_path for path, _ in paths):
            paths.append((checkpoint_path, False))
    if not paths:
        return
    now = utc_now()
    for path, is_report in paths:
        value = read_json_file(path, None)
        if not isinstance(value, dict) or terminal_resume_state(value):
            continue
        value.update(
            {
                "status": "resuming",
                "resume_state": "resuming",
                "finished_at": None,
                "manual_stop": False,
                "waiting_for_device": False,
                "last_error": None,
                "resume_requested_at": now,
                "resume_requested_by": str(requested_by),
                "scheduler_updated_at": now,
            }
        )
        if is_report:
            current = value.get("current_cycle")
            if isinstance(current, dict):
                current["status"] = "resuming"
                current["last_error"] = None
        else:
            value["phase_name"] = "resuming"
        try:
            write_json_file(path, value)
        except OSError as exc:
            raise ApiError(
                "unable to persist device resume state: %s" % exc,
                HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc


def _clear_device_manual_stop_markers(job: Dict[str, Any], serial: str) -> None:
    """Clear only the selected device's manual-stop gate before resuming it."""
    serial_value = str(serial)
    with JOBS_LOCK:
        manual_devices = [
            str(value)
            for value in (job.get("manualStopDevices", []) or [])
            if str(value) != serial_value
        ]
        job["manualStopDevices"] = list(dict.fromkeys(manual_devices))
        configured_devices = {
            str(value) for value in (job.get("devices", []) or [])
        }
        job["manualStopRequested"] = bool(
            manual_devices and configured_devices.issubset(set(manual_devices))
        )
        # A device-level explicit resume is an operator opt-in for the current
        # job.  Clear stale whole-job stop timestamps, while preserving gates
        # for other devices that remain in ``manualStopDevices``.
        job["stopRequestedAt"] = None
        job["stopDeadlineEpoch"] = None
        if not manual_devices:
            job["partialStopRequestedAt"] = None


def _report_allows_parallel_resume(job: Mapping[str, Any], serial: str) -> bool:
    path, checkpoint = _job_device_artifacts(job, serial)
    value = read_json_file(path, {}) if path is not None and path.is_file() else {}
    if not isinstance(value, Mapping):
        value = {}
    state = _effective_resume_state(value, checkpoint, default="")
    return _can_resume_device_state(
        state,
        resume_mode=_job_resume_mode(job, value),
    )


def _resume_request_lock(identifier: str, serial: str) -> threading.RLock:
    """Return the process-local lock for one job/device resume request."""
    key = (str(identifier), str(serial))
    with _RESUME_REQUEST_LOCKS_GUARD:
        lock = _RESUME_REQUEST_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RESUME_REQUEST_LOCKS[key] = lock
        return lock


def resume_job_device(identifier: str, serial: str) -> Dict[str, Any]:
    """Serialize one device resume request before touching durable claims."""
    serial_value = str(serial).strip()
    with _resume_request_lock(identifier, serial_value):
        return _resume_job_device_unlocked(identifier, serial_value)


def _resume_job_device_unlocked(identifier: str, serial: str) -> Dict[str, Any]:
    """Request one device to continue a persisted job under the same job id."""
    serial = str(serial).strip()
    if not serial:
        raise ApiError("device serial is required")
    with JOBS_LOCK:
        job = JOBS.get(identifier)
    if job is None:
        # A service restart can race the initial disk scan. Load the snapshot
        # directly so the endpoint remains useful without creating a new job.
        candidate = RUN_ROOT / identifier / "job.json"
        value = read_json_file(candidate, None)
        if isinstance(value, Mapping):
            job = dict(value)
            job.update(
                {
                    "jobDir": str(candidate.parent),
                    "reportRoot": str(candidate.parent / "reports"),
                    "logPath": str(candidate.parent / "console.log"),
                    "configPath": str(candidate.parent / "config.json"),
                    "process": None,
                }
            )
            execution_run_id = _job_execution_run_id(job)
            if execution_run_id:
                job["runId"] = execution_run_id
            with JOBS_LOCK:
                JOBS[identifier] = job
        else:
            raise ApiError("任务不存在", HTTPStatus.NOT_FOUND)
    if serial not in [str(value) for value in job.get("devices", [])]:
        raise ApiError("device is not part of this job", HTTPStatus.NOT_FOUND)

    refresh_job(job)
    # A resume claim is created before the child process is spawned.  During
    # that short window the old report can still say ``paused``; never treat
    # it as permission to detach the newly claimed worker and launch a second
    # one.  Clean up only a marker whose claim and local owner are both gone.
    with JOBS_LOCK:
        inflight_devices = job.get("resumeInFlightDevices")
        inflight_marked = isinstance(inflight_devices, list) and serial in {
            str(value) for value in inflight_devices
        }
        inflight_marked = inflight_marked or (
            (str(identifier), serial) in RESUME_IN_FLIGHT
        )
    if inflight_marked:
        durable_claim = _read_resume_claim(job, serial)
        claim_parent = _resume_claim_parent(job, serial)
        claim_report_path, claim_checkpoint = _job_device_artifacts(job, serial)
        claim_report = (
            read_json_file(claim_report_path, {})
            if claim_report_path is not None and claim_report_path.is_file()
            else {}
        )
        claim_state = _effective_resume_state(
            claim_report if isinstance(claim_report, Mapping) else {},
            claim_checkpoint,
            default="",
        )
        live_owners = _live_process_owners(job, serial)
        claim_live = bool(
            durable_claim
            and claim_parent is not None
            and resume_claim_active(
                claim_parent,
                job_id=str(job.get("id") or identifier),
                device_serial=serial,
            )
        )
        primary_key = str(job.get("_primaryProcessKey") or "primary")
        shared_primary_only = bool(
            len(job.get("devices", []) or []) > 1
            and live_owners == [primary_key]
        )
        detached_shared_claim = bool(
            durable_claim
            and claim_parent is not None
            and claim_state in MANUAL_RECOVERY_STATES
            and (not live_owners or shared_primary_only)
            and str(job.get("status") or "") != "resuming"
        )
        if detached_shared_claim:
            # A multi-device CLI uses one host PID for every per-device claim.
            # When one device exhausts recovery, that PID stays alive for its
            # siblings even though it no longer controls this serial. The
            # persisted manual state is authoritative in that case.
            release_resume_claim(
                claim_parent, str(durable_claim.get("claim_id") or "")
            )
            with JOBS_LOCK:
                _forget_resume_claim(job, serial)
        elif claim_live or live_owners:
            raise ApiError(
                "设备正在恢复中，请勿重复操作",
                HTTPStatus.CONFLICT,
            )
        else:
            with JOBS_LOCK:
                _forget_resume_claim(job, serial)
    process = job.get("process")
    owners = _live_process_owners(job, serial)
    primary_key = str(job.get("_primaryProcessKey") or "primary")
    # A primary invocation may still be running sibling devices.  Parallel
    # manual resume is allowed only when that primary owns no active worker for
    # this serial; any secondary/resume owner is an unconditional conflict.
    if owners and any(owner != primary_key for owner in owners):
        raise ApiError(
            "该任务已有执行进程，请等待当前恢复完成",
            HTTPStatus.CONFLICT,
        )
    report_path, checkpoint = _job_device_artifacts(job, serial)
    # Keep the merged durable view available for checkpoint-only runs.  A
    # heartbeat can be committed before ``report.json`` exists, so the
    # request must be written beside that checkpoint rather than attempting to
    # read a missing report or relying on an undefined local value.
    snapshot = _device_resume_snapshot(serial, job)
    # A checkpoint can predate report.json.  Use the canonical directory for
    # the resume request while keeping the legacy report path handling below.
    if report_path is None and checkpoint is not None:
        report_path = _expected_device_report_path(job, serial)
    if report_path is None:
        raise ApiError("该设备没有可恢复的 report.json", HTTPStatus.CONFLICT)
    report = read_json_file(report_path, {}) if report_path.is_file() else {}
    if not isinstance(report, Mapping):
        raise ApiError("设备报告无法读取", HTTPStatus.UNPROCESSABLE_ENTITY)
    raw_state = _effective_resume_state(report, checkpoint, default="")
    if raw_state in {
        "completed",
        "passed",
        "completed_with_crashes",
        "completed_with_errors",
    }:
        return public_job(job, include_log=True)
    if not _can_resume_device_state(
        raw_state,
        resume_mode=_job_resume_mode(job, report),
    ):
        raise ApiError(
            "该设备当前不处于可恢复状态，请先等待中断证据落盘",
            HTTPStatus.CONFLICT,
        )

    # A primary CLI can continue sibling devices after this serial entered a
    # manual gate. Remove only the stopped serial from its ownership map so a
    # secondary invocation can safely claim it under the same job id.
    if owners and owners == [primary_key] and raw_state in MANUAL_RECOVERY_STATES:
        process_devices = job.get("_processDevices")
        if not isinstance(process_devices, dict):
            process_devices = {}
            job["_processDevices"] = process_devices
        values = process_devices.get(primary_key)
        if not isinstance(values, list):
            values = [str(value) for value in job.get("devices", [])]
        process_devices[primary_key] = [
            str(value) for value in values if str(value) != serial
        ]
        old_parent = _resume_claim_parent(job, serial)
        old_claim = _read_resume_claim(job, serial)
        if old_parent is not None and old_claim is not None:
            release_resume_claim(
                old_parent, str(old_claim.get("claim_id") or "")
            )
        _forget_resume_claim(job, serial)
        persist_job(job)
        owners = _live_process_owners(job, serial)
    if owners:
        raise ApiError(
            "device already has a live worker",
            HTTPStatus.CONFLICT,
        )

    cleanup_status = _best_effort_remote_worker_cleanup_status(serial)
    if cleanup_status == _REMOTE_CLEANUP_FAILED:
        raise ApiError(
            "previous device control could not be verified released",
            HTTPStatus.CONFLICT,
        )
    if cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
        waiting_reason = (
            "device transport is unavailable; resume will wait for reconnect"
        )
        _clear_device_manual_stop_markers(job, serial)
        _persist_job_device_resume_state(
            job,
            serial,
            "waiting_for_device",
            waiting_reason,
            renew_wait_window=True,
        )
        # A manual click is an explicit request, but it must not create a host
        # worker while the transport is absent.  Keep the request beside the
        # checkpoint so the same job/device can be dispatched after reconnect.
        request_dir = (
            Path(str(snapshot.get("checkpoint_path"))).parent
            if snapshot.get("checkpoint_path")
            else report_path.parent
        )
        request_manual_resume(request_dir, requested_by="web")
        with JOBS_LOCK:
            waiting_devices = job.get("resumeWaitingDevices")
            if not isinstance(waiting_devices, list):
                waiting_devices = []
                job["resumeWaitingDevices"] = waiting_devices
            if serial not in {str(value) for value in waiting_devices}:
                waiting_devices.append(serial)
            job["resumeDevice"] = serial
            job["resumeRequestedAt"] = utc_now()
            job["resumeManual"] = True
            job["status"] = "waiting_for_device"
            job["phase"] = "waiting_for_device"
            job["finishedAt"] = None
            job["lastError"] = waiting_reason
        persist_job(job)
        return public_job(job, include_log=True)

    key = (str(identifier), serial)
    claim_parent = _resume_claim_parent(job, serial)
    if claim_parent is None:
        raise ApiError("该设备没有可恢复的持久化目录", HTTPStatus.CONFLICT)
    claim = acquire_resume_claim(
        claim_parent,
        job_id=str(identifier),
        device_serial=serial,
        requested_by="web",
    )
    if claim is None:
        raise ApiError(
            "该设备已有恢复执行进程，请等待其释放",
            HTTPStatus.CONFLICT,
        )
    with JOBS_LOCK:
        inflight_devices = job.get("resumeInFlightDevices")
        if not isinstance(inflight_devices, list):
            inflight_devices = []
            job["resumeInFlightDevices"] = inflight_devices
        if key in RESUME_IN_FLIGHT or serial in [str(item) for item in inflight_devices]:
            release_resume_claim(claim_parent, str(claim.get("claim_id") or ""))
            raise ApiError("device resume is already queued", HTTPStatus.CONFLICT)
        RESUME_IN_FLIGHT.add(key)
        inflight_devices.append(serial)
        claims = job.get("_resumeClaims")
        if not isinstance(claims, dict):
            claims = {}
            job["_resumeClaims"] = claims
        claims[serial] = str(claim.get("claim_id") or "")
        preacquired = job.get("_resumeClaimPreacquired")
        if not isinstance(preacquired, list):
            preacquired = []
            job["_resumeClaimPreacquired"] = preacquired
        if serial not in {str(item) for item in preacquired}:
            preacquired.append(serial)
        job["resumeInFlight"] = True
        job["resumeDevice"] = serial
        job["resumeRequestedAt"] = utc_now()
        job["resumeManual"] = True
        job["status"] = "resuming"
        job["phase"] = "resuming"
        job["finishedAt"] = None
        _clear_device_manual_stop_markers(job, serial)

    try:
        # Make the durable transition visible before the subprocess is
        # created.  This also prevents a late heartbeat from preserving the
        # previous manual stop marker during the resume handoff.
        _mark_device_resume_requested(job, serial, requested_by="web")
        request_dir = (
            Path(str(snapshot.get("checkpoint_path"))).parent
            if snapshot.get("checkpoint_path")
            else report_path.parent
        )
        request_manual_resume(request_dir, requested_by="web")
        log_stream = job.get("logStream")
        if log_stream is None:
            log_stream = open_job_log(Path(str(job["logPath"])))
            job["logStream"] = log_stream
        _job_log(job, "手动恢复设备 %s，继续同一任务剩余压力时间" % serial)
        process = _launch_job_cli(job, devices=[serial])
        update_resume_claim(
            claim_parent,
            str(claim.get("claim_id") or ""),
            worker_pid=getattr(process, "pid", None) if process is not None else None,
            launched_at=utc_now(),
        )
        persist_job(job)
        return public_job(job, include_log=True)
    except Exception:
        release_resume_claim(claim_parent, str(claim.get("claim_id") or ""))
        with JOBS_LOCK:
            _release_resume_claim_if_unowned(job, serial)
            job["status"] = "needs_manual_resume"
            job["phase"] = "needs_manual_resume"
        persist_job(job)
        raise


def _finalize_interrupted_reports(
    job: Mapping[str, Any], *, terminal_stop: bool = False
) -> None:
    """Close reports left open when the host worker is forcibly interrupted.

    A device-level stop remains resumable, while an explicit whole-job stop is
    terminal. Keeping those operations distinct prevents a stopped job from
    reappearing in the active-task list after the worker or service exits.
    """
    now = utc_now()
    for path in _cached_job_report_paths(job):
        report = _cached_json_document(path, None)
        if not isinstance(report, dict):
            continue
        current_state = str(
            report.get("resume_state") or report.get("status") or ""
        )
        if not terminal_stop and current_state in AUTOMATIC_RECOVERY_STATES | MANUAL_RECOVERY_STATES:
            # Keep the durable checkpoint and evidence available for the
            # explicit resume endpoint; a web stop must not erase it.
            continue
        if not terminal_stop and current_state not in {"running", "resuming"}:
            continue
        next_state = "stopped" if terminal_stop else "paused"
        stop_reason = "用户在网页控制台停止了任务"
        report["status"] = next_state
        report["resume_state"] = next_state
        report["finished_at"] = now if terminal_stop else None
        report["manual_stop"] = True
        report["recovery_category"] = "manual_stop"
        report["recovery_reason"] = stop_reason
        report["last_error"] = stop_reason
        if terminal_stop:
            report["remaining_seconds"] = 0.0
        current = report.get("current_cycle")
        if isinstance(current, dict):
            current["status"] = next_state
            current["last_error"] = stop_reason
        errors = report.setdefault("errors", [])
        if isinstance(errors, list) and stop_reason not in errors:
            errors.append(stop_reason)
        try:
            ReportGenerator.write_all(report, path.parent)
        except Exception:
            # Keep the JSON report usable even if HTML regeneration is unavailable.
            write_json_file(path, report)
        checkpoint_path = path.parent / "checkpoint.json"
        checkpoint = read_json_file(checkpoint_path, None)
        if isinstance(checkpoint, dict):
            checkpoint["resume_state"] = next_state
            checkpoint["phase_name"] = next_state
            checkpoint["manual_stop"] = True
            checkpoint["recovery_category"] = "manual_stop"
            checkpoint["recovery_reason"] = stop_reason
            checkpoint["last_error"] = stop_reason
            checkpoint["scheduler_updated_at"] = now
            if terminal_stop:
                checkpoint["remaining_seconds"] = 0.0
                checkpoint["finished_at"] = now
            try:
                write_json_file(checkpoint_path, checkpoint)
            except OSError:
                pass


def _signal_local_stop(process: subprocess.Popen) -> bool:
    """Ask the CLI scheduler to cancel its device workers gracefully."""
    if process.poll() is not None:
        return True
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _terminate_local_process(process: subprocess.Popen) -> None:
    """Bound local cleanup if a device-side stop cannot release the worker."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except (OSError, ValueError):
            pass


_PERSISTED_SIGNATURES: Dict[str, str] = {}
_PERSISTED_LOCK = threading.Lock()


def persist_job(job: Mapping[str, Any]) -> None:
    job_dir = Path(str(job["jobDir"]))
    payload = public_job(job, include_log=False)
    identifier = str(job.get("id") or "")
    try:
        signature = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        signature = ""
    with _PERSISTED_LOCK:
        if signature and _PERSISTED_SIGNATURES.get(identifier) == signature:
            # Unchanged snapshots were being rewritten for every job on every
            # poll, which is expensive on Windows with a real-time AV scanner.
            return
    write_json_file(job_dir / "job.json", payload)
    with _PERSISTED_LOCK:
        if signature:
            _PERSISTED_SIGNATURES[identifier] = signature


def log_tail(path: Path, max_bytes: int = 24000) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def public_job(job: Mapping[str, Any], include_log: bool = False) -> Dict[str, Any]:
    value = {
        key: copy.deepcopy(job.get(key))
        for key in (
            "id", "runId", "status", "startedAt", "finishedAt", "packageName", "apkName",
            "devices", "duration", "cycles", "workers", "pid", "exitCode", "phase", "evidence",
            "installation", "manualStopRequested", "manualStopDevices",
            "manualEndDevices", "reportRoot", "jobDir", "configPath",
        )
    }
    # Older job snapshots may have been persisted while the worker was still
    # reported as ``running``. The terminal job status is authoritative once
    # the process is gone, so never expose a contradictory evidence phase.
    if value.get("status") not in ACTIVE_JOB_STATUSES:
        evidence = value.get("evidence")
        if isinstance(evidence, dict):
            evidence["phase"] = value.get("status")
            if value.get("finishedAt"):
                evidence.setdefault("finishedAt", value["finishedAt"])
    value["elapsed"] = elapsed_text(job)
    report_summaries = [
        _cached_report_summary(path) for path in _cached_job_report_paths(job)
    ]
    value["reports"] = [item for item in report_summaries if item]
    if include_log:
        value["log"] = log_tail(Path(str(job.get("logPath") or "")))
        if value.get("status") in ACTIVE_JOB_STATUSES:
            device_progress: Dict[str, Dict[str, Any]] = {}
            for serial_value in job.get("devices", []) or []:
                serial = str(serial_value)
                snapshot = _device_resume_snapshot(serial, job)
                has_artifact = bool(snapshot.get("report") or snapshot.get("checkpoint"))
                device_progress[serial] = {
                    "status": _public_device_resume_status(snapshot["status"])
                    if has_artifact else str(job.get("status") or "preparing"),
                    "progress": int(snapshot["progress"]) if has_artifact else 0,
                    "cycle": int(snapshot["cycle"]) if has_artifact else 0,
                    "elapsedSeconds": float(snapshot["elapsed_seconds"]) if has_artifact else 0.0,
                    "remainingSeconds": float(snapshot["remaining_seconds"]) if has_artifact else None,
                    "operations": int(snapshot["operation_count"]) if has_artifact else 0,
                    "resumeAttempts": int(snapshot["resume_attempts"]) if has_artifact else 0,
                    "lastError": snapshot["last_error"] if has_artifact else None,
                }
            value["deviceProgress"] = device_progress
    return value


def list_jobs(include_log: bool = False) -> List[Dict[str, Any]]:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        refresh_job(job)
    return sorted(
        (public_job(job, include_log=include_log) for job in jobs),
        key=lambda item: str(item.get("startedAt") or ""),
        reverse=True,
    )


def stop_job(identifier: str) -> List[Dict[str, Any]]:
    """Stop one complete web job and make the decision terminal and durable."""
    with JOBS_LOCK:
        job = JOBS.get(identifier)
    if job is None:
        raise ApiError("任务不存在", HTTPStatus.NOT_FOUND)

    refresh_job(job)
    if str(job.get("status") or "") in {"completed", "failed", "stopped"}:
        return [
            {"serial": str(serial), "stopped": True}
            for serial in job.get("devices", [])
        ]

    serials = list(
        dict.fromkeys(
            str(value).strip()
            for value in job.get("devices", [])
            if str(value).strip()
        )
    )
    connected = DeviceManager.get_device_states()
    results: List[Dict[str, Any]] = []
    for serial in serials:
        if connected.get(serial, {}).get("adb_state") != "device":
            # The durable job gate is still authoritative for an offline
            # device. It will not be relaunched when the transport returns.
            results.append(
                {
                    "serial": serial,
                    "stopped": True,
                    "message": "设备当前未连接，任务已标记为停止",
                }
            )
            continue
        try:
            stopped = MutexController(serial).request_stop_remote_fastbot()
            results.append(
                {
                    "serial": serial,
                    "stopped": bool(stopped),
                    **({} if stopped else {"error": "设备端压测进程未能确认停止"}),
                }
            )
        except Exception as exc:
            results.append({"serial": serial, "stopped": False, "error": str(exc)})

    with JOBS_LOCK:
        job["manualStopRequested"] = True
        job["manualStopDevices"] = serials
        job["stopRequestedAt"] = job.get("stopRequestedAt") or utc_now()
        installation = job.get("installation")
        if isinstance(installation, dict):
            _finish_install_processes(job)
            installation["status"] = "cancelled"
            installation["pendingDevices"] = []
        processes = _live_job_processes(job)
        process = job.get("process")
        if process is not None and not processes and _is_process_handle(process):
            try:
                if process.poll() is None:
                    processes = {"primary": process}
            except (OSError, ValueError, AttributeError):
                pass
        if processes:
            job["status"] = "stopping"
            job["phase"] = "stopping"
            job["finishedAt"] = None
            job["stopDeadlineEpoch"] = time.time() + LOCAL_STOP_GRACE_SECONDS
            for local_process in processes.values():
                if not _signal_local_stop(local_process):
                    _terminate_local_process(local_process)
        else:
            job["status"] = "stopped"
            job["phase"] = "stopped"
            job["finishedAt"] = utc_now()
            job["stopDeadlineEpoch"] = None
            _finalize_interrupted_reports(job, terminal_stop=True)
            stream = job.pop("logStream", None)
            if stream is not None:
                stream.close()
        persist_job(job)
    return results


def stop_devices(serials: Iterable[str]) -> List[Dict[str, Any]]:
    serial_values = list(
        dict.fromkeys(str(value).strip() for value in serials if str(value).strip())
    )
    results = []
    manual_stop_reason = "pressure stopped by web console"
    connected = DeviceManager.get_device_states()
    for serial in serial_values:
        if connected.get(serial, {}).get("adb_state") != "device":
            results.append({"serial": serial, "stopped": False, "error": "设备未连接或未授权"})
            continue
        try:
            stopped = MutexController(serial).request_stop_remote_fastbot()
            results.append({"serial": serial, "stopped": bool(stopped)})
        except Exception as exc:
            results.append({"serial": serial, "stopped": False, "error": str(exc)})
    with JOBS_LOCK:
        for job in JOBS.values():
            job_devices = {
                str(value) for value in job.get("devices", [])
                if str(value).strip()
            }
            targeted = job_devices.intersection(serial_values)
            if job.get("status") in ACTIVE_JOB_STATUSES and targeted:
                manual_devices = job.get("manualStopDevices")
                if not isinstance(manual_devices, list):
                    manual_devices = []
                manual_devices = list(
                    dict.fromkeys(
                        [str(value) for value in manual_devices]
                        + sorted(targeted)
                    )
                )
                job["manualStopDevices"] = manual_devices
                # Persist the per-device manual gate immediately.  This is
                # essential when the target is offline and has no worker left
                # to publish its final paused report; without it a reconnect
                # could make the scheduler treat the device as automatic.
                for value in sorted(targeted):
                    _persist_job_device_resume_state(
                        job,
                        value,
                        "paused",
                        manual_stop_reason,
                        manual_stop=True,
                    )
                stop_entire_job = bool(job_devices) and job_devices.issubset(
                    set(manual_devices)
                )
                if job.get("status") == "awaiting_install":
                    installation = job.get("installation")
                    if isinstance(installation, dict):
                        devices_state = installation.get("devices", {})
                        if isinstance(devices_state, dict):
                            for value in targeted:
                                item = devices_state.get(value)
                                if isinstance(item, dict):
                                    item["status"] = "cancelled"
                                    item["installed"] = bool(item.get("installed"))
                        pending = [
                            str(value)
                            for value in installation.get("pendingDevices", [])
                            if str(value) not in targeted
                        ]
                        installation["pendingDevices"] = pending
                        if stop_entire_job or not pending:
                            _finish_install_processes(job)
                            installation["status"] = "cancelled"
                            job["status"] = "stopped"
                            job["phase"] = "stopped"
                            job["finishedAt"] = utc_now()
                            stream = job.pop("logStream", None)
                            if stream is not None:
                                stream.close()
                        else:
                            # Keep the install gate alive for sibling devices.
                            installation["status"] = "waiting"
                            job["status"] = "awaiting_install"
                            job["phase"] = "awaiting_install"
                    persist_job(job)
                    continue
                if stop_entire_job:
                    processes = _live_job_processes(job)
                    process = job.get("process")
                    if process is not None and not processes:
                        processes = {"primary": process}
                    # This endpoint pauses devices and remains resumable. Only
                    # the job-level stop endpoint sets the terminal stop gate.
                    job["manualStopRequested"] = False
                    job["stopRequestedAt"] = job.get("stopRequestedAt") or utc_now()
                    if processes:
                        job["status"] = "stopping"
                        job["phase"] = "stopping"
                        job["stopDeadlineEpoch"] = job.get("stopDeadlineEpoch") or (
                            time.time() + LOCAL_STOP_GRACE_SECONDS
                        )
                        for local_process in processes.values():
                            _signal_local_stop(local_process)
                    else:
                        # Device-level stop remains resumable even when it
                        # happens to target every device in this job. The
                        # whole-job endpoint uses ``stop_job`` and is terminal.
                        job["status"] = "paused"
                        job["phase"] = "paused"
                        job["finishedAt"] = None
                        job["stopDeadlineEpoch"] = None
                else:
                    # A per-device stop is represented in the device report and
                    # must not cancel sibling workers in the same CLI process.
                    job["manualStopRequested"] = False
                    job["partialStopRequestedAt"] = job.get(
                        "partialStopRequestedAt"
                    ) or utc_now()
                persist_job(job)
    return results


def sync_fastbot(serials: Iterable[str]) -> Dict[str, Any]:
    values = list(dict.fromkeys(str(value).strip() for value in serials if str(value).strip()))
    if not values:
        raise ApiError("请至少选择一台设备")
    command = [sys.executable, str(CLI_PATH), "sync-fastbot", "--devices", ",".join(values)]
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApiError("同步 Fastbot 失败：%s" % exc, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
    output = (result.stdout or result.stderr or "").strip()
    parsed: Any = None
    first = output.find("[")
    if first >= 0:
        try:
            parsed = json.loads(output[first:])
        except json.JSONDecodeError:
            parsed = None
    return {"ok": result.returncode == 0, "results": parsed, "output": output}


def report_roots() -> List[Path]:
    roots = [RUN_ROOT]
    try:
        roots.append(resolve_project_path("./reports"))
    except Exception:
        pass
    unique: List[Path] = []
    for root in roots:
        resolved = root.resolve(strict=False)
        if resolved not in unique and resolved.exists():
            unique.append(resolved)
    return unique


def report_paths(limit: int = 500) -> List[Path]:
    paths: List[Path] = []
    for root in report_roots():
        try:
            paths.extend(root.rglob("report.json"))
        except OSError:
            continue
    paths = list(dict.fromkeys(path.resolve() for path in paths if path.is_file()))
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:limit]


def report_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:20]


def duration_seconds(report: Mapping[str, Any]) -> int:
    statistics = report.get("statistics", {})
    if isinstance(statistics, Mapping) and "pressure_elapsed_seconds" in statistics:
        try:
            return max(0, int(float(statistics.get("pressure_elapsed_seconds") or 0)))
        except (TypeError, ValueError):
            return 0
    try:
        start = datetime.fromisoformat(str(report.get("started_at")))
        finish = datetime.fromisoformat(str(report.get("finished_at")))
        return max(0, int((finish - start).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _report_progress_totals(
    report: Mapping[str, Any], checkpoint: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Merge completed and partial progress without counting either twice.

    New reports keep aggregate statistics limited to immutable completed
    cycles, while some older writers included ``current_cycle`` in those
    aggregates.  ``max(aggregate, completed + partial)`` is valid for both
    layouts and keeps repeated resumes from inflating time or operations.
    """
    cycles = unique_cycle_rows(report)
    completed_numbers = completed_cycle_numbers(report)
    completed_values = [
        item
        for item in cycles
        if isinstance(item, Mapping) and is_completed_cycle(item)
    ]
    completed_elapsed = sum(
        _nonnegative_float(item.get("pressure_elapsed_seconds"), 0.0)
        for item in completed_values
    )
    completed_operations = sum(
        _nonnegative_int(item.get("operation_count"), 0)
        for item in completed_values
    )
    terminal_state = bool(
        terminal_resume_state(report) or terminal_resume_state(checkpoint)
    )

    current: Mapping[str, Any] = (
        report.get("current_cycle")
        if not terminal_state and isinstance(report.get("current_cycle"), Mapping)
        else {}
    )
    checkpoint_cycle = _nonnegative_int((checkpoint or {}).get("cycle"), 0)
    if not terminal_state and checkpoint_cycle > 0 and checkpoint_cycle not in completed_numbers:
        current = checkpoint or current
    current_cycle = _nonnegative_int(
        current.get("cycle") or current.get("cycle_number"), 0
    )
    current_is_partial = bool(
        current and current_cycle > 0 and current_cycle not in completed_numbers
    )
    partial_elapsed = _nonnegative_float(
        current.get("elapsed_seconds")
        if "elapsed_seconds" in current
        else current.get("pressure_elapsed_seconds"),
        0.0,
    ) if current_is_partial else 0.0
    partial_operations = (
        _nonnegative_int(current.get("operation_count"), 0)
        if current_is_partial else 0
    )

    statistics = report.get("statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    aggregate_elapsed = _nonnegative_float(
        statistics.get("pressure_elapsed_seconds"), 0.0
    )
    aggregate_operations = _nonnegative_int(
        statistics.get("total_operations"), 0
    )
    return {
        "elapsed_seconds": max(
            aggregate_elapsed, completed_elapsed + partial_elapsed
        ),
        "operations": max(
            aggregate_operations, completed_operations + partial_operations
        ),
        "completed_elapsed_seconds": completed_elapsed,
        "completed_operations": completed_operations,
        "partial_elapsed_seconds": partial_elapsed,
        "partial_operations": partial_operations,
        "completed_numbers": completed_numbers,
        "current_cycle": dict(current) if current_is_partial else None,
    }


def _run_batch_directory_for_report(path: Path) -> Optional[Path]:
    """Infer a run directory from a report even when ``job.json`` is gone.

    Older console sessions could leave ``runs/<test-id>/reports`` behind while
    the batch metadata was missing.  Those reports are still safe to manage as
    long as the path is directly below the configured run root and the
    ``reports`` segment is part of the resolved path.
    """
    try:
        resolved = path.resolve(strict=False)
        run_root = RUN_ROOT.resolve(strict=False)
        relative = resolved.relative_to(run_root)
    except (OSError, ValueError, TypeError):
        return None
    parts = relative.parts
    if len(parts) < 3 or str(parts[1]).lower() != "reports":
        return None
    candidate = (run_root / parts[0]).resolve(strict=False)
    if candidate.parent != run_root:
        return None
    try:
        resolved.relative_to((candidate / "reports").resolve(strict=False))
    except (OSError, ValueError):
        return None
    return candidate


def report_job_directory(path: Path) -> Optional[Path]:
    resolved = path.resolve(strict=False)
    for parent in resolved.parents:
        if not (parent / "job.json").is_file():
            continue
        try:
            resolved.relative_to((parent / "reports").resolve(strict=False))
            return parent
        except ValueError:
            continue
    # Keep legacy report trees discoverable after their batch metadata was
    # removed.  The helper only accepts a direct child of RUN_ROOT, so this
    # fallback cannot turn an unrelated report root into a deletable job.
    return _run_batch_directory_for_report(resolved)


def report_test_id(path: Path, report: Mapping[str, Any]) -> str:
    job_dir = report_job_directory(path)
    if job_dir is not None:
        job = read_json_file(job_dir / "job.json", {})
        if isinstance(job, Mapping) and str(job.get("id") or "").strip():
            return str(job["id"])
        return job_dir.name
    return str(report.get("run_id") or path.parent.name)


def report_pressure_target_seconds(
    report: Mapping[str, Any], job: Optional[Mapping[str, Any]] = None
) -> int:
    config = report.get("config", {})
    test = config.get("test", {}) if isinstance(config, Mapping) else {}
    if not isinstance(test, Mapping):
        test = {}
    duration_minutes = test.get("fastbot_duration_minutes")
    cycles = test.get("max_cycles")
    if duration_minutes is None and isinstance(job, Mapping):
        duration_minutes = job.get("duration")
    if cycles is None and isinstance(job, Mapping):
        cycles = job.get("cycles")
    try:
        return max(0, int(float(duration_minutes or 0) * 60 * int(cycles or 0)))
    except (TypeError, ValueError):
        return 0


def report_pressure_percent(elapsed: int, target: int, raw_status: str) -> float:
    if target <= 0:
        return 0.0
    percent = max(0.0, min(100.0, elapsed * 100.0 / target))
    if raw_status != "completed" and percent >= 100.0:
        percent = 99.9
    return round(percent, 1)


# Reports created before process-death evidence was separated from crash
# evidence put ``process_exit`` rows in ``report["crashes"]``.  Keep the
# compatibility rule in the read path so those reports are corrected without
# rewriting historical files or interrupting a running job.
_CONFIRMED_REPORT_CRASH_TYPES = frozenset(
    {"java", "anr", "native", "app_crash_page"}
)


def _is_confirmed_report_crash(value: Any) -> bool:
    """Return whether a persisted row represents a target-app crash.

    ``process_exit`` is only promoted when ActivityManager explicitly reports
    an app crash.  Older generic crash rows did not persist ``crash_type``;
    retain those rows when they carry a crash signature or stack evidence so
    existing report and replay fixtures remain usable.
    """
    if not isinstance(value, Mapping):
        return False
    crash_type = str(value.get("crash_type") or "").strip().lower()
    if crash_type in _CONFIRMED_REPORT_CRASH_TYPES:
        return True
    if bool(value.get("confirmed_app_crash")):
        return True
    if crash_type == "process_exit":
        return str(value.get("process_event") or "").strip().lower() == "am_crash"
    if crash_type:
        return False
    # A few pre-taxonomy reports only stored signature/stacktrace fields.
    return bool(
        str(value.get("signature") or "").strip()
        or str(value.get("stacktrace") or value.get("raw_log") or "").strip()
        or isinstance(value.get("app_crash_page"), Mapping)
    )


def _confirmed_report_crashes(report: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    crashes = report.get("crashes", [])
    if not isinstance(crashes, list):
        return []
    return [
        item for item in crashes
        if isinstance(item, Mapping) and _is_confirmed_report_crash(item)
    ]


def _confirmed_report_crash_cycles(report: Mapping[str, Any]) -> set[int]:
    cycles: set[int] = set()
    for crash in _confirmed_report_crashes(report):
        try:
            cycle = crash.get("cycle")
            if cycle is not None and not isinstance(cycle, bool):
                cycles.add(int(cycle))
        except (TypeError, ValueError, OverflowError):
            continue
    return cycles


def _public_cycle_status(
    report: Mapping[str, Any], cycle: Mapping[str, Any], confirmed_cycles: Optional[set[int]] = None
) -> str:
    status = str(cycle.get("status") or "unknown")
    if status != "crash":
        return status
    if confirmed_cycles is None:
        confirmed_cycles = _confirmed_report_crash_cycles(report)
    try:
        cycle_number = int(cycle.get("cycle"))
    except (TypeError, ValueError, OverflowError):
        cycle_number = None
    # Legacy cycle rows were marked ``crash`` solely because a process_exit
    # record was appended.  Present those cycles as failed runtime handling;
    # a real crash keeps the original crash status.
    return "crash" if cycle_number in confirmed_cycles else "failed"


def _is_non_actionable_stall_record(value: Mapping[str, Any]) -> bool:
    record_type = str(value.get("type") or "")
    if record_type in {"fallback_recovery", "stall_observation"}:
        return True
    if record_type != "stall":
        return False
    recovery = value.get("recovery", {})
    if not isinstance(recovery, Mapping):
        recovery = {}
    attempted = bool(recovery.get("attempted"))
    success = bool(recovery.get("success"))
    interrupted = bool(recovery.get("interrupted"))
    return not attempted or success or interrupted


def _actionable_issue_values(report: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    issues = report.get("issues", [])
    if not isinstance(issues, list):
        return []
    return [
        item for item in issues
        if isinstance(item, Mapping) and not _is_non_actionable_stall_record(item)
    ]


def _stall_record_values(report: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    records: List[Mapping[str, Any]] = []
    raw_records = report.get("stall_records", [])
    if isinstance(raw_records, list):
        records.extend(item for item in raw_records if isinstance(item, Mapping))
    raw_issues = report.get("issues", [])
    if isinstance(raw_issues, list):
        records.extend(
            item for item in raw_issues
            if isinstance(item, Mapping) and _is_non_actionable_stall_record(item)
        )
    deduplicated: List[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        key = str(record.get("issue_id") or "record-%s" % index)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    return deduplicated


def _legacy_pressure_issue_type(issue: Mapping[str, Any]) -> str:
    """Classify legacy generic pressure errors when a report is read.

    Reports written before the subsystem-specific issue taxonomy contain only
    ``pressure_error``. Derive the more useful category from their preserved
    error text so old runs do not keep blaming the application or collectors.
    """
    issue_type = str(issue.get("type") or "").strip()
    # ``app_process_exit`` was the old name for a runtime lifecycle event.
    # Translate it at read time so the issue remains visible for diagnosis but
    # is never presented as an application crash.
    if issue_type == "app_process_exit":
        return "runtime_process_exit"
    if issue_type not in {"pressure_error", ""}:
        return issue_type or "unknown"
    text = "\n".join(
        str(issue.get(key) or "")
        for key in ("title", "problem_description", "key_log_excerpt", "suggested_investigation")
    ).lower()
    if (
        ("runtime_failures" in text or "failure.json" in text)
        and any(
            marker in text
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
    if "audit gate" in text or "complete_audit_gate" in text or "startup gate" in text:
        return "startup_gate_error"
    if any(
        marker in text
        for marker in (
            "launcher",
            "activitymanager",
            "activity manager",
            "am start",
            "foreground/adb",
            "target package did not return to foreground",
        )
    ):
        return "foreground_recovery_error"
    if "unable to resume paused pressure-engine pids" in text:
        return "pressure_handoff_error"
    if "collector did not stop" in text or "collector could not stop" in text:
        return "collector_error"
    if (
        "screenshot capture returned no data" in text
        or "screencap returned no data" in text
    ):
        return "screenshot_unavailable"
    if "fastbot exited with return code" in text:
        return "pressure_engine_error"
    if "uiautomator2 could not be restored" in text:
        return "uiautomator_error"
    return "pressure_error"


_LEGACY_PRESSURE_LABELS = {
    "startup_gate_error": "启动门禁准备失败",
    "foreground_recovery_error": "前台恢复/ADB 交接失败",
    "pressure_handoff_error": "Fastbot 控制交接失败",
    "pressure_engine_error": "Fastbot 引擎异常退出",
    "collector_error": "证据采集器停止失败",
    "screenshot_unavailable": "截图验证不可用",
    "uiautomator_error": "uiautomator2 控制恢复失败",
    "artifact_persistence_error": "运行产物落盘失败",
    "pressure_error": "压测框架或准备失败",
    "runtime_process_exit": "目标进程退出（未确认应用崩溃）",
}


_LEGACY_PRESSURE_SUGGESTIONS = {
    "startup_gate_error": "启动门禁未完成，先确认审核中、确定和隐私政策步骤均已完成，再单设备复测。",
    "foreground_recovery_error": "报告未发现应用崩溃证据；检查设备在线状态、Launcher/ActivityManager 和 am start 返回。",
    "pressure_handoff_error": "Fastbot 已退出或暂停 PID 已消失；这条记录本身不能作为应用崩溃结论。",
    "pressure_engine_error": "检查对应阶段 fastbot.log 的最后动作、Fastbot 返回码和设备端运行时文件。",
    "collector_error": "检查截图采集线程、磁盘空间和 ADB 通道；这不是应用崩溃证据。",
    "screenshot_unavailable": "检查页面是否启用了 FLAG_SECURE 或厂商截图限制，并结合前台 Activity 连续采样确认恢复；该错误与 Fastbot JAR 同步无关。",
    "uiautomator_error": "检查 uiautomator2 与 Fastbot 的 UiAutomation 控制权交接和设备连接。",
    "artifact_persistence_error": "检查运行目录路径长度、磁盘空间和目录写权限；这是框架运行产物落盘失败，与 Fastbot JAR、ABI、APK 安装或被测应用崩溃无关。",
    "runtime_process_exit": "检查 ActivityManager 退出原因、LMK/厂商后台策略和恢复过程；当前记录没有确认的应用崩溃证据。",
}


def _successful_fallback_count(report: Mapping[str, Any]) -> int:
    statistics = report.get("statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    explicit = int(statistics.get("fallback_recovery_success_count", 0) or 0)
    record_values = _stall_record_values(report)
    records = sum(
        str(item.get("type") or "") == "fallback_recovery"
        or bool(
            item.get("recovery", {}).get("success")
            if isinstance(item.get("recovery"), Mapping)
            else False
        )
        for item in record_values
    )
    attempts = int(statistics.get("stall_recovery_count", 0) or 0)
    failures = int(statistics.get("stall_recovery_failure_count", 0) or 0)
    if explicit or record_values:
        return max(explicit, records)
    return max(0, attempts - failures)


def _cached_report_summary(path: Path) -> Optional[Dict[str, Any]]:
    """Return a report summary, recomputing only when the files change."""
    try:
        stat = path.stat()
    except OSError:
        return None
    checkpoint_path = path.parent / _CHECKPOINT_FILE_NAME
    try:
        checkpoint_stat = checkpoint_path.stat()
        checkpoint_key = (
            int(checkpoint_stat.st_mtime_ns),
            int(checkpoint_stat.st_size),
        )
    except OSError:
        checkpoint_key = (0, 0)
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size), checkpoint_key)
    cache_key = str(path)
    with _REPORT_SUMMARY_LOCK:
        cached = _REPORT_SUMMARY_CACHE.get(cache_key)
        if cached is not None and cached[0] == key:
            return copy.deepcopy(cached[1])
    value = report_summary(path)
    with _REPORT_SUMMARY_LOCK:
        _REPORT_SUMMARY_CACHE[cache_key] = (key, value)
        if len(_REPORT_SUMMARY_CACHE) > 1024:
            _REPORT_SUMMARY_CACHE.pop(next(iter(_REPORT_SUMMARY_CACHE)))
    return value


def report_summary(path: Path) -> Optional[Dict[str, Any]]:
    report = _cached_json_document(path, None)
    if not isinstance(report, dict):
        return None
    statistics = report.get("statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    device = report.get("device_info", {})
    if not isinstance(device, Mapping):
        device = {}
    raw_status = str(report.get("status") or "unknown")
    checkpoint = _cached_checkpoint(
        path.parent,
        run_id=str(report.get("run_id") or "") or None,
        device_serial=str(report.get("device_serial") or "") or None,
    )
    resume_state = _effective_resume_state(report, checkpoint, default=raw_status)
    resume_state, operation_stalled, stalled_for_seconds = _runtime_heartbeat_state(
        resume_state,
        report,
        checkpoint,
    )
    terminal_state = terminal_resume_state(report) or terminal_resume_state(checkpoint)
    if raw_status in {"completed", "passed"} or resume_state in {
        "completed",
        "passed",
        "completed_with_crashes",
    }:
        status = "passed"
    elif resume_state == MANUALLY_ENDED_STATE or bool(report.get("manual_end")):
        # Keep the public report contract stable while exposing the precise
        # internal marker through ``resumeState``.
        status = "stopped"
    elif resume_state in {
        "recovering",
        "waiting_for_device",
        "resuming",
        "paused",
        "needs_manual_resume",
    }:
        status = resume_state
    elif raw_status in {"running", "preparing", "installing"}:
        status = "running"
    elif raw_status in {"interrupted", "stopped"}:
        status = "stopped"
    else:
        status = "failed"
    cycles_detail = unique_cycle_rows(report)
    confirmed_crash_cycles = _confirmed_report_crash_cycles(report)
    progress_totals = _report_progress_totals(report, checkpoint)
    completed_numbers = progress_totals["completed_numbers"]
    current_cycle = progress_totals.get("current_cycle") or {}
    current_is_partial = bool(current_cycle)
    target_seconds = report_pressure_target_seconds(report)
    elapsed_seconds = int(_nonnegative_float(progress_totals["elapsed_seconds"], 0.0))
    config = report.get("config", {})
    test = config.get("test", {}) if isinstance(config, Mapping) else {}
    try:
        expected_cycles = int(test.get("max_cycles", 0) or 0) if isinstance(test, Mapping) else 0
    except (TypeError, ValueError):
        expected_cycles = 0
    passed_cycles = sum(
        1 for cycle in cycles_detail
        if isinstance(cycle, Mapping)
        and _public_cycle_status(report, cycle, confirmed_crash_cycles)
        in {"passed", "completed"}
    )
    failed_cycles = sum(
        1 for cycle in cycles_detail
        if isinstance(cycle, Mapping)
        and _public_cycle_status(report, cycle, confirmed_crash_cycles)
        in {"failed", "crash", "error"}
    )
    interrupted_cycles = sum(
        1 for cycle in cycles_detail
        if isinstance(cycle, Mapping)
        and _public_cycle_status(report, cycle, confirmed_crash_cycles)
        in {"interrupted", "stopped"}
    )
    failed_cycle = next((
        cycle for cycle in reversed(cycles_detail)
        if _public_cycle_status(report, cycle, confirmed_crash_cycles) in {"failed", "crash", "error"}
    ), {}) if status == "failed" else {}
    return {
        "id": report_id(path),
        "testId": report_test_id(path, report),
        "runId": str(report.get("run_id") or path.parent.name),
        "app": str(report.get("package_name") or "未知应用"),
        "packageName": str(report.get("package_name") or ""),
        "device": " ".join(
            value for value in (
                str(device.get("manufacturer") or "").strip(),
                str(device.get("model") or "").strip(),
            ) if value
        ) or str(report.get("device_serial") or "未知设备"),
        "manufacturer": str(device.get("manufacturer") or ""),
        "model": str(device.get("model") or ""),
        "androidVersion": str(device.get("android_version") or ""),
        "sdk": str(device.get("sdk") or ""),
        "screenSize": str(device.get("screen_size") or ""),
        "deviceSerial": str(report.get("device_serial") or ""),
        "startedAt": report.get("started_at"),
        "finishedAt": report.get("finished_at"),
        "durationSeconds": elapsed_seconds,
        "pressureTargetSeconds": target_seconds,
        "pressurePercent": report_pressure_percent(
            elapsed_seconds,
            target_seconds,
            "completed" if status == "passed" else status,
        ),
        "cycles": len(completed_numbers),
        "expectedCycles": expected_cycles,
        "passedCycles": passed_cycles,
        "failedCycles": failed_cycles,
        "interruptedCycles": interrupted_cycles,
        "operations": int(progress_totals["operations"]),
        # Do not trust the persisted aggregate for legacy reports: before
        # ``process_exit`` was separated from crashes, ``crash_count`` counted
        # every process-death row.  The filtered rows are authoritative.
        "crashes": len(_confirmed_report_crashes(report)),
        "stalls": int(statistics.get("stall_detection_count", 0) or 0),
        "fallbackRecoveries": _successful_fallback_count(report),
        "stallRecoveryFailures": int(
            statistics.get("stall_recovery_failure_count", 0) or 0
        ),
        "issues": len(_actionable_issue_values(report)),
        "status": status,
        "rawStatus": raw_status,
        "resumeState": resume_state,
        "fastbotAlive": (checkpoint or {}).get("fastbot_alive"),
        "fastbotStartedAt": (checkpoint or {}).get("fastbot_started_at"),
        "lastOperationAt": (checkpoint or {}).get("last_operation_at"),
        "operationStalled": operation_stalled,
        "stalledForSeconds": stalled_for_seconds,
        "resumeAttempts": max(
            _nonnegative_int(report.get("resume_attempts"), 0),
            _nonnegative_int((checkpoint or {}).get("resume_attempts"), 0),
        ),
        "remainingSeconds": 0.0
        if terminal_state
        else _nonnegative_float(
            report.get("remaining_seconds", (checkpoint or {}).get("remaining_seconds", 0)),
            0.0,
        ),
        "lastHeartbeat": report.get("last_heartbeat")
        or (checkpoint or {}).get("last_heartbeat"),
        "currentCycle": dict(current_cycle) if current_is_partial else None,
        "permissionFailureReason": describe_permission_failure(failed_cycle) or None,
        "lastError": (
            report.get("last_error")
            if terminal_state
            else report.get("last_error")
            or (checkpoint or {}).get("last_error")
        ),
        "checkpointAvailable": bool(checkpoint),
        "checkpointPath": str(path.parent / "checkpoint.json") if checkpoint else "",
        "foregroundComponent": (checkpoint or {}).get("foreground_component")
        or report.get("foreground_component"),
        "fastbotPid": (checkpoint or {}).get("fastbot_pid"),
        "remoteLogPath": (checkpoint or {}).get("remote_log_path"),
        "remoteLogOffset": _nonnegative_int(
            (checkpoint or {}).get("remote_log_offset"), 0
        ),
        "apkFingerprint": (checkpoint or {}).get("apk_fingerprint")
        or report.get("apk_fingerprint"),
        "canResume": _can_resume_device_state(
            resume_state,
            resume_mode=_job_resume_mode(None, report),
        ),
        "canEnd": False,
        "manualEnd": resume_state == MANUALLY_ENDED_STATE
        or bool(report.get("manual_end")),
        "artifacts": [
            name for name, artifact in report_artifacts(path, report).items() if artifact.is_file()
        ],
    }


def report_index() -> Dict[str, Path]:
    return {report_id(path): path for path in report_paths()}


def report_artifacts(path: Path, report: Mapping[str, Any]) -> Dict[str, Path]:
    artifacts: Dict[str, Path] = {"json": path, "html": path.with_name("report.html")}
    raw = report.get("artifacts", {})
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            candidate = Path(str(value)).resolve(strict=False)
            try:
                candidate.relative_to(path.parent.resolve())
            except ValueError:
                continue
            artifacts[str(name)] = candidate
    for name, filename in (
        ("fastbot", "fastbot.log"),
        ("logcat", "logcat.log"),
        ("screen_record", "screen_record.mp4"),
    ):
        artifacts.setdefault(name, path.parent / filename)
    screen_record = artifacts.get("screen_record")
    if screen_record is not None and not valid_screen_record(screen_record):
        artifacts.pop("screen_record", None)
    return artifacts


def valid_screen_record(path: Path) -> bool:
    """Reject interrupted MP4 pulls that only contain a partial mdat box."""
    try:
        file_size = path.stat().st_size
        if file_size < 24:
            return False
        box_types: set[bytes] = set()
        box_count = 0
        offset = 0
        with path.open("rb") as stream:
            while offset + 8 <= file_size and box_count < 1024:
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    return False
                box_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if box_size == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if box_size < header_size or offset + box_size > file_size:
                    return False
                box_types.add(box_type)
                offset += box_size
                box_count += 1
        return (
            offset == file_size
            and {b"ftyp", b"mdat", b"moov"}.issubset(box_types)
        )
    except OSError:
        return False


def _safe_report_artifact(value: Any, report_dir: Path) -> Optional[Path]:
    if not str(value or "").strip():
        return None
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = report_dir / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(report_dir.resolve())
    except ValueError:
        return None
    try:
        return candidate if candidate.is_file() and candidate.stat().st_size > 0 else None
    except OSError:
        return None


def report_crash_artifacts(
    path: Path, report: Mapping[str, Any]
) -> Tuple[Dict[str, Path], Dict[int, Dict[str, str]]]:
    artifacts: Dict[str, Path] = {}
    aliases: Dict[int, Dict[str, str]] = {}
    crashes = _confirmed_report_crashes(report)
    for index, crash in enumerate(crashes):
        if not isinstance(crash, Mapping):
            continue
        sources: List[Mapping[str, Any]] = []
        page = crash.get("app_crash_page", {})
        if isinstance(page, Mapping) and isinstance(page.get("artifacts"), Mapping):
            sources.append(page["artifacts"])
        runtime_failure = crash.get("runtime_failure", {})
        evidence = (
            runtime_failure.get("evidence", {})
            if isinstance(runtime_failure, Mapping)
            else {}
        )
        if isinstance(evidence, Mapping) and isinstance(evidence.get("artifacts"), Mapping):
            sources.append(evidence["artifacts"])
        # Runtime failures are normally copied onto the crash entry, but older
        # reports only kept them on their cycle.  Keep those evidence files
        # addressable too so the report remains useful after a schema change.
        cycle_number = crash.get("cycle")
        phase_number = crash.get("phase")
        for cycle in unique_cycle_rows(report):
            if not isinstance(cycle, Mapping) or cycle.get("cycle") != cycle_number:
                continue
            for failure in cycle.get("runtime_failures", []):
                if not isinstance(failure, Mapping):
                    continue
                if phase_number is not None and failure.get("phase") != phase_number:
                    continue
                failure_evidence = failure.get("evidence", {})
                if isinstance(failure_evidence, Mapping) and isinstance(
                    failure_evidence.get("artifacts"), Mapping
                ):
                    sources.append(failure_evidence["artifacts"])
        for source in sources:
            for name, value in source.items():
                key = re.sub(r"[^a-z0-9_-]+", "-", str(name).lower()).strip("-")
                if not key:
                    continue
                candidate = _safe_report_artifact(value, path.parent)
                if candidate is None:
                    continue
                alias = "crash-%s-%s" % (index, key)
                artifacts.setdefault(alias, candidate)
                aliases.setdefault(index, {}).setdefault(str(name), alias)
    return artifacts, aliases


def _integer_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _matching_cycle(
    report: Mapping[str, Any], cycle_number: Optional[int]
) -> Optional[Mapping[str, Any]]:
    if cycle_number is None:
        return None
    cycles = unique_cycle_rows(report)
    for cycle in cycles:
        if not isinstance(cycle, Mapping):
            continue
        if _integer_or_none(cycle.get("cycle")) == cycle_number:
            return cycle
    return None


def _matching_runtime_failure(
    report: Mapping[str, Any], cycle_number: Optional[int], phase_number: Optional[int]
) -> Optional[Mapping[str, Any]]:
    cycle = _matching_cycle(report, cycle_number)
    if not isinstance(cycle, Mapping):
        return None
    failures = cycle.get("runtime_failures", [])
    if not isinstance(failures, list):
        return None
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if phase_number is not None and _integer_or_none(failure.get("phase")) != phase_number:
            continue
        return failure
    return None


def _phase_log_provenance(
    path: Path, report: Mapping[str, Any], crash: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """Find the exact Fastbot log byte range that produced a crash.

    Reports written before structured trajectories were introduced still retain
    ``pressure_phases.log_provenance``.  Looking it up here prevents a later
    phase (or the cycle-level aggregate) from being replayed by mistake.
    """
    cycle_number = _integer_or_none(crash.get("cycle"))
    phase_number = _integer_or_none(crash.get("phase"))
    cycle = _matching_cycle(report, cycle_number)
    if isinstance(cycle, Mapping):
        phases = cycle.get("pressure_phases", [])
        if isinstance(phases, list):
            for index, phase in enumerate(phases, 1):
                if not isinstance(phase, Mapping):
                    continue
                phase_id = _integer_or_none(phase.get("phase")) or index
                if phase_number is not None and phase_id != phase_number:
                    continue
                provenance = phase.get("log_provenance")
                if isinstance(provenance, Mapping) and provenance.get("path"):
                    return dict(provenance)
                # A few early report writers persisted offsets directly on the
                # phase instead of nesting them under log_provenance.
                if phase.get("path") or phase.get("log_path"):
                    return {
                        "path": phase.get("path") or phase.get("log_path"),
                        "start_offset": phase.get("start_offset", 0),
                        "end_offset": phase.get("end_offset"),
                    }

    # Some intermediate report writers only persisted the cycle trajectory.
    trajectory_values: List[Any] = [crash.get("trajectory_path")]
    if isinstance(cycle, Mapping):
        artifacts = cycle.get("artifacts", {})
        if isinstance(artifacts, Mapping):
            trajectory_values.append(artifacts.get("trajectory"))
    for value in trajectory_values:
        candidate = _safe_report_artifact(value, path.parent)
        if candidate is None:
            continue
        trajectory = read_json_file(candidate, None)
        if not isinstance(trajectory, Mapping):
            continue
        phases = trajectory.get("pressure_phases", [])
        if not isinstance(phases, list):
            continue
        for index, phase in enumerate(phases, 1):
            if not isinstance(phase, Mapping):
                continue
            phase_id = _integer_or_none(phase.get("phase")) or index
            if phase_number is not None and phase_id != phase_number:
                continue
            provenance = phase.get("log_provenance")
            if isinstance(provenance, Mapping) and provenance.get("path"):
                return dict(provenance)

    # Last-resort support for reports that only kept the cycle report and the
    # shared fastbot.log.  Limit the read window so opening an overnight report
    # never loads gigabytes of log data into the web process.
    fallback = path.parent / "fastbot.log"
    if not fallback.is_file():
        raw_artifacts = report.get("artifacts", {})
        if isinstance(raw_artifacts, Mapping):
            fallback = _safe_report_artifact(raw_artifacts.get("fastbot"), path.parent) or fallback
    try:
        size = fallback.stat().st_size
    except OSError:
        return None
    window = 64 * 1024 * 1024
    return {
        "path": str(fallback),
        "start_offset": max(0, size - window),
        "end_offset": size,
        "fallback_window": True,
    }


def _trajectory_has_events(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get("events"), list) and bool(value.get("events"))


def _report_replay_options(report: Mapping[str, Any]) -> Dict[str, Any]:
    config = report.get("config", {})
    if not isinstance(config, Mapping):
        return {}
    options = config.get("crash_replay", {})
    return dict(options) if isinstance(options, Mapping) else {}


def _planned_report_trajectory(
    path: Path,
    report: Mapping[str, Any],
    crash: Mapping[str, Any],
    trajectory: Mapping[str, Any],
) -> Dict[str, Any]:
    provenance = trajectory.get("log_provenance", {})
    log_path: Optional[Path] = None
    if isinstance(provenance, Mapping):
        log_path = _safe_report_artifact(provenance.get("path"), path.parent)
    if log_path is None:
        fallback = path.parent / "fastbot.log"
        if fallback.is_file():
            log_path = fallback
    return build_replay_trajectory(
        trajectory,
        _report_replay_options(report),
        package_name=str(report.get("package_name") or ""),
        pressure_log_path=log_path,
        cycle=crash.get("cycle"),
        phase=crash.get("phase"),
        crash_at=crash.get("detected_at"),
    )


def _materialize_log_trajectory(
    path: Path, report: Mapping[str, Any], crash: Mapping[str, Any]
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Build a replayable trajectory from the crash phase's Fastbot text log."""
    provenance = _phase_log_provenance(path, report, crash)
    if not isinstance(provenance, Mapping):
        return None, None
    log_path = _safe_report_artifact(provenance.get("path"), path.parent)
    if log_path is None:
        return None, None
    start_offset = max(0, _integer_or_none(provenance.get("start_offset")) or 0)
    end_offset = _integer_or_none(provenance.get("end_offset"))
    try:
        parsed = parse_fastbot_log(
            log_path,
            start_offset=start_offset,
            end_offset=end_offset,
            max_events=MAX_REPLAY_EVENTS,
        )
    except (OSError, ValueError, TypeError):
        return None, None
    events = [dict(item) for item in parsed.get("events", []) if isinstance(item, Mapping)]
    cycle_number = _integer_or_none(crash.get("cycle"))
    phase_number = _integer_or_none(crash.get("phase"))
    for event in events:
        if cycle_number is not None:
            event.setdefault("cycle", cycle_number)
        if phase_number is not None:
            event.setdefault("phase", phase_number)
    trajectory: Dict[str, Any] = {
        "collected_at": utc_now(),
        "device_serial": report.get("device_serial"),
        "package_name": report.get("package_name"),
        "engine": "fastbot",
        "events": events,
        "event_count": len(events),
        "operation_count": len(events),
        "event_source": "fastbot.log",
        "replayable": bool(events),
        "events_truncated": bool(parsed.get("truncated")),
        "trace": parsed,
        "log_provenance": dict(provenance),
        "cycle": cycle_number,
        "phase": phase_number,
        "note": (
            (
                "从 Fastbot 日志尾部窗口生成结构化回放事件；旧报告未保存阶段偏移。"
                if provenance.get("fallback_window")
                else "从崩溃发生阶段的 Fastbot 文本日志生成结构化回放事件。"
            )
            if events
            else "该阶段日志没有可识别的结构化动作；FBM 二进制模型不能直接回放。"
        ),
    }
    trajectory = _planned_report_trajectory(path, report, crash, trajectory)
    if not events:
        return None, trajectory

    # Keep the generated artifact inside the report directory and write it
    # atomically so a simultaneous report refresh never observes half a JSON.
    cycle_label = "%03d" % cycle_number if cycle_number is not None else "unknown"
    phase_label = "%03d" % phase_number if phase_number is not None else "unknown"
    replay_dir = path.parent / "replay_trajectories"
    replay_path = replay_dir / ("trajectory_cycle_%s_phase_%s.json" % (cycle_label, phase_label))
    try:
        write_json_file(replay_path, trajectory)
    except (OSError, ValueError, TypeError):
        # The in-memory trajectory is still useful to the replay endpoint.
        return None, trajectory
    return replay_path, trajectory


def _crash_trajectory(
    path: Path, report: Mapping[str, Any], crash: Mapping[str, Any]
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Return the crash-specific trajectory, repairing legacy reports lazily."""
    cycle_number = _integer_or_none(crash.get("cycle"))
    phase_number = _integer_or_none(crash.get("phase"))
    if cycle_number is not None and phase_number is not None:
        cached_path = path.parent / "replay_trajectories" / (
            "trajectory_cycle_%03d_phase_%03d.json" % (cycle_number, phase_number)
        )
        cached = read_json_file(cached_path, None)
        if _trajectory_has_events(cached):
            return cached_path, _planned_report_trajectory(
                path, report, crash, cached
            )

    runtime_failure = crash.get("runtime_failure")
    if not isinstance(runtime_failure, Mapping):
        runtime_failure = _matching_runtime_failure(
            report,
            _integer_or_none(crash.get("cycle")),
            _integer_or_none(crash.get("phase")),
        ) or {}
    # Rebuild from the exact phase log first.  A legacy cycle-level trajectory
    # can contain later actions and is not a faithful crash replay source.
    generated_path, generated = _materialize_log_trajectory(path, report, crash)
    if generated is not None and _trajectory_has_events(generated):
        return generated_path, _planned_report_trajectory(
            path, report, crash, generated
        )

    candidates: List[Any] = []
    evidence = runtime_failure.get("evidence", {}) if isinstance(runtime_failure, Mapping) else {}
    if isinstance(evidence, Mapping):
        artifacts = evidence.get("artifacts", {})
        if isinstance(artifacts, Mapping):
            candidates.append(artifacts.get("trajectory"))
    candidates.append(crash.get("trajectory_path"))
    raw_artifacts = report.get("artifacts", {})
    if isinstance(raw_artifacts, Mapping):
        candidates.append(raw_artifacts.get("crash_trajectory"))

    if cycle_number is not None and phase_number is not None:
        candidates.insert(
            1,
            path.parent
            / "replay_trajectories"
            / ("trajectory_cycle_%03d_phase_%03d.json" % (cycle_number, phase_number)),
        )

    first_empty: Optional[Tuple[Path, Dict[str, Any]]] = None
    for value in candidates:
        candidate = _safe_report_artifact(value, path.parent)
        if candidate is None:
            continue
        trajectory = read_json_file(candidate, None)
        if not isinstance(trajectory, dict):
            continue
        if _trajectory_has_events(trajectory):
            return candidate, _planned_report_trajectory(
                path, report, crash, trajectory
            )
        if first_empty is None:
            first_empty = (candidate, trajectory)

    if generated is not None:
        return generated_path, _planned_report_trajectory(
            path, report, crash, generated
        )
    if first_empty is not None:
        candidate, trajectory = first_empty
        return candidate, _planned_report_trajectory(
            path, report, crash, trajectory
        )
    return None, None


def _short_replay_attempts(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    attempts: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result = item.get("result", {})
        if not isinstance(result, Mapping):
            result = {}
        attempts.append({
            "attempt": int(item.get("attempt", len(attempts) + 1) or len(attempts) + 1),
            "restored": bool(item.get("restored", False)),
            "replayed": bool(item.get("replayed", False)),
            "reproducible": bool(item.get("reproducible", False)),
            "error": str(item.get("error") or result.get("reason") or ""),
            "startMode": str(item.get("startMode") or "") or None,
            "trajectoryPath": str(item.get("trajectoryPath") or "") or None,
            "replayPlan": item.get("replayPlan") if isinstance(item.get("replayPlan"), Mapping) else None,
            "replayStart": item.get("replayStart") if isinstance(item.get("replayStart"), Mapping) else None,
        })
    return attempts


def _public_replay_plan(value: Any) -> Dict[str, Any]:
    """Expose bounded replay metadata in the frontend's camel-case shape."""
    if not isinstance(value, Mapping):
        return {}

    def number(key: str, default: Optional[float] = None) -> Optional[float]:
        raw = value.get(key)
        if raw is None:
            return default
        try:
            parsed = float(raw)
        except (TypeError, ValueError, OverflowError):
            return default
        if parsed.is_integer():
            return int(parsed)
        return parsed

    anchor = value.get("page_anchor")
    public_anchor: Optional[Dict[str, Any]] = None
    if isinstance(anchor, Mapping):
        public_anchor = {
            "activity": str(anchor.get("activity") or ""),
            "component": str(anchor.get("component") or ""),
            "source": str(anchor.get("source") or ""),
            "confidence": str(anchor.get("confidence") or ""),
            "observedAt": anchor.get("observed_at"),
            "eventId": anchor.get("event_id"),
        }
    return {
        "version": number("version"),
        "strategy": str(value.get("strategy") or ""),
        "requestedWindowSeconds": number("requested_window_seconds"),
        "actualWindowSeconds": number("actual_window_seconds"),
        "maxEvents": number("max_events"),
        "originalEventCount": number("original_event_count", 0),
        "selectedEventCount": number("selected_event_count", 0),
        "omittedEventCount": number("omitted_event_count", 0),
        "selectedByTime": bool(value.get("selected_by_time", False)),
        "crashAt": value.get("crash_at"),
        "crashTimestampAlignment": value.get("crash_timestamp_alignment"),
        "crashCutoffApplied": bool(value.get("crash_cutoff_applied", False)),
        "startAt": value.get("start_at"),
        "endAt": value.get("end_at"),
        "requirePageAnchor": bool(value.get("require_page_anchor", True)),
        "allowColdActivityStart": bool(
            value.get("allow_cold_activity_start", False)
        ),
        "pageAnchor": public_anchor,
    }


def _manual_replays(path: Path, signature: str) -> List[Dict[str, Any]]:
    value = read_json_file(path.parent / "manual_replays.json", [])
    if not isinstance(value, list):
        return []
    return [
        dict(item) for item in value
        if isinstance(item, Mapping) and str(item.get("crashSignature") or "") == signature
    ][-10:]


def _replay_progress_path(report_path: Path, serial: str, crash_index: int) -> Path:
    """Return a bounded, report-local progress file for one manual replay."""
    safe_serial = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(serial)).strip("._") or "device"
    return report_path.parent / (
        "manual_replay_progress_%s_%s.json" % (safe_serial, int(crash_index))
    )


def _write_replay_progress(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist replay progress without allowing a telemetry write to abort replay."""
    try:
        write_json_file(path, dict(payload))
    except OSError:
        # Progress is advisory.  A short Windows antivirus/share violation
        # must never stop device input or turn a successful replay into 500.
        pass


def replay_report_progress(identifier: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Read the latest asynchronous progress snapshot for a manual replay."""
    path = report_index().get(identifier)
    if path is None:
        raise ApiError("报告不存在", HTTPStatus.NOT_FOUND)
    report = _cached_json_document(path, None)
    if not isinstance(report, Mapping):
        raise ApiError("报告文件无法读取", HTTPStatus.UNPROCESSABLE_ENTITY)
    try:
        crash_index = int(payload.get("crashIndex", -1))
    except (TypeError, ValueError) as exc:
        raise ApiError("crashIndex 必须是整数") from exc
    crashes = _confirmed_report_crashes(report)
    if crash_index < 0 or crash_index >= len(crashes):
        raise ApiError("崩溃记录不存在", HTTPStatus.NOT_FOUND)
    serial = str(report.get("device_serial") or "").strip()
    progress_path = _replay_progress_path(path, serial, crash_index)
    value = read_json_file(progress_path, None)
    if isinstance(value, Mapping):
        return dict(value)
    crash = crashes[crash_index] if isinstance(crashes[crash_index], Mapping) else {}
    trajectory_path, trajectory = _crash_trajectory(path, report, crash)
    events = trajectory.get("events", []) if isinstance(trajectory, Mapping) else []
    total = len(events) if isinstance(events, list) else 0
    plan = (
        _public_replay_plan(trajectory.get("replay_plan"))
        if isinstance(trajectory, Mapping)
        else {}
    )
    return {
        "status": "idle",
        "executed": 0,
        "total": total,
        "skipped": 0,
        "updatedAt": utc_now(),
        "reportId": identifier,
        "crashIndex": crash_index,
        "trajectory": str(trajectory_path) if trajectory_path else "",
        "eventCount": total,
        "replayPlan": plan,
    }


def normalized_crashes(path: Path, report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    crashes = _confirmed_report_crashes(report)
    _artifacts, aliases = report_crash_artifacts(path, report)
    values: List[Dict[str, Any]] = []
    for index, crash in enumerate(crashes):
        if not isinstance(crash, Mapping):
            continue
        page = crash.get("app_crash_page", {})
        if not isinstance(page, Mapping):
            page = {}
        runtime_failure = crash.get("runtime_failure", {})
        if not isinstance(runtime_failure, Mapping):
            runtime_failure = {}
        cycle_number = crash.get("cycle")
        phase_number = crash.get("phase")
        # Some reports predate the crash-level runtime_failure link.  Find the
        # matching cycle evidence so the UI can still expose the saved bundle.
        if not runtime_failure:
            for cycle in report.get("cycles", []):
                if not isinstance(cycle, Mapping) or cycle.get("cycle") != cycle_number:
                    continue
                for failure in cycle.get("runtime_failures", []):
                    if not isinstance(failure, Mapping):
                        continue
                    if phase_number is not None and failure.get("phase") != phase_number:
                        continue
                    runtime_failure = failure
                    break
                if runtime_failure:
                    break
        runtime_evidence = runtime_failure.get("evidence", {})
        if not isinstance(runtime_evidence, Mapping):
            runtime_evidence = {}
        runtime_detection = runtime_evidence.get("detection", {})
        if not isinstance(runtime_detection, Mapping):
            runtime_detection = {}
        # Older runtime-failure reports saved the UI hierarchy but did not yet
        # persist ``page_text`` or the clipboard result. Recover the visible
        # crash-page content from that hierarchy so historical reports remain
        # useful to developers.
        hierarchy_text = ""
        hierarchy_sources: List[Mapping[str, Any]] = []
        for source in (
            page.get("artifacts"),
            runtime_evidence.get("artifacts"),
        ):
            if isinstance(source, Mapping):
                hierarchy_sources.append(source)
        for source in hierarchy_sources:
            hierarchy_path = _safe_report_artifact(
                source.get("ui_hierarchy"), path.parent
            )
            if hierarchy_path is None:
                continue
            try:
                hierarchy_text = CrashHandler.extract_app_crash_page_text(
                    hierarchy_path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                hierarchy_text = ""
            if hierarchy_text:
                break

        copy_text = str(
            crash.get("copied_crash_content")
            or page.get("copied_text")
            or crash.get("page_content")
            or page.get("page_text")
            or runtime_evidence.get("copied_text")
            or runtime_evidence.get("page_text")
            or hierarchy_text
            or ""
        )
        crash_log = str(
            crash.get("stacktrace")
            or crash.get("raw_log")
            or runtime_detection.get("stacktrace")
            or copy_text
            or crash.get("message")
            or ""
        )
        trajectory_path, trajectory = _crash_trajectory(path, report, crash)
        events = trajectory.get("events", []) if isinstance(trajectory, Mapping) else []
        event_count = len(events) if isinstance(events, list) else 0
        replay_plan = (
            _public_replay_plan(trajectory.get("replay_plan"))
            if isinstance(trajectory, Mapping)
            else {}
        )
        # A replay can execute an in-memory trajectory even if Windows briefly
        # rejects the atomic artifact rename.  The persisted path is preferred,
        # but event availability is the actual capability flag.
        replay_available = bool(event_count)
        replay_reason = ""
        if trajectory is None:
            replay_reason = "未保存崩溃轨迹"
        elif not event_count:
            replay_reason = "未找到该崩溃阶段的结构化动作；FBM 是二进制模型，不是回放脚本。"
        screenshot = ""
        alias_map = aliases.get(index, {})
        for key in ("crash_page_screenshot", "screenshot"):
            if alias_map.get(key):
                screenshot = alias_map[key]
                break
        foreground_component = str(
            page.get("component_at_capture")
            or page.get("component")
            or runtime_evidence.get("foreground_component")
            or ""
        )
        package_name = str(report.get("package_name") or "")
        is_app_crash_page = bool(page) or (
            bool(package_name)
            and CrashHandler.is_app_crash_page_component(
                foreground_component, package_name
            )
        )
        copy_status = str(page.get("copy_status") or "")
        if not copy_status:
            copy_status = "captured" if (
                crash.get("copied_crash_content")
                or page.get("copied_text")
                or runtime_evidence.get("copied_text")
            ) else "page_text_fallback" if copy_text else (
                "runtime_evidence_only" if runtime_evidence else "missing"
            )
        copy_note = str(page.get("copy_error") or "")
        if not copy_note and copy_status in {"runtime_evidence_only", "page_text_fallback"}:
            copy_note = (
                "应用崩溃页剪贴板文本未成功读取，已使用页面层级文本和运行时证据"
                "作为复制兜底；截图、前台组件和日志均已保存。"
            )
        signature = str(crash.get("signature") or "")
        occurrences = crash.get("occurrence_details", [])
        if not isinstance(occurrences, list):
            occurrences = []
        raw_occurrence_count = crash.get("occurrences", len(occurrences) or 1)
        if isinstance(raw_occurrence_count, list):
            occurrence_count = len(raw_occurrence_count)
        else:
            try:
                occurrence_count = int(raw_occurrence_count or 1)
            except (TypeError, ValueError):
                occurrence_count = len(occurrences) or 1
        values.append({
            "index": index,
            "cycle": int(crash.get("cycle", 0) or 0),
            "phase": int(crash.get("phase", 0) or 0),
            "detectedAt": crash.get("detected_at"),
            "exceptionType": str(crash.get("exception_type") or crash.get("crash_type") or "UnknownCrash"),
            "message": str(crash.get("message") or ""),
            "location": ".".join(
                value for value in (
                    str(crash.get("class") or "").strip(),
                    str(crash.get("method") or "").strip(),
                ) if value
            ),
            "source": str(crash.get("source") or ""),
            "signature": signature,
            "occurrenceCount": max(1, occurrence_count),
            "occurrences": [dict(item) for item in occurrences if isinstance(item, Mapping)],
            "crashLog": crash_log,
            "copiedInfo": copy_text,
            "pageComponent": foreground_component,
            "pageType": (
                "app_crash_page"
                if is_app_crash_page
                else "runtime_page"
                if foreground_component
                else "unknown"
            ),
            "pageCapturedAt": page.get("captured_at"),
            "copyStatus": copy_status,
            "copyNote": copy_note,
            "screenshotArtifact": screenshot or None,
            "reproducible": bool(crash.get("reproducible", False)),
            "replay": {
                "available": replay_available,
                "reason": replay_reason,
                "eventCount": event_count,
                "originalEventCount": int(
                    replay_plan.get("originalEventCount", event_count) or 0
                ),
                "selectedEventCount": int(
                    replay_plan.get("selectedEventCount", event_count) or 0
                ),
                "omittedEventCount": int(
                    replay_plan.get("omittedEventCount", 0) or 0
                ),
                "windowSeconds": replay_plan.get("requestedWindowSeconds"),
                "actualWindowSeconds": replay_plan.get("actualWindowSeconds"),
                "pageAnchor": replay_plan.get("pageAnchor"),
                "replayPlan": replay_plan,
                "eventSource": str(trajectory.get("event_source") or "") if isinstance(trajectory, Mapping) else "",
                "note": str(trajectory.get("note") or "") if isinstance(trajectory, Mapping) else "",
                "automaticAttempts": _short_replay_attempts(crash.get("replay_attempts")),
                "manualAttempts": _manual_replays(path, signature),
            },
        })
    return values


def normalized_cycles(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    cycles = unique_cycle_rows(report)
    confirmed_crash_cycles = _confirmed_report_crash_cycles(report)
    confirmed_signatures_by_cycle: Dict[int, set[str]] = {}
    for crash in _confirmed_report_crashes(report):
        try:
            cycle_number = int(crash.get("cycle"))
        except (TypeError, ValueError, OverflowError):
            continue
        signature = str(crash.get("signature") or "").strip()
        if signature:
            confirmed_signatures_by_cycle.setdefault(cycle_number, set()).add(signature)
    values: List[Dict[str, Any]] = []
    for index, cycle in enumerate(cycles):
        if not isinstance(cycle, Mapping):
            continue
        errors = cycle.get("errors", [])
        if not isinstance(errors, list):
            errors = []
        signatures = cycle.get("crash_signatures", [])
        if not isinstance(signatures, list):
            signatures = []
        try:
            cycle_number = int(cycle.get("cycle", index + 1) or index + 1)
        except (TypeError, ValueError, OverflowError):
            cycle_number = index + 1
        confirmed_signatures = confirmed_signatures_by_cycle.get(cycle_number, set())
        warnings = cycle.get("collection_warnings", [])
        if not isinstance(warnings, list):
            warnings = []
        phases = cycle.get("pressure_phases", [])
        if not isinstance(phases, list):
            phases = []
        stop_reasons = list(dict.fromkeys(
            str(phase.get("stop_reason") or "").strip()
            for phase in phases
            if isinstance(phase, Mapping) and str(phase.get("stop_reason") or "").strip()
        ))
        permission_failure = describe_permission_failure(cycle)
        values.append({
            "cycle": cycle_number,
            "status": _public_cycle_status(report, cycle, confirmed_crash_cycles),
            "pressureElapsedSeconds": max(0, int(float(cycle.get("pressure_elapsed_seconds", 0) or 0))),
            "operations": max(0, int(cycle.get("operation_count", 0) or 0)),
            "crashSignatures": [
                str(item)
                for item in signatures
                if str(item).strip() in confirmed_signatures
            ],
            "errors": [item for item in [permission_failure, *map(str, errors)] if item.strip()],
            "warnings": [str(item) for item in warnings if str(item).strip()],
            "stopReasons": stop_reasons,
        })
    return values


def _normalized_issue_records(issues: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for index, issue in enumerate(issues):
        normalized_type = _legacy_pressure_issue_type(issue)
        normalized_title = str(issue.get("title") or "未命名问题")
        normalized_suggestion = str(issue.get("suggested_investigation") or "")
        if normalized_type != str(issue.get("type") or ""):
            normalized_title = _LEGACY_PRESSURE_LABELS.get(
                normalized_type, normalized_title
            )
            normalized_suggestion = _LEGACY_PRESSURE_SUGGESTIONS.get(
                normalized_type, normalized_suggestion
            )
        device = issue.get("device", {})
        if not isinstance(device, Mapping):
            device = {}
        recovery = issue.get("recovery", {})
        if not isinstance(recovery, Mapping):
            recovery = {}
        try:
            cycle_number = max(0, int(issue.get("cycle", 0) or 0))
        except (TypeError, ValueError):
            cycle_number = 0
        try:
            phase_number = max(0, int(issue.get("phase", 0) or 0))
        except (TypeError, ValueError):
            phase_number = 0
        values.append({
            "id": str(issue.get("issue_id") or "issue-%s" % (index + 1)),
            "type": normalized_type,
            "severity": str(issue.get("severity") or "unknown"),
            "title": str(issue.get("title") or "未命名问题"),
            "description": str(issue.get("problem_description") or ""),
            "cycle": cycle_number,
            "phase": phase_number,
            "detectedAt": issue.get("detected_at"),
            "component": str(issue.get("foreground_component") or ""),
            "keyLog": str(issue.get("key_log_excerpt") or ""),
            "suggestion": normalized_suggestion,
            "recoveryError": str(recovery.get("error") or ""),
            "recoverySuccess": bool(recovery.get("success")),
            "recoveryAction": str(recovery.get("action") or ""),
            "deviceModel": " ".join(
                value for value in (
                    str(device.get("manufacturer") or "").strip(),
                    str(device.get("model") or "").strip(),
                ) if value
            ),
        })
        if normalized_type != str(issue.get("type") or ""):
            values[-1]["title"] = normalized_title
    return values


def normalized_issues(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return _normalized_issue_records(_actionable_issue_values(report))


def normalized_stall_records(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    values = _normalized_issue_records(_stall_record_values(report))
    for value in values:
        if value["type"] != "stall":
            continue
        recovered = bool(value.get("recoverySuccess"))
        value["type"] = "fallback_recovery" if recovered else "stall_observation"
        value["title"] = "%s：%s" % (
            "Monkey 保底恢复" if recovered else "页面静止观察",
            value.get("component") or "unknown page",
        )
        value["description"] = (
            "%s 该记录是 Monkey 页面脱困或静止观察，不等同于应用卡死或稳定性缺陷。"
            % value.get("description", "")
        ).strip()
    return values


def report_collection_warnings(report: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    cycles = unique_cycle_rows(report)
    for cycle in cycles:
        if not isinstance(cycle, Mapping):
            continue
        warnings = cycle.get("collection_warnings", [])
        if isinstance(warnings, list):
            values.extend(str(item) for item in warnings if str(item).strip())
        phases = cycle.get("pressure_phases", [])
        if not isinstance(phases, list):
            continue
        for phase in phases:
            if not isinstance(phase, Mapping):
                continue
            warnings = phase.get("collection_warnings", [])
            if isinstance(warnings, list):
                values.extend(str(item) for item in warnings if str(item).strip())
    return list(dict.fromkeys(values))


def device_report_detail(path: Path, job: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    report = _cached_json_document(path, None)
    if not isinstance(report, dict):
        raise ApiError("报告文件无法读取", HTTPStatus.UNPROCESSABLE_ENTITY)
    summary = _cached_report_summary(path) or {}
    target = report_pressure_target_seconds(report, job)
    elapsed = duration_seconds(report)
    summary.update({
        "reportAvailable": True,
        "pressureTargetSeconds": target,
        "pressurePercent": report_pressure_percent(elapsed, target, str(report.get("status") or "unknown")),
        "cycleResults": normalized_cycles(report),
        "crashDetail": normalized_crashes(path, report),
        "issueDetail": normalized_issues(report),
        "recoveryDetail": normalized_stall_records(report),
        "collectionWarnings": report_collection_warnings(report),
        "errors": [str(item) for item in report.get("errors", []) if str(item).strip()]
        if isinstance(report.get("errors"), list) else [],
    })
    checkpoint = _cached_checkpoint(
        path.parent,
        run_id=str(report.get("run_id") or "") or None,
        device_serial=str(report.get("device_serial") or "") or None,
    )
    if checkpoint:
        summary.update({
            "checkpointAvailable": True,
            "checkpointPath": str(path.parent / "checkpoint.json"),
            "foregroundComponent": checkpoint.get("foreground_component")
            or summary.get("foregroundComponent"),
            "fastbotPid": checkpoint.get("fastbot_pid"),
            "remoteLogPath": checkpoint.get("remote_log_path"),
            "remoteLogOffset": _nonnegative_int(checkpoint.get("remote_log_offset"), 0),
            "apkFingerprint": checkpoint.get("apk_fingerprint")
            or summary.get("apkFingerprint"),
            "canResume": _can_resume_device_state(
                summary.get("resumeState"),
                resume_mode=_job_resume_mode(job, report),
            ),
        })
    resume_state = str(summary.get("resumeState") or "").strip().lower()
    summary["canEnd"] = bool(
        isinstance(job, Mapping)
        and str(job.get("status") or "") in ACTIVE_JOB_STATUSES
        and resume_state not in TERMINAL_RESUME_STATES
    )
    return summary


def _checkpoint_device_summary(
    job: Mapping[str, Any], serial: str, snapshot: Mapping[str, Any]
) -> Dict[str, Any]:
    """Build a report-shaped row when only a durable checkpoint exists."""
    checkpoint = snapshot.get("checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    report = snapshot.get("report")
    report = report if isinstance(report, Mapping) else {}
    state = str(
        snapshot.get("resume_state")
        or _effective_resume_state(report, checkpoint, default="recovering")
    )
    terminal_state = state in {
        "completed",
        "passed",
        "completed_with_crashes",
        "completed_with_errors",
        "failed",
        MANUALLY_ENDED_STATE,
    }
    if state in {"completed", "passed", "completed_with_crashes", "completed_with_errors"}:
        status = "passed"
    elif state in {
        "recovering",
        "waiting_for_device",
        "resuming",
        "paused",
        "needs_manual_resume",
        "running",
    }:
        status = state
    elif state == MANUALLY_ENDED_STATE:
        status = "stopped"
    else:
        status = "failed"
    target = max(
        0,
        int(
            _nonnegative_float(job.get("duration"), 0.0)
            * 60
            * max(1, _nonnegative_int(job.get("cycles"), 1))
        ),
    )
    elapsed = min(target, int(_nonnegative_float(snapshot.get("elapsed_seconds"), 0.0)))
    current = snapshot.get("current_cycle")
    current = dict(current) if isinstance(current, Mapping) else None
    cycle_number = _nonnegative_int(snapshot.get("cycle"), 0)
    if current is None and cycle_number and not terminal_state:
        current = {
            "cycle": cycle_number,
            "phase": _nonnegative_int(snapshot.get("phase"), 0),
            "pressure_elapsed_seconds": _nonnegative_float(
                checkpoint.get("elapsed_seconds"), 0.0
            ),
            "operation_count": _nonnegative_int(
                checkpoint.get("operation_count"), 0
            ),
            "status": state,
            "remaining_seconds": _nonnegative_float(
                snapshot.get("remaining_seconds"), 0.0
            ),
            "last_error": snapshot.get("last_error"),
        }
    device_info = report.get("device_info", {})
    if not isinstance(device_info, Mapping):
        device_info = {}
    device_name = " ".join(
        value
        for value in (
            str(device_info.get("manufacturer") or "").strip(),
            str(device_info.get("model") or "").strip(),
        )
        if value
    ) or serial
    test_id = str(job.get("id") or checkpoint.get("job_id") or "")
    row_id = hashlib.sha256(
        ("checkpoint:%s:%s" % (test_id, serial)).encode("utf-8")
    ).hexdigest()[:20]
    cycle_results: List[Dict[str, Any]] = []
    if current is not None and cycle_number:
        cycle_results.append(
            {
                "cycle": cycle_number,
                "status": state,
                "pressureElapsedSeconds": int(
                    _nonnegative_float(
                        current.get("pressure_elapsed_seconds")
                        or current.get("elapsed_seconds"),
                        0.0,
                    )
                ),
                "operations": _nonnegative_int(current.get("operation_count"), 0),
                "crashSignatures": [],
                "errors": [
                    str(snapshot.get("last_error"))
                    if snapshot.get("last_error")
                    else ""
                ],
                "warnings": [],
                "stopReasons": [],
            }
        )
        cycle_results[0]["errors"] = [
            item for item in cycle_results[0]["errors"] if item
        ]
    return {
        "id": row_id,
        "testId": test_id,
        "runId": str(checkpoint.get("job_id") or test_id),
        "app": str(job.get("packageName") or report.get("package_name") or ""),
        "packageName": str(
            job.get("packageName") or report.get("package_name") or checkpoint.get("package_name") or ""
        ),
        "device": device_name,
        "manufacturer": str(device_info.get("manufacturer") or ""),
        "model": str(device_info.get("model") or ""),
        "androidVersion": str(device_info.get("android_version") or ""),
        "sdk": str(device_info.get("sdk") or ""),
        "screenSize": str(device_info.get("screen_size") or ""),
        "deviceSerial": str(serial),
        "startedAt": checkpoint.get("cycle_started_at")
        or checkpoint.get("saved_at")
        or job.get("startedAt"),
        "finishedAt": (
            report.get("finished_at") or checkpoint.get("finished_at")
            if terminal_state
            else None
        ),
        "durationSeconds": elapsed,
        "pressureTargetSeconds": target,
        "pressurePercent": report_pressure_percent(elapsed, target, state),
        "cycles": _nonnegative_int(snapshot.get("completed_cycles"), 0),
        "expectedCycles": max(0, _nonnegative_int(job.get("cycles"), 0)),
        "passedCycles": 0,
        "failedCycles": 0,
        "interruptedCycles": 0,
        "operations": _nonnegative_int(snapshot.get("operation_count"), 0),
        "crashes": 0,
        "stalls": 0,
        "fallbackRecoveries": 0,
        "stallRecoveryFailures": 0,
        "issues": 0,
        "status": status,
        "rawStatus": state,
        "resumeState": state,
        "fastbotAlive": snapshot.get("fastbot_alive"),
        "fastbotStartedAt": snapshot.get("fastbot_started_at"),
        "lastOperationAt": snapshot.get("last_operation_at"),
        "operationStalled": bool(snapshot.get("operation_stalled")),
        "stalledForSeconds": snapshot.get("stalled_for_seconds"),
        "resumeAttempts": _nonnegative_int(snapshot.get("resume_attempts"), 0),
        "remainingSeconds": 0.0
        if terminal_state
        else _nonnegative_float(snapshot.get("remaining_seconds"), 0.0),
        "lastHeartbeat": snapshot.get("last_heartbeat"),
        "currentCycle": current,
        "lastError": snapshot.get("last_error"),
        "artifacts": [
            "checkpoint.json"
            if snapshot.get("checkpoint_available")
            else ""
        ],
        "reportAvailable": False,
        "checkpointAvailable": True,
        "checkpointPath": str(snapshot.get("checkpoint_path") or ""),
        "cycleResults": cycle_results,
        "errors": [str(snapshot.get("last_error"))]
        if snapshot.get("last_error")
        else [],
        "foregroundComponent": snapshot.get("foreground_component"),
        "fastbotPid": checkpoint.get("fastbot_pid"),
        "remoteLogPath": snapshot.get("remote_log_path"),
        "remoteLogOffset": _nonnegative_int(snapshot.get("remote_log_offset"), 0),
        "apkFingerprint": snapshot.get("apk_fingerprint"),
        "canResume": _can_resume_device_state(
            state,
            resume_mode=_job_resume_mode(job, report),
        ),
        "canEnd": bool(
            str(job.get("status") or "") in ACTIVE_JOB_STATUSES
            and state not in TERMINAL_RESUME_STATES
        ),
        "manualEnd": state == MANUALLY_ENDED_STATE
        or bool(report.get("manual_end"))
        or bool(checkpoint.get("manual_end")),
    }


def report_detail(identifier: str) -> Dict[str, Any]:
    path = report_index().get(identifier)
    if path is None:
        raise ApiError("报告不存在", HTTPStatus.NOT_FOUND)
    report = _cached_json_document(path, None)
    if not isinstance(report, dict):
        raise ApiError("报告文件无法读取", HTTPStatus.UNPROCESSABLE_ENTITY)
    summary = _cached_report_summary(path) or {}
    summary["cyclesDetail"] = unique_cycle_rows(report)
    summary["cycleResults"] = normalized_cycles(report)
    summary["crashDetail"] = normalized_crashes(path, report)
    summary["issueDetail"] = normalized_issues(report)
    summary["recoveryDetail"] = normalized_stall_records(report)
    summary["errors"] = report.get("errors", [])
    return summary


def _job_snapshot(job_dir: Path) -> Dict[str, Any]:
    value = read_json_file(job_dir / "job.json", {})
    job = dict(value) if isinstance(value, Mapping) else {}
    job.setdefault("id", job_dir.name)
    job.setdefault("jobDir", str(job_dir))
    job.setdefault("reportRoot", str(job_dir / "reports"))
    job.setdefault("configPath", str(job_dir / "config.json"))
    job.setdefault("logPath", str(job_dir / "console.log"))
    with JOBS_LOCK:
        live = JOBS.get(str(job.get("id") or job_dir.name))
        if isinstance(live, Mapping):
            for key in ("status", "phase", "finishedAt", "exitCode", "elapsed", "evidence"):
                if key in live:
                    job[key] = live[key]
    return job


def _test_report_groups(limit: int = 200) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    try:
        job_paths = sorted(
            RUN_ROOT.glob("*/job.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        job_paths = []
    for job_path in job_paths:
        job_dir = job_path.parent.resolve(strict=False)
        key = "job:%s" % job_dir
        groups[key] = {
            "key": key,
            "jobDir": job_dir,
            "job": _job_snapshot(job_dir),
            "paths": [],
            "checkpointDevices": {},
            "sortEpoch": job_path.stat().st_mtime,
        }
    for path in report_paths(limit=2000):
        report = read_json_file(path, {})
        if not isinstance(report, Mapping):
            continue
        job_dir = report_job_directory(path)
        if job_dir is not None:
            key = "job:%s" % job_dir.resolve(strict=False)
            group = groups.setdefault(key, {
                "key": key,
                "jobDir": job_dir,
                "job": _job_snapshot(job_dir),
                "paths": [],
                "checkpointDevices": {},
                "sortEpoch": 0.0,
            })
        else:
            run_id = str(report.get("run_id") or path.parent.name)
            key = "run:%s" % run_id
            group = groups.setdefault(key, {
                "key": key,
                "jobDir": None,
                "job": {},
                "paths": [],
                "sortEpoch": 0.0,
            })
        group["paths"].append(path)
        try:
            group["sortEpoch"] = max(float(group["sortEpoch"]), path.stat().st_mtime)
        except OSError:
            pass
    # A checkpoint can be the only durable artifact during a report write or
    # after a host restart.  Keep it attached to the existing job group so the
    # report UI and resume endpoint expose the same device row.
    for group in groups.values():
        job = group.get("job", {})
        if not isinstance(job, Mapping):
            continue
        serials = job.get("devices", [])
        if not isinstance(serials, list):
            continue
        checkpoint_devices = group.setdefault("checkpointDevices", {})
        if not isinstance(checkpoint_devices, dict):
            checkpoint_devices = {}
            group["checkpointDevices"] = checkpoint_devices
        for serial_value in serials:
            serial = str(serial_value).strip()
            if not serial:
                continue
            snapshot = _device_resume_snapshot(serial, job)
            if not snapshot.get("checkpoint"):
                continue
            if snapshot.get("report_available"):
                continue
            checkpoint_path = snapshot.get("checkpoint_path")
            if isinstance(checkpoint_path, Path):
                checkpoint_devices[serial] = checkpoint_path
            elif checkpoint_path:
                checkpoint_devices[serial] = Path(str(checkpoint_path))
        try:
            for serial, checkpoint_path in checkpoint_devices.items():
                group["sortEpoch"] = max(
                    float(group.get("sortEpoch", 0.0)),
                    checkpoint_path.stat().st_mtime,
                )
        except (OSError, TypeError, ValueError):
            pass
    values = list(groups.values())
    values.sort(key=lambda item: float(item.get("sortEpoch", 0)), reverse=True)
    return values[:limit]


def _test_status(job_status: str, devices: List[Mapping[str, Any]], missing: int) -> str:
    statuses = [str(item.get("status") or "incomplete") for item in devices]
    # Preserve the device-state-machine phases in the batch response.  A
    # recovery state is actionable information, not a generic failed result.
    if job_status in {
        "recovering",
        "waiting_for_device",
        "resuming",
        "paused",
        "needs_manual_resume",
    }:
        if "running" not in statuses:
            return job_status
    if job_status in ACTIVE_JOB_STATUSES:
        return "running"
    for state in (
        "resuming",
        "recovering",
        "waiting_for_device",
        "needs_manual_resume",
        "paused",
    ):
        if state in statuses:
            return state
    if "running" in statuses:
        return "incomplete"
    if "failed" in statuses or job_status == "failed":
        return "failed"
    if missing:
        return "stopped" if job_status == "stopped" else "incomplete"
    if "stopped" in statuses or job_status == "stopped":
        return "stopped"
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "incomplete"


def test_report_summary(group: Mapping[str, Any]) -> Dict[str, Any]:
    job = group.get("job", {})
    if not isinstance(job, Mapping):
        job = {}
    paths = [path for path in group.get("paths", []) if isinstance(path, Path)]
    device_reports = [_cached_report_summary(path) for path in paths]
    devices = [dict(item) for item in device_reports if item]
    expected_serials = [
        str(item) for item in job.get("devices", []) if str(item).strip()
    ] if isinstance(job.get("devices"), list) else []
    if not expected_serials:
        expected_serials = [str(item.get("deviceSerial") or "") for item in devices]
    expected_serials = list(dict.fromkeys(item for item in expected_serials if item))
    try:
        expected_cycles_per_device = max(0, int(job.get("cycles", 0) or 0))
        target_per_device = max(0, int(float(job.get("duration", 0) or 0) * 60 * expected_cycles_per_device))
    except (TypeError, ValueError):
        expected_cycles_per_device = 0
        target_per_device = 0
    by_serial = {str(item.get("deviceSerial") or ""): item for item in devices}
    # Materialize checkpoint-only rows before calculating missing devices.  A
    # report may be absent for a short atomic-write window, but the checkpoint
    # is still authoritative evidence that this device belongs to the job.
    if isinstance(job, Mapping):
        for serial in expected_serials:
            if serial in by_serial:
                continue
            snapshot = _device_resume_snapshot(serial, job)
            if not snapshot.get("checkpoint"):
                continue
            row = _checkpoint_device_summary(job, serial, snapshot)
            devices.append(row)
            by_serial[serial] = row
    for item in devices:
        if not int(item.get("pressureTargetSeconds", 0) or 0) and target_per_device:
            item["pressureTargetSeconds"] = target_per_device
            item["pressurePercent"] = report_pressure_percent(
                int(item.get("durationSeconds", 0) or 0),
                target_per_device,
                str(item.get("rawStatus") or "unknown"),
            )
        # Checkpoint-only rows are intentionally distinct from a materialized
        # report. They carry durable progress, but must not inflate report
        # counts or cause aggregate statistics to double-count in-flight data.
        if "reportAvailable" not in item:
            item["reportAvailable"] = True
    missing_serials = [serial for serial in expected_serials if serial not in by_serial]
    job_status = str(job.get("status") or "unknown")
    for serial in missing_serials:
        devices.append({
            "id": "",
            "testId": str(job.get("id") or ""),
            "runId": "",
            "app": str(job.get("packageName") or ""),
            "packageName": str(job.get("packageName") or ""),
            "device": serial,
            "manufacturer": "",
            "model": "",
            "androidVersion": "",
            "sdk": "",
            "screenSize": "",
            "deviceSerial": serial,
            "startedAt": job.get("startedAt"),
            "finishedAt": job.get("finishedAt"),
            "durationSeconds": 0,
            "pressureTargetSeconds": target_per_device,
            "pressurePercent": 0.0,
            "cycles": 0,
            "expectedCycles": expected_cycles_per_device,
            "operations": 0,
            "crashes": 0,
            "stalls": 0,
            "fallbackRecoveries": 0,
            "stallRecoveryFailures": 0,
            "issues": 0,
            "status": "running" if job_status in ACTIVE_JOB_STATUSES else "stopped",
            "rawStatus": "missing",
            "artifacts": [],
            "reportAvailable": False,
            "canEnd": job_status in ACTIVE_JOB_STATUSES,
        })
    for item in devices:
        resume_state = str(item.get("resumeState") or "").strip().lower()
        item["canEnd"] = bool(
            job_status in ACTIVE_JOB_STATUSES
            and resume_state not in TERMINAL_RESUME_STATES
        )
    order = {serial: index for index, serial in enumerate(expected_serials)}
    devices.sort(key=lambda item: order.get(str(item.get("deviceSerial") or ""), len(order)))
    report_devices = [item for item in devices if item.get("reportAvailable")]
    status = _test_status(job_status, devices, len(missing_serials))
    # One device running a few seconds beyond its own deadline must not hide
    # missing pressure time on another device.  Credit each device only up to
    # its configured target before calculating the batch coverage.
    elapsed_seconds = sum(
        min(
            max(0, int(item.get("durationSeconds", 0) or 0)),
            int(item.get("pressureTargetSeconds", 0) or 0),
        )
        if int(item.get("pressureTargetSeconds", 0) or 0) > 0
        else max(0, int(item.get("durationSeconds", 0) or 0))
        for item in devices
    )
    target_seconds = sum(int(item.get("pressureTargetSeconds", 0) or 0) for item in devices)
    pressure_percent = report_pressure_percent(
        elapsed_seconds, target_seconds, "completed" if status == "passed" else status
    )
    started_values = [str(item.get("startedAt")) for item in devices if item.get("startedAt")]
    finished_values = [str(item.get("finishedAt")) for item in devices if item.get("finishedAt")]
    first = report_devices[0] if report_devices else {}
    test_id = str(job.get("id") or first.get("testId") or first.get("runId") or "unknown")
    return {
        "id": hashlib.sha256(str(group.get("key") or test_id).encode("utf-8")).hexdigest()[:20],
        "testId": test_id,
        "runId": str(first.get("runId") or ""),
        "app": str(job.get("apkName") or first.get("app") or job.get("packageName") or "未知应用"),
        "apkName": str(job.get("apkName") or ""),
        "packageName": str(job.get("packageName") or first.get("packageName") or ""),
        "startedAt": job.get("startedAt") or (min(started_values) if started_values else None),
        "finishedAt": job.get("finishedAt") or (max(finished_values) if finished_values and status not in {"running", "incomplete"} else None),
        "status": status,
        "rawStatus": job_status,
        "deviceCount": len(expected_serials) or len(devices),
        "reportedDevices": len(report_devices),
        "passedDevices": sum(1 for item in report_devices if item.get("status") == "passed"),
        "failedDevices": sum(1 for item in report_devices if item.get("status") == "failed"),
        "runningDevices": sum(
            1
            for item in devices
            if str(item.get("status") or "")
            in {"running", "recovering", "waiting_for_device", "resuming"}
        ),
        "stoppedDevices": sum(
            1
            for item in devices
            if str(item.get("status") or "") in {"stopped", "paused", "needs_manual_resume"}
        ),
        "pressureElapsedSeconds": elapsed_seconds,
        "pressureTargetSeconds": target_seconds,
        "pressurePercent": pressure_percent,
        "recordedCycles": sum(int(item.get("cycles", 0) or 0) for item in report_devices),
        "expectedCycles": expected_cycles_per_device * (len(expected_serials) or len(devices))
        if expected_cycles_per_device else sum(int(item.get("expectedCycles", 0) or 0) for item in report_devices),
        "operations": sum(int(item.get("operations", 0) or 0) for item in report_devices),
        "crashes": sum(int(item.get("crashes", 0) or 0) for item in report_devices),
        "stalls": sum(int(item.get("stalls", 0) or 0) for item in report_devices),
        "fallbackRecoveries": sum(
            int(item.get("fallbackRecoveries", 0) or 0) for item in report_devices
        ),
        "stallRecoveryFailures": sum(
            int(item.get("stallRecoveryFailures", 0) or 0)
            for item in report_devices
        ),
        "issues": sum(int(item.get("issues", 0) or 0) for item in report_devices),
        "resumeState": (
            status
            if status in {
                "recovering",
                "waiting_for_device",
                "resuming",
                "paused",
                "needs_manual_resume",
            }
            else None
        ),
        "resumeAttempts": max(
            [int(item.get("resumeAttempts", 0) or 0) for item in devices] or [0]
        ),
        "remainingSeconds": sum(
            _nonnegative_float(item.get("remainingSeconds"), 0.0) for item in devices
        ),
        "lastHeartbeat": max(
            [str(item.get("lastHeartbeat")) for item in devices if item.get("lastHeartbeat")]
            or [""],
        ) or None,
        "devices": devices,
    }


def test_report_index() -> Dict[str, Dict[str, Any]]:
    groups = _test_report_groups()
    return {test_report_summary(group)["id"]: group for group in groups}


def _test_report_group_aliases(group: Mapping[str, Any]) -> set[str]:
    """Return current and legacy identifiers for one test-report group."""
    aliases: set[str] = set()
    try:
        summary = test_report_summary(group)
    except (OSError, TypeError, ValueError):
        summary = {}
    for key in ("id", "testId", "runId"):
        value = str(summary.get(key) or "").strip()
        if value:
            aliases.add(value)

    # The first implementation used different prefixes for a report tree with
    # and without job.json.  Accept those hashes so an already-rendered delete
    # button remains valid while the directory is being reconciled.
    group_key = str(group.get("key") or "").strip()
    if group_key:
        aliases.add(hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:20])
    test_id = str(summary.get("testId") or "").strip()
    if test_id:
        aliases.add(test_id)
        aliases.add(
            hashlib.sha256(("run:%s" % test_id).encode("utf-8")).hexdigest()[:20]
        )

    job_dir = group.get("jobDir")
    if isinstance(job_dir, Path):
        try:
            resolved = job_dir.resolve(strict=False)
            aliases.add(resolved.name)
            aliases.add(
                hashlib.sha256(("job:%s" % resolved).encode("utf-8")).hexdigest()[:20]
            )
        except (OSError, ValueError):
            pass
    for path in group.get("paths", []):
        if isinstance(path, Path):
            aliases.add(report_id(path))
            report = read_json_file(path, {})
            if isinstance(report, Mapping):
                # Very old rows were keyed by the report's run_id rather than
                # the containing batch directory.  Keep that identifier valid
                # when a report was moved or its metadata was reconstructed.
                run_id = str(
                    report.get("run_id") or report.get("runId") or ""
                ).strip()
                if run_id:
                    aliases.add(run_id)
                    aliases.add(
                        hashlib.sha256(
                            ("run:%s" % run_id).encode("utf-8")
                        ).hexdigest()[:20]
                    )
    return aliases


def _find_test_report_group(identifier: str) -> Optional[Dict[str, Any]]:
    """Resolve a report group by its API id, batch id, or legacy id."""
    normalized = unquote(str(identifier or "")).strip()
    if not normalized:
        return None
    index = test_report_index()
    group = index.get(normalized)
    if group is not None:
        return group
    for candidate in index.values():
        if not isinstance(candidate, Mapping):
            continue
        if normalized in _test_report_group_aliases(candidate):
            return dict(candidate)
    return None


def _group_has_active_report_artifact(group: Mapping[str, Any]) -> bool:
    """Detect an in-flight legacy report when job.json is unavailable."""
    for path in group.get("paths", []):
        if not isinstance(path, Path):
            continue
        report = read_json_file(path, {})
        if not isinstance(report, Mapping):
            continue
        sources: List[Mapping[str, Any]] = [report]
        checkpoint = read_json_file(path.parent / "checkpoint.json", {})
        if isinstance(checkpoint, Mapping):
            sources.append(checkpoint)
        for source in sources:
            state = str(
                source.get("resume_state")
                or source.get("resumeState")
                or source.get("status")
                or ""
            ).strip().lower()
            if state in ACTIVE_JOB_STATUSES:
                return True
    return False


def test_report_detail(identifier: str) -> Dict[str, Any]:
    group = _find_test_report_group(identifier)
    if group is None:
        raise ApiError("测试报告不存在", HTTPStatus.NOT_FOUND)
    summary = test_report_summary(group)
    job = group.get("job", {})
    if not isinstance(job, Mapping):
        job = {}
    details = {
        str(item.get("deviceSerial") or ""): item
        for item in (
            device_report_detail(path, job)
            for path in group.get("paths", [])
            if isinstance(path, Path)
        )
    }
    checkpoint_devices = group.get("checkpointDevices", {})
    if isinstance(checkpoint_devices, Mapping):
        for serial_value in checkpoint_devices:
            serial = str(serial_value)
            if serial in details:
                continue
            snapshot = _device_resume_snapshot(serial, job)
            if snapshot.get("checkpoint"):
                details[serial] = _checkpoint_device_summary(job, serial, snapshot)
    summary["devices"] = [
        details.get(str(item.get("deviceSerial") or ""), item)
        for item in summary.get("devices", [])
    ]
    evidence = job.get("evidence", {})
    summary["errors"] = (
        [str(item) for item in evidence.get("errors", []) if str(item).strip()]
        if isinstance(evidence, Mapping) and isinstance(evidence.get("errors"), list)
        else []
    )
    return summary


def list_test_reports() -> List[Dict[str, Any]]:
    return [test_report_summary(group) for group in _test_report_groups()]


def delete_test_report(identifier: str) -> Dict[str, Any]:
    group = _find_test_report_group(identifier)
    if group is None:
        raise ApiError("测试报告不存在", HTTPStatus.NOT_FOUND)

    job_dir_value = group.get("jobDir")
    if isinstance(job_dir_value, str) and job_dir_value.strip():
        job_dir_value = Path(job_dir_value)
    if not isinstance(job_dir_value, Path):
        raise ApiError("该报告不属于测试运行目录，不能在此删除", HTTPStatus.CONFLICT)

    run_root = RUN_ROOT.resolve(strict=False)
    job_dir = job_dir_value.resolve(strict=False)
    try:
        relative = job_dir.relative_to(run_root)
    except ValueError as exc:
        raise ApiError("拒绝删除运行目录之外的文件", HTTPStatus.FORBIDDEN) from exc
    if len(relative.parts) != 1 or job_dir.parent != run_root:
        raise ApiError("拒绝删除非测试批次目录", HTTPStatus.FORBIDDEN)

    job = group.get("job", {})
    if not isinstance(job, Mapping):
        job = {}
    test_id = str(job.get("id") or job_dir.name).strip()
    if not test_id or test_id != job_dir.name:
        raise ApiError("测试编号与运行目录不一致，拒绝删除", HTTPStatus.CONFLICT)

    disk_job = read_json_file(job_dir / "job.json", {})
    disk_status = (
        str(disk_job.get("status") or "") if isinstance(disk_job, Mapping) else ""
    )
    legacy_artifacts_active = _group_has_active_report_artifact(group)
    with JOBS_LOCK:
        live_job = JOBS.get(test_id)
        live_status = (
            str(live_job.get("status") or "")
            if isinstance(live_job, Mapping)
            else ""
        )
        if (
            live_status in ACTIVE_JOB_STATUSES
            or disk_status in ACTIVE_JOB_STATUSES
            or legacy_artifacts_active
        ):
            raise ApiError("测试仍在运行，停止并完整收尾后才能删除报告", HTTPStatus.CONFLICT)
        if not job_dir.is_dir():
            raise ApiError("测试报告不存在", HTTPStatus.NOT_FOUND)
        try:
            shutil.rmtree(job_dir)
        except OSError as exc:
            raise ApiError(
                "删除测试报告失败：%s" % exc,
                HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc
        JOBS.pop(test_id, None)

    return {"deleted": True, "id": identifier, "testId": test_id}


def replay_report_crash(identifier: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    path = report_index().get(identifier)
    if path is None:
        raise ApiError("报告不存在", HTTPStatus.NOT_FOUND)
    report = _cached_json_document(path, None)
    if not isinstance(report, dict):
        raise ApiError("报告文件无法读取", HTTPStatus.UNPROCESSABLE_ENTITY)
    try:
        crash_index = int(payload.get("crashIndex", -1))
    except (TypeError, ValueError) as exc:
        raise ApiError("crashIndex 必须是整数") from exc
    crashes = _confirmed_report_crashes(report)
    if crash_index < 0 or crash_index >= len(crashes):
        raise ApiError("崩溃记录不存在", HTTPStatus.NOT_FOUND)
    crash = crashes[crash_index]
    if not isinstance(crash, Mapping):
        raise ApiError("崩溃记录无法读取", HTTPStatus.UNPROCESSABLE_ENTITY)
    requested_start_mode = str(
        payload.get("startMode", payload.get("start_mode", "auto")) or "auto"
    ).strip().lower()
    if requested_start_mode not in {"auto", "current"}:
        raise ApiError(
            "startMode 必须是 auto 或 current",
            HTTPStatus.BAD_REQUEST,
        )
    trajectory_path, trajectory = _crash_trajectory(path, report, crash)
    events = trajectory.get("events", []) if isinstance(trajectory, Mapping) else []
    if not isinstance(events, list) or not events:
        raise ApiError(
            "没有可回放的结构化轨迹：未找到该崩溃阶段的结构化动作；FBM 是 Fastbot 的二进制模型，不是回放脚本。",
            HTTPStatus.CONFLICT,
        )
    serial = str(report.get("device_serial") or "").strip()
    package_name = str(report.get("package_name") or "").strip()
    if not serial or not PACKAGE_PATTERN.fullmatch(package_name):
        raise ApiError("报告缺少有效的设备或包名", HTTPStatus.UNPROCESSABLE_ENTITY)
    with JOBS_LOCK:
        active_jobs = [
            str(job.get("id") or "") for job in JOBS.values()
            if str(job.get("status") or "") in ACTIVE_JOB_STATUSES
            and serial in [str(item) for item in job.get("devices", [])]
        ]
    if active_jobs:
        raise ApiError("设备正在执行任务 %s，不能同时复现" % active_jobs[0], HTTPStatus.CONFLICT)
    states = DeviceManager.get_device_states()
    if str(states.get(serial, {}).get("adb_state") or "") != "device":
        raise ApiError("原测试设备当前未连接或未授权：%s" % serial, HTTPStatus.CONFLICT)
    if not _package_installed(serial, package_name):
        raise ApiError("原测试 APK 当前未安装在设备上：%s" % serial, HTTPStatus.CONFLICT)
    with REPLAY_LOCK:
        if serial in REPLAY_DEVICES:
            raise ApiError("该设备已有崩溃复现正在执行", HTTPStatus.CONFLICT)
        REPLAY_DEVICES.add(serial)
    started_at = utc_now()
    progress_path = _replay_progress_path(path, serial, crash_index)
    total_events = len(events)
    replay_plan = (
        dict(trajectory.get("replay_plan", {}))
        if isinstance(trajectory, Mapping)
        and isinstance(trajectory.get("replay_plan"), Mapping)
        else {}
    )
    replay_context = {
        "startMode": requested_start_mode,
        "trajectoryPath": str(trajectory_path or ""),
        "eventCount": total_events,
        "replayPlan": replay_plan,
    }

    def update_progress(status: str, **values: Any) -> None:
        snapshot: Dict[str, Any] = {
            "status": status,
            "executed": int(values.pop("executed", 0) or 0),
            "total": total_events,
            "skipped": int(values.pop("skipped", 0) or 0),
            "updatedAt": utc_now(),
            "reportId": identifier,
            "crashIndex": crash_index,
            **replay_context,
        }
        snapshot.update(values)
        _write_replay_progress(progress_path, snapshot)

    def fail_replay_preparation(
        detail: Any,
        *,
        stage: str,
        replay_start: Optional[Mapping[str, Any]] = None,
        preparation: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Report an actionable setup failure without misclassifying it as 500."""
        reason = str(detail or "未知原因").strip() or "未知原因"
        message = (
            "无法%s：%s。请先在设备上打开保存的页面，再点击“从当前页复现”。"
            % (stage, reason)
        )
        values: Dict[str, Any] = {"message": message, "error": reason}
        if replay_start is not None:
            values["replayStart"] = dict(replay_start)
        if preparation is not None:
            values["preparation"] = dict(preparation)
        update_progress("failed", **values)
        raise ApiError(message, HTTPStatus.CONFLICT)

    update_progress(
        "preparing",
        message=(
            "正在恢复崩溃前页面"
            if requested_start_mode == "auto"
            else "请保持设备上的目标页面不变"
        ),
    )
    try:
        report_config = report.get("config", {})
        if not isinstance(report_config, Mapping):
            report_config = {}
        # CrashHandler historically received only crash_detection options.
        # Keep those keys at the top level while also passing crash_replay so
        # the bounded timeout and page-restore policy apply to manual runs.
        handler_options: Dict[str, Any] = dict(report_config)
        detection_options = report_config.get("crash_detection", {})
        if isinstance(detection_options, Mapping):
            handler_options.update(detection_options)
        handler = CrashHandler(serial, package_name, handler_options)
        handler.adb_path = DeviceManager.adb_path
        start_result: Dict[str, Any] = {}
        start_preparer = getattr(handler, "prepare_replay_start", None)
        try:
            if callable(start_preparer):
                raw_start_result = start_preparer(
                    trajectory,
                    package_name,
                    requested_start_mode,
                )
                if isinstance(raw_start_result, Mapping):
                    start_result = dict(raw_start_result)
            if not start_result:
                # Compatibility with an older CrashHandler implementation.  This
                # is a soft launcher activation only; it deliberately never calls
                # force-stop, so a missing newer helper cannot destroy task state.
                launch = DeviceManager._run_adb(
                    [
                        "shell",
                        "monkey",
                        "-p",
                        package_name,
                        "-c",
                        "android.intent.category.LAUNCHER",
                        "1",
                    ],
                    serial=serial,
                    timeout=30,
                    retries=1,
                )
                # A lightweight foreground probe is useful for old integrations
                # that do not expose prepare_replay_start, and keeps the fallback
                # from claiming success solely because `monkey` accepted input.
                verify = DeviceManager._run_adb(
                    ["shell", "dumpsys", "activity", "activities"],
                    serial=serial,
                    timeout=10,
                    retries=1,
                )
                launch_code = getattr(launch, "returncode", 0)
                verify_code = getattr(verify, "returncode", 0)
                launch_ok = not isinstance(launch_code, int) or launch_code == 0
                verify_ok = not isinstance(verify_code, int) or verify_code == 0
                start_result = {
                    "success": launch_ok and verify_ok,
                    "mode": requested_start_mode,
                    "strategy": "legacy_soft_launcher_start",
                    "task_preserved": True,
                    "reason": "legacy replay start helper unavailable",
                }
                if not launch_ok:
                    start_result["error"] = (
                        launch.stderr or launch.stdout or "unable to activate target task"
                    ).strip()
                elif not verify_ok:
                    start_result["error"] = (
                        getattr(verify, "stderr", "")
                        or getattr(verify, "stdout", "")
                        or "unable to verify target task"
                    ).strip()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            prior_start = getattr(handler, "last_replay_start", {})
            start_result = dict(prior_start) if isinstance(prior_start, Mapping) else {}
            start_result.update(
                {
                    "success": False,
                    "mode": requested_start_mode,
                    "strategy": str(
                        start_result.get("strategy")
                        or "replay_start_preparation_error"
                    ),
                    "error": str(exc),
                }
            )
            replay_context["replayStart"] = start_result
            fail_replay_preparation(
                exc,
                stage="恢复崩溃前页面",
                replay_start=start_result,
            )
        replay_context["replayStart"] = start_result
        update_progress(
            "preparing",
            executed=0,
            message=str(
                start_result.get("reason")
                or start_result.get("error")
                or "页面起点准备完成"
            ),
            replayStart=start_result,
        )
        if start_result.get("success") is False:
            fail_replay_preparation(
                start_result.get("error") or "崩溃前页面无法恢复",
                stage="恢复崩溃前页面",
                replay_start=start_result,
            )

        # Startup gates are handled only in automatic mode.  In current-page
        # mode the operator has explicitly positioned the nested Fragment, so
        # probing and tapping a gate here could move away from the intended
        # reproduction page.
        if requested_start_mode == "auto":
            gate_preparer = getattr(handler, "prepare_replay_state", None)
            if callable(gate_preparer):
                try:
                    preparation = gate_preparer(package_name, report_config)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    preparation = {
                        "success": False,
                        "strategy": "replay_gate_preparation_error",
                        "error": str(exc),
                    }
                    replay_context["preparation"] = preparation
                    fail_replay_preparation(
                        exc,
                        stage="完成启动审核/隐私门禁",
                        replay_start=start_result,
                        preparation=preparation,
                    )
                if isinstance(preparation, Mapping):
                    preparation = dict(preparation)
                    replay_context["preparation"] = preparation
                    update_progress(
                        "preparing",
                        executed=0,
                        message=str(
                            preparation.get("reason") or "门禁准备完成"
                        ),
                        preparation=dict(preparation),
                    )
                    if preparation.get("success") is False:
                        fail_replay_preparation(
                            preparation.get("error") or "启动审核/隐私门禁未完成",
                            stage="完成启动审核/隐私门禁",
                            replay_start=start_result,
                            preparation=preparation,
                        )
        def progress_callback(value: Mapping[str, Any]) -> None:
            update_progress(
                str(value.get("status") or "running"),
                executed=int(value.get("executed", 0) or 0),
                skipped=int(value.get("skipped", 0) or 0),
                message=str(value.get("message") or "正在执行崩溃前事件"),
                foregroundRecoveryCount=int(
                    value.get("foregroundRecoveryCount", 0) or 0
                ),
                foregroundComponent=str(value.get("foregroundComponent") or ""),
            )
        try:
            replayed = handler.replay_trajectory(
                dict(trajectory), package_name, progress_callback=progress_callback
            )
        except TypeError as exc:
            # Keep compatibility with older test doubles and integrations that
            # still expose the two-argument replay method.
            if "progress_callback" not in str(exc) and "unexpected keyword" not in str(exc):
                raise
            replayed = handler.replay_trajectory(dict(trajectory), package_name)
        replay_result = dict(handler.last_replay_result)
        same_crash = replayed and handler.verify_crash_reproducible(
            str(crash.get("signature") or ""), replay_result
        )
        result = {
            "id": uuid.uuid4().hex,
            "reportId": identifier,
            "crashIndex": crash_index,
            "crashSignature": str(crash.get("signature") or ""),
            "deviceSerial": serial,
            "startedAt": started_at,
            "finishedAt": utc_now(),
            "status": "reproduced" if same_crash else "not_reproduced" if replayed else "failed",
            "executed": bool(replay_result.get("executed", replayed)),
            "eventCount": int(replay_result.get("event_count", 0) or 0),
            "expectedSignature": str(crash.get("signature") or ""),
            "actualSignature": str(replay_result.get("signature") or ""),
            "sameCrash": bool(same_crash),
            "error": str(replay_result.get("reason") or ""),
            "failedEventId": replay_result.get("failed_event_id"),
            "failedEvent": replay_result.get("failed_event"),
            "skippedEventCount": int(
                replay_result.get("skipped_event_count", 0) or 0
            ),
            "skippedEvents": replay_result.get("skipped_events", []),
            "transport": str(replay_result.get("transport") or ""),
            "foregroundRecoveryCount": int(
                replay_result.get("foreground_recovery_count", 0) or 0
            ),
            "elapsedSeconds": replay_result.get("elapsed_seconds"),
            "startMode": requested_start_mode,
            "replayPlan": replay_result.get("replay_plan", replay_plan),
            "replayStart": replay_result.get(
                "replay_start", replay_context.get("replayStart", start_result)
            ),
            "trajectoryPath": str(trajectory_path or ""),
        }
        history_path = path.parent / "manual_replays.json"
        history = read_json_file(history_path, [])
        if not isinstance(history, list):
            history = []
        history.append(result)
        write_json_file(history_path, history[-100:])
        update_progress(
            "completed" if replayed else "failed",
            executed=result["eventCount"],
            skipped=result["skippedEventCount"],
            message=(
                "已复现相同崩溃"
                if result["sameCrash"]
                else "事件执行完成"
                if replayed
                else result["error"] or "回放执行失败"
            ),
            sameCrash=result["sameCrash"],
            foregroundRecoveryCount=result["foregroundRecoveryCount"],
        )
        return result
    except ApiError:
        raise
    except Exception as exc:
        update_progress("failed", message=str(exc), error=str(exc))
        raise ApiError("崩溃复现执行失败：%s" % exc, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
    finally:
        with REPLAY_LOCK:
            REPLAY_DEVICES.discard(serial)


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent stale console instances from sharing the same port on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "FastbotConsole/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:5173")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        super().end_headers()

    def send_internal_error(self, exc: Exception) -> None:
        sys.stderr.write("%s %s %s\n" % (utc_now(), self.command, self.path))
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        sys.stderr.flush()
        self.send_json(
            {"error": "本地服务异常：%s" % exc},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/health":
                self.send_json(runtime_health())
            elif path == "/api/config":
                self.send_json(default_ui_config())
            elif path == "/api/configs":
                self.send_json({"configs": available_configs()})
            elif path == "/api/draft":
                self.send_json(read_draft())
            elif path == "/api/devices":
                self.send_json({"devices": list_devices(), "refreshedAt": utc_now()})
            elif path == "/api/runs":
                query = parse_qs(parsed.query)
                self.send_json({"runs": list_jobs(include_log=query.get("log") == ["1"])})
            elif re.fullmatch(r"/api/runs/[^/]+", path):
                identifier = unquote(path.rsplit("/", 1)[1])
                with JOBS_LOCK:
                    job = JOBS.get(identifier)
                if job is None:
                    raise ApiError("任务不存在", HTTPStatus.NOT_FOUND)
                refresh_job(job)
                self.send_json(public_job(job, include_log=True))
            elif path == "/api/reports":
                summaries = [_cached_report_summary(item) for item in report_paths()]
                self.send_json({"reports": [item for item in summaries if item]})
            elif path == "/api/test-reports":
                self.send_json({"tests": list_test_reports()})
            elif re.fullmatch(r"/api/test-reports/[^/]+", path):
                self.send_json(test_report_detail(unquote(path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/api/reports/[^/]+", path):
                self.send_json(report_detail(unquote(path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/api/reports/[^/]+/artifact", path):
                identifier = unquote(path.split("/")[3])
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.send_report_artifact(identifier, name)
            elif re.fullmatch(r"/api/reports/[^/]+/replay-progress", path):
                identifier = unquote(path.split("/")[3])
                query = parse_qs(parsed.query)
                crash_index = query.get("crashIndex", ["-1"])[0]
                self.send_json(
                    replay_report_progress(identifier, {"crashIndex": crash_index})
                )
            else:
                if path.startswith("/api/"):
                    raise ApiError("接口不存在", HTTPStatus.NOT_FOUND)
                self.send_static(path)
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.send_internal_error(exc)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/apks":
                self.receive_apk()
            elif path == "/api/runs":
                created = start_run(self.read_json())
                audit_event(
                    "start_run",
                    job_id=created.get("id"),
                    devices=created.get("devices"),
                    package=created.get("packageName"),
                    duration=created.get("duration"),
                )
                self.send_json(created, HTTPStatus.CREATED)
            elif path == "/api/runs/preflight":
                result = preflight_run(self.read_json())
                self.send_json(result)
            elif path == "/api/devices/stop":
                payload = self.read_json()
                serials = payload.get("devices", [])
                if not isinstance(serials, list):
                    raise ApiError("devices 必须是数组")
                results = stop_devices(serials)
                audit_event("stop_devices", devices=serials, results=results)
                self.send_json({"results": results})
            elif path == "/api/devices/sync-fastbot":
                payload = self.read_json()
                serials = payload.get("devices", [])
                if not isinstance(serials, list):
                    raise ApiError("devices 必须是数组")
                result = sync_fastbot(serials)
                self.send_json(result)
            elif re.fullmatch(r"/api/runs/[^/]+/stop", path):
                identifier = unquote(path.split("/")[3])
                results = stop_job(identifier)
                audit_event("stop_job", job_id=identifier, results=results)
                self.send_json({"results": results})
            elif re.fullmatch(r"/api/runs/[^/]+/confirm-install", path):
                identifier = unquote(path.split("/")[3])
                confirmed = confirm_job_install(identifier)
                audit_event("confirm_install", job_id=identifier)
                self.send_json(confirmed)
            elif re.fullmatch(r"/api/jobs/[^/]+/devices/[^/]+/resume", path):
                # Resume is deliberately scoped to one device and the
                # original job id.  The helper rejects a second in-flight
                # request with 409 so duplicate UI clicks cannot create two
                # workers for the same serial.
                parts = path.split("/")
                identifier = unquote(parts[3])
                serial = unquote(parts[5])
                resumed = resume_job_device(identifier, serial)
                audit_event("resume_device", job_id=identifier, device=serial)
                self.send_json(resumed)
            elif re.fullmatch(r"/api/jobs/[^/]+/devices/[^/]+/end", path):
                # Ending is also scoped to one device.  It writes an
                # immutable terminal marker and releases only that device's
                # claim, so sibling workers in a shared invocation continue.
                parts = path.split("/")
                identifier = unquote(parts[3])
                serial = unquote(parts[5])
                ended = end_job_device(identifier, serial)
                audit_event("end_device", job_id=identifier, device=serial)
                self.send_json(ended)
            elif re.fullmatch(r"/api/reports/[^/]+/replay", path):
                identifier = unquote(path.split("/")[3])
                replayed = replay_report_crash(identifier, self.read_json())
                audit_event("replay_crash", report=identifier)
                self.send_json(replayed)
            else:
                raise ApiError("接口不存在", HTTPStatus.NOT_FOUND)
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.send_internal_error(exc)

    def do_PUT(self) -> None:
        try:
            if urlparse(self.path).path != "/api/draft":
                raise ApiError("Not found", HTTPStatus.NOT_FOUND)
            self.send_json(save_draft(self.read_json()))
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.send_internal_error(exc)

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path
            if not re.fullmatch(r"/api/test-reports/[^/]+", path):
                raise ApiError("接口不存在", HTTPStatus.NOT_FOUND)
            identifier = unquote(path.rsplit("/", 1)[1])
            deleted = delete_test_report(identifier)
            audit_event("delete_report", report=identifier)
            self.send_json(deleted)
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.send_internal_error(exc)

    def read_json(self) -> Dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise ApiError("请求必须使用 application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError("无效的 Content-Length") from exc
        if length <= 0 or length > MAX_JSON_BYTES:
            raise ApiError("请求内容为空或过大", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("请求 JSON 无法解析") from exc
        if not isinstance(value, dict):
            raise ApiError("请求 JSON 必须是对象")
        return value

    def receive_apk(self) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/vnd.android.package-archive":
            raise ApiError("请选择本地 APK 文件", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        filename = safe_name(unquote(self.headers.get("X-Filename", "")), "upload.apk")
        if not filename.lower().endswith(".apk"):
            raise ApiError("文件扩展名必须是 .apk")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError("无效的文件大小") from exc
        if length <= 0 or length > MAX_APK_BYTES:
            raise ApiError("APK 文件为空或超过 4GB", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        identifier = "%s-%s" % (uuid.uuid4().hex[:12], filename)
        destination = UPLOAD_ROOT / identifier
        remaining = length
        digest = hashlib.sha256()
        try:
            with destination.open("wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError("APK 上传中断")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        package_name, package_error = apk_package_name(destination)
        if not package_name:
            destination.unlink(missing_ok=True)
            raise ApiError(package_error, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.send_json(
            {
                "name": filename,
                "path": str(destination.resolve()),
                "size": length,
                "sha256": digest.hexdigest(),
                "packageName": package_name,
            },
            HTTPStatus.CREATED,
        )

    def send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_report_artifact(self, identifier: str, name: str) -> None:
        path = report_index().get(identifier)
        if path is None:
            raise ApiError("报告不存在", HTTPStatus.NOT_FOUND)
        report = read_json_file(path, {})
        artifacts = report_artifacts(path, report)
        crash_artifacts, _aliases = report_crash_artifacts(path, report)
        artifacts.update(crash_artifacts)
        artifact = artifacts.get(name)
        if artifact is None or not artifact.is_file():
            raise ApiError("报告产物不存在", HTTPStatus.NOT_FOUND)
        inline = name in {"html", "screen_record"} or artifact.suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".webp", ".gif",
        }
        self.send_file(artifact, download=not inline)

    def send_static(self, request_path: str) -> None:
        if not DIST_ROOT.is_dir():
            raise ApiError("前端尚未构建，请先运行 npm run build", HTTPStatus.SERVICE_UNAVAILABLE)
        relative = unquote(request_path).lstrip("/") or "index.html"
        candidate = (DIST_ROOT / relative).resolve(strict=False)
        try:
            candidate.relative_to(DIST_ROOT.resolve())
        except ValueError as exc:
            raise ApiError("无效路径", HTTPStatus.FORBIDDEN) from exc
        if not candidate.is_file():
            candidate = DIST_ROOT / "index.html"
        self.send_file(candidate, download=False)

    def send_file(self, path: Path, download: bool) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe_name(path.name, "download"))
        self.end_headers()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def _persisted_resume_settings(config_path: Path) -> Dict[str, Any]:
    value = read_json_file(config_path, {})
    resume = value.get("resume", {}) if isinstance(value, Mapping) else {}
    options = dict(resume) if isinstance(resume, Mapping) else {}
    options.setdefault("enabled", True)
    options.setdefault("mode", "auto")
    options.setdefault("max_attempts", 3)
    try:
        options["max_attempts"] = max(0, min(20, int(options["max_attempts"])))
    except (TypeError, ValueError):
        options["max_attempts"] = 3
    options["enabled"] = _ui_bool(options["enabled"], True)
    options["mode"] = (
        "manual"
        if str(options["mode"] or "").strip().lower() == "manual"
        else "auto"
    )
    return options


def _persisted_job_device_states(job: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    states: Dict[str, Dict[str, Any]] = {}
    manual_stop_devices = {
        str(value)
        for value in (job.get("manualStopDevices", []) or [])
        if str(value).strip()
    }
    serials = job.get("devices", [])
    if not isinstance(serials, list):
        serials = []
    for serial_value in serials:
        serial = str(serial_value)
        report_path, checkpoint = _job_device_artifacts(job, serial)
        path = report_path or (
            _expected_device_report_path(job, serial)
            if checkpoint is not None
            else None
        )
        report = (
            read_json_file(report_path, {})
            if report_path is not None and report_path.is_file()
            else {}
        )
        report = report if isinstance(report, Mapping) else {}
        has_artifact = report_path is not None or checkpoint is not None
        state = _effective_resume_state(report, checkpoint, default="")
        # ``manually_ended`` is a distinct immutable per-device terminal
        # marker.  Do not run it through the legacy ``stopped``/manual-pause
        # compatibility conversion below, otherwise a refresh would make the
        # device resumable again.
        manually_ended = state == MANUALLY_ENDED_STATE
        if serial in manual_stop_devices and not manually_ended and state not in {
            "completed",
            "passed",
            "completed_with_crashes",
            "completed_with_errors",
        }:
            # A user stop is a per-device manual gate, even when a sibling
            # worker in the same CLI invocation is still running.
            state = "paused"
        if state in {"running", "resuming"}:
            # A host restart makes an old live worker an interrupted worker;
            # preserve its checkpoint and let the normal auto queue continue.
            state = "recovering"
        if not has_artifact or state in {"", "unknown"}:
            # Installation/queued jobs have no device checkpoint yet.  They
            # require an explicit operator action after a service restart and
            # must never be mistaken for interrupted pressure workers.
            state = "needs_manual_resume"
        if not manually_ended and state in {"failed", "interrupted", "stopped"}:
            decision = classify_recovery(
                {
                    **dict(report),
                    **(
                        dict(checkpoint)
                        if isinstance(checkpoint, Mapping)
                        else {}
                    ),
                }
            )
            if decision.get("automatic"):
                state = str(decision.get("state") or "recovering")
            elif state in {"interrupted", "stopped"}:
                state = "paused"
        if state not in {
            "completed",
            "passed",
            "completed_with_crashes",
            "completed_with_errors",
            "failed",
            MANUALLY_ENDED_STATE,
            "paused",
            "needs_manual_resume",
            "recovering",
            "waiting_for_device",
        }:
            decision = classify_recovery(report)
            state = str(decision.get("state") or "recovering")
        states[serial] = {
            "state": state,
            "report": dict(report),
            "checkpoint": checkpoint,
            "report_path": path,
            "has_artifact": has_artifact,
            "attempts": max(
                _nonnegative_int(report.get("resume_attempts"), 0),
                _nonnegative_int((checkpoint or {}).get("resume_attempts"), 0),
            ),
            "history": [
                item
                for source in (report, checkpoint or {})
                for key in ("retry_history", "resume_history", "interruptions")
                for item in (
                    source.get(key, [])
                    if isinstance(source.get(key, []), list)
                    else []
                )
                if isinstance(item, Mapping)
            ],
        }
    return states


def _pid_is_alive(pid: Any) -> bool:
    """Best-effort liveness check for a legacy persisted CLI PID."""
    try:
        value = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if value <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a harmless probe on Windows: signal 0 is
        # treated as a console control event and can interrupt the web service
        # itself. Query the process handle instead.
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

            process_query_limited_information = 0x1000
            error_access_denied = 5
            handle = open_process(
                process_query_limited_information,
                0,
                value,
            )
            if not handle:
                # Access denied still proves that a process exists. Invalid or
                # stale persisted PIDs return false and remain safe to replace.
                return ctypes.get_last_error() == error_access_denied
            try:
                exit_code = ctypes.c_uint32()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                close_handle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(value, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError, SystemError, ValueError, OverflowError):
        # On Windows an invalid/stale process handle can surface as WinError
        # 87, and CPython has versions that wrap the same failure in
        # ``SystemError``.  A persisted PID is only a duplicate-worker guard;
        # never let a malformed old value prevent the web service from booting.
        return False
    return True


def load_persisted_jobs() -> None:
    """Restore job metadata and, when allowed, restart the same run id.

    This function is called explicitly from ``main`` after the module is
    imported.  No subprocess is started during import, which keeps tests and
    embedding applications side-effect free.
    """
    for path in RUN_ROOT.glob("*/job.json"):
        job = read_json_file(path, None)
        if not isinstance(job, dict) or not job.get("id"):
            continue
        job_id = str(job["id"])
        job["jobDir"] = str(path.parent)
        job["reportRoot"] = str(path.parent / "reports")
        job["logPath"] = str(path.parent / "console.log")
        job["configPath"] = str(path.parent / "config.json")
        # Migrate legacy snapshots that only stored the web id.  The reports
        # themselves may have been written under a distinct execution id;
        # preserve that id so every subsequent resume targets the same tree.
        execution_run_id = _job_execution_run_id(job)
        if execution_run_id:
            job["runId"] = execution_run_id
        try:
            started = datetime.fromisoformat(str(job.get("startedAt")))
            job["startedEpoch"] = started.timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            try:
                job["startedEpoch"] = path.stat().st_mtime
            except OSError:
                job["startedEpoch"] = time.time()
        job["process"] = None
        job["_processes"] = {}
        job["_processDevices"] = {}
        original_status = str(job.get("status") or "unknown")
        installation = job.get("installation")
        installation_status = (
            str(installation.get("status") or "").strip().lower()
            if isinstance(installation, Mapping)
            else ""
        )
        whole_job_stop = bool(
            job.get("manualStopRequested")
            or job.get("stopRequestedAt")
            or original_status in {"stopped", "stopping"}
            or installation_status == "cancelled"
        )
        if original_status == "awaiting_install":
            if whole_job_stop:
                # A cancelled install is terminal until the operator starts a
                # new run.  Do not reopen package installers or allow a later
                # refresh to launch the CLI from this snapshot.
                job["status"] = "stopped"
                job["phase"] = "stopped"
                job["manualStopRequested"] = True
                job["finishedAt"] = job.get("finishedAt") or utc_now()
            else:
                # Package installation is intentionally interactive.  Keep
                # the gate visible after a service restart, but never repeat
                # adb install until a new explicit user action requests it.
                job["status"] = "awaiting_install"
                job["phase"] = "awaiting_install"
                job["finishedAt"] = None
            with JOBS_LOCK:
                JOBS[job_id] = job
            if not whole_job_stop:
                try:
                    job["logStream"] = open_job_log(Path(str(job["logPath"])))
                except OSError:
                    job["logStream"] = None
            persist_job(job)
            continue

        if original_status in {"completed", "failed", "stopped", "stopping"} or whole_job_stop:
            # Terminal and explicitly stopped snapshots are metadata only.
            # Device reports can contain an older recovering heartbeat, but
            # that must not override the job-level stop/terminal decision.
            if whole_job_stop and original_status not in {
                "completed",
                "failed",
                "stopped",
            }:
                job["status"] = "stopped"
                job["phase"] = "stopped"
                job["finishedAt"] = job.get("finishedAt") or utc_now()
                job["stopDeadlineEpoch"] = None
                _finalize_interrupted_reports(job, terminal_stop=True)
            with JOBS_LOCK:
                JOBS[job_id] = job
            persist_job(job)
            continue

        # ``_resumeClaims`` is intentionally reconstructed only for jobs that
        # are eligible to remain active.  Loading a terminal/manual job must be
        # side-effect free and cannot retire or recreate worker ownership.
        _hydrate_resume_claims(job)

        device_states = _persisted_job_device_states(job)
        pending_manual_request = any(
            _pending_manual_resume_request(job, serial) is not None
            for serial in device_states
        )
        manual_stop_devices = {
            str(value)
            for value in (job.get("manualStopDevices", []) or [])
            if str(value).strip()
        }
        manual_stop = bool(
            job.get("manualStopRequested")
            or (
                job.get("stopRequestedAt")
                and not manual_stop_devices
            )
            or (
                original_status in {"stopped", "stopping"}
                and not manual_stop_devices
            )
        )
        manually_ended = any(
            value.get("state") == MANUALLY_ENDED_STATE
            for value in device_states.values()
        )
        all_terminal = all(
            value.get("state") in TERMINAL_RESUME_STATES
            for value in device_states.values()
        ) and bool(device_states)
        completed = all(
            value.get("state")
            in {
                "completed",
                "passed",
                "completed_with_crashes",
                "completed_with_errors",
            }
            for value in device_states.values()
        ) and bool(device_states)
        if manually_ended and all_terminal:
            # The per-device marker is the durable source of truth if the
            # service stopped after writing it but before updating job.json.
            # Never reopen that fully terminal batch as a manual-resume job.
            job["status"] = "stopped"
            job["phase"] = "stopped"
            job["finishedAt"] = job.get("finishedAt") or utc_now()
            job["evidence"] = _cached_job_evidence(job)
            with JOBS_LOCK:
                JOBS[job_id] = job
            persist_job(job)
            continue
        elif completed and not manual_stop:
            job["status"] = "completed"
            job["phase"] = "completed"
            job["finishedAt"] = job.get("finishedAt") or utc_now()
            job["evidence"] = _cached_job_evidence(job)
        else:
            manual_serials = [
                serial
                for serial, value in device_states.items()
                if value.get("state") in MANUAL_RECOVERY_STATES
            ]
            manual_serials.extend(
                serial
                for serial in manual_stop_devices
                if serial not in manual_serials
            )
            automatic_serials = [
                serial
                for serial, value in device_states.items()
                if value.get("has_artifact")
                and (
                    value.get("state") in AUTOMATIC_RECOVERY_STATES
                    or value.get("state") == "recovering"
                )
            ]
            waiting_serials = {
                serial
                for serial, value in device_states.items()
                if value.get("state") == "waiting_for_device"
            }
            live_claim_serials = _live_resume_claim_serials(job)
            automatic_serials = [
                serial for serial in automatic_serials if serial not in live_claim_serials
            ]
            legacy_process_alive = bool(
                not live_claim_serials
                and _pid_is_alive(job.get("pid"))
                and original_status in ACTIVE_JOB_STATUSES
            )
            if legacy_process_alive:
                # Older job snapshots did not have per-device claim files. A
                # live persisted CLI PID is treated as owning all configured
                # devices until it exits, preventing a duplicate invocation.
                automatic_serials = []
                job["_legacyProcessAlive"] = True
            config_path = Path(str(job["configPath"]))
            resume = _persisted_resume_settings(config_path)
            max_attempts = int(resume.get("max_attempts", 3) or 0)
            exhausted = [
                serial
                for serial in automatic_serials
                if int(device_states.get(serial, {}).get("attempts", 0) or 0) >= max_attempts
            ]
            automatic_serials = [serial for serial in automatic_serials if serial not in exhausted]
            manual_serials.extend(exhausted)
            # A persisted reconnect deadline must remain authoritative across
            # web-service restarts.  Expired waits become a manual gate instead
            # of receiving a fresh automatic retry budget.
            now = time.time()
            eligible_automatic: List[str] = []
            for serial in automatic_serials:
                item = device_states.get(serial, {})
                if item.get("state") == "waiting_for_device":
                    report = item.get("report") if isinstance(item.get("report"), Mapping) else {}
                    checkpoint = item.get("checkpoint") if isinstance(item.get("checkpoint"), Mapping) else {}
                    wait_deadline = _retry_due_epoch(
                        report.get("wait_deadline_at")
                        or checkpoint.get("wait_deadline_at")
                    )
                    if wait_deadline is None:
                        wait_started = _retry_due_epoch(
                            report.get("wait_started_at")
                            or checkpoint.get("wait_started_at")
                        )
                        if wait_started is not None:
                            try:
                                wait_seconds = max(
                                    1.0,
                                    float(resume.get("wait_for_device_seconds", 1800) or 1800),
                                )
                            except (TypeError, ValueError, OverflowError):
                                wait_seconds = 1800.0
                            wait_deadline = wait_started + wait_seconds
                    if wait_deadline is not None and wait_deadline <= now:
                        _persist_job_device_resume_state(
                            job,
                            serial,
                            "needs_manual_resume",
                            "wait_for_device_timeout: persisted reconnect deadline elapsed",
                        )
                        waiting_serials.discard(serial)
                        manual_serials.append(serial)
                        continue
                eligible_automatic.append(serial)
            automatic_serials = eligible_automatic
            # The old invocation may have exited without releasing its remote
            # controllers. Verify each candidate before starting a replacement
            # after a service restart; sibling devices remain independent.
            cleaned_automatic: List[str] = []
            if pending_manual_request:
                # The shared dispatcher handles both the explicit target and
                # any automatic siblings under one claim/cleanup pass below.
                cleaned_automatic = list(automatic_serials)
            else:
                for serial in automatic_serials:
                    cleanup_status = _best_effort_remote_worker_cleanup_status(serial)
                    if cleanup_status == _REMOTE_CLEANUP_RELEASED:
                        cleaned_automatic.append(serial)
                    elif cleanup_status == _REMOTE_CLEANUP_DEVICE_UNAVAILABLE:
                        _persist_job_device_resume_state(
                            job,
                            serial,
                            "waiting_for_device",
                            "device transport is unavailable before automatic resume",
                        )
                        waiting_serials.add(serial)
                    else:
                        _persist_job_device_resume_state(
                            job,
                            serial,
                            "needs_manual_resume",
                            "previous Fastbot/uiautomator2 control could not be verified released",
                        )
                        waiting_serials.discard(serial)
                        manual_serials.append(serial)
            automatic_serials = cleaned_automatic
            device_level_pause = bool(
                original_status == "paused"
                and manual_stop_devices
                and not job.get("manualStopRequested")
            )
            if device_level_pause and not automatic_serials and not waiting_serials:
                # A per-device stop is intentionally resumable. Preserve the
                # paused batch state across a service restart until the
                # operator explicitly resumes that device.
                job["status"] = "paused"
                job["phase"] = "paused"
                job["finishedAt"] = None
            elif manual_stop:
                job["status"] = "paused"
                job["phase"] = "paused"
                job["finishedAt"] = None
            elif live_claim_serials or legacy_process_alive:
                # The old worker is still observable through its durable claim
                # (or legacy PID). Keep the job active and let refresh_job pick
                # it up once the owner exits.
                job["status"] = (
                    "running"
                    if any(value.get("state") == "running" for value in device_states.values())
                    else "resuming"
                )
                job["phase"] = job["status"]
                job["finishedAt"] = None
            elif automatic_serials and resume.get("enabled") and resume.get("mode") == "auto":
                # One manually paused device must not hold back recoverable
                # siblings from the same job. The device cards carry the
                # per-serial manual state while the job remains observable as
                # resuming for the automatic subset.
                job["status"] = "resuming"
                job["phase"] = "resuming"
                job["finishedAt"] = None
            elif automatic_serials:
                # Manual mode suppresses the background dispatch, but the
                # device remains recoverable and must stay visible with an
                # actionable resume button.
                job["status"] = (
                    "waiting_for_device" if waiting_serials else "recovering"
                )
                job["phase"] = job["status"]
                job["finishedAt"] = None
            elif waiting_serials:
                # Do not collapse an offline device into a manual failure just
                # because no worker can be started during service boot.
                job["status"] = "waiting_for_device"
                job["phase"] = "waiting_for_device"
                job["finishedAt"] = None
            elif manual_serials:
                job["status"] = "needs_manual_resume"
                job["phase"] = "needs_manual_resume"
                job["finishedAt"] = None
            else:
                job["status"] = "needs_manual_resume"
                job["phase"] = "needs_manual_resume"
                job["finishedAt"] = None
            job["evidence"] = _cached_job_evidence(job)
            with JOBS_LOCK:
                JOBS[job_id] = job
            if (
                not manual_stop
                and (
                    pending_manual_request
                    or (
                        automatic_serials
                        and resume.get("enabled")
                        and resume.get("mode") == "auto"
                    )
                )
            ):
                try:
                    if pending_manual_request:
                        _maybe_dispatch_persisted_recovery(job)
                    else:
                        _launch_job_cli(job, devices=automatic_serials)
                except Exception as exc:
                    # Keep an offline sibling in its reconnect state if the
                    # other device's launch fails.  Only a verified cleanup or
                    # launch failure for that serial should create a manual
                    # gate; one device must not hide another's wait window.
                    evidence = _cached_job_evidence(job)
                    evidence_states = {
                        str(item.get("resumeState") or "")
                        for item in evidence.get("reports", {}).values()
                        if isinstance(item, Mapping)
                    }
                    state = (
                        "waiting_for_device"
                        if waiting_serials or "waiting_for_device" in evidence_states
                        else "needs_manual_resume"
                    )
                    job["status"] = state
                    job["phase"] = state
                    job["finishedAt"] = None
                    job["lastError"] = str(exc)
                    job["evidence"] = evidence
        with JOBS_LOCK:
            JOBS[job_id] = job
        persist_job(job)


def apply_startup_retention() -> None:
    """Trim expired runs and uploads before the job registry reads the disk.

    Long pressure campaigns leave tens of gigabytes behind; cleaning before the
    first scan keeps the console's startup and polling work proportional to the
    data that is actually still relevant.
    """
    try:
        from core.retention import (
            apply_cleanup,
            list_run_directories,
            plan_run_cleanup,
            plan_upload_cleanup,
        )
    except Exception:
        return
    try:
        max_age_days = int(os.environ.get("APP_PRESS_TEST_RETENTION_DAYS", "14"))
        max_total_gb = float(os.environ.get("APP_PRESS_TEST_RETENTION_GB", "60"))
        run_dirs = list_run_directories(RUN_ROOT)

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        protected = {path.name for path in sorted(run_dirs, key=_mtime, reverse=True)[:5]}
        active_states = {
            "running",
            "preparing",
            "installing",
            "stopping",
            "paused",
            "recovering",
            "waiting_for_device",
            "resuming",
            "needs_manual_resume",
        }
        referenced_apks = set()
        for directory in run_dirs:
            job_path = directory / "job.json"
            if not job_path.is_file():
                continue
            try:
                snapshot = read_json_file(job_path, {})
            except Exception:
                continue
            if not isinstance(snapshot, Mapping):
                continue
            if str(snapshot.get("status") or "") in active_states:
                protected.add(directory.name)
            config_path = directory / "config.json"
            if config_path.is_file():
                try:
                    run_config = read_json_file(config_path, {})
                except Exception:
                    run_config = {}
                app = run_config.get("app") if isinstance(run_config, Mapping) else None
                apk = app.get("apk_path") if isinstance(app, Mapping) else None
                if apk:
                    referenced_apks.add(str(Path(str(apk)).resolve(strict=False)))

        run_plan = plan_run_cleanup(
            RUN_ROOT,
            protected_ids=protected,
            keep_crash_runs=True,
            max_age_days=max_age_days,
            max_total_gb=max_total_gb,
        )
        upload_plan = plan_upload_cleanup(
            UPLOAD_ROOT,
            keep_paths=referenced_apks,
            max_age_days=30,
        )
        removed = apply_cleanup(list(run_plan) + list(upload_plan))
        if removed:
            sys.stderr.write(
                "retention: removed %d item(s) (%d runs, %d uploads)\n"
                % (removed, len(run_plan), len(upload_plan))
            )
    except Exception:
        sys.stderr.write("retention: cleanup skipped after an error\n")


def warm_polling_caches() -> None:
    """Pre-compute the polled views so the first page load is not a cold wait.

    The caches are keyed on file identity, so this only pays off once per
    service start; afterwards each poll recomputes just the files that changed.
    """
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        try:
            _cached_job_evidence(job)
            public_job(job, include_log=False)
        except Exception:
            sys.stderr.write(
                "console cache warm failed for %s\n" % job.get("id")
            )


def start_cache_warmer() -> None:
    thread = threading.Thread(
        target=warm_polling_caches, name="console-cache-warmer", daemon=True
    )
    thread.start()


_ADB_WATCHDOG_INTERVAL = 10.0


def _adb_server_answers(timeout: float = 15.0) -> bool:
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _adb_device_count(timeout: float = 15.0) -> int:
    """Return the number of connected devices, or -1 when adb does not answer."""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if result.returncode != 0:
        return -1
    return sum(1 for line in result.stdout.splitlines() if "\tdevice" in line)


def _restart_adb_server() -> bool:
    try:
        subprocess.run(
            ["adb", "start-server"], capture_output=True, text=True, timeout=45
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(15):
        time.sleep(2)
        if _adb_server_answers():
            return True
    return False


def _adb_watchdog_loop() -> None:
    """Restart the ADB server when it dies so a crash is not a full outage.

    The local ``adb.exe`` occasionally dies with an illegal-instruction fault
    (0xc000001d).  Every device transport goes with it, which then looks like
    devices randomly disconnecting.  Restarting the server restores enumeration
    without touching the running jobs.  A sudden drop from "devices present" to
    "zero devices" is also repaired with ``adb reconnect`` before the workers
    notice.
    """
    failures = 0
    last_count = _adb_device_count()
    while True:
        time.sleep(_ADB_WATCHDOG_INTERVAL)
        count = _adb_device_count()
        if count >= 0:
            failures = 0
            if last_count >= 1 and count == 0:
                try:
                    subprocess.run(
                        ["adb", "reconnect"], capture_output=True, text=True, timeout=30
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
                time.sleep(3)
                recovered = _adb_device_count()
                audit_event("adb_reconnect", devices=recovered)
                sys.stderr.write(
                    "adb watchdog: device count dropped to 0; reconnect -> %d\n"
                    % recovered
                )
                count = recovered
            last_count = count
            continue
        failures += 1
        if failures < 2:
            continue
        if _restart_adb_server():
            recovered = _adb_device_count()
            audit_event("adb_server_restart", reason="watchdog", devices=recovered)
            sys.stderr.write(
                "adb watchdog: server was down; restarted it (%d devices)\n" % recovered
            )
            last_count = recovered
            failures = 0
        else:
            sys.stderr.write("adb watchdog: server restart failed\n")


def start_adb_watchdog() -> None:
    thread = threading.Thread(
        target=_adb_watchdog_loop, name="adb-watchdog", daemon=True
    )
    thread.start()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fastbot local web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding a non-loopback address; the console has no auth",
    )
    args = parser.parse_args(argv)
    loopback = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if args.host not in loopback and not args.allow_remote:
        # The console can start/stop pressure runs and read every report. It is
        # unauthenticated by design because it only binds loopback; exposing it
        # to the LAN must be an explicit, conscious decision.
        parser.error(
            "--host %s exposes the unauthenticated console beyond this machine; "
            "pass --allow-remote to confirm" % args.host
        )
    apply_startup_retention()
    load_persisted_jobs()
    start_cache_warmer()
    start_adb_watchdog()
    audit_event("console_start", host=args.host, port=args.port)
    server = ExclusiveThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    print("App 测试平台: http://%s:%s/" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
