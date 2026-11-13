"""Configuration loading, validation, and local-path safety checks."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .fastbot_assets import normalize_runtime_options, runtime_asset_md5, runtime_remote_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"
# A Fastbot worker owns several ADB transports and native processes. Keep the
# host-side concurrency bounded while allowing the supported nine-device rig
# to run all devices at the same time.
MAX_SAFE_WORKERS = 9
_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")
_URL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SENSITIVE_KEYS = {"password", "secret", "token", "access_key", "api_key"}


class ConfigurationError(ValueError):
    """Raised when a settings file cannot be used safely."""


def _mapping(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError("%s must be a JSON object" % name)
    return value


def _integer(
    mapping: Dict[str, Any], key: str, default: int, minimum: int
) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(
            "%s must be an integer greater than or equal to %s" % (key, minimum)
        )
    mapping[key] = value
    return value


def _boolean(mapping: Dict[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError("%s must be true or false" % key)
    mapping[key] = value
    return value


def _number(
    mapping: Dict[str, Any], key: str, default: float, minimum: float
) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError("%s must be a number" % key)
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ConfigurationError("%s must be finite" % key)
    if normalized < minimum:
        raise ConfigurationError(
            "%s must be greater than or equal to %s" % (key, minimum)
        )
    mapping[key] = normalized
    return normalized


def resolve_local_apk_path(
    value: str,
    base_dir: Optional[Path] = None,
    require_exists: bool = False,
) -> Path:
    """Resolve a local APK path while rejecting URLs and network shares."""
    raw = str(value).strip()
    if not raw:
        raise ConfigurationError("app.apk_path must not be empty")
    if "\x00" in raw or _URL_PATTERN.match(raw):
        raise ConfigurationError("app.apk_path must be a local filesystem path")
    if raw.startswith(("\\\\", "//")):
        raise ConfigurationError("network-share APK paths are not allowed")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base_dir or PROJECT_ROOT) / path
    path = path.resolve(strict=False)
    resolved_text = str(path)
    if resolved_text.startswith(("\\\\", "//")):
        raise ConfigurationError("network-share APK paths are not allowed")
    if path.suffix.lower() != ".apk":
        raise ConfigurationError("app.apk_path must point to an .apk file")
    if require_exists and not path.is_file():
        raise ConfigurationError("local APK file does not exist: %s" % path)
    return path


def resolve_project_path(value: str) -> Path:
    """Resolve an output path relative to the configured output directory."""
    raw = str(value).strip()
    if not raw or "\x00" in raw or _URL_PATTERN.match(raw):
        raise ConfigurationError("output paths must be local filesystem paths")
    if raw.startswith(("\\\\", "//")):
        raise ConfigurationError("network-share output paths are not allowed")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        output_root = Path(
            os.environ.get(
                "APP_PRESS_TEST_OUTPUT_ROOT",
                r"E:\app_press_test" if os.name == "nt" else str(PROJECT_ROOT),
            )
        ).expanduser()
        if not output_root.is_absolute():
            output_root = PROJECT_ROOT / output_root
        path = output_root / path
    path = path.resolve(strict=False)
    if str(path).startswith(("\\\\", "//")):
        raise ConfigurationError("network-share output paths are not allowed")
    # Pressure artifacts must never consume the system drive.  Keep the
    # configured relative layout while failing over from E: to D: when E:
    # is nearly full.
    drive = path.drive.upper()
    if drive == "E:" and shutil.disk_usage("E:\\").free < 5 * 1024**3:
        relative = Path(str(path)[3:].lstrip("\\/"))
        fallback = Path("D:/") / relative
        if fallback.drive.upper() != "D:" or not Path("D:/").exists():
            raise ConfigurationError("E: is low on space and D: is unavailable")
        path = fallback.resolve(strict=False)
    return path


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and normalize a JSON settings file without touching any device."""
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    config_path = config_path.resolve(strict=False)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError("configuration file does not exist: %s" % config_path) from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "invalid JSON in %s at line %s column %s"
            % (config_path, exc.lineno, exc.colno)
        ) from exc
    except OSError as exc:
        raise ConfigurationError("unable to read configuration: %s" % exc) from exc

    config = copy.deepcopy(_mapping(raw, "settings"))
    # Tab-specific coordinate/Activity automation was removed.  Ignore the
    # legacy field so old project JSON files remain loadable without bringing
    # the page-operation controller back into a runtime configuration.
    config.pop("tab_rotation", None)
    app = _mapping(config.get("app"), "app")
    package_name = str(app.get("package_name", "")).strip()
    if not _PACKAGE_PATTERN.fullmatch(package_name):
        raise ConfigurationError("app.package_name is not a valid Android package name")
    app["package_name"] = package_name
    app["apk_path"] = str(
        resolve_local_apk_path(
            str(app.get("apk_path", "")), base_dir=config_path.parent
        )
    )

    test = _mapping(config.setdefault("test", {}), "test")
    _integer(test, "fastbot_duration_minutes", 30, 1)
    _integer(test, "fastbot_throttle_ms", 500, 0)
    _integer(test, "max_cycles", 1, 1)
    _integer(test, "crash_replay_attempts", 3, 0)
    _integer(test, "fastbot_timeout_grace_seconds", 60, 1)
    _boolean(test, "clear_app_data_each_cycle", True)
    clear_by_device = test.setdefault("clear_app_data_each_cycle_by_device", {})
    if not isinstance(clear_by_device, dict):
        raise ConfigurationError(
            "test.clear_app_data_each_cycle_by_device must be a JSON object"
        )
    for serial, enabled in clear_by_device.items():
        if not str(serial).strip() or not isinstance(enabled, bool):
            raise ConfigurationError(
                "test.clear_app_data_each_cycle_by_device values must be true or false"
            )
    _boolean(test, "continue_without_data_clear_on_failure", False)
    _boolean(test, "stop_on_crash", False)
    # Preparation can fail before any pressure event when an ad or a vendor
    # prompt hides the startup gate; such a cycle is retried from a clean reset.
    preparation_retries = _integer(test, "preparation_retries", 3, 0)
    if preparation_retries > 10:
        raise ConfigurationError(
            "test.preparation_retries must be less than or equal to 10"
        )
    # Fastbot opens video ads at random; a rack of phones must stay quiet.
    _boolean(test, "keep_device_silent", True)
    # Touch overlays help recording/reproduction but add system windows that
    # Fastbot's input guard counts; default off and explicitly cleared per run.
    _boolean(test, "touch_overlay", False)
    # Stability runs start fully permissioned; the explicit list covers
    # permissions that ``pm install -g`` did not grant, and the appops map
    # covers special permissions that are not runtime grants at all.
    grant_permissions = test.get("grant_permissions", [])
    if not isinstance(grant_permissions, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in grant_permissions
    ):
        raise ConfigurationError(
            "test.grant_permissions must be a list of permission strings"
        )
    appops_permissions = test.get("appops_permissions", {})
    if not isinstance(appops_permissions, dict):
        raise ConfigurationError(
            "test.appops_permissions must be a JSON object mapping op names to "
            "allow/deny/ignore/default"
        )
    for op, mode in appops_permissions.items():
        if not isinstance(op, str) or not op.strip():
            raise ConfigurationError(
                "test.appops_permissions keys must be non-empty op names"
            )
        if not isinstance(mode, str) or mode.strip().lower() not in (
            "allow",
            "deny",
            "ignore",
            "default",
        ):
            raise ConfigurationError(
                "test.appops_permissions.%s must be allow, deny, ignore or default"
                % op
            )
    _boolean(test, "disable_package_verifier", False)
    permission_settle = _integer(
        test, "permission_dialog_settle_seconds", 2, 0
    )
    if permission_settle > 10:
        raise ConfigurationError(
            "permission_dialog_settle_seconds must be less than or equal to 10"
        )

    devices = config.setdefault("devices", [{"serial": "auto", "alias": "auto"}])
    if not isinstance(devices, list) or not devices:
        raise ConfigurationError("devices must be a non-empty JSON array")
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ConfigurationError("devices[%s] must be a JSON object" % index)
        serial = str(device.get("serial", "")).strip()
        if not serial:
            raise ConfigurationError("devices[%s].serial must not be empty" % index)
        device["serial"] = serial
        device["alias"] = str(device.get("alias", serial))

    logging_config = _mapping(config.setdefault("logging", {}), "logging")
    logging_config.setdefault("level", "INFO")
    logging_config.setdefault("log_dir", "./logs")
    if not isinstance(logging_config["log_dir"], str):
        raise ConfigurationError("logging.log_dir must be a path string")
    resolve_project_path(logging_config["log_dir"])

    screenshot = _mapping(config.setdefault("screenshot", {}), "screenshot")
    screenshot.setdefault("enabled", True)
    if not isinstance(screenshot["enabled"], bool):
        raise ConfigurationError("screenshot.enabled must be true or false")
    _integer(screenshot, "interval_seconds", 10, 1)

    screen_recording = _mapping(
        config.setdefault("screen_recording", {}), "screen_recording"
    )
    if _boolean(screen_recording, "enabled", False):
        raise ConfigurationError(
            "screen_recording.enabled must remain false because screen recording "
            "is disabled"
        )

    stall_watchdog = _mapping(
        config.setdefault("stall_watchdog", {}), "stall_watchdog"
    )
    watchdog_enabled = _boolean(stall_watchdog, "enabled", True)
    stall_watchdog.setdefault(
        "mode", "recover_once" if watchdog_enabled else "observe"
    )
    if stall_watchdog["mode"] not in {"observe", "recover_once", "recover"}:
        raise ConfigurationError(
            "stall_watchdog.mode must be 'observe', 'recover_once', or 'recover'"
        )
    sample_interval = _integer(
        stall_watchdog, "sample_interval_seconds", 10, 5
    )
    if sample_interval > 60:
        raise ConfigurationError(
            "stall_watchdog.sample_interval_seconds must be less than or equal to 60"
        )
    unchanged_seconds = _integer(
        stall_watchdog, "unchanged_seconds", 150, 30
    )
    if unchanged_seconds < sample_interval * 3:
        raise ConfigurationError(
            "stall_watchdog.unchanged_seconds must cover at least three samples"
        )
    startup_grace = _integer(
        stall_watchdog, "startup_grace_seconds", 30, 0
    )
    if startup_grace > 600:
        raise ConfigurationError(
            "stall_watchdog.startup_grace_seconds must be less than or equal to 600"
        )
    max_hash_distance = _integer(
        stall_watchdog, "max_hash_distance", 6, 0
    )
    if max_hash_distance > 64:
        raise ConfigurationError(
            "stall_watchdog.max_hash_distance must be less than or equal to 64"
        )
    max_probe_failures = _integer(
        stall_watchdog, "max_probe_failures", 3, 1
    )
    if max_probe_failures > 20:
        raise ConfigurationError(
            "stall_watchdog.max_probe_failures must be less than or equal to 20"
        )
    if stall_watchdog["mode"] in {"recover_once", "recover"}:
        default_recoveries = 1 if stall_watchdog["mode"] == "recover_once" else 3
        max_recoveries = _integer(
            stall_watchdog,
            "max_recoveries_per_cycle",
            default_recoveries,
            1,
        )
        if max_recoveries > 20:
            raise ConfigurationError(
                "stall_watchdog.max_recoveries_per_cycle must be less than or equal to 20"
            )

    paths = _mapping(config.setdefault("paths", {}), "paths")
    paths.setdefault("reports_dir", "./reports")
    if not isinstance(paths["reports_dir"], str):
        raise ConfigurationError("paths.reports_dir must be a path string")
    resolve_project_path(paths["reports_dir"])

    parallel = _mapping(config.setdefault("parallel", {}), "parallel")
    parallel["max_workers"] = min(
        MAX_SAFE_WORKERS, _integer(parallel, "max_workers", 4, 1)
    )

    resume = _mapping(config.setdefault("resume", {}), "resume")
    _boolean(resume, "enabled", True)
    resume.setdefault("mode", "auto")
    if resume["mode"] not in {"auto", "manual"}:
        raise ConfigurationError("resume.mode must be 'auto' or 'manual'")
    max_resume_attempts = _integer(resume, "max_attempts", 3, 0)
    if max_resume_attempts > 20:
        raise ConfigurationError(
            "resume.max_attempts must be less than or equal to 20"
        )
    checkpoint_interval = _integer(
        resume, "checkpoint_interval_seconds", 10, 5
    )
    if checkpoint_interval > 10:
        raise ConfigurationError(
            "resume.checkpoint_interval_seconds must be between 5 and 10"
        )
    wait_for_device = _integer(
        resume, "wait_for_device_seconds", 1800, 1
    )
    if wait_for_device > 86400:
        raise ConfigurationError(
            "resume.wait_for_device_seconds must be less than or equal to 86400"
        )
    _boolean(resume, "resume_partial_cycle", True)
    _boolean(resume, "clear_app_data_on_resume", False)
    _boolean(resume, "reinstall_on_resume", False)

    fastbot = _mapping(config.setdefault("fastbot", {}), "fastbot")
    fastbot.setdefault("engine", "fastbot")
    fastbot.setdefault(
        "classpath",
        runtime_remote_path(),
    )
    normalize_runtime_options(fastbot)
    fastbot.setdefault("expected_monkeyq_md5", runtime_asset_md5())
    fastbot.setdefault("main_class", "com.android.commands.monkey.Monkey")
    fastbot.setdefault("extra_args", [])
    event_percentages = _mapping(
        fastbot.setdefault(
            "event_percentages",
            {"touch": 70, "motion": 25, "pinchzoom": 5},
        ),
        "fastbot.event_percentages",
    )
    if not isinstance(fastbot["engine"], str):
        raise ConfigurationError("fastbot.engine must be a string")
    if fastbot["engine"] not in {"fastbot", "system-monkey"}:
        raise ConfigurationError(
            "fastbot.engine must be either 'fastbot' or 'system-monkey'"
        )
    if not isinstance(fastbot["classpath"], str) or not fastbot["classpath"]:
        raise ConfigurationError("fastbot.classpath must be a non-empty string")
    if not isinstance(fastbot["main_class"], str) or not fastbot["main_class"]:
        raise ConfigurationError("fastbot.main_class must be a non-empty string")
    classpath_entries = fastbot["classpath"].split(":")
    if any(
        not re.fullmatch(r"/(?:sdcard|data/local/tmp)/[A-Za-z0-9_./-]+", entry)
        or ".." in Path(entry).parts
        for entry in classpath_entries
    ):
        raise ConfigurationError(
            "fastbot.classpath may contain only device-local files under "
            "/sdcard or /data/local/tmp"
        )
    if not re.fullmatch(
        r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)*[A-Za-z_$][A-Za-z0-9_$]*",
        fastbot["main_class"],
    ):
        raise ConfigurationError("fastbot.main_class is not a valid Java class name")
    if not isinstance(fastbot["extra_args"], list):
        raise ConfigurationError("fastbot.extra_args must be a JSON array")
    expected_monkeyq_md5 = fastbot.get("expected_monkeyq_md5")
    if expected_monkeyq_md5 is not None:
        if (
            not isinstance(expected_monkeyq_md5, str)
            or not re.fullmatch(r"[A-Fa-f0-9]{32}", expected_monkeyq_md5)
        ):
            raise ConfigurationError(
                "fastbot.expected_monkeyq_md5 must be a 32-character MD5 value"
            )
        fastbot["expected_monkeyq_md5"] = expected_monkeyq_md5.lower()
    expected_native_md5 = fastbot.get("expected_fastbot_native_md5")
    if expected_native_md5 is not None:
        if (
            not isinstance(expected_native_md5, str)
            or not re.fullmatch(r"[A-Fa-f0-9]{32}", expected_native_md5)
        ):
            raise ConfigurationError(
                "fastbot.expected_fastbot_native_md5 must be a 32-character MD5 value"
            )
        fastbot["expected_fastbot_native_md5"] = expected_native_md5.lower()
    allowed_event_names = {"touch", "motion", "pinchzoom"}
    unknown_event_names = set(event_percentages).difference(allowed_event_names)
    if unknown_event_names:
        raise ConfigurationError(
            "fastbot.event_percentages contains unsupported events: %s"
            % ", ".join(sorted(unknown_event_names))
        )
    for event_name, default in (("touch", 70), ("motion", 25), ("pinchzoom", 5)):
        percentage = _integer(event_percentages, event_name, default, 0)
        if percentage > 100:
            raise ConfigurationError(
                "fastbot.event_percentages.%s must be less than or equal to 100"
                % event_name
            )
    if sum(event_percentages.values()) != 100:
        raise ConfigurationError(
            "fastbot.event_percentages values must add up to 100"
        )
    blocked_package_options = {
        "-p",
        "--pkg",
        "--package",
        "--target-package",
        "--component",
    }
    for value in fastbot["extra_args"]:
        option_name = str(value).split("=", 1)[0]
        if option_name in blocked_package_options:
            raise ConfigurationError(
                "fastbot.extra_args cannot override the configured target package"
            )

    diagnostics = _mapping(config.setdefault("diagnostics", {}), "diagnostics")
    diagnostics.setdefault("logcat_mode", "full")
    if diagnostics["logcat_mode"] not in {"full", "crash_only"}:
        raise ConfigurationError(
            "diagnostics.logcat_mode must be 'full' or 'crash_only'"
        )

    process_monitor = _mapping(
        config.setdefault("process_monitor", {}), "process_monitor"
    )
    _boolean(process_monitor, "enabled", True)
    process_interval = _integer(
        process_monitor, "sample_interval_seconds", 5, 1
    )
    if process_interval > 60:
        raise ConfigurationError(
            "process_monitor.sample_interval_seconds must be less than or equal to 60"
        )
    missing_confirmations = _integer(
        process_monitor, "missing_confirmations", 2, 1
    )
    if missing_confirmations > 10:
        raise ConfigurationError(
            "process_monitor.missing_confirmations must be less than or equal to 10"
        )
    disconnect_confirmations = _integer(
        process_monitor, "disconnect_confirmations", 2, 1
    )
    if disconnect_confirmations > 10:
        raise ConfigurationError(
            "process_monitor.disconnect_confirmations must be less than or equal to 10"
        )
    monitor_grace = _integer(
        process_monitor, "startup_grace_seconds", 5, 0
    )
    if monitor_grace > 120:
        raise ConfigurationError(
            "process_monitor.startup_grace_seconds must be less than or equal to 120"
        )
    _boolean(process_monitor, "foreground_probe_enabled", True)

    crash_detection = _mapping(
        config.setdefault("crash_detection", {}), "crash_detection"
    )
    crash_detection.setdefault(
        "crash_page_activities",
        ["com.ahzy.common.module.mine.crash.AhzyCrashActivity"],
    )
    crash_detection.setdefault("crash_page_activity_patterns", [])
    crash_detection.setdefault("crash_page_text_patterns", [])
    crash_detection.setdefault("crash_page_resource_ids", [])
    for key in (
        "crash_page_activities",
        "crash_page_activity_patterns",
        "crash_page_text_patterns",
        "crash_page_resource_ids",
    ):
        values = crash_detection[key]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ConfigurationError(
                "crash_detection.%s must be an array of non-empty strings" % key
            )
        crash_detection[key] = [value.strip() for value in values]
    for pattern in crash_detection["crash_page_activity_patterns"] + crash_detection[
        "crash_page_text_patterns"
    ]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigurationError(
                "invalid crash-detection regular expression %r: %s" % (pattern, exc)
            ) from exc

    crash_replay = _mapping(
        config.setdefault("crash_replay", {}), "crash_replay"
    )
    replay_window = _integer(crash_replay, "window_seconds", 120, 10)
    if replay_window > 900:
        raise ConfigurationError(
            "crash_replay.window_seconds must be less than or equal to 900"
        )
    replay_events = _integer(crash_replay, "max_events", 400, 1)
    if replay_events > 5000:
        raise ConfigurationError(
            "crash_replay.max_events must be less than or equal to 5000"
        )
    restore_timeout = _integer(
        crash_replay, "activity_restore_timeout_seconds", 8, 1
    )
    if restore_timeout > 30:
        raise ConfigurationError(
            "crash_replay.activity_restore_timeout_seconds must be less than or equal to 30"
        )
    _boolean(crash_replay, "require_page_anchor", True)
    _boolean(crash_replay, "allow_cold_activity_start", False)

    crash_recovery = _mapping(
        config.setdefault("crash_recovery", {}), "crash_recovery"
    )
    _boolean(crash_recovery, "enabled", True)
    max_restarts = _integer(
        crash_recovery, "max_restarts_per_cycle", 2, 0
    )
    if max_restarts > 20:
        raise ConfigurationError(
            "crash_recovery.max_restarts_per_cycle must be less than or equal to 20"
        )
    # 0 means "keep restarting until the time budget is spent". A repeated crash
    # still leaves a chance to reach another feature branch, so these limits are
    # opt-in; only an app that dies immediately on every launch is terminal.
    per_signature = _integer(
        crash_recovery, "max_restarts_per_signature", 0, 0
    )
    if per_signature > 50:
        raise ConfigurationError(
            "crash_recovery.max_restarts_per_signature must be less than or equal to 50"
        )
    total_restarts = _integer(
        crash_recovery, "max_total_restarts_per_cycle", 0, 0
    )
    if total_restarts > 100:
        raise ConfigurationError(
            "crash_recovery.max_total_restarts_per_cycle must be less than or equal to 100"
        )
    startup_window = _integer(
        crash_recovery, "startup_crash_window_seconds", 15, 0
    )
    if startup_window > 300:
        raise ConfigurationError(
            "crash_recovery.startup_crash_window_seconds must be less than or equal to 300"
        )
    max_startup_crashes = _integer(
        crash_recovery, "max_startup_crashes", 5, 1
    )
    if max_startup_crashes > 50:
        raise ConfigurationError(
            "crash_recovery.max_startup_crashes must be less than or equal to 50"
        )
    minimum_remaining = _integer(
        crash_recovery, "minimum_remaining_seconds", 5, 1
    )
    if minimum_remaining > 300:
        raise ConfigurationError(
            "crash_recovery.minimum_remaining_seconds must be less than or equal to 300"
        )
    restart_backoff = _number(
        crash_recovery, "restart_backoff_seconds", 1.0, 0.0
    )
    if restart_backoff > 60:
        raise ConfigurationError(
            "crash_recovery.restart_backoff_seconds must be less than or equal to 60"
        )

    input_safety = _mapping(config.setdefault("input_safety", {}), "input_safety")
    _boolean(input_safety, "enabled", True)
    hide_timeout = _number(input_safety, "hide_timeout_seconds", 3.0, 0.1)
    if hide_timeout > 15:
        raise ConfigurationError("input_safety.hide_timeout_seconds must be <= 15")
    attempts = _integer(input_safety, "recovery_attempts", 3, 1)
    if attempts > 10:
        raise ConfigurationError("input_safety.recovery_attempts must be <= 10")
    default_text = input_safety.setdefault("default_text", "test123")
    if not isinstance(default_text, str) or "\x00" in default_text or len(default_text) > 4096:
        raise ConfigurationError("input_safety.default_text must be a string of at most 4096 characters without NUL")
    if input_safety["enabled"]:
        fastbot["engine"] = "fastbot"
        if fastbot["classpath"] != runtime_remote_path() or fastbot["main_class"] != "com.android.commands.monkey.Monkey":
            raise ConfigurationError("strict input safety requires the bundled Fastbot runtime and main class")
        for value in fastbot["extra_args"]:
            if str(value).split("=", 1)[0] in {"-f", "--setup", "--port", "--agent"}:
                raise ConfigurationError("strict input safety does not allow overriding the guarded event source")

    coverage = _mapping(config.setdefault("coverage", {}), "coverage")
    minimum_duration_ratio = _number(
        coverage, "minimum_pressure_duration_ratio", 0.90, 0.0
    )
    if minimum_duration_ratio > 1.0:
        raise ConfigurationError(
            "coverage.minimum_pressure_duration_ratio must be less than or equal to 1"
        )
    _boolean(coverage, "require_nonzero_operations", True)

    retention = _mapping(config.setdefault("retention", {}), "retention")
    _boolean(retention, "enabled", True)
    retention_age = _integer(
        retention, "max_age_days", 14, 1
    )
    if retention_age > 3650:
        raise ConfigurationError("retention.max_age_days must be at most 3650")
    retention_total = _number(retention, "max_total_gb", 60.0, 1.0)
    if retention_total > 100000.0:
        raise ConfigurationError("retention.max_total_gb is unreasonably large")
    _integer(retention, "uploads_max_age_days", 30, 1)
    _boolean(retention, "keep_crash_runs", True)

    scenario = _mapping(config.setdefault("scenario", {}), "scenario")
    scenario.setdefault("login_enabled", False)
    scenario.setdefault("target_page", "HomePage")
    scenario.setdefault("pre_login_steps", [])
    scenario.setdefault("pre_login_steps_by_device", {})
    scenario.setdefault("navigation_steps", [])
    scenario.setdefault("navigation_steps_by_device", {})
    scenario.setdefault("stop_packages", [])
    input_cases = _mapping(scenario.setdefault("input_cases", {}), "scenario.input_cases")
    for name, case in list(input_cases.items()):
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError("input case names must be non-empty strings")
        if isinstance(case, list):
            case = {"values": case, "mode": "sequential"}
            input_cases[name] = case
        case = _mapping(case, "scenario.input_cases.%s" % name)
        values = case.get("values")
        if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or "\x00" in value or len(value) > 4096
            for value in values
        ):
            raise ConfigurationError("input case values must be a non-empty array of strings")
        if case.setdefault("mode", "sequential") != "sequential":
            raise ConfigurationError("input case mode must be 'sequential'")
    random_input_data = scenario.get("random_input_data")
    if random_input_data is not None and (
        not isinstance(random_input_data, str) or random_input_data not in input_cases
    ):
        raise ConfigurationError("scenario.random_input_data must reference an existing input case")
    audit_recovery = _mapping(
        scenario.setdefault("audit_recovery", {}),
        "scenario.audit_recovery",
    )
    # Every normalized run checks the startup audit gate after a launch. A
    # caller can still opt out explicitly with ``enabled: false``.
    audit_recovery.setdefault("enabled", True)
    audit_recovery.setdefault("activity", "")
    audit_recovery.setdefault(
        "audit_resource_id", "%s:id/auditRadioButton" % package_name
    )
    audit_recovery.setdefault("confirm_text", "\u786e\u5b9a")
    if not isinstance(scenario["login_enabled"], bool):
        raise ConfigurationError("scenario.login_enabled must be true or false")
    if not isinstance(scenario["pre_login_steps"], list):
        raise ConfigurationError("scenario.pre_login_steps must be a JSON array")
    if not isinstance(scenario["pre_login_steps_by_device"], dict):
        raise ConfigurationError(
            "scenario.pre_login_steps_by_device must be a JSON object"
        )
    if not isinstance(scenario["navigation_steps"], list):
        raise ConfigurationError("scenario.navigation_steps must be a JSON array")
    if not isinstance(scenario["navigation_steps_by_device"], dict):
        raise ConfigurationError(
            "scenario.navigation_steps_by_device must be a JSON object"
        )
    if not isinstance(scenario["stop_packages"], list) or any(
        not isinstance(package, str) or not _PACKAGE_PATTERN.fullmatch(package.strip())
        for package in scenario["stop_packages"]
    ):
        raise ConfigurationError(
            "scenario.stop_packages must be an array of valid package names"
        )
    if "login_locators" in scenario and not isinstance(
        scenario["login_locators"], dict
    ):
        raise ConfigurationError("scenario.login_locators must be a JSON object")
    if any(not isinstance(step, dict) for step in scenario["pre_login_steps"]):
        raise ConfigurationError("each scenario.pre_login_steps item must be an object")
    if any(not isinstance(step, dict) for step in scenario["navigation_steps"]):
        raise ConfigurationError("each scenario.navigation_steps item must be an object")
    if not isinstance(audit_recovery["enabled"], bool):
        raise ConfigurationError(
            "scenario.audit_recovery.enabled must be true or false"
        )
    if audit_recovery["enabled"]:
        activity = str(audit_recovery["activity"]).strip()
        resource_id = str(audit_recovery["audit_resource_id"]).strip()
        confirm_text = str(audit_recovery["confirm_text"]).strip()
        component_pattern = re.compile(
            r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+/\.?[A-Za-z0-9_.$]+"
        )
        # Generic web runs intentionally leave the activity blank: the
        # executor detects the gate by its package-qualified resource id so
        # it still works when the app recreates the page from another
        # splash/privacy activity after a data reset.  Dedicated framework
        # configs may continue to provide an exact component.
        if activity and not component_pattern.fullmatch(activity):
            raise ConfigurationError(
                "scenario.audit_recovery.activity must be an Android component"
            )
        if not re.fullmatch(
            r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+:id/[A-Za-z0-9_]+",
            resource_id,
        ):
            raise ConfigurationError(
                "scenario.audit_recovery.audit_resource_id is invalid"
            )
        if not confirm_text:
            raise ConfigurationError(
                "scenario.audit_recovery.confirm_text must not be empty"
            )
    for serial, steps in scenario["navigation_steps_by_device"].items():
        if not str(serial).strip() or not isinstance(steps, list):
            raise ConfigurationError(
                "each scenario.navigation_steps_by_device entry must be an array"
            )
        if any(not isinstance(step, dict) for step in steps):
            raise ConfigurationError(
                "each device-specific navigation step must be an object"
            )
    for serial, steps in scenario["pre_login_steps_by_device"].items():
        if not str(serial).strip() or not isinstance(steps, list):
            raise ConfigurationError(
                "each scenario.pre_login_steps_by_device entry must be an array"
            )
        if any(not isinstance(step, dict) for step in steps):
            raise ConfigurationError(
                "each device-specific pre-login step must be an object"
            )

    all_steps = list(scenario["pre_login_steps"]) + list(scenario["navigation_steps"])
    for key in ("pre_login_steps_by_device", "navigation_steps_by_device"):
        for steps in scenario[key].values():
            all_steps.extend(steps)
    for step in all_steps:
        if str(step.get("action", "")).lower() in {"input", "set_text"}:
            if "data" in step:
                if not isinstance(step["data"], str) or step["data"] not in input_cases:
                    raise ConfigurationError("input step data must reference an existing input case")
                if "text" in step:
                    raise ConfigurationError("input steps must use either data or text")
            elif "text" in step and not isinstance(step["text"], str):
                raise ConfigurationError("input step text must be a string")

    config["_config_path"] = str(config_path)
    return config


def require_local_apk(config: Mapping[str, Any]) -> Path:
    """Return the configured APK only when it exists on this computer."""
    app = config.get("app")
    if not isinstance(app, Mapping):
        raise ConfigurationError("app must be a JSON object")
    return resolve_local_apk_path(str(app.get("apk_path", "")), require_exists=True)


def reportable_config(value: Any) -> Any:
    """Copy settings for reports while dropping internals and common secrets."""
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.startswith("_"):
                continue
            if name.lower() in _SENSITIVE_KEYS:
                result[name] = "***"
            else:
                result[name] = reportable_config(item)
        return result
    if isinstance(value, list):
        return [reportable_config(item) for item in value]
    return value
