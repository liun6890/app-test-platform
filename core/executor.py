"""Fastbot execution and passive test-artifact collection."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .mutex_controller import MutexController
from .fastbot_assets import (
    bundled_native_remote_path,
    first_supported_abi,
    normalize_runtime_options,
    runtime_asset_path,
    runtime_bundle_has_native,
    runtime_remote_path,
)
from .fastbot_trace import parse_fastbot_log
from .stall_monitor import StallDetector, fingerprint_png
from .input_safety import (
    SYSTEM_DIALOG_PACKAGES,
    hide_input_method, input_safety_options, probe_input_method, touch_path_is_owned,
)


logger = logging.getLogger(__name__)


class PressureEngineError(RuntimeError):
    """Raised when the device-side pressure engine cannot run reliably."""


class _DetachedRemoteProcess:
    """Small process-compatible handle for a pressure engine detached on device."""

    def __init__(
        self,
        remote_pid: Optional[str],
        remote_log_path: str,
        remote_status_path: Optional[str] = None,
        remote_pid_path: Optional[str] = None,
        poller: Optional[Callable[["_DetachedRemoteProcess"], Optional[int]]] = None,
        signaler: Optional[Callable[["_DetachedRemoteProcess", int], None]] = None,
        remote_intent_path: Optional[str] = None,
    ):
        self.remote_pid = remote_pid
        self.remote_log_path = remote_log_path
        self.remote_status_path = remote_status_path
        self.remote_pid_path = remote_pid_path
        self.remote_intent_path = remote_intent_path
        self.started_at = datetime.now(timezone.utc)
        self.stop_intent_reason: Optional[str] = None
        self.stop_intent_at: Optional[str] = None
        self.returncode: Optional[int] = None
        self._poller = poller
        self._signaler = signaler
        self._next_poll_at = 0.0

    def poll(self) -> Optional[int]:
        if self.returncode is None and self._poller is not None:
            result = self._poller(self)
            if result is not None:
                self.returncode = int(result)
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        if self.returncode is None and self._signaler is not None:
            self._signaler(self, int(signal_number))

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        # Windows' signal module does not expose SIGKILL, while Android's
        # shell still accepts the conventional numeric signal value.
        self.send_signal(getattr(signal, "SIGKILL", 9))

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while self.returncode is None:
            self.poll()
            if self.returncode is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("fastbot", timeout)
            time.sleep(0.1)
        return int(self.returncode)


class TestExecutor:
    """Run Fastbot and collect logs, video, screenshots, and model files."""

    MAX_SCREEN_RECORD_SECONDS = 180
    DETACHED_PROCESS_POLL_INTERVAL_SECONDS = 1.0
    FASTBOT_STARTUP_VERIFY_SECONDS = 3.0
    FASTBOT_STARTUP_RETRY_ATTEMPTS = 3
    FASTBOT_STARTUP_RETRY_DELAYS = (2.0, 4.0)
    # An IME panel or system dialog can hold window focus above the target app.
    # Fastbot's guarded event source then blocks every injected event, and a
    # relaunch cannot remove that window. Try one controlled BACK per recovery
    # and stop the run once this many attempts fail, instead of restarting the
    # engine forever.
    BLOCKING_WINDOW_DISMISS_LIMIT = 2
    # Fastbot 2.x derives a default output directory by concatenating every
    # ``-p`` package name.  Android rejects that path on devices with several
    # vendor permission controllers before the first event is injected.  Keep
    # the explicit directory component comfortably below the filesystem limit.
    FASTBOT_OUTPUT_DIRECTORY_PREFIX = "/sdcard/fbpt_"
    FASTBOT_OUTPUT_DIRECTORY_DIGEST_LENGTH = 12
    PRESSURE_STOP_REASONS = frozenset(
        {
            "deadline",
            "audit_recovery",
            "permission_recovery",
            "foreground_recovery",
            "stall_recovery",
            "crash",
            "manual",
        }
    )
    LOCAL_STOP_INTENT_MAX_AGE_SECONDS = 120.0
    DETACHED_EXIT_SETTLE_SECONDS = 3.0
    # Each poll costs a dumpsys round trip per device.  Seven devices at 0.2s
    # meant ~2100 adb calls a minute, which is what pushed the adb server into
    # its illegal-instruction crashes.  0.5s still notices a stolen foreground
    # well within a second and cuts that load by 60%.
    FOREGROUND_GUARD_INTERVAL_SECONDS = 0.5
    # Keep polling foreground immediately, but ignore cached-Activity-only
    # audit signals while custom Fastbot performs its own launch transition.
    FASTBOT_FOREGROUND_GUARD_INTERVAL_SECONDS = 0.5
    # Observed vendor launch transitions can outlive Fastbot's roughly four-
    # second startup verification and remain on SplashActivity for another
    # two to three seconds. This delays only cached-Activity audit inference;
    # crash, permission, and off-package checks continue immediately.
    FASTBOT_FOREGROUND_GUARD_STARTUP_GRACE_SECONDS = 10.0
    FOREGROUND_RESTORE_SETTLE_SECONDS = 0.35
    # Foreground checks run beside the pressure engine. A blocked dumpsys
    # must never hold the guard for the full diagnostic timeout.
    FOREGROUND_PROBE_TIMEOUT_SECONDS = 1.5
    FOREGROUND_ZERO_WAIT_PROBE_SECONDS = 1.0
    FOREGROUND_LOSS_CONFIRM_SECONDS = 0.6
    FOREGROUND_LOSS_CONFIRM_PROBES = 2
    FOREGROUND_RELAUNCH_TIMEOUT_SECONDS = 12
    # ActivityManager may acknowledge an explicit launch only after a vendor
    # launcher has finished a transition.  Verify the actual foreground for a
    # bounded window before declaring the handoff failed.
    FOREGROUND_RELAUNCH_VERIFY_SECONDS = 8.0
    # A timed-out ``am start`` can still complete on the device after the host
    # command deadline. Give ActivityManager one final bounded window before
    # stopping the pressure engine and reporting a real recovery failure.
    FOREGROUND_RELAUNCH_FINAL_GRACE_SECONDS = 18.0
    AUDIT_RUNTIME_CANDIDATE_PROBES = 2
    AUDIT_RUNTIME_CANDIDATE_SECONDS = 0.2
    AUDIT_RUNTIME_HANDOFF_COOLDOWN_SECONDS = 5.0
    # Permission prompts are transient and can be raised repeatedly by one
    # action (camera, microphone, location, and media are often separate
    # requests).  Keep the handoff edge-triggered so the guard never queues a
    # second stop while the first one is being consumed by the phase owner.
    PERMISSION_RUNTIME_HANDOFF_COOLDOWN_SECONDS = 1.0
    PERMISSION_RECOVERY_MAX_ATTEMPTS = 12
    PERMISSION_RECOVERY_VERIFY_SECONDS = 3.0
    PERMISSION_RECOVERY_POLL_SECONDS = 0.2
    PERMISSION_RECOVERY_TARGET_STABLE_SECONDS = 0.6
    FOREGROUND_GUARD_STOP_TIMEOUT_SECONDS = 25
    SCREENSHOT_CAPTURE_TIMEOUT_SECONDS = 10
    SCREENSHOT_STOP_TIMEOUT_SECONDS = 35
    LOGCAT_BOUNDARY_TAG = "APP_PRESS_TEST_BOUNDARY"
    LOGCAT_BOUNDARY_WAIT_SECONDS = 3.0
    AUDIT_UI_DUMP_PATH = "/sdcard/app_press_test_audit_recovery.xml"
    AUDIT_PRELAUNCH_TIMEOUT_SECONDS = 20.0
    # A freshly restarted app can briefly reject the UiAutomation session that
    # belongs to an older uiautomator2 Device object. Keep the fallback bounded
    # so a broken transport cannot consume the whole preparation deadline.
    AUDIT_ADB_DUMP_TIMEOUT_SECONDS = 4.0
    AUDIT_ADB_READ_TIMEOUT_SECONDS = 3.0
    AUDIT_TAP_RETRY_ATTEMPTS = 2
    AUDIT_TAP_RETRY_DELAY_SECONDS = 0.3
    PRIVACY_ACCEPT_TEXTS = (
        "同意并继续",
        "同意并使用",
        "同意并进入",
        "我同意",
        "同意",
    )
    # Some APKs expose a full-screen agreement link after a relaunch.  It is
    # an in-app page (not the MIUI consent dialog above), so there is no
    # positive button for the recovery flow to tap.  Only these short title
    # markers are eligible for the bounded BACK fallback below.
    AGREEMENT_PAGE_MARKERS = (
        "\u7528\u6237\u534f\u8bae",  # 用户协议
        "\u9690\u79c1\u653f\u7b56",  # 隐私政策
        "\u7528\u6237\u987b\u77e5",  # 用户须知
        "\u4e2a\u4eba\u4fe1\u606f\u4fdd\u62a4",  # 个人信息保护
        "User Agreement",
        "Privacy Policy",
    )
    # Support correctly encoded labels as well as English/framework variants.
    # Historical configs may contain mojibake, so those values remain in the
    # candidate list at the end of the configured-text search.
    AUDIT_TEXTS = (
        "\u5ba1\u6838\u4e2d",  # 审核中
        "\u5ba1\u6838",  # 审核
        "Audit",
        "Review",
    )
    CONFIRM_TEXTS = (
        "\u786e\u5b9a",  # 确定
        "\u786e\u8ba4",  # 确认
        "OK",
        "Ok",
        "Confirm",
        "CONFIRM",
        "\u540c\u610f",  # 同意
        "Agree",
    )
    AUDIT_CONFIRM_RESOURCE_IDS = (
        "android:id/button1",
        "android:id/buttonPositive",
    )
    UI_BOUNDS_PATTERN = re.compile(
        r"^\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]$"
    )
    # Runtime permission requests are shown by a system-owned activity. Keep
    # only that narrowly-scoped prompt in front of the target while it settles;
    # other system pages must still be restored to the target app.
    PERMISSION_CONTROLLER_PACKAGES = frozenset(
        {
            "com.android.permissioncontroller",
            "com.google.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.google.android.packageinstaller",
            "com.lbe.security.miui",
            "com.hihonor.systemmanager",
            # Vendor permission UIs observed on Huawei/Honor, vivo/OPPO and
            # Samsung builds.  Keep this list explicit so a random third-party
            # activity whose name happens to contain "permission" is never
            # treated as a system grant dialog.
            "com.huawei.systemmanager",
            "com.vivo.permissionmanager",
            "com.vivo.permissionmanager2",
            "com.coloros.securitypermission",
            "com.oplus.permissioncontroller",
            "com.samsung.android.permissioncontroller",
            "com.miui.securitycenter",
        }
    )
    # These packages may own a narrowly scoped permission dialog, but allowing
    # the whole package gives Monkey/Fastbot access to privileged launcher
    # activities. MIUI Security Center can emit DEV_CLOSE from its main
    # activity, while Huawei System Manager is itself launcher-visible and can
    # be selected as Fastbot's initial app instead of the requested target.
    PRESSURE_ENGINE_PACKAGE_DENYLIST = frozenset(
        {
            "com.miui.securitycenter",
            "com.huawei.systemmanager",
        }
    )
    PERMISSION_DIALOG_ACTIVITY_MARKERS = (
        "grantpermissionsactivity",
        "permissiondialog",
        "permissionrequestactivity",
        "servicepermissionactivity",
    )
    # MIUI reuses the word ``Permission`` in the class name for its first-run
    # privacy notice.  It is a consent page, not an Android runtime grant;
    # keeping it separate prevents the recovery path from looking for an
    # ``Allow`` button and then relaunching the app when it is not found.
    PRIVACY_DIALOG_COMPONENTS = frozenset(
        {
            "com.miui.securitycenter/com.miui.permcenter.permissions.systemapppermissiondialogactivity",
        }
    )
    PRIVACY_DIALOG_ACTIVITY_MARKERS = (
        "systemapppermissiondialogactivity",
    )
    # Vendor permission/consent popups are often *windows* rather than
    # Activities (MIUI/HyperOS runs the runtime permission prompt from
    # ``com.lbe.security.miui`` while dumpsys still reports our app as the
    # resumed Activity, or briefly reports the launcher instead).  An
    # Activity-only probe therefore concludes "the user left the app" and
    # relaunches it on every popup.  These packages own such transient windows.
    SYSTEM_DIALOG_WINDOW_PACKAGES = SYSTEM_DIALOG_PACKAGES
    # How long a known vendor dialog window may sit above the target before the
    # guard falls back to the normal recovery path.  It gives Fastbot's own
    # allow-list a chance to answer the prompt.
    SYSTEM_DIALOG_GRACE_SECONDS = 10.0
    APP_CRASH_ACTIVITY = "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
    PERMISSION_ALLOW_TEXTS = frozenset(
        {
            "仅在使用中允许",
            "仅使用期间允许",
            "在使用时允许",
            "仅在使用该应用时允许",
            "仅在使用该应用时允许访问",
            "使用应用时允许",
            "使用时允许",
            "使用期间允许",
            "本次使用允许",
            "仅此一次",
            "本次运行允许",
            "本次允许",
            "始终允许",
            "允许",
            "允許",
            "允许访问全部照片和视频",
            # MIUI 14/Android 14 shortens the media grant action to this
            # label and exposes no usable resource-id.
            "允许访问全部",
            "允许所有照片和视频",
            "允许全部照片和视频",
            "Allow",
            "Allow only while using the app",
            "While using the app",
            "Only this time",
            "Allow all the time",
            "Allow while using the app",
            "Allow all photos and videos",
            "ALLOW",
        }
    )
    PERMISSION_DENY_TEXT_MARKERS = (
        "拒绝",
        "不允许",
        "禁止",
        "取消",
        "deny",
        "disallow",
        "not_allow",
        "dont_allow",
        "don't allow",
        "do not allow",
        "not now",
        "no thanks",
    )
    PERMISSION_ALLOW_RESOURCE_MARKERS = (
        "permission_allow_all_button",
        "permission_allow_foreground_only_button",
        "permission_allow_one_time_button",
        "permission_allow_always_button",
        "permission_allow_button",
        "permission_allow",
        "button_allow",
        "btn_allow",
        "allow_button",
    )

    def __init__(
        self, device_serial: str, package_name: str, config: Dict[str, Any]
    ):
        """Initialize an executor for one package on one Android device."""
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            raise ValueError("Invalid Android package name: %s" % package_name)
        self.device_serial = device_serial
        self.package_name = package_name
        self.config = config
        self.adb_path = os.environ.get("ADB_PATH", "adb")
        self.report_dir = self._local_path(config.get("_report_dir", "reports"))
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._process_files: Dict[int, BinaryIO] = {}
        self._logcat_processes: Dict[int, subprocess.Popen] = {}
        self._logcat_capture_offsets: Dict[int, int] = {}
        self._screen_records: Dict[int, Tuple[str, Path]] = {}
        self._screen_processes: Dict[int, subprocess.Popen] = {}
        self._screenshot_jobs: Dict[int, Tuple[threading.Event, threading.Thread]] = {}
        self._fastbot_process: Optional[Any] = None
        self._fastbot_native_info: Optional[Dict[str, Any]] = None
        self._foreground_guard: Optional[
            Tuple[threading.Event, threading.Thread]
        ] = None
        self._foreground_guard_stop_failed = False
        self._foreground_restore_failed = False
        # Set while the foreground guard is handing Fastbot back to the target
        # app.  The pressure process can exit during this handoff, before the
        # worker has had a chance to publish the final restore error.
        self._foreground_recovery_requested = False
        self._foreground_recovery_stop_requested = False
        self._foreground_restore_error: Optional[str] = None
        self.input_recovery_reason: Optional[str] = None
        self._launch_component: Optional[str] = None
        self._permission_controller_package: Optional[str] = None
        self._foreground_restore_count = 0
        self._permission_dialog_count = 0
        # First sighting of a vendor dialog window; the guard only skips the
        # foreground-loss decision while this stays within the grace window.
        self._system_dialog_since: Optional[float] = None
        self._system_dialog_package: Optional[str] = None
        self._permission_recovery_count = 0
        self._permission_recovery_lock = threading.Lock()
        self._permission_recovery_requested = False
        self._permission_recovery_claimed = False
        self._permission_recovery_stop_requested = False
        self._permission_recovery_component: Optional[str] = None
        self._permission_recovery_paused_pids: List[str] = []
        self._permission_recovery_history: List[Dict[str, Any]] = []
        self._permission_recovery_evidence: List[Dict[str, Any]] = []
        self._audit_recovery_count = 0
        self._audit_recovery_lock = threading.Lock()
        self._audit_recovery_requested = False
        self._audit_recovery_claimed = False
        self._audit_recovery_stop_requested = False
        self._audit_recovery_component: Optional[str] = None
        self._audit_recovery_paused_pids: List[str] = []
        self._deadline_stop_requested = False
        self._pressure_stop_intent_lock = threading.Lock()
        self._pressure_stop_intent_reason: Optional[str] = None
        self._pressure_stop_intent_at: Optional[str] = None
        self._last_pressure_stop_reason: Optional[str] = None
        self._last_pressure_stop_error: Optional[str] = None
        # The public stop result also includes host cleanup and return codes.
        # Keep the device-side verification separately so an intentional
        # deadline/foreground handoff race cannot be mistaken for a live
        # pressure engine.
        self._last_pressure_stop_remote_stopped: Optional[bool] = None
        # A mutex release can fail after the pressure process itself has
        # already exited.  Keep that control-plane failure separate from the
        # device-side process verdict so callers can fail the whole run while
        # still preserving crash evidence.
        self._uiautomator_release_failed = False
        self._control_release_failed = False
        self._mutex_release_failed = False
        self._control_release_error: Optional[str] = None
        self._pressure_active = threading.Event()
        self._last_foreground_component: Optional[str] = None
        self._last_foreground_component_at: Optional[float] = None
        self._foreground_restore_lock = threading.Lock()
        self._last_foreground_restore_at = 0.0
        self._blocking_window_dismiss_count = 0
        self._blocking_window_blocked = False
        self._last_audit_ui_dump_succeeded = False
        self._cached_audit_tap_center: Optional[Tuple[int, int]] = None
        self._cached_confirm_tap_center: Optional[Tuple[int, int]] = None
        self._cached_privacy_tap_center: Optional[Tuple[int, int]] = None
        self._cached_audit_activity: Optional[str] = None
        self._cached_privacy_activity: Optional[str] = None
        self._last_audit_handoff_at = float("-inf")
        self._last_permission_handoff_at = float("-inf")
        self._last_ui_dump_xml = ""
        self._last_ui_dump_error: Optional[str] = None
        self._stall_events: List[Dict[str, Any]] = []
        self._stall_detection_total = 0
        self._stall_events_lock = threading.Lock()
        self._stall_watchdog_status: Dict[str, Any] = {
            "enabled": False,
            "state": "disabled",
            "total_probe_failures": 0,
            "consecutive_probe_failures": 0,
            "degraded_periods": [],
        }
        self._screenshot_collection_failed = False
        self._screenshot_collection_error: Optional[str] = None
        self._stall_recovery_requested = False
        self._stall_recovery_claimed = False
        self._stall_recovery_stop_requested = False
        self._crash_stop_requested = False
        self._app_crash_page_lock = threading.Lock()
        self._app_crash_page_detection: Optional[Dict[str, Any]] = None
        self._app_process_exit_lock = threading.Lock()
        self._app_process_exit_detection: Optional[Dict[str, Any]] = None
        self._app_crash_lock = threading.Lock()
        self._app_crash_detection: Optional[Dict[str, Any]] = None
        self._device_disconnect_detection: Optional[Dict[str, Any]] = None
        # Runtime detections belong to one configured cycle.  The executor is
        # intentionally reused between cycles, so keep an explicit lifecycle
        # hook instead of relying on the pressure-start path (which is not
        # reached when preparation fails before Fastbot launches).
        self._active_cycle: Optional[int] = None

    @staticmethod
    def _creation_flags() -> int:
        if os.name == "nt":
            return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return 0

    @staticmethod
    def _local_path(value: str) -> Path:
        """Resolve a host artifact path while rejecting network locations."""
        raw = str(value).strip()
        if (
            not raw
            or "\x00" in raw
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw)
            or raw.startswith(("\\\\", "//"))
        ):
            raise ValueError("executor artifact paths must be local filesystem paths")
        path = Path(raw).expanduser().resolve(strict=False)
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("network-share artifact paths are not allowed")
        return path

    def set_report_dir(self, report_dir: str) -> None:
        """Set the directory used for executor-generated artifacts."""
        self.report_dir = self._local_path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def _run_adb(
        self,
        arguments: List[str],
        timeout: float = 30.0,
        check: bool = False,
        retries: int = 3,
    ) -> subprocess.CompletedProcess:
        command = [self.adb_path, "-s", self.device_serial] + arguments
        last_result: Optional[subprocess.CompletedProcess] = None
        last_error: Optional[BaseException] = None
        for attempt in range(1, retries + 1):
            try:
                last_result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
                if last_result.returncode == 0:
                    return last_result
                last_error = RuntimeError(
                    last_result.stderr.strip()
                    or last_result.stdout.strip()
                    or "ADB failed"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(attempt)

        if check:
            raise RuntimeError("ADB command failed: %s" % last_error)
        if last_result is not None:
            return last_result
        raise RuntimeError("Unable to execute ADB: %s" % last_error)

    def _fastbot_options(self) -> Dict[str, Any]:
        options = self.config.get("fastbot", {})
        if not isinstance(options, dict):
            return {}
        return options

    def _fastbot_output_directory(self) -> str:
        """Return a short, stable output directory unique to this target.

        The directory is reused between phases after the previous contents
        have been removed.  Including the serial prevents two devices from
        colliding when they share a package name, while the fixed-length digest
        keeps Fastbot's nested crash/report paths well within Android limits.
        """
        identity = "%s\0%s" % (self.device_serial, self.package_name)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return "%s%s" % (
            self.FASTBOT_OUTPUT_DIRECTORY_PREFIX,
            digest[: self.FASTBOT_OUTPUT_DIRECTORY_DIGEST_LENGTH],
        )

    def _legacy_fastbot_output_patterns(self, duration_minutes: int) -> List[str]:
        """Return package-scoped paths emitted by older Fastbot launches."""
        # Keep the exact historical path for callers/tests that use a single
        # permission package, and a package-scoped wildcard for launches that
        # included vendor permission controllers in the implicit name.
        package = self.package_name
        return [
            "/sdcard/fastbot-%s--running-minutes-%d"
            % (package, int(duration_minutes)),
            "/sdcard/fastbot-%s-*" % package,
        ]

    def _stall_watchdog_options(self) -> Dict[str, Any]:
        options = self.config.get("stall_watchdog", {})
        if not isinstance(options, dict):
            return {}
        return options

    def _prepare_pressure_engine_start(self) -> None:
        """Clear any previous engine and stale manual-stop signal before launch."""
        self._pressure_active.clear()
        self._last_foreground_component = None
        self._last_foreground_component_at = None
        self._stall_recovery_stop_requested = False
        self._blocking_window_dismiss_count = 0
        self._blocking_window_blocked = False
        self._foreground_guard_stop_failed = False
        self._foreground_restore_failed = False
        self._foreground_recovery_requested = False
        self._foreground_recovery_stop_requested = False
        self._foreground_restore_error = None
        self.input_recovery_reason = None
        with self._permission_recovery_lock:
            self._permission_recovery_requested = False
            self._permission_recovery_claimed = False
            self._permission_recovery_stop_requested = False
            self._permission_recovery_component = None
            self._permission_recovery_paused_pids = []
            self._permission_recovery_history = []
            self._permission_recovery_evidence = []
        with self._audit_recovery_lock:
            self._audit_recovery_requested = False
            self._audit_recovery_claimed = False
            self._audit_recovery_component = None
            self._audit_recovery_paused_pids = []
        controller = MutexController(self.device_serial)
        if not controller._kill_fastbot_if_running():
            raise RuntimeError(
                "Unable to verify that a previous Fastbot process stopped on %s"
                % self.device_serial
            )
        if not controller.clear_stop_request():
            raise RuntimeError(
                "Unable to clear a previous Fastbot stop request on %s"
                % self.device_serial
            )
        self._last_pressure_stop_reason = None
        self._last_pressure_stop_error = None
        self._last_pressure_stop_remote_stopped = None
        self._uiautomator_release_failed = False
        self._control_release_failed = False
        self._mutex_release_failed = False
        self._control_release_error = None
        self._screenshot_collection_failed = False
        self._screenshot_collection_error = None
        with self._pressure_stop_intent_lock:
            self._pressure_stop_intent_reason = None
            self._pressure_stop_intent_at = None
        self._fastbot_native_info = None
        self._crash_stop_requested = False
        with self._app_crash_page_lock:
            self._app_crash_page_detection = None
        with self._app_process_exit_lock:
            self._app_process_exit_detection = None
            self._device_disconnect_detection = None
        with self._app_crash_lock:
            self._app_crash_detection = None

    def begin_cycle(self, cycle: Optional[int] = None) -> None:
        """Reset phase detections before preparation for a new cycle.

        This method deliberately performs no device I/O.  A cycle can fail
        while its startup gate is being prepared, before ``start_fastbot``
        reaches ``_prepare_pressure_engine_start``; clearing here prevents a
        prior cycle's crash/page marker from being attached to that failure.
        """
        try:
            self._active_cycle = int(cycle) if cycle is not None else None
        except (TypeError, ValueError):
            self._active_cycle = None
        with self._app_crash_page_lock:
            self._app_crash_page_detection = None
        with self._app_process_exit_lock:
            self._app_process_exit_detection = None
            self._device_disconnect_detection = None
        with self._app_crash_lock:
            self._app_crash_detection = None
        self._crash_stop_requested = False

    def _fastbot_classpath(self) -> str:
        options = self._fastbot_options()
        if "classpath" not in options:
            options["classpath"] = runtime_remote_path()
        normalize_runtime_options(options)
        classpath = str(options.get("classpath") or "")
        entries = classpath.split(":")
        if not entries or any(
            not re.fullmatch(r"/(?:sdcard|data/local/tmp)/[A-Za-z0-9_./-]+", entry)
            or ".." in Path(entry).parts
            for entry in entries
        ):
            raise ValueError(
                "fastbot.classpath may contain only device-local files under "
                "/sdcard or /data/local/tmp"
            )
        return classpath

    def _fastbot_native_abi(self) -> Optional[str]:
        """Return the ABI used by AiClient to locate bundled native code."""
        values: List[str] = []
        for property_name in ("ro.product.cpu.abilist", "ro.product.cpu.abi"):
            result = self._run_adb(
                ["shell", "getprop", property_name], retries=2, timeout=10
            )
            if result.returncode == 0:
                values.append(result.stdout or "")
                abi = first_supported_abi(values)
                if abi:
                    return abi
        return None

    def _verify_fastbot_files(self) -> Dict[str, Any]:
        """Verify the single runtime APK before launch."""
        missing = []
        for remote_file in self._fastbot_classpath().split(":"):
            if not remote_file:
                continue
            result = self._run_adb(
                ["shell", "ls", remote_file], retries=4, timeout=10
            )
            if result.returncode != 0:
                missing.append(remote_file)
        if missing:
            raise FileNotFoundError(
                "Fastbot files are missing on %s: %s"
                % (self.device_serial, ", ".join(missing))
            )
        expected_md5 = str(
            self._fastbot_options().get("expected_monkeyq_md5", "")
        ).strip().lower()
        if expected_md5 and not re.fullmatch(r"[a-f0-9]{32}", expected_md5):
            raise ValueError("fastbot.expected_monkeyq_md5 must be a 32-character MD5 value")
        if expected_md5 and runtime_remote_path() not in self._fastbot_classpath().split(":"):
            raise ValueError(
                "fastbot.expected_monkeyq_md5 requires the bundled runtime APK in classpath"
            )
        actual_md5 = ""
        if expected_md5:
            result = self._run_adb(
                ["shell", "md5sum", runtime_remote_path()], retries=4, timeout=10
            )
            actual_match = re.search(r"\b([A-Fa-f0-9]{32})\b", result.stdout or "")
            actual_md5 = actual_match.group(1).lower() if actual_match else "unavailable"
            if result.returncode != 0 or actual_md5 != expected_md5:
                raise RuntimeError(
                    "Fastbot runtime APK checksum mismatch on %s: expected %s, got %s; "
                    "run 'python cli.py sync-fastbot --devices %s'"
                    % (self.device_serial, expected_md5, actual_md5, self.device_serial)
                )

        abi = self._fastbot_native_abi()
        if not abi:
            raise RuntimeError(
                "Fastbot native library ABI could not be determined on %s; "
                "run 'adb -s %s shell getprop ro.product.cpu.abi'"
                % (self.device_serial, self.device_serial)
            )
        runtime_path = runtime_asset_path()
        if not runtime_bundle_has_native(abi, runtime_path):
            raise FileNotFoundError(
                "Fastbot runtime APK has no native library for ABI %s: %s"
                % (abi, runtime_path)
            )
        native_info = {
            "abi": abi,
            "path": bundled_native_remote_path(abi),
            "bundled": True,
        }
        self._fastbot_native_info = native_info
        return {"classpath": self._fastbot_classpath(), "native": native_info}

    @staticmethod
    def _xpath_literal(value: str) -> str:
        """Return one XPath string literal without assuming its quote style."""
        text = str(value)
        if "'" not in text:
            return "'%s'" % text
        if '"' not in text:
            return '"%s"' % text
        parts = text.split("'")
        values: List[str] = []
        for index, part in enumerate(parts):
            if part:
                values.append("'%s'" % part)
            if index < len(parts) - 1:
                values.append('"\'"')
        return "concat(%s)" % ", ".join(values)

    def _fastbot_audit_xpath_actions(self) -> List[Dict[str, Any]]:
        """Build the every-launch audit rule for this package only."""
        options = self._audit_recovery_options()
        if not options:
            return []

        candidates = [
            str(options.get("activity") or "").strip(),
            str(self._cached_audit_activity or "").strip(),
        ]
        if not any(candidates):
            try:
                candidates.append(str(self._resolve_launch_component() or "").strip())
            except RuntimeError:
                logger.warning(
                    "Unable to resolve the audit Activity for Fastbot on %s",
                    self.device_serial,
                    exc_info=True,
                )

        activity = ""
        for candidate in candidates:
            normalized = self._normalize_component(candidate)
            package, separator, activity_name = normalized.partition("/")
            if separator and package == self.package_name and activity_name:
                activity = activity_name
                break
        if not activity:
            logger.warning(
                "Fastbot audit XPath is disabled on %s because no Activity was resolved",
                self.device_serial,
            )
            return []

        resource_id = self._xpath_literal(str(options["audit_resource_id"]))
        confirm_text = self._xpath_literal(str(options["confirm_text"]))
        return [
            {
                "prob": 1,
                "activity": activity,
                "times": 1000,
                "actions": [
                    {
                        "xpath": "//*[@resource-id=%s]" % resource_id,
                        "action": "CLICK",
                        "throttle": 500,
                    },
                    {
                        "xpath": "//*[@text=%s]" % confirm_text,
                        "action": "CLICK",
                        "throttle": 1500,
                    },
                ],
            }
        ]

    def _push_fastbot_config(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        generated_actions = self.report_dir / "max.xpath.actions"
        generated_actions.write_text(
            json.dumps(
                self._fastbot_audit_xpath_actions(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        local_files = {
            "max.config": project_root / "config" / "fastbot" / "max.config",
            "max.xpath.actions": generated_actions,
        }
        for name, local_file in local_files.items():
            if not local_file.is_file():
                continue
            result = self._run_adb(
                ["push", str(local_file), "/sdcard/%s" % name],
                timeout=30,
                retries=2,
            )
            if result.returncode != 0:
                logger.warning(
                    "Unable to push %s to %s: %s",
                    name,
                    self.device_serial,
                    result.stderr.strip(),
                )

    @property
    def pressure_engine(self) -> str:
        """Return the explicitly selected local input engine."""
        if input_safety_options(self.config).get("enabled"):
            return "fastbot"
        engine = str(self._fastbot_options().get("engine", "fastbot")).strip().lower()
        if engine not in {"fastbot", "system-monkey"}:
            raise ValueError(
                "fastbot.engine must be either 'fastbot' or 'system-monkey'"
            )
        return engine

    def _spawn_logged_remote(self, remote_command: str, label: str) -> subprocess.Popen:
        """Start one device-local command and retain its output for the report."""
        output_path = self.report_dir / "fastbot.log"
        output_handle = output_path.open("ab")
        output_handle.write(
            ("\n=== %s started %s ===\n" % (label, datetime.now(timezone.utc).isoformat())).encode(
                "utf-8"
            )
        )
        output_handle.flush()
        try:
            process = subprocess.Popen(
                [
                    self.adb_path,
                    "-s",
                    self.device_serial,
                    "shell",
                    remote_command,
                ],
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
        except Exception:
            output_handle.close()
            raise
        self._process_files[id(process)] = output_handle
        self._fastbot_process = process
        return process

    def _spawn_detached_fastbot(
        self, remote_command: str, label: str
    ) -> _DetachedRemoteProcess:
        """Launch Fastbot in a device session that survives an ADB reconnect."""
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]", "_", self.device_serial)
        # Microseconds prevent two recovery phases on the same device from
        # sharing one remote log name within the same wall-clock second.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        remote_log_path = "/sdcard/app_press_test_fastbot_%s_%s.log" % (
            safe_serial,
            stamp,
        )
        remote_status_path = remote_log_path + ".status"
        remote_pid_path = remote_log_path + ".pid"
        remote_intent_path = remote_log_path + ".intent"
        output_path = self.report_dir / "fastbot.log"
        with output_path.open("ab") as output_handle:
            output_handle.write(
                (
                    "\n=== %s started %s (detached) ===\n"
                    % (label, datetime.now(timezone.utc).isoformat())
                ).encode("utf-8")
            )
            output_handle.flush()

        # ``setsid`` prevents the engine from inheriting the transient ADB
        # shell session. Its output stays on the selected device until the
        # normal cleanup path has reconnected and collected it.
        # Keep the status writer outside the Fastbot process.  The command
        # itself contains ``exec app_process``; wrapping it in a subshell lets
        # the parent record the real exit code after Fastbot terminates.
        inner_command = "echo $$ > %s; (%s) > %s 2>&1; rc=$?; echo $rc > %s; exit $rc" % (
            shlex.quote(remote_pid_path),
            remote_command,
            shlex.quote(remote_log_path),
            shlex.quote(remote_status_path),
        )
        launch_command = (
            "rm -f {log} {status} {pid} {intent}; setsid sh -c {command} "
            "</dev/null >/dev/null 2>&1 & "
            "i=0; while [ ! -s {pid} ] && [ $i -lt 50 ]; "
            "do sleep 0.1; i=$((i+1)); done; cat {pid} 2>/dev/null"
        ).format(
            log=shlex.quote(remote_log_path),
            status=shlex.quote(remote_status_path),
            pid=shlex.quote(remote_pid_path),
            intent=shlex.quote(remote_intent_path),
            command=shlex.quote(inner_command),
        )
        try:
            result = subprocess.run(
                [
                    self.adb_path,
                    "-s",
                    self.device_serial,
                    "shell",
                    launch_command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "Unable to launch detached Fastbot on %s: %s"
                % (self.device_serial, exc)
            ) from exc

        remote_pid = next(
            reversed(re.findall(r"(?m)^\s*(\d+)\s*$", result.stdout or "")),
            None,
        )
        if result.returncode != 0 and remote_pid is None:
            # A device can finish the launch and then momentarily disappear
            # before the ADB response returns. Verify the one selected device
            # instead of replaying the launch command and risking two engines.
            processes = MutexController(self.device_serial)._remote_processes()
            if processes:
                remote_pid = next(iter(processes))
            else:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(
                    "Unable to launch detached Fastbot on %s: %s"
                    % (self.device_serial, detail or "ADB returned no process ID")
                )
        if remote_pid is None:
            raise RuntimeError(
                "Detached Fastbot launch on %s returned no process ID"
                % self.device_serial
            )

        process = _DetachedRemoteProcess(
            remote_pid,
            remote_log_path,
            remote_status_path,
            remote_pid_path,
            poller=self._poll_detached_fastbot,
            signaler=self._signal_detached_fastbot,
            remote_intent_path=remote_intent_path,
        )
        self._fastbot_process = process
        return process

    def _remember_pressure_stop_intent(
        self,
        reason: str,
        requested_at: str,
        process: Optional[Any] = None,
    ) -> Tuple[Optional[str], bool]:
        """Store one stop owner, allowing a late explicit manual stop to win."""
        normalized = str(reason or "").strip().lower()
        if normalized not in self.PRESSURE_STOP_REASONS:
            return None, False
        changed = False
        with self._pressure_stop_intent_lock:
            current = self._pressure_stop_intent_reason
            if current and normalized != "manual":
                normalized = current
                requested_at = self._pressure_stop_intent_at or requested_at
            else:
                changed = current != normalized
                self._pressure_stop_intent_reason = normalized
                self._pressure_stop_intent_at = requested_at
        if process is not None:
            try:
                process.stop_intent_reason = normalized
                process.stop_intent_at = requested_at
            except (AttributeError, TypeError):
                pass
        return normalized, changed

    def _record_pressure_stop_intent(
        self, reason: str, process: Optional[Any] = None
    ) -> Optional[str]:
        """Persist why the framework will stop Fastbot before sending a signal."""
        target = process if process is not None else self._fastbot_process
        requested_at = datetime.now(timezone.utc).isoformat()
        normalized, changed = self._remember_pressure_stop_intent(
            reason, requested_at, target
        )
        if not normalized or not changed:
            return normalized

        payload = {
            "reason": normalized,
            "requested_at": requested_at,
            "device_serial": self.device_serial,
            "remote_log_path": str(getattr(target, "remote_log_path", "") or ""),
        }
        try:
            with (self.report_dir / "fastbot_stop_intents.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning(
                "Unable to persist local Fastbot stop intent on %s",
                self.device_serial,
                exc_info=True,
            )

        remote_path = getattr(target, "remote_intent_path", None)
        if remote_path:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            command = "umask 077; printf '%%s\\n' %s > %s" % (
                shlex.quote(encoded),
                shlex.quote(str(remote_path)),
            )
            try:
                result = self._run_adb(["shell", command], timeout=5, retries=1)
                if result.returncode != 0:
                    logger.warning(
                        "Device rejected the remote Fastbot stop intent on %s: %s",
                        self.device_serial,
                        (result.stderr or result.stdout).strip(),
                    )
            except Exception:
                # The in-memory and host-side records are already durable. A
                # transient ADB loss must not prevent an urgent engine stop.
                logger.warning(
                    "Unable to persist remote Fastbot stop intent on %s",
                    self.device_serial,
                    exc_info=True,
                )
        return normalized

    def _read_remote_pressure_stop_intent(
        self, process: Any
    ) -> Optional[str]:
        """Recover a stop owner from the detached device-side metadata file."""
        remembered = str(getattr(process, "stop_intent_reason", "") or "")
        if remembered in self.PRESSURE_STOP_REASONS:
            remembered, _changed = self._remember_pressure_stop_intent(
                remembered,
                str(getattr(process, "stop_intent_at", "") or "")
                or datetime.now(timezone.utc).isoformat(),
                process,
            )
            return remembered
        remote_path = getattr(process, "remote_intent_path", None)
        if not remote_path:
            return None
        try:
            result = self._run_adb(
                ["shell", "cat", str(remote_path)], timeout=5, retries=1
            )
            if result.returncode != 0:
                return None
            payload = json.loads((result.stdout or "").strip())
            reason = str(payload.get("reason") or "").strip().lower()
            requested_at = str(payload.get("requested_at") or "").strip()
            if reason not in self.PRESSURE_STOP_REASONS:
                return None
            remembered, _changed = self._remember_pressure_stop_intent(
                reason,
                requested_at or datetime.now(timezone.utc).isoformat(),
                process,
            )
            return remembered
        except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.debug(
                "Unable to read remote Fastbot stop intent on %s",
                self.device_serial,
                exc_info=True,
            )
            return None

    def _read_local_pressure_stop_intent(
        self, process: Any
    ) -> Optional[str]:
        """Recover a recent stop owner when the detached intent file raced cleanup."""
        path = self.report_dir / "fastbot_stop_intents.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        if not lines:
            return None

        started_at = getattr(process, "started_at", None)
        if not isinstance(started_at, datetime):
            started_at = None
        now = datetime.now(timezone.utc)
        remote_path = str(getattr(process, "remote_log_path", "") or "")
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("device_serial") != self.device_serial
                or not remote_path
                or payload.get("remote_log_path") != remote_path
            ):
                continue
            reason = str(payload.get("reason") or "").strip().lower()
            if reason not in self.PRESSURE_STOP_REASONS:
                continue
            requested_at = str(payload.get("requested_at") or "").strip()
            requested_time: Optional[datetime] = None
            if requested_at:
                try:
                    requested_time = datetime.fromisoformat(
                        requested_at.replace("Z", "+00:00")
                    )
                    if requested_time.tzinfo is None:
                        requested_time = requested_time.replace(tzinfo=timezone.utc)
                    requested_time = requested_time.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    requested_time = None
            if requested_time is None or not (
                timedelta(0)
                <= now - requested_time
                <= timedelta(seconds=self.LOCAL_STOP_INTENT_MAX_AGE_SECONDS)
            ):
                continue
            if started_at is not None and requested_time < started_at - timedelta(
                seconds=2
            ):
                continue
            remembered, _changed = self._remember_pressure_stop_intent(
                reason,
                requested_at or now.isoformat(),
                process,
            )
            return remembered
        return None

    def _read_fastbot_progress(self, path: Path) -> Dict[str, Any]:
        """Read local and detached device-side Fastbot progress markers."""
        local_size = 0
        local_text = ""
        try:
            local_size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(max(0, local_size - 256 * 1024))
                local_text = stream.read().decode("utf-8", errors="replace")
        except OSError:
            pass

        remote_size: Optional[int] = None
        remote_text = ""
        process = self._fastbot_process
        remote_path = str(getattr(process, "remote_log_path", "") or "")
        if remote_path:
            try:
                size_result = self._run_adb(
                    ["shell", "wc", "-c", remote_path], timeout=5, retries=1
                )
                if size_result.returncode == 0:
                    match = re.search(r"(?m)^\s*(\d+)\b", size_result.stdout or "")
                    if match:
                        remote_size = int(match.group(1))
                tail_result = self._run_adb(
                    ["shell", "tail", "-c", "262144", remote_path],
                    timeout=5,
                    retries=1,
                )
                if tail_result.returncode == 0:
                    remote_text = str(tail_result.stdout or "")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                logger.debug(
                    "Unable to read detached Fastbot progress on %s",
                    self.device_serial,
                    exc_info=True,
                )

        text = remote_text or local_text
        ids = [
            int(value)
            for value in re.findall(
                r"(?:Event id:\s*|Sending event #)(\d+)", text, re.I
            )
        ]
        return {
            "log_size": local_size + (remote_size or 0),
            "local_log_size": local_size,
            "remote_log_size": remote_size,
            "last_event_id": ids[-1] if ids else None,
            "event_count_in_tail": len(ids),
            # Wait Event lines can grow indefinitely without injecting input.
            "progress_marker": (ids[-1],) if ids else None,
        }

    def _poll_detached_fastbot(
        self, process: _DetachedRemoteProcess
    ) -> Optional[int]:
        """Read the detached wrapper's status without treating ADB loss as exit."""
        now = time.monotonic()
        if now < process._next_poll_at:
            return process.returncode
        process._next_poll_at = now + self.DETACHED_PROCESS_POLL_INTERVAL_SECONDS
        if not process.remote_pid:
            return 1
        status_path = process.remote_status_path or ""
        pid = shlex.quote(str(process.remote_pid))
        probe = (
            "if [ -s {status} ]; then cat {status}; "
            "elif [ -r /proc/{pid}/stat ]; then "
            "read proc_pid proc_name proc_state proc_rest < /proc/{pid}/stat; "
            "case $proc_state in Z|X) echo EXITED;; *) echo RUNNING;; esac; "
            "elif kill -0 {pid} 2>/dev/null; then echo RUNNING; "
            "else echo EXITED; fi"
        ).format(status=shlex.quote(status_path), pid=pid)
        try:
            result = self._run_adb(
                ["shell", probe], timeout=5, retries=1
            )
        except Exception:
            logger.debug(
                "Unable to poll detached Fastbot on %s",
                self.device_serial,
                exc_info=True,
            )
            return None
        if result.returncode != 0:
            # A short USB re-enumeration is not proof that the engine exited.
            return None
        token = next(
            (line.strip() for line in reversed((result.stdout or "").splitlines()) if line.strip()),
            "",
        )
        if re.fullmatch(r"-?\d+", token):
            self._read_remote_pressure_stop_intent(process)
            return int(token)
        if token == "EXITED":
            # A SIGKILL or vendor shell failure can prevent the status file
            # from being written, but the recorded wrapper PID is gone.
            self._read_remote_pressure_stop_intent(process)
            return 1
        return None

    def _signal_detached_fastbot(
        self, process: _DetachedRemoteProcess, signal_number: int
    ) -> None:
        """Signal the detached Fastbot session, including its app_process child."""
        if not process.remote_pid:
            return
        pid = str(process.remote_pid)
        command = (
            "kill -%d -%s 2>/dev/null || kill -%d %s 2>/dev/null || true"
            % (int(signal_number), pid, int(signal_number), pid)
        )
        try:
            self._run_adb(["shell", command], timeout=5, retries=1)
        except Exception:
            logger.warning(
                "Unable to signal detached Fastbot PID %s on %s",
                pid,
                self.device_serial,
                exc_info=True,
            )

    def _collect_detached_fastbot_log(self, process: Any) -> bool:
        """Append collected device-side Fastbot output to the normal local log."""
        if not isinstance(process, _DetachedRemoteProcess):
            return True
        if getattr(process, "_log_collected", False):
            return True
        stop_intent = self._read_remote_pressure_stop_intent(process)
        remote_path = process.remote_log_path
        staged_path = self.report_dir / (".%s" % Path(remote_path).name)
        try:
            result = self._run_adb(
                ["pull", remote_path, str(staged_path)], timeout=60, retries=6
            )
            if result.returncode != 0 or not staged_path.is_file():
                logger.warning(
                    "Unable to collect detached Fastbot log from %s: %s",
                    self.device_serial,
                    (result.stderr or result.stdout).strip(),
                )
                return False
            with (self.report_dir / "fastbot.log").open("ab") as output_handle:
                output_handle.write(
                    (
                        "\n=== Detached Fastbot output collected %s ===\n"
                        % datetime.now(timezone.utc).isoformat()
                    ).encode("utf-8")
                )
                output_handle.write(staged_path.read_bytes())
                if stop_intent:
                    output_handle.write(
                        (
                            "\n=== Framework stop intent: %s at %s ===\n"
                            % (
                                stop_intent,
                                getattr(process, "stop_intent_at", None)
                                or "unknown time",
                            )
                        ).encode("utf-8")
                    )
                output_handle.flush()
            self._run_adb(["shell", "rm", "-f", remote_path], timeout=10, retries=1)
            if process.remote_status_path:
                self._run_adb(
                    ["shell", "rm", "-f", process.remote_status_path],
                    timeout=10,
                    retries=1,
                )
            if process.remote_pid_path:
                self._run_adb(
                    ["shell", "rm", "-f", process.remote_pid_path],
                    timeout=10,
                    retries=1,
                )
            if process.remote_intent_path:
                self._run_adb(
                    ["shell", "rm", "-f", process.remote_intent_path],
                    timeout=10,
                    retries=1,
                )
            try:
                staged_path.unlink()
            except OSError:
                pass
            process._log_collected = True
            return True
        except (OSError, RuntimeError):
            logger.warning(
                "Unable to collect detached Fastbot log from %s",
                self.device_serial,
                exc_info=True,
            )
            return False

    def _remove_stale_fastbot_output(self, duration_minutes: int) -> bool:
        """Remove the short output directory and package-scoped legacy paths.

        The wildcard is deliberately rooted at the validated package name; it
        only removes artifacts that this executor could have created.  A stale
        directory must be gone before Fastbot parses ``--output-directory``
        because the patched runtime renames an existing directory instead of
        reusing it.
        """
        paths = [self._fastbot_output_directory()]
        paths.extend(self._legacy_fastbot_output_patterns(duration_minutes))
        success = True
        for remote_path in paths:
            try:
                result = self._run_adb(
                    ["shell", "rm", "-rf", remote_path], timeout=10, retries=2
                )
            except Exception as exc:
                logger.warning(
                    "Unable to clear stale Fastbot output on %s (%s): %s",
                    self.device_serial,
                    remote_path,
                    exc,
                )
                success = False
                continue
            if result.returncode != 0:
                logger.warning(
                    "Unable to clear stale Fastbot output on %s (%s): %s",
                    self.device_serial,
                    remote_path,
                    (result.stderr or result.stdout).strip(),
                )
                success = False
        return success

    def _fastbot_log_tail(self, start_offset: int) -> str:
        path = self.report_dir / "fastbot.log"
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, int(start_offset)))
                return stream.read().decode("utf-8", errors="replace")[-4000:]
        except OSError:
            return ""

    def _fastbot_failure_excerpt(self) -> str:
        """Extract the short actionable part of a failed Fastbot log."""
        content = self._fastbot_log_tail(0)
        if not content:
            return ""
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        markers = (
            "UnsatisfiedLinkError",
            "Could not load library",
            "No implementation found",
            "NoSuchMethodException",
            "FileNotFoundException",
            "Permission denied",
            "Internal error",
            "*** ERROR ***",
        )
        selected = [
            line for line in lines if any(marker.lower() in line.lower() for marker in markers)
        ]
        if selected:
            return " | ".join(selected[-8:])[:1800]
        return " ".join(lines[-8:])[:1800]

    def _verify_fastbot_startup(
        self, process: _DetachedRemoteProcess, log_offset: int
    ) -> Optional[str]:
        """Return an early-exit detail, or ``None`` once the engine is alive."""
        deadline = time.monotonic() + self.FASTBOT_STARTUP_VERIFY_SECONDS
        while time.monotonic() < deadline:
            returncode = process.poll()
            if returncode is not None:
                self._collect_detached_fastbot_log(process)
                return (
                    "Fastbot exited during startup with return code %s.\n%s"
                    % (returncode, self._fastbot_log_tail(log_offset).strip())
                ).strip()
            time.sleep(0.25)
        return None

    @staticmethod
    def _fastbot_startup_retry_reason(error: Any) -> Optional[str]:
        """Classify startup failures that are safe to retry in the same phase."""
        detail = str(error or "")
        lowered = detail.lower()
        if "already registered" in lowered:
            return "stale UiAutomation registration"
        # Recent vivo builds can kill the first app_process launched directly
        # after uiautomator2 releases UiAutomation.  This happens inside the
        # bounded startup window (shell rc 137), before pressure events begin;
        # a clean handoff and retry succeeds without losing cycle progress.
        if re.search(r"return code\s+(?:137|-9)\b", lowered):
            return "vendor UiAutomation handoff killed Fastbot during startup"
        return None

    def _cleanup_failed_fastbot_start(
        self, process: Optional[Any], startup_error: Any
    ) -> bool:
        """Stop and verify a partially launched engine without replacing its error.

        A detached shell can outlive the ADB command that launched it.  Every
        failed startup therefore needs a device-side absence check before a
        stale-UiAutomation retry or a following test cycle is allowed.
        """
        self._pressure_active.clear()
        cleanup_errors: List[str] = []
        remote_stopped: Optional[bool] = None

        if isinstance(process, _DetachedRemoteProcess):
            try:
                self.stop_fastbot(process)
            except Exception as exc:
                cleanup_errors.append("failed-start stop raised: %s" % exc)
            remote_stopped = self._last_pressure_stop_remote_stopped
        else:
            # Production launches always return _DetachedRemoteProcess.  Keep
            # lightweight unit-test adapters free from real ADB calls while
            # still clearing their in-memory ownership.
            if process is not None:
                remote_stopped = True

        if remote_stopped is not True and (
            process is None or isinstance(process, _DetachedRemoteProcess)
        ):
            try:
                remote_stopped = bool(self.verify_pressure_engine_stopped())
            except Exception as exc:
                remote_stopped = False
                cleanup_errors.append(
                    "failed-start device verification raised: %s" % exc
                )

        guard_stopped = True
        if self._foreground_guard is not None:
            try:
                guard_stopped = bool(self._stop_foreground_guard())
            except Exception as exc:
                guard_stopped = False
                cleanup_errors.append("failed-start guard stop raised: %s" % exc)
        if self._foreground_guard_stop_failed or not guard_stopped:
            cleanup_errors.append("failed-start foreground guard did not stop")

        if remote_stopped is not True:
            cleanup_errors.append(
                "device-side Fastbot stop verification failed after startup error"
            )
            self._last_pressure_stop_remote_stopped = False
        else:
            self._last_pressure_stop_remote_stopped = True

        if self._fastbot_process is process or process is None:
            self._fastbot_process = None

        if cleanup_errors:
            existing = str(self._last_pressure_stop_error or "").strip()
            values = ([existing] if existing else []) + cleanup_errors
            self._last_pressure_stop_error = "; ".join(dict.fromkeys(values))
            logger.error(
                "Fastbot startup cleanup was incomplete on %s after %s: %s",
                self.device_serial,
                startup_error,
                self._last_pressure_stop_error,
            )
        return bool(remote_stopped is True and guard_stopped)

    @staticmethod
    def _system_monkey_event_count(duration_minutes: int, throttle_ms: int) -> int:
        """Choose enough events to cover the requested duration without an unbounded count."""
        duration_ms = int(duration_minutes) * 60 * 1000
        if throttle_ms > 0:
            count = (duration_ms + int(throttle_ms) - 1) // int(throttle_ms)
        else:
            # With no throttle the device can inject very quickly; leave a
            # generous bounded count and let the caller's hard deadline win.
            count = duration_ms * 10
        # The built-in Monkey may batch trackball/key events and finish much
        # sooner than ``count * throttle`` suggests. Keep a large floor so the
        # orchestration layer can enforce the requested wall-clock duration.
        return min(1_000_000, max(100_000, count))

    def _random_event_percentages(self) -> Dict[str, int]:
        """Return the configured in-app touch/gesture mix for random input."""
        configured = self._fastbot_options().get("event_percentages")
        if configured is None:
            return {"touch": 70, "motion": 25, "pinchzoom": 5}
        if not isinstance(configured, dict):
            raise ValueError("fastbot.event_percentages must be a JSON object")
        allowed = {"touch", "motion", "pinchzoom"}
        if set(configured).difference(allowed):
            raise ValueError("fastbot.event_percentages contains unsupported events")
        result: Dict[str, int] = {}
        for name, default in (("touch", 70), ("motion", 25), ("pinchzoom", 5)):
            value = configured.get(name, default)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 100
            ):
                raise ValueError(
                    "fastbot.event_percentages.%s must be an integer from 0 to 100"
                    % name
                )
            result[name] = value
        if sum(result.values()) != 100:
            raise ValueError("fastbot.event_percentages values must add up to 100")
        return result

    @staticmethod
    def _foreground_component_from_output(output: str) -> Optional[str]:
        """Parse modern and vendor Android foreground-activity formats."""
        lines = str(output or "").splitlines()
        component_pattern = re.compile(
            r"\b([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)"
        )
        for marker in (
            "topResumedActivity",
            "mResumedActivity",
            "ResumedActivity",
            "mCurrentFocus",
            "mFocusedApp",
        ):
            marker_pattern = re.compile(r"\b%s\s*[:=]" % re.escape(marker))
            for line in lines:
                if not marker_pattern.search(line):
                    continue
                match = component_pattern.search(line)
                if match:
                    return TestExecutor._normalize_component(
                        "%s/%s" % (match.group(1), match.group(2))
                    )
        return None

    def _current_foreground_component(
        self, timeout: Optional[float] = None
    ) -> Optional[str]:
        """Read the top package/activity with an optional total probe budget.

        The foreground guard runs concurrently with Fastbot. Vendor
        ``dumpsys`` commands can occasionally block while ActivityManager is
        handing a task to the launcher. A bounded call must therefore cover
        both activity and window fallbacks together, rather than allowing two
        independent ten-second ADB waits.
        """
        bounded = timeout is not None
        if bounded:
            requested = float(timeout)
            budget = (
                self.FOREGROUND_ZERO_WAIT_PROBE_SECONDS
                if requested <= 0
                else max(0.1, requested)
            )
            deadline: Optional[float] = time.monotonic() + budget
        else:
            deadline = None
        commands = (
            ["shell", "dumpsys", "activity", "activities"],
            ["shell", "dumpsys", "window", "windows"],
        )
        last_error: Optional[BaseException] = None
        for command in commands:
            if deadline is None:
                command_timeout = 10.0
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                command_timeout = min(10.0, budget, max(0.1, remaining))
            try:
                result = self._run_adb(
                    list(command),
                    timeout=command_timeout,
                    retries=1,
                )
            except Exception as exc:
                last_error = exc
                continue
            component = self._foreground_component_from_output(
                result.stdout or ""
            )
            if component:
                return component
        # Preserve the historical diagnostic behavior for unbounded callers,
        # while bounded guard probes degrade to "unknown" and retry later.
        if last_error is not None and not bounded:
            raise last_error
        return None

    def _foreground_component_probe(self, timeout: float) -> Optional[str]:
        """Call the bounded foreground probe while supporting old test doubles."""
        try:
            return self._current_foreground_component(timeout=timeout)
        except TypeError as exc:
            # A few external adapters override the old no-argument helper.
            # Keep them usable; the built-in implementation above always
            # accepts the keyword and remains strictly bounded.
            message = str(exc).lower()
            if "unexpected keyword" not in message and "keyword argument" not in message:
                raise
            return self._current_foreground_component()

    def _current_foreground_package(self) -> Optional[str]:
        """Read the current top package using passive ADB diagnostics."""
        component = self._current_foreground_component()
        if not component:
            return None
        return component.split("/", 1)[0]

    def _is_app_crash_page_component(self, component: str) -> bool:
        package, separator, activity = str(component or "").partition("/")
        if not separator or package != self.package_name:
            return False
        if activity.startswith("."):
            activity = package + activity
        options = self.config.get("crash_detection", {})
        activities = (
            options.get("crash_page_activities", [])
            if isinstance(options, dict)
            else []
        )
        configured = [
            str(value).strip()
            for value in activities
            if isinstance(value, str) and str(value).strip()
        ] if isinstance(activities, list) else []
        if self.APP_CRASH_ACTIVITY not in configured:
            configured.append(self.APP_CRASH_ACTIVITY)
        for candidate in configured:
            normalized = self.package_name + candidate if candidate.startswith(".") else candidate
            if activity == normalized or activity.endswith(
                "." + normalized.rsplit(".", 1)[-1]
            ):
                return True
        patterns = (
            options.get("crash_page_activity_patterns", [])
            if isinstance(options, dict)
            else []
        )
        if isinstance(patterns, list):
            for pattern in patterns:
                try:
                    if re.search(str(pattern), activity):
                        return True
                except re.error:
                    continue
        return False

    def probe_app_crash_page(self) -> Optional[Dict[str, Any]]:
        """Probe only the foreground component and retain a configured crash page."""
        component = self._current_foreground_component()
        if component and self._is_app_crash_page_component(component):
            self._record_app_crash_page(component)
        return self.app_crash_page_detection

    def probe_target_process(self) -> Dict[str, Any]:
        """Return target PID state while distinguishing an ADB transport loss."""
        detected_at = datetime.now(timezone.utc).isoformat()
        state = self._run_adb(["get-state"], timeout=5, retries=1)
        state_text = (state.stdout or "").strip()
        if state.returncode != 0 or state_text != "device":
            return {
                "connected": False,
                "running": None,
                "pids": [],
                "detected_at": detected_at,
                "error": (state.stderr or state.stdout or "device unavailable").strip(),
            }
        result = self._run_adb(
            ["shell", "pidof", self.package_name], timeout=5, retries=1
        )
        pids = [
            int(value)
            for value in re.findall(r"\b\d+\b", result.stdout or "")
        ]
        return {
            "connected": True,
            "running": bool(pids),
            "pids": pids,
            "detected_at": detected_at,
            "error": (
                (result.stderr or "").strip()
                if result.returncode not in (0, 1)
                else ""
            ),
        }

    def record_app_process_exit(self, detection: Dict[str, Any]) -> None:
        with self._app_process_exit_lock:
            if self._app_process_exit_detection is None:
                self._app_process_exit_detection = dict(detection)

    def record_app_crash(self, detection: Dict[str, Any]) -> None:
        """Retain the highest-fidelity live crash seen during this phase."""
        with self._app_crash_lock:
            if self._app_crash_detection is None:
                self._app_crash_detection = dict(detection)

    @property
    def app_crash_detection(self) -> Optional[Dict[str, Any]]:
        with self._app_crash_lock:
            return (
                dict(self._app_crash_detection)
                if self._app_crash_detection is not None
                else None
            )

    @property
    def app_process_exit_detection(self) -> Optional[Dict[str, Any]]:
        with self._app_process_exit_lock:
            return (
                dict(self._app_process_exit_detection)
                if self._app_process_exit_detection is not None
                else None
            )

    def record_device_disconnect(self, detection: Dict[str, Any]) -> None:
        with self._app_process_exit_lock:
            if self._device_disconnect_detection is None:
                self._device_disconnect_detection = dict(detection)

    @property
    def device_disconnect_detection(self) -> Optional[Dict[str, Any]]:
        with self._app_process_exit_lock:
            return (
                dict(self._device_disconnect_detection)
                if self._device_disconnect_detection is not None
                else None
            )

    def capture_process_diagnostics(self, output_dir: str) -> Dict[str, str]:
        """Capture best-effort ActivityManager state after an abnormal exit."""
        directory = self._local_path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        commands = {
            "adb_state": ["get-state"],
            "processes": ["shell", "ps", "-A"],
            "activity_processes": ["shell", "dumpsys", "activity", "processes"],
            "activity_exit_info": [
                "shell",
                "dumpsys",
                "activity",
                "exit-info",
                self.package_name,
            ],
        }
        artifacts: Dict[str, str] = {}
        for name, command in commands.items():
            try:
                result = self._run_adb(command, timeout=20, retries=1)
                content = "stdout:\n%s\n\nstderr:\n%s\nreturncode: %s\n" % (
                    result.stdout or "",
                    result.stderr or "",
                    result.returncode,
                )
            except Exception as exc:
                content = "error: %s\n" % exc
            path = directory / (name + ".txt")
            path.write_text(content, encoding="utf-8")
            artifacts[name] = str(path)
        return artifacts

    def preflight_pressure_engine(self) -> Dict[str, Any]:
        """Validate pressure-engine prerequisites before the first cycle."""
        result: Dict[str, Any] = {
            "engine": self.pressure_engine,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "success": False,
        }
        if self.pressure_engine == "fastbot":
            verification = self._verify_fastbot_files()
        else:
            verification = {}
        result["success"] = True
        result["classpath"] = (
            self._fastbot_classpath() if self.pressure_engine == "fastbot" else None
        )
        if isinstance(verification, Mapping) and isinstance(
            verification.get("native"), Mapping
        ):
            result["native"] = dict(verification["native"])
        return result

    def _record_app_crash_page(self, component: str) -> None:
        with self._app_crash_page_lock:
            if self._app_crash_page_detection is not None:
                return
            self._app_crash_page_detection = {
                "component": component,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "detection_source": "foreground_component",
            }
        logger.error(
            "Target app crash page detected on %s: %s",
            self.device_serial,
            component,
        )

    @property
    def app_crash_page_detection(self) -> Optional[Dict[str, Any]]:
        """Return immutable evidence that the current phase reached a crash page."""
        with self._app_crash_page_lock:
            return (
                dict(self._app_crash_page_detection)
                if self._app_crash_page_detection is not None
                else None
            )

    def mark_crash_stop_requested(self) -> None:
        """Classify the next pressure-engine stop as crash recovery."""
        self._crash_stop_requested = True
        self._record_pressure_stop_intent("crash")

    @classmethod
    def _is_privacy_dialog_component(cls, component: str) -> bool:
        """Return whether a known vendor privacy-consent Activity is visible."""
        package, separator, activity = str(component or "").partition("/")
        if not separator:
            return False
        package = package.strip().lower()
        activity = activity.strip().lower()
        if activity.startswith("."):
            activity = package + activity
        normalized = "%s/%s" % (package, activity)
        if normalized in cls.PRIVACY_DIALOG_COMPONENTS:
            return True
        # Keep the fallback narrowly scoped to MIUI's security-center package;
        # unrelated applications may legitimately use a class containing
        # ``PermissionDialog``.
        return package == "com.miui.securitycenter" and any(
            marker in activity for marker in cls.PRIVACY_DIALOG_ACTIVITY_MARKERS
        )

    @classmethod
    def _is_permission_dialog_component(cls, component: str) -> bool:
        """Return whether a component is a runtime permission prompt."""
        package, separator, activity = str(component).partition("/")
        if not separator:
            return False
        if cls._is_privacy_dialog_component(component):
            return False
        package = package.lower()
        activity = activity.lower()
        package_is_controller = package in cls.PERMISSION_CONTROLLER_PACKAGES or (
            package.endswith(".permissioncontroller")
            or package.endswith(".packageinstaller")
        )
        return package_is_controller and any(
            marker in activity for marker in cls.PERMISSION_DIALOG_ACTIVITY_MARKERS
        )

    def _system_dialog_window_present(self) -> Optional[str]:
        """Return the package of a visible vendor system dialog window.

        Activity parsing cannot see these popups: on MIUI/HyperOS the runtime
        permission prompt is a *window* owned by ``com.lbe.security.miui`` while
        dumpsys still reports the target app (or, for one probe, the launcher)
        as the resumed activity.  Treating either as a real foreground loss
        makes the executor relaunch the app every time a prompt appears.
        """
        try:
            result = self._run_adb(
                ["shell", "dumpsys", "window", "windows"], timeout=5, retries=1
            )
        except Exception:
            logger.debug(
                "Unable to inspect windows for a system dialog on %s",
                self.device_serial,
                exc_info=True,
            )
            return None
        output = str(getattr(result, "stdout", "") or "")
        if not output:
            return None
        for line in output.splitlines():
            if re.search(r"Window #\d+\s+Window\{", line):
                continue
            match = re.search(r"\bpackage=([A-Za-z0-9_.]+)", line)
            if not match:
                continue
            package = match.group(1).lower()
            if package in self.SYSTEM_DIALOG_WINDOW_PACKAGES:
                return package
        return None

    def _system_dialog_above_target(self) -> bool:
        """Return whether a known vendor dialog is (still) our transient blocker."""
        package = self._system_dialog_window_present()
        now = time.monotonic()
        if not package:
            self._system_dialog_since = None
            self._system_dialog_package = None
            return False
        if self._system_dialog_since is None or package != self._system_dialog_package:
            self._system_dialog_since = now
            self._system_dialog_package = package
            logger.info(
                "Vendor system dialog window from %s sits above %s on %s; "
                "waiting up to %.0fs before treating it as foreground loss",
                package,
                self.package_name,
                self.device_serial,
                self.SYSTEM_DIALOG_GRACE_SECONDS,
            )
        if now - self._system_dialog_since >= self.SYSTEM_DIALOG_GRACE_SECONDS:
            return False
        return True

    def mute_device_audio(self) -> bool:
        """Silence the ringer and the media stream before pressure starts.

        Fastbot opens video ads at random, so a rack of phones that is being
        pressure-tested must stay quiet.  Every step is best effort: a vendor
        ROM without ``cmd audio`` still gets the legacy ``media volume`` path
        and the ``settings`` fallbacks, and no failure is ever raised.
        """
        test_options = self.config.get("test", {})
        if isinstance(test_options, Mapping) and not bool(
            test_options.get("keep_device_silent", True)
        ):
            return False
        commands = (
            ("cmd", "audio", "set-ringer-mode", "SILENT"),
            ("cmd", "media_session", "volume", "--stream", "3", "--set", "0"),
            ("cmd", "media_session", "volume", "--stream", "2", "--set", "0"),
            ("cmd", "media_session", "volume", "--stream", "5", "--set", "0"),
            ("media", "volume", "--stream", "3", "--set", "0"),
            ("settings", "put", "global", "mode_ringer", "0"),
            ("settings", "put", "system", "sound_effects_enabled", "0"),
            ("settings", "put", "system", "vibrate_when_ringing", "0"),
        )
        applied = False
        for command in commands:
            try:
                result = self._run_adb(
                    ["shell"] + list(command), timeout=5, retries=1
                )
            except Exception:
                logger.debug(
                    "Silencing step %s failed on %s",
                    " ".join(command),
                    self.device_serial,
                    exc_info=True,
                )
                continue
            stderr = str(getattr(result, "stderr", "") or "")
            if "No shell command implementation" in stderr or "not found" in stderr:
                continue
            applied = True
        logger.info(
            "Silenced media and ringer streams on %s (best effort, applied=%s)",
            self.device_serial,
            applied,
        )
        return applied

    def configure_touch_overlay(self, enabled: Optional[bool] = None) -> bool:
        """Show or hide the developer touch overlay on this device.

        ``pointer_location`` draws the coordinate bar and ``show_touches`` draws
        the touch circles.  They are useful while recording or reproducing a
        crash, but each one is an extra system window: Fastbot's input guard
        counts windows, so a long pressure run should start with them off.
        """
        if enabled is None:
            test_options = self.config.get("test", {})
            enabled = bool(
                test_options.get("touch_overlay", False)
                if isinstance(test_options, Mapping)
                else False
            )
        value = "1" if enabled else "0"
        applied = False
        for key in ("pointer_location", "show_touches"):
            try:
                self._run_adb(
                    ["shell", "settings", "put", "system", key, value],
                    timeout=5,
                    retries=1,
                )
                applied = True
            except Exception:
                logger.debug(
                    "Unable to set %s on %s", key, self.device_serial, exc_info=True
                )
        logger.info(
            "Touch overlay %s on %s (applied=%s)",
            "enabled" if enabled else "disabled",
            self.device_serial,
            applied,
        )
        return applied

    def _permission_dialog_settle_seconds(self) -> int:
        test_options = self.config.get("test", {})
        if not isinstance(test_options, dict):
            return 2
        value = test_options.get("permission_dialog_settle_seconds", 2)
        if isinstance(value, bool) or not isinstance(value, int):
            return 2
        return min(10, max(0, value))

    def _remote_system_monkey_pids(self, quick: bool = False) -> List[str]:
        """Return only device-local Fastbot/Monkey PIDs for pause/resume.

        Foreground recovery is latency-sensitive. When ``quick`` is set, use
        the PID captured at launch (plus one short ``pidof`` probe) instead of
        the reconnect-heavy full process scan used by normal cleanup paths.
        """
        remote_pid = getattr(self._fastbot_process, "remote_pid", None)
        pids: Set[str] = set()
        if remote_pid and re.fullmatch(r"[1-9][0-9]*", str(remote_pid)):
            pids.add(str(remote_pid))
        if quick:
            try:
                probe = self._run_adb(
                    ["shell", "pidof", "com.android.commands.monkey"],
                    timeout=1.0,
                    retries=1,
                )
                if probe.returncode == 0:
                    pids.update(
                        value
                        for value in (probe.stdout or "").split()
                        if re.fullmatch(r"[1-9][0-9]*", value)
                    )
            except Exception:
                logger.debug(
                    "Unable to perform quick Fastbot PID probe on %s",
                    self.device_serial,
                    exc_info=True,
                )
            if not pids:
                return []
            parent_probe_timeout = 2.0
        else:
            controller = MutexController(self.device_serial)
            processes = controller._remote_processes()
            pids.update(str(pid) for pid in (processes or {}))
            parent_probe_timeout = 8.0
        if not pids:
            return []
        # Detached Fastbot is wrapped by a device-local ``sh -c`` process.
        # Pausing only the app_process child can leave the wrapper holding the
        # shell/UiAutomation hand-off on MIUI, making the next ``am start`` or
        # ``dumpsys`` block until the ADB timeout.  Include validated parents
        # from the same Fastbot command line, but never broaden this to all
        # shells or application processes.
        try:
            probe = self._run_adb(
                ["shell", "ps", "-A", "-o", "PID,PPID,ARGS"],
                timeout=parent_probe_timeout,
                retries=1,
            )
            if probe.returncode == 0:
                rows: Dict[str, Tuple[str, str]] = {}
                for line in (probe.stdout or "").splitlines():
                    columns = line.strip().split(None, 2)
                    if len(columns) < 3 or not columns[0].isdigit() or not columns[1].isdigit():
                        continue
                    rows[columns[0]] = (columns[1], columns[2].lower())
                changed = True
                while changed:
                    changed = False
                    # Walk from the detected app_process child up through its
                    # detached shell wrapper.  The wrapper is the *parent*
                    # of the Fastbot PID, not its child.
                    for pid in list(pids):
                        row = rows.get(pid)
                        if row is None:
                            continue
                        parent, _child_command = row
                        parent_row = rows.get(parent)
                        if (
                            parent_row is not None
                            and parent not in pids
                            and any(
                                marker in parent_row[1]
                                for marker in ("fastbot", "monkey", "app_process")
                            )
                        ):
                            pids.add(parent)
                            changed = True
        except Exception:
            logger.debug(
                "Unable to resolve detached Fastbot parent PIDs on %s",
                self.device_serial,
                exc_info=True,
            )
        # Signal the app_process child before its detached shell parent.  On
        # MIUI stopping the wrapper first can keep the ADB activity-manager
        # hand-off blocked until the wrapper's timeout expires.
        return sorted(pids, key=lambda value: int(value), reverse=True)

    def _foreground_pressure_pids(self) -> List[str]:
        """Get pressure-engine PIDs without blocking foreground recovery."""
        try:
            return self._remote_system_monkey_pids(quick=True)
        except TypeError as exc:
            # Keep compatibility with adapters overriding the old helper
            # signature; the built-in path above is always bounded.
            message = str(exc).lower()
            if "unexpected keyword" not in message and "keyword argument" not in message:
                raise
            return self._remote_system_monkey_pids()

    def _signal_remote_pids(self, pids: List[str], signal_number: int) -> List[str]:
        """Signal only validated device-local PIDs and return successful targets."""
        successful = []
        for pid in pids:
            if not re.fullmatch(r"[1-9][0-9]*", str(pid)):
                continue
            result = self._run_adb(
                ["shell", "kill", "-%s" % int(signal_number), str(pid)],
                timeout=5,
                retries=1,
            )
            # A pressure process can exit between the SIGSTOP and SIGCONT
            # calls.  Android then returns ``No such process`` even though
            # there is no remaining work to recover.  Treat that specific
            # outcome as a completed target; transport and permission errors
            # remain failures and are still reported by the caller.
            output = "%s\n%s" % (result.stdout or "", result.stderr or "")
            process_gone = result.returncode != 0 and any(
                marker in output.lower()
                for marker in ("no such process", "process not found")
            )
            if result.returncode == 0 or process_gone:
                successful.append(str(pid))
        return successful

    def _live_remote_pids(self, pids: Iterable[str]) -> Optional[Set[str]]:
        """Return the subset of PIDs still present, or ``None`` on probe failure."""
        wanted = {
            str(pid) for pid in pids if re.fullmatch(r"[1-9][0-9]*", str(pid))
        }
        if not wanted:
            return set()
        try:
            result = self._run_adb(
                ["shell", "ps", "-A", "-o", "PID"],
                timeout=5,
                retries=1,
            )
        except Exception:
            logger.debug("Unable to probe remote Fastbot PIDs on %s", self.device_serial, exc_info=True)
            return None
        if result.returncode != 0:
            return None
        present: Set[str] = set()
        for line in (result.stdout or "").splitlines():
            token = line.strip().split(None, 1)[0] if line.strip() else ""
            if token in wanted:
                present.add(token)
        return present

    def _resume_paused_remote_pids(
        self, pids: Iterable[str]
    ) -> Tuple[Set[str], Set[str]]:
        """Resume live paused PIDs and report only confirmed failures.

        A SIGCONT can legitimately fail with ``no such process`` when Fastbot
        exited while the stop request was being queued.  Such a PID needs no
        recovery and must not turn an otherwise clean stall/audit handoff into
        an unrecoverable pressure-engine error.
        """
        pending = {
            str(pid) for pid in pids if re.fullmatch(r"[1-9][0-9]*", str(pid))
        }
        resumed: Set[str] = set()
        for attempt in range(2):
            if not pending:
                break
            try:
                signaled = set(self._signal_remote_pids(sorted(pending), 18))
            except Exception:
                signaled = set()
                logger.debug("Unable to resume paused Fastbot PIDs", exc_info=True)
            resumed.update(signaled.intersection(pending))
            pending.difference_update(signaled)
            if not pending:
                break
            live = self._live_remote_pids(pending)
            if live is not None:
                # PIDs which disappeared have already terminated and therefore
                # require no SIGCONT. Only live PIDs remain actionable.
                pending.intersection_update(live)
            if pending and attempt == 0:
                time.sleep(0.15)
        return resumed, pending

    def _settle_permission_dialog(self, stop_event: threading.Event) -> None:
        """Briefly pause Monkey input so a runtime permission prompt can render."""
        seconds = self._permission_dialog_settle_seconds()
        if seconds <= 0:
            return
        paused_pids = self._signal_remote_pids(
            self._remote_system_monkey_pids(), 19  # Android SIGSTOP
        )
        if not paused_pids:
            return
        try:
            logger.info(
                "Paused system Monkey input for %s seconds while the permission "
                "dialog settles on %s",
                seconds,
                self.device_serial,
            )
            stop_event.wait(seconds)
        finally:
            resumed_pids, unresolved = self._resume_paused_remote_pids(paused_pids)
            not_resumed = sorted(unresolved)
            if not_resumed:
                logger.warning(
                    "Unable to resume system Monkey PIDs on %s: %s",
                    self.device_serial,
                    ", ".join(not_resumed),
                )
            else:
                logger.info(
                    "Resumed system Monkey input after the permission dialog "
                    "settle window on %s",
                    self.device_serial,
                )

    def _resolve_launch_component(self) -> Optional[str]:
        """Resolve and cache the target package's explicit launcher component."""
        if self._launch_component:
            return self._launch_component
        result = self._run_adb(
            [
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                self.package_name,
            ],
            timeout=15,
            retries=1,
        )
        if result.returncode != 0:
            return None
        component_pattern = re.compile(
            r"%s/[A-Za-z0-9_.$]+" % re.escape(self.package_name)
        )
        for line in reversed((result.stdout or "").splitlines()):
            candidate = line.strip()
            if component_pattern.fullmatch(candidate):
                self._launch_component = candidate
                return candidate
        return None

    def _resolve_permission_controller_package(self) -> Optional[str]:
        """Resolve the system runtime-permission activity used by this device."""
        if self._permission_controller_package:
            return self._permission_controller_package
        result = self._run_adb(
            [
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.content.pm.action.REQUEST_PERMISSIONS",
            ],
            timeout=15,
            retries=1,
        )
        if result.returncode != 0:
            return None
        for line in reversed((result.stdout or "").splitlines()):
            component = line.strip()
            if self._is_permission_dialog_component(component):
                package = component.split("/", 1)[0]
                self._permission_controller_package = package
                return package
        return None

    def _permission_controller_packages_for_monkey(self) -> List[str]:
        """Return installed permission-controller packages accepted by Monkey.

        Some vendor builds resolve the generic permission intent to AOSP's
        controller but route an app's actual request through a vendor package
        (MIUI uses ``com.lbe.security.miui``). Both packages must be in
        Monkey's narrow allowlist or the request is rejected before its dialog
        can be shown.
        """
        packages: List[str] = []
        resolved = self._resolve_permission_controller_package()
        if resolved and resolved.lower() not in self.PRESSURE_ENGINE_PACKAGE_DENYLIST:
            packages.append(resolved)
        elif resolved:
            logger.warning(
                "Excluding unsafe pressure-engine package on %s: %s",
                self.device_serial,
                resolved,
            )

        for candidate in sorted(self.PERMISSION_CONTROLLER_PACKAGES):
            if candidate.lower() in self.PRESSURE_ENGINE_PACKAGE_DENYLIST:
                continue
            if candidate in packages:
                continue
            try:
                result = self._run_adb(
                    ["shell", "pm", "path", candidate],
                    timeout=5,
                    retries=1,
                )
            except RuntimeError:
                continue
            if result.returncode == 0 and (result.stdout or "").strip():
                packages.append(candidate)
        return packages

    def _permission_controller_packages_for_fastbot(self) -> List[str]:
        """Return installed controller packages for Fastbot's activity filter.

        Fastbot 2.3.x builds its implicit output-directory name by joining all
        ``-p`` values into one filename. Passing the complete static vendor
        list therefore exceeds Android's per-component filename limit and the
        engine exits before injecting its first event. Reuse the installed,
        device-specific list used by system Monkey and keep it within a
        conservative filename budget. Vendor-specific controllers are ranked
        ahead of generic AOSP aliases so MIUI/Huawei/vivo prompts remain
        admitted even when a device exposes many legacy package aliases.
        """
        try:
            discovered = self._permission_controller_packages_for_monkey()
        except Exception:
            logger.debug(
                "Unable to discover installed permission controllers on %s",
                self.device_serial,
                exc_info=True,
            )
            discovered = []
        valid = {
            str(package)
            for package in discovered
            if re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", str(package))
            and str(package).lower() not in self.PRESSURE_ENGINE_PACKAGE_DENYLIST
        }
        resolved = str(self._permission_controller_package or "")
        if (
            re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", resolved)
            and resolved.lower() not in self.PRESSURE_ENGINE_PACKAGE_DENYLIST
        ):
            valid.add(resolved)
        priority = (
            "com.lbe.security.miui",
            "com.miui.securitycenter",
            "com.huawei.systemmanager",
            "com.hihonor.systemmanager",
            "com.vivo.permissionmanager2",
            "com.vivo.permissionmanager",
            "com.coloros.securitypermission",
            "com.oplus.permissioncontroller",
            "com.samsung.android.permissioncontroller",
        )
        ordered: List[str] = []
        for package in (resolved, *priority, *sorted(valid)):
            if package and package in valid and package not in ordered:
                ordered.append(package)

        # Leave room for the target package, the fixed Fastbot suffix, and a
        # small filesystem margin.  The target itself is not part of this
        # return value but is included in the budget calculation.
        max_component_length = 220
        fixed_length = len("/sdcard/fastbot-") + len(self.package_name) + len(
            "-running-minutes-999"
        )
        budget = max(0, max_component_length - fixed_length)
        selected: List[str] = []
        used = 0
        for package in ordered:
            additional = len(package) + 1
            if selected and used + additional > budget:
                continue
            if not selected and additional > budget:
                logger.warning(
                    "Permission controller allowlist budget is too small on %s",
                    self.device_serial,
                )
                break
            selected.append(package)
            used += additional
        return selected

    @property
    def foreground_restore_count(self) -> int:
        return self._foreground_restore_count

    @property
    def permission_dialog_count(self) -> int:
        """Return the number of distinct runtime permission prompt entries."""
        return self._permission_dialog_count

    @property
    def audit_recovery_count(self) -> int:
        """Return successful in-pressure audit-page recoveries."""
        return self._audit_recovery_count

    def request_audit_recovery(
        self, component: str, *, paused_pids: Optional[List[str]] = None
    ) -> bool:
        """Request a safe Fastbot handoff for a known startup-gate page."""
        normalized = self._normalize_component(component)
        now = time.monotonic()
        with self._audit_recovery_lock:
            if (
                self._audit_recovery_requested
                or self._audit_recovery_claimed
                or now - self._last_audit_handoff_at
                < self.AUDIT_RUNTIME_HANDOFF_COOLDOWN_SECONDS
            ):
                return False
        owned_pids = list(paused_pids) if paused_pids is not None else None
        if owned_pids is None:
            owned_pids = self._signal_remote_pids(
                self._remote_system_monkey_pids(), 19  # Android SIGSTOP
            )
        if not owned_pids:
            logger.error(
                "Unable to freeze Fastbot for startup-gate handoff on %s",
                self.device_serial,
            )
            return False
        with self._audit_recovery_lock:
            if (
                self._audit_recovery_requested
                or self._audit_recovery_claimed
                or now - self._last_audit_handoff_at
                < self.AUDIT_RUNTIME_HANDOFF_COOLDOWN_SECONDS
            ):
                if paused_pids is None:
                    self._resume_paused_remote_pids(owned_pids)
                return False
            self._audit_recovery_requested = True
            self._audit_recovery_component = normalized
            self._audit_recovery_paused_pids = list(owned_pids)
            self._last_audit_handoff_at = now
            return True

    def claim_audit_recovery_request(self) -> bool:
        """Claim one guard request so the wait loop stops Fastbot once."""
        with self._audit_recovery_lock:
            if not self._audit_recovery_requested or self._audit_recovery_claimed:
                return False
            self._audit_recovery_claimed = True
            return True

    def mark_audit_recovery_stop_requested(self) -> None:
        """Classify the next pressure-engine exit as an intentional handoff."""
        self._audit_recovery_stop_requested = True
        self._record_pressure_stop_intent("audit_recovery")

    @property
    def audit_recovery_component(self) -> Optional[str]:
        with self._audit_recovery_lock:
            return self._audit_recovery_component

    def mark_audit_recovery_completed(self) -> None:
        """Record a gate that was verified after Fastbot fully released UI control."""
        self._audit_recovery_count += 1

    @property
    def permission_recovery_count(self) -> int:
        """Return the number of permission handoffs completed in this run."""
        return self._permission_recovery_count

    @property
    def permission_recovery_requested(self) -> bool:
        """Return whether the guard has queued a permission handoff."""
        with self._permission_recovery_lock:
            return bool(self._permission_recovery_requested)

    @property
    def permission_recovery_stop_requested(self) -> bool:
        """Return whether the queued handoff has been converted to a stop."""
        with self._permission_recovery_lock:
            return bool(self._permission_recovery_stop_requested)

    @property
    def permission_recovery_component(self) -> Optional[str]:
        with self._permission_recovery_lock:
            return self._permission_recovery_component

    @property
    def permission_recovery_history(self) -> List[Dict[str, Any]]:
        """Return a copy of permission handoff records for report assembly."""
        with self._permission_recovery_lock:
            return [dict(item) for item in self._permission_recovery_history]

    @property
    def permission_recovery_evidence(self) -> List[Dict[str, Any]]:
        """Return references to permission recovery evidence captured so far."""
        with self._permission_recovery_lock:
            return [dict(item) for item in self._permission_recovery_evidence]

    def request_permission_recovery(
        self, component: str, *, paused_pids: Optional[List[str]] = None
    ) -> bool:
        """Freeze Fastbot and queue an exclusive permission-dialog handoff.

        The foreground guard is deliberately the first observer of a runtime
        permission Activity.  It must stop input before trying to dump or tap
        the hierarchy; otherwise the custom Fastbot activity controller can
        restart the app while Android is still rendering the dialog.  An empty
        PID list is still a valid request: the phase owner will stop the
        tracked process and record that freezing could not be verified.
        """
        normalized = self._normalize_component(component)
        now = time.monotonic()
        with self._permission_recovery_lock:
            if (
                self._permission_recovery_requested
                or self._permission_recovery_claimed
                or now - self._last_permission_handoff_at
                < self.PERMISSION_RUNTIME_HANDOFF_COOLDOWN_SECONDS
            ):
                return False

        owned_pids = list(paused_pids) if paused_pids is not None else None
        freeze_error: Optional[str] = None
        if owned_pids is None:
            try:
                # The quick probe only follows the tracked Fastbot process and
                # its validated shell parent, keeping this urgent path bounded.
                candidates = self._foreground_pressure_pids()
                owned_pids = self._signal_remote_pids(candidates, 19)  # SIGSTOP
            except Exception as exc:
                owned_pids = []
                freeze_error = "%s: %s" % (type(exc).__name__, str(exc))
        else:
            owned_pids = [
                str(pid)
                for pid in owned_pids
                if re.fullmatch(r"[1-9][0-9]*", str(pid))
            ]

        with self._permission_recovery_lock:
            # A concurrent guard iteration may have won the edge while the
            # bounded PID probe was running.  Release only PIDs owned by this
            # invocation when that happens.
            if (
                self._permission_recovery_requested
                or self._permission_recovery_claimed
                or now - self._last_permission_handoff_at
                < self.PERMISSION_RUNTIME_HANDOFF_COOLDOWN_SECONDS
            ):
                duplicate = True
            else:
                duplicate = False
                self._permission_recovery_requested = True
                self._permission_recovery_component = normalized
                self._permission_recovery_paused_pids = list(owned_pids)
                self._last_permission_handoff_at = now
                self._permission_recovery_history.append(
                    {
                        "component": normalized,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "paused_pids": list(owned_pids),
                        "freeze_verified": bool(owned_pids),
                        "freeze_error": freeze_error,
                    }
                )
        if duplicate:
            if owned_pids:
                self._resume_paused_remote_pids(owned_pids)
            return False
        if not owned_pids:
            logger.error(
                "Permission dialog detected on %s but Fastbot could not be frozen; "
                "queued a fail-closed stop",
                self.device_serial,
            )
        else:
            logger.warning(
                "Permission dialog detected on %s; froze Fastbot PIDs %s and "
                "queued an exclusive handoff",
                self.device_serial,
                ",".join(owned_pids),
            )
        return True

    def claim_permission_recovery_request(self) -> bool:
        """Claim one permission handoff so the wait loop stops Fastbot once."""
        with self._permission_recovery_lock:
            if (
                not self._permission_recovery_requested
                or self._permission_recovery_claimed
            ):
                return False
            self._permission_recovery_claimed = True
            return True

    def mark_permission_recovery_stop_requested(self) -> None:
        """Classify the next pressure stop as a permission handoff."""
        with self._permission_recovery_lock:
            self._permission_recovery_stop_requested = True
        self._record_pressure_stop_intent("permission_recovery")

    def mark_permission_recovery_completed(
        self, result: Optional[Mapping[str, Any]] = None
    ) -> None:
        """Record a completed handoff and retain its bounded result metadata."""
        self._permission_recovery_count += 1
        if result is None:
            return
        with self._permission_recovery_lock:
            self._permission_recovery_history.append(dict(result))

    def record_permission_recovery_evidence(
        self, evidence: Mapping[str, Any]
    ) -> None:
        """Attach evidence paths to the current handoff without losing errors."""
        with self._permission_recovery_lock:
            self._permission_recovery_evidence.append(dict(evidence))

    @property
    def stall_detection_count(self) -> int:
        """Return unconsumed passive page-stall observations."""
        with self._stall_events_lock:
            return self._stall_detection_total

    def consume_stall_events(self) -> List[Dict[str, Any]]:
        """Return and clear observations collected by the current pressure phase."""
        with self._stall_events_lock:
            events = [dict(event) for event in self._stall_events]
            self._stall_events.clear()
            self._stall_detection_total = 0
        return events

    @property
    def stall_watchdog_status(self) -> Dict[str, Any]:
        """Return probe health without consuming stall observations."""
        with self._stall_events_lock:
            status = dict(self._stall_watchdog_status)
            status["degraded_periods"] = [
                dict(item)
                for item in self._stall_watchdog_status.get(
                    "degraded_periods", []
                )
                if isinstance(item, dict)
            ]
            return status

    def claim_stall_recovery_request(self) -> bool:
        """Claim the recovery request emitted by the current watchdog phase."""
        with self._stall_events_lock:
            if not self._stall_recovery_requested or self._stall_recovery_claimed:
                return False
            self._stall_recovery_claimed = True
            return True

    @property
    def stall_recovery_claimed(self) -> bool:
        """Return whether this pressure phase already claimed a recovery request."""
        with self._stall_events_lock:
            return bool(self._stall_recovery_claimed)

    def mark_stall_recovery_stop_requested(self) -> None:
        """Mark the next Fastbot stop as an intentional watchdog recovery stop."""
        self._stall_recovery_stop_requested = True
        self._record_pressure_stop_intent("stall_recovery")

    @property
    def screenshot_collection_failed(self) -> bool:
        """Return whether a screenshot collector failed to stop cleanly."""
        return self._screenshot_collection_failed

    @property
    def screenshot_collection_error(self) -> Optional[str]:
        """Return the diagnostic from the most recent collector stop failure."""
        return self._screenshot_collection_error

    def mark_screenshot_collection_failed(self, error: Any = None) -> None:
        """Publish a collector shutdown failure for the cycle handoff contract."""
        self._screenshot_collection_failed = True
        message = str(error or "screenshot collector did not stop cleanly").strip()
        if message and not self._screenshot_collection_error:
            self._screenshot_collection_error = message

    @property
    def uiautomator_release_failed(self) -> bool:
        """Return whether restoring uiautomator2 during a handoff failed."""
        return bool(self._uiautomator_release_failed)

    @property
    def control_release_failed(self) -> bool:
        """Return whether any device-control release transaction failed."""
        return bool(self._control_release_failed)

    @property
    def mutex_release_failed(self) -> bool:
        """Return whether the mutex could not be released cleanly."""
        return bool(self._mutex_release_failed)

    @property
    def control_release_error(self) -> Optional[str]:
        """Return the first bounded diagnostic for a failed control release."""
        return self._control_release_error

    def mark_control_release_failed(self, error: Any = None) -> None:
        """Publish a hard control-handoff failure for the cycle scheduler."""
        self._uiautomator_release_failed = True
        self._control_release_failed = True
        self._mutex_release_failed = True
        message = str(error or "device control release failed").strip()
        if message and not self._control_release_error:
            self._control_release_error = message

    @property
    def screenshot_collection_active(self) -> bool:
        """Return whether any registered screenshot collector is still alive."""
        return any(thread.is_alive() for _event, thread in self._screenshot_jobs.values())

    @property
    def pressure_stop_reason(self) -> Optional[str]:
        """Return the classified owner of the most recent completed stop."""
        return self._last_pressure_stop_reason

    @property
    def pressure_stop_intent_reason(self) -> Optional[str]:
        """Return the owner recorded before the pressure process was signaled."""
        with self._pressure_stop_intent_lock:
            return self._pressure_stop_intent_reason

    @property
    def pressure_stop_error(self) -> Optional[str]:
        """Return bounded diagnostics from the most recent stop attempt."""
        return self._last_pressure_stop_error

    @property
    def pressure_stop_verified(self) -> Optional[bool]:
        """Return whether the most recent stop verified device-side absence.

        ``None`` means that no stop has completed yet.  This is deliberately
        separate from ``stop_fastbot``'s boolean, which also reflects host
        return codes and foreground-guard cleanup.
        """
        return self._last_pressure_stop_remote_stopped

    def verify_pressure_engine_stopped(self) -> bool:
        """Actively verify and, if needed, clear a remote Fastbot process.

        This is used after a startup exception where ``stop_fastbot`` never
        received a process handle.  It keeps the next cycle fail-closed while
        still allowing a harmless failed launch (with no remote process left)
        to be recorded and continued.
        """
        controller = MutexController(self.device_serial)
        try:
            verified = bool(controller._kill_fastbot_if_running())
        except Exception as exc:
            self._last_pressure_stop_remote_stopped = False
            self._last_pressure_stop_error = str(exc)
            return False
        self._last_pressure_stop_remote_stopped = verified
        if not verified:
            self._last_pressure_stop_error = (
                "device-side Fastbot stop verification failed"
            )
        return verified

    @staticmethod
    def _normalize_component(component: str) -> str:
        package, separator, activity = str(component).partition("/")
        if not separator:
            return str(component)
        if activity.startswith("."):
            activity = package + activity
        return "%s/%s" % (package, activity)

    def _audit_recovery_options(self) -> Optional[Dict[str, Any]]:
        # Older saved configs may not contain an ``audit_recovery`` block.
        # They default to the package-relative control, while a new config can
        # explicitly disable the web console's every-launch policy.
        scenario = self.config.get("scenario", {})
        options = scenario.get("audit_recovery", {}) if isinstance(scenario, dict) else {}
        if not isinstance(options, dict):
            options = {}
        if options.get("enabled") is False:
            return None
        activity = str(options.get("activity", "")).strip()
        resource_id = str(options.get("audit_resource_id", "")).strip() or (
            "%s:id/auditRadioButton" % self.package_name
        )
        confirm_text = str(options.get("confirm_text", "")).strip() or "确定"
        confirm_resource_id = str(options.get("confirm_resource_id", "")).strip()
        privacy_resource_id = str(
            options.get("privacy_resource_id", "")
        ).strip() or ("%s:id/btn_submit" % self.package_name)
        configured_privacy_texts = options.get("privacy_accept_texts", [])
        if not isinstance(configured_privacy_texts, list):
            configured_privacy_texts = []
        privacy_accept_texts = tuple(
            dict.fromkeys(
                [
                    str(value).strip()
                    for value in configured_privacy_texts
                    if str(value).strip()
                ]
                + list(self.PRIVACY_ACCEPT_TEXTS)
            )
        )
        # ``activity`` is optional for generic APKs.  Some applications show
        # the same audit gate from more than one splash/privacy activity (and
        # may recreate it after clearing data), so the UI control itself is
        # the authoritative signal when no single activity is known.
        if not resource_id or not confirm_text:
            return None
        result: Dict[str, Any] = {
            "activity": self._normalize_component(activity) if activity else "",
            "audit_resource_id": resource_id,
            "confirm_text": confirm_text,
        }
        # Keep the legacy normalized shape stable when no explicit resource
        # id was configured; callers already treat the field as optional.
        if confirm_resource_id:
            result["confirm_resource_id"] = confirm_resource_id
        if str(options.get("privacy_resource_id", "")).strip():
            result["privacy_resource_id"] = privacy_resource_id
        if configured_privacy_texts:
            result["privacy_accept_texts"] = privacy_accept_texts
        return result

    @classmethod
    def _find_audit_text_node(
        cls, nodes: List[ET.Element], texts: Iterable[str]
    ) -> Optional[ET.Element]:
        """Find an enabled node using exact, bounded label candidates."""
        candidates = [
            str(value).strip()
            for value in texts
            if str(value).strip()
        ]
        for text in dict.fromkeys(candidates):
            node = cls._find_ui_node(nodes, text=text)
            if node is not None:
                return node
        return None

    @classmethod
    def _find_audit_node(
        cls, nodes: List[ET.Element], options: Mapping[str, Any]
    ) -> Optional[ET.Element]:
        """Resolve the audit control by resource id before localized text."""
        resource_id = str(options.get("audit_resource_id", "")).strip()
        if resource_id:
            node = cls._find_ui_node(nodes, resource_id=resource_id)
            if node is not None:
                return node
        return cls._find_audit_text_node(nodes, cls.AUDIT_TEXTS)

    @classmethod
    def _find_confirm_node(
        cls, nodes: List[ET.Element], options: Mapping[str, Any]
    ) -> Optional[ET.Element]:
        """Resolve the positive confirmation by id before localized text."""
        resource_ids: List[str] = []
        configured_id = str(options.get("confirm_resource_id", "")).strip()
        if configured_id:
            resource_ids.append(configured_id)
        resource_ids.extend(cls.AUDIT_CONFIRM_RESOURCE_IDS)
        audit_resource_id = str(options.get("audit_resource_id", "")).strip()
        if ":id/" in audit_resource_id:
            package = audit_resource_id.split(":id/", 1)[0]
            resource_ids.extend(
                (f"{package}:id/confirmButton", f"{package}:id/btn_confirm")
            )
        for resource_id in dict.fromkeys(resource_ids):
            node = cls._find_ui_node(nodes, resource_id=resource_id)
            if node is not None:
                return node
        configured_text = str(options.get("confirm_text", "")).strip()
        return cls._find_audit_text_node(
            nodes, [configured_text, *cls.CONFIRM_TEXTS]
        )

    def _is_audit_activity(self, component: str) -> bool:
        options = self._audit_recovery_options()
        if not options:
            return False
        # An empty activity means the generic workflow deliberately watches
        # every activity in the target package and relies on the audit control
        # in the hierarchy as the page detector.
        return not options["activity"] or self._normalize_component(component) == options["activity"]

    def _is_runtime_audit_candidate(self, component: str) -> bool:
        """Match an Activity backed by controls observed before Fastbot started."""
        normalized = self._normalize_component(component)
        audit_activity = self._normalize_component(
            str(self._cached_audit_activity or "")
        )
        privacy_activity = self._normalize_component(
            str(self._cached_privacy_activity or "")
        )
        audit_evidence = bool(
            self._cached_audit_tap_center
            or self._cached_confirm_tap_center
        )
        privacy_evidence = self._cached_privacy_tap_center is not None
        return bool(
            (audit_evidence and audit_activity and normalized == audit_activity)
            or (
                privacy_evidence
                and privacy_activity
                and normalized == privacy_activity
            )
        )

    def _audit_control_visible(self) -> bool:
        """Return whether any blocking startup-gate control is rendered.

        Generic workflows cannot safely guess an activity name.  Dumping the
        hierarchy only after the foreground package has matched keeps this
        probe scoped to the target APK and lets the same guard handle a gate
        that reappears after a relaunch or privacy/data-reset flow.
        """
        options = self._audit_recovery_options()
        if not options:
            return False
        nodes = self._dump_ui_nodes()
        # ``xml.etree.Element`` is false-y when it has no child nodes.  UI
        # controls are leaf nodes, so test identity explicitly rather than
        # wrapping the result in ``bool(element)``.
        return (
            self._find_audit_node(nodes, options) is not None
            or self._find_privacy_accept_node(nodes, options) is not None
            or self._find_in_app_agreement_title(nodes) is not None
        )

    def _dump_ui_nodes_from_adb(
        self,
        *,
        dump_timeout: float = 15.0,
        read_timeout: float = 10.0,
    ) -> List[ET.Element]:
        """Read a hierarchy through the shell-side uiautomator command."""
        dumped = self._run_adb(
            ["shell", "uiautomator", "dump", self.AUDIT_UI_DUMP_PATH],
            timeout=max(1.0, float(dump_timeout)),
            retries=1,
        )
        if dumped.returncode != 0:
            self._last_ui_dump_error = (
                "uiautomator dump failed (rc=%s): %s"
                % (
                    dumped.returncode,
                    (dumped.stderr or dumped.stdout or "").strip(),
                )
            )
            return []
        content = self._run_adb(
            ["shell", "cat", self.AUDIT_UI_DUMP_PATH],
            timeout=max(1.0, float(read_timeout)),
            retries=1,
        )
        if content.returncode != 0 or not (content.stdout or "").strip():
            self._last_ui_dump_error = (
                "uiautomator dump output could not be read (rc=%s): %s"
                % (
                    content.returncode,
                    (content.stderr or content.stdout or "").strip(),
                )
            )
            return []
        self._last_ui_dump_xml = content.stdout or ""
        try:
            root = ET.fromstring(content.stdout)
        except ET.ParseError as exc:
            self._last_ui_dump_error = "%s: %s" % (type(exc).__name__, str(exc))
            logger.warning(
                "Unable to parse audit-page UI hierarchy on %s",
                self.device_serial,
            )
            return []
        self._last_audit_ui_dump_succeeded = True
        return list(root.iter("node"))

    def _dump_ui_nodes(self, device: Any = None) -> List[ET.Element]:
        self._last_audit_ui_dump_succeeded = False
        self._last_ui_dump_xml = ""
        self._last_ui_dump_error = None
        if device is not None:
            dump_hierarchy = getattr(device, "dump_hierarchy", None)
            if not callable(dump_hierarchy):
                self._last_ui_dump_error = "device has no dump_hierarchy method"
                return []
            try:
                hierarchy = str(dump_hierarchy(compressed=True) or "")
                self._last_ui_dump_xml = hierarchy
                root = ET.fromstring(hierarchy)
            except (ET.ParseError, TypeError, ValueError, RuntimeError) as exc:
                self._last_ui_dump_error = "%s: %s" % (type(exc).__name__, str(exc))
                logger.warning(
                    "Unable to read the audit-page hierarchy through uiautomator2 on %s",
                    self.device_serial,
                    exc_info=True,
                )
                return []
            self._last_audit_ui_dump_succeeded = True
            return list(root.iter("node"))
        return self._dump_ui_nodes_from_adb()

    def _dump_audit_nodes(self, device: Any = None) -> List[ET.Element]:
        """Probe the startup gate with a bounded uiautomator2/ADB fallback.

        ``device.dump_hierarchy`` is preferred because it uses the same live
        session as the scenario navigator. If that object is stale after a
        crash/relaunch, a shell-side dump can still succeed while the caller
        refreshes the uiautomator2 connection for the next operation.
        """
        try:
            nodes = (
                self._dump_ui_nodes(device)
                if device is not None
                else self._dump_ui_nodes()
            )
        except InterruptedError:
            raise
        except Exception as exc:
            self._last_audit_ui_dump_succeeded = False
            self._last_ui_dump_error = "%s: %s" % (type(exc).__name__, str(exc))
            logger.warning(
                "Audit-page hierarchy probe through uiautomator2 failed on %s; "
                "trying direct ADB",
                self.device_serial,
                exc_info=True,
            )
            nodes = []

        if device is None or self._last_audit_ui_dump_succeeded:
            return nodes

        primary_error = self._last_ui_dump_error
        try:
            fallback_nodes = self._dump_ui_nodes_from_adb(
                dump_timeout=self.AUDIT_ADB_DUMP_TIMEOUT_SECONDS,
                read_timeout=self.AUDIT_ADB_READ_TIMEOUT_SECONDS,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            self._last_audit_ui_dump_succeeded = False
            self._last_ui_dump_error = "%s: %s" % (type(exc).__name__, str(exc))
            logger.debug(
                "Direct ADB audit hierarchy fallback failed on %s",
                self.device_serial,
                exc_info=True,
            )
            return nodes
        if self._last_audit_ui_dump_succeeded:
            logger.warning(
                "Audit-page hierarchy recovered through direct ADB on %s "
                "after the uiautomator2 probe failed: %s",
                self.device_serial,
                primary_error or "unknown error",
            )
            return fallback_nodes
        return nodes

    @classmethod
    def _ui_node_center(cls, node: ET.Element) -> Optional[Tuple[int, int]]:
        match = cls.UI_BOUNDS_PATTERN.fullmatch(str(node.attrib.get("bounds", "")))
        if not match:
            return None
        left, top, right, bottom = (int(value) for value in match.groups())
        if right <= left or bottom <= top:
            return None
        return ((left + right) // 2, (top + bottom) // 2)

    @staticmethod
    def _find_ui_node(
        nodes: List[ET.Element], *, resource_id: str = "", text: str = ""
    ) -> Optional[ET.Element]:
        for node in nodes:
            if resource_id and node.attrib.get("resource-id") != resource_id:
                continue
            if text and node.attrib.get("text") != text:
                continue
            if node.attrib.get("enabled", "true") != "true":
                continue
            return node
        return None

    @classmethod
    def _find_privacy_accept_node(
        cls, nodes: List[ET.Element], options: Mapping[str, Any]
    ) -> Optional[ET.Element]:
        """Find only the explicit positive control on the privacy page."""
        privacy_context = any(
            marker in (
                str(node.attrib.get("text", ""))
                + " "
                + str(node.attrib.get("content-desc", ""))
            )
            for node in nodes
            for marker in ("用户须知", "隐私政策", "用户协议", "个人信息")
        )
        configured_resource_id = str(options.get("privacy_resource_id", "")).strip()
        package_name = cls._privacy_package_from_options(options)
        resource_ids = []
        if configured_resource_id:
            resource_ids.append(configured_resource_id)
        if package_name:
            resource_ids.append("%s:id/btn_submit" % package_name)
        # MIUI's SystemAppPermissionDialogActivity uses the framework
        # ``button1``/``button2`` pair instead of the app's ``btn_submit``.
        # Only consider button1 when the surrounding hierarchy proves this is
        # a consent page, so a generic Android alert cannot be accepted by
        # accident.
        resource_ids.extend(("android:id/button1", "com.miui.securitycenter:id/button1"))
        for resource_id in dict.fromkeys(resource_ids):
            node = cls._find_ui_node(nodes, resource_id=resource_id)
            if node is not None and privacy_context:
                return node
        texts = options.get("privacy_accept_texts", cls.PRIVACY_ACCEPT_TEXTS)
        if not isinstance(texts, (list, tuple)):
            texts = cls.PRIVACY_ACCEPT_TEXTS
        for text in texts:
            node = cls._find_ui_node(nodes, text=str(text))
            if node is not None and (
                privacy_context
                or str(text) in {"同意并继续", "同意并使用", "同意并进入", "我同意"}
            ):
                return node
        return None

    @classmethod
    def _find_in_app_agreement_title(
        cls, nodes: List[ET.Element]
    ) -> Optional[ET.Element]:
        """Find a short, top-of-page agreement title in the target hierarchy.

        Agreement links can also occur in normal app content.  Restrict the
        match to a short title near the top of the viewport and let callers
        additionally verify that no explicit consent control is present.
        """
        for node in nodes:
            raw_values = (
                str(node.attrib.get("text") or "").strip(),
                str(node.attrib.get("content-desc") or "").strip(),
            )
            bounds = str(node.attrib.get("bounds") or "")
            match = cls.UI_BOUNDS_PATTERN.fullmatch(bounds)
            top = int(match.group(2)) if match else 0
            for raw in raw_values:
                if not raw or len(raw) > 48 or (match and top > 500):
                    continue
                normalized = re.sub(
                    r"[\s《》〈〉「」『』【】（）()<>]", "", raw
                ).casefold()
                if any(
                    str(marker).replace(" ", "").casefold() in normalized
                    for marker in cls.AGREEMENT_PAGE_MARKERS
                ):
                    return node
        return None

    def _dismiss_in_app_agreement_page(self, nodes: List[ET.Element]) -> bool:
        """Leave a target-app agreement page without accepting/rejecting it."""
        title = self._find_in_app_agreement_title(nodes)
        if title is None:
            return False
        node_package = str(title.attrib.get("package") or "").strip()
        if node_package and node_package != self.package_name:
            return False
        # A hierarchy without package attributes is only trusted when the
        # foreground probe confirms that the target APK owns the window.
        if not node_package:
            try:
                component = self._current_foreground_component(timeout=0.8)
            except Exception:
                component = None
            if component and component.split("/", 1)[0] != self.package_name:
                return False
            if not component:
                return False
        result = self._run_adb(
            ["shell", "input", "keyevent", "4"], timeout=5.0, retries=1
        )
        if result.returncode != 0:
            logger.warning(
                "Unable to leave in-app agreement page on %s: %s",
                self.device_serial,
                (result.stderr or result.stdout or "").strip(),
            )
            return False
        logger.info(
            "Left in-app agreement page during recovery on %s",
            self.device_serial,
        )
        return True

    @staticmethod
    def _privacy_package_from_options(options: Mapping[str, Any]) -> str:
        resource_id = str(options.get("audit_resource_id", ""))
        return resource_id.split(":id/", 1)[0] if ":id/" in resource_id else ""

    @classmethod
    def _find_permission_allow_node(
        cls, nodes: List[ET.Element]
    ) -> Optional[ET.Element]:
        """Find a positive runtime-permission action across vendor UIs.

        AOSP exposes stable resource ids, while MIUI and several older vendor
        controllers expose only a localized label/content description.  Keep
        the positive match deliberately narrow and reject any node whose id or
        label looks like a negative action before considering generic
        ``allow`` markers.
        """
        def values(node: ET.Element) -> Tuple[str, str, str]:
            return (
                str(node.attrib.get("resource-id", "")).strip(),
                str(node.attrib.get("text", "")).strip(),
                str(node.attrib.get("content-desc", "")).strip(),
            )

        def enabled_clickable(node: ET.Element) -> bool:
            if node.attrib.get("enabled", "true").lower() != "true":
                return False
            # Some dumps omit ``clickable`` for framework Buttons.  Only
            # reject an explicit non-clickable value so MIUI's label-only
            # controls remain supported.
            if node.attrib.get("clickable", "true").lower() == "false":
                return False
            return cls._ui_node_center(node) is not None

        def is_denial(node: ET.Element) -> bool:
            resource_id, text, description = values(node)
            haystack = " ".join((resource_id, text, description)).lower()
            return any(marker in haystack for marker in cls.PERMISSION_DENY_TEXT_MARKERS)

        def normalized_label(value: str) -> str:
            return re.sub(r"\s+", " ", str(value).strip()).casefold()

        # Resource ids are the least ambiguous and should win over text.  Do
        # this by marker priority rather than XML order: Android 14/15 Honor
        # pages expose both ``allow_all`` and ``allow_selected`` controls, and
        # selecting the latter opens PhotoPicker instead of completing the
        # permission request.
        for marker in cls.PERMISSION_ALLOW_RESOURCE_MARKERS:
            for node in nodes:
                if not enabled_clickable(node) or is_denial(node):
                    continue
                resource_id = str(node.attrib.get("resource-id", "")).lower()
                if marker in resource_id:
                    return node

        allow_texts = {
            normalized_label(value) for value in cls.PERMISSION_ALLOW_TEXTS
        }
        for node in nodes:
            if not enabled_clickable(node) or is_denial(node):
                continue
            _resource_id, text, description = values(node)
            if normalized_label(text) in allow_texts or normalized_label(description) in allow_texts:
                return node

        # Label-only vendor builds sometimes append the app name or a
        # punctuation mark to the positive text.  Permit a small, explicit
        # suffix set but never substring-match a generic sentence.
        for node in nodes:
            if not enabled_clickable(node) or is_denial(node):
                continue
            _resource_id, text, description = values(node)
            for label in (text, description):
                normalized = normalized_label(label)
                if normalized in {"允许", "allow", "允許"}:
                    return node
        return None

    def _tap_ui_node(self, node: ET.Element, device: Any = None) -> bool:
        center = self._ui_node_center(node)
        if center is None:
            return False
        if input_safety_options(self.config).get("enabled"):
            if node.attrib.get("package") != self.package_name:
                return False
            state = probe_input_method(
                lambda arguments, timeout: self._run_adb(arguments, timeout=timeout, retries=1)
            )
            if not state["known"] or state["visible"]:
                return False
            nodes = self._dump_ui_nodes(device)
            if not touch_path_is_owned(nodes, self.package_name, [center]):
                return False
            component = self._foreground_component_probe(self.FOREGROUND_PROBE_TIMEOUT_SECONDS)
            if not component or component.split("/", 1)[0] != self.package_name:
                return False
        result = self._run_adb(
            ["shell", "input", "tap", str(center[0]), str(center[1])],
            timeout=10,
            retries=1,
        )
        return result.returncode == 0

    def _tap_ui_node_with_retry(
        self,
        node: ET.Element,
        *,
        attempts: Optional[int] = None,
        delay: Optional[float] = None,
        device: Any = None,
    ) -> bool:
        """Tap a verified node a small number of times, never indefinitely."""
        limit = max(
            1,
            int(
                self.AUDIT_TAP_RETRY_ATTEMPTS
                if attempts is None
                else attempts
            ),
        )
        pause = max(
            0.0,
            float(
                self.AUDIT_TAP_RETRY_DELAY_SECONDS if delay is None else delay
            ),
        )
        for attempt in range(1, limit + 1):
            try:
                tapped = (
                    self._tap_ui_node(node, device=device)
                    if device is not None else self._tap_ui_node(node)
                )
                if tapped:
                    return True
            except Exception:
                logger.warning(
                    "Startup-gate tap failed on %s (%s/%s)",
                    self.device_serial,
                    attempt,
                    limit,
                    exc_info=True,
                )
            if attempt < limit:
                time.sleep(pause)
        return False

    def _tap_cached_audit_controls(self, stop_event: threading.Event) -> bool:
        """Use bounds captured before Fastbot owns UiAutomation.

        Fastbot's custom ``app_process`` keeps the UiAutomation registration
        even while SIGSTOP'd, so a concurrent ``uiautomator dump`` may fail.
        The startup pass records the stable Xiaomi/framework control bounds;
        when a later dump fails, those bounds let us clear the same page while
        the engine is paused without sending random input to the app.
        """
        if input_safety_options(self.config).get("enabled"):
            return False
        audit_center = self._cached_audit_tap_center
        confirm_center = self._cached_confirm_tap_center
        if audit_center is None or confirm_center is None:
            return False
        # A failed hierarchy dump is expected while custom Fastbot owns
        # UiAutomation, but cached coordinates must never be fired on a
        # normal home page.  They are valid only on the Activity where the
        # audit controls were captured (normally SplashActivity).
        current_component = self._last_foreground_component
        if not current_component:
            current_component = self._current_foreground_component()
        cached_activity = self._cached_audit_activity
        if cached_activity and current_component:
            if self._normalize_component(current_component) != cached_activity:
                return False
        for center in (audit_center, confirm_center):
            result = self._run_adb(
                ["shell", "input", "tap", str(center[0]), str(center[1])],
                timeout=10,
                retries=1,
            )
            if result.returncode != 0:
                return False
            if stop_event.wait(0.5):
                return False
        self._audit_recovery_count += 1
        logger.info(
            "Audit selection recovery completed from cached bounds on %s",
            self.device_serial,
        )
        return True

    def _tap_cached_confirm_control(self, stop_event: threading.Event) -> bool:
        """Tap only a cached confirmation button after selecting the radio.

        Fastbot can keep UiAutomation registered while it is SIGSTOP'd, so a
        follow-up hierarchy dump may fail.  The radio has already been tapped
        at that point; tapping the pair again can toggle it back off on Xiaomi.
        """
        if input_safety_options(self.config).get("enabled"):
            return False
        confirm_center = self._cached_confirm_tap_center
        if confirm_center is None:
            return False
        current_component = self._last_foreground_component
        if not current_component:
            current_component = self._current_foreground_component()
        cached_activity = self._cached_audit_activity
        if cached_activity and current_component:
            if self._normalize_component(current_component) != cached_activity:
                return False
        result = self._run_adb(
            ["shell", "input", "tap", str(confirm_center[0]), str(confirm_center[1])],
            timeout=10,
            retries=1,
        )
        if result.returncode != 0 or stop_event.wait(0.5):
            return False
        self._audit_recovery_count += 1
        logger.info(
            "Audit confirmation recovery completed from cached bounds on %s",
            self.device_serial,
        )
        return True

    def _tap_cached_privacy_control(self, stop_event: threading.Event) -> bool:
        """Accept a known privacy page when Fastbot blocks hierarchy dumps."""
        if input_safety_options(self.config).get("enabled"):
            return False
        center = self._cached_privacy_tap_center
        if center is None:
            return False
        current_component = self._last_foreground_component
        if not current_component:
            current_component = self._current_foreground_component()
        cached_activity = self._cached_privacy_activity
        if cached_activity and current_component:
            if self._normalize_component(current_component) != cached_activity:
                return False
        result = self._run_adb(
            ["shell", "input", "tap", str(center[0]), str(center[1])],
            timeout=10,
            retries=1,
        )
        if result.returncode != 0 or stop_event.wait(0.5):
            return False
        self._audit_recovery_count += 1
        logger.info(
            "Privacy-policy recovery completed from cached bounds on %s",
            self.device_serial,
        )
        return True

    def prepare_audit_gate(
        self,
        timeout: float = AUDIT_PRELAUNCH_TIMEOUT_SECONDS,
        *,
        device: Any = None,
    ) -> bool:
        """Complete audit, confirmation and privacy gates before Fastbot.

        Fastbot can inject an event immediately after it starts.  A
        foreground guard running in another thread therefore has a race on a
        freshly relaunched app: the first random tap may dismiss/exit the
        Xiaomi page before the guard's first probe.  This synchronous pass is
        executed while the ``fastbot`` mutex is held, before the engine is
        launched.  It is intentionally idempotent and returns success when
        none of the startup controls is present.
        """
        options = self._audit_recovery_options()
        if not options:
            return True
        deadline = time.monotonic() + min(30.0, max(1.0, float(timeout)))
        saw_gate = False
        awaiting_confirm = False
        audit_confirmed = False
        clear_since: Optional[float] = None
        while time.monotonic() < deadline:
            try:
                nodes = self._dump_audit_nodes(device)
            except InterruptedError:
                raise
            except Exception:
                self._last_audit_ui_dump_succeeded = False
                nodes = []
                logger.warning(
                    "Audit-page hierarchy probe raised on %s; retrying within "
                    "the preparation deadline",
                    self.device_serial,
                    exc_info=True,
                )
            if not nodes and not self._last_audit_ui_dump_succeeded:
                # A failed dump is unknown, not evidence that the gate chain
                # cleared. Keep the stability timer reset and retry within the
                # existing deadline so a privacy page appearing after the
                # audit-to-confirm transition is still observed.
                clear_since = None
                time.sleep(0.5 if saw_gate and not awaiting_confirm else 0.2)
                continue
            audit_node = self._find_audit_node(nodes, options)
            confirm_node = self._find_confirm_node(nodes, options)
            privacy_node = self._find_privacy_accept_node(nodes, options)

            # A full-screen agreement page has no audit radio or positive
            # consent button. Leave it before evaluating the normal gate chain;
            # otherwise Xiaomi can remain on the blank agreement WebView until
            # the watchdog aborts the cycle.
            if (
                audit_node is None
                and confirm_node is None
                and privacy_node is None
                and self._dismiss_in_app_agreement_page(nodes)
            ):
                saw_gate = True
                clear_since = None
                awaiting_confirm = False
                audit_confirmed = False
                time.sleep(0.5)
                continue

            # A vendor may transition directly from the audit page to its
            # privacy page.  In that frame the previous ``awaiting_confirm``
            # state is stale and the confirmation control is gone; keeping
            # that state ahead of the privacy probe would wait until the whole
            # preparation deadline expires.  The privacy detector is narrowly
            # scoped to the consent page, so it is safe to consume it first.
            if privacy_node is not None:
                saw_gate = True
                clear_since = None
                awaiting_confirm = False
                center = self._ui_node_center(privacy_node)
                if center is not None:
                    self._cached_privacy_tap_center = center
                current_component = self._current_foreground_component()
                if current_component:
                    self._cached_privacy_activity = self._normalize_component(current_component)
                logger.info(
                    "Privacy-policy gate detected before Fastbot on %s; accepting it",
                    self.device_serial,
                )
                if not self._tap_ui_node_with_retry(privacy_node, device=device):
                    logger.warning(
                        "Privacy-policy gate tap failed on %s; retrying the "
                        "hierarchy probe",
                        self.device_serial,
                    )
                    time.sleep(0.25)
                    continue
                time.sleep(0.5)
                continue

            # Once the radio is selected, confirmation takes priority even
            # though the same audit control remains rendered on this page.
            # Re-clicking a checked RadioButton can undo the selection on
            # vendor builds and prevents the chain from ever advancing.
            if awaiting_confirm:
                clear_since = None
                if confirm_node is not None:
                    center = self._ui_node_center(confirm_node)
                    if center is not None:
                        self._cached_confirm_tap_center = center
                    if self._tap_ui_node_with_retry(confirm_node, device=device):
                        awaiting_confirm = False
                        audit_confirmed = True
                time.sleep(0.25)
                continue

            if audit_node is not None and not audit_confirmed:
                saw_gate = True
                clear_since = None
                audit_center = self._ui_node_center(audit_node)
                if audit_center is not None:
                    self._cached_audit_tap_center = audit_center
                current_component = self._current_foreground_component()
                if current_component:
                    self._cached_audit_activity = self._normalize_component(current_component)
                logger.info(
                    "Audit gate detected before Fastbot on %s; selecting it",
                    self.device_serial,
                )
                if audit_node.attrib.get("checked", "false") == "true":
                    if confirm_node is not None:
                        center = self._ui_node_center(confirm_node)
                        if center is not None:
                            self._cached_confirm_tap_center = center
                        if self._tap_ui_node_with_retry(confirm_node, device=device):
                            audit_confirmed = True
                        else:
                            awaiting_confirm = True
                    else:
                        awaiting_confirm = True
                elif self._tap_ui_node_with_retry(audit_node, device=device):
                    awaiting_confirm = True
                time.sleep(0.25)
                continue

            if audit_node is not None and audit_confirmed:
                # Wait for the confirmed page to leave rather than treating
                # its still-visible radio as a new gate or as completion.
                clear_since = None
                time.sleep(0.25)
                continue

            # Require a stable, successfully dumped page after the last gate.
            # This prevents the audit-to-privacy transition from being mistaken
            # for completion during a short blank/loading frame.
            if clear_since is None:
                clear_since = time.monotonic()
            if time.monotonic() - clear_since >= 1.0:
                if saw_gate:
                    logger.info(
                        "Startup audit/privacy gates completed before Fastbot on %s",
                        self.device_serial,
                    )
                return True
            time.sleep(0.2)
        # Fail closed when any step was observed but not verified as cleared.
        return False

    def _recover_audit_page(self, stop_event: threading.Event) -> bool:
        """Pause the pressure engine and complete an audit selection shown mid-run."""
        options = self._audit_recovery_options()
        if not options:
            return False
        paused_pids = self._signal_remote_pids(
            self._remote_system_monkey_pids(), 19  # Android SIGSTOP
        )
        if not paused_pids:
            return False
        try:
            audit_nodes = self._dump_ui_nodes()
            privacy_node = self._find_privacy_accept_node(audit_nodes, options)
            if privacy_node is not None:
                privacy_center = self._ui_node_center(privacy_node)
                if privacy_center is not None:
                    self._cached_privacy_tap_center = privacy_center
                current_component = self._last_foreground_component
                if not current_component:
                    current_component = self._current_foreground_component()
                if current_component:
                    self._cached_privacy_activity = self._normalize_component(current_component)
                logger.warning(
                    "Privacy-policy gate reappeared during pressure on %s; accepting it",
                    self.device_serial,
                )
                if not self._tap_ui_node(privacy_node) or stop_event.wait(0.5):
                    return False
                remaining_nodes = self._dump_ui_nodes()
                if self._find_privacy_accept_node(remaining_nodes, options) is not None:
                    return False
                self._audit_recovery_count += 1
                return True
            audit_node = self._find_audit_node(audit_nodes, options)
            if audit_node is None:
                if self._dismiss_in_app_agreement_page(audit_nodes):
                    if stop_event.wait(0.5):
                        return False
                    self._audit_recovery_count += 1
                    logger.info(
                        "In-app agreement recovery completed on %s",
                        self.device_serial,
                    )
                    return True
                if not self._last_audit_ui_dump_succeeded:
                    if self._tap_cached_privacy_control(stop_event):
                        return True
                    return self._tap_cached_audit_controls(stop_event)
            if audit_node is None:
                return False
            # Cache the confirmation bounds from the same successful dump
            # before changing the radio state.  If the next dump is blocked by
            # Fastbot's UiAutomation registration, only the confirmation may
            # be tapped; repeating the radio tap can unselect it.
            confirm_from_initial_dump = self._find_confirm_node(audit_nodes, options)
            if confirm_from_initial_dump is not None:
                confirm_center = self._ui_node_center(confirm_from_initial_dump)
                if confirm_center is not None:
                    self._cached_confirm_tap_center = confirm_center
            logger.warning(
                "Audit selection reappeared during pressure on %s; recovering it",
                self.device_serial,
            )
            if not self._tap_ui_node(audit_node) or stop_event.wait(0.5):
                return False
            confirm_node = self._find_confirm_node(self._dump_ui_nodes(), options)
            if confirm_node is None and not self._last_audit_ui_dump_succeeded:
                return self._tap_cached_confirm_control(stop_event)
            if confirm_node is None or not self._tap_ui_node(confirm_node):
                return False
            if stop_event.wait(1.0):
                return False
            remaining_nodes = self._dump_ui_nodes()
            privacy_node = self._find_privacy_accept_node(remaining_nodes, options)
            if privacy_node is None and not self._last_audit_ui_dump_succeeded:
                if self._tap_cached_privacy_control(stop_event):
                    remaining_nodes = self._dump_ui_nodes()
                    privacy_node = self._find_privacy_accept_node(remaining_nodes, options)
            if privacy_node is not None:
                privacy_center = self._ui_node_center(privacy_node)
                if privacy_center is not None:
                    self._cached_privacy_tap_center = privacy_center
                current_component = self._last_foreground_component
                if not current_component:
                    current_component = self._current_foreground_component()
                if current_component:
                    self._cached_privacy_activity = self._normalize_component(current_component)
                logger.warning(
                    "Privacy-policy gate reappeared during pressure on %s; recovering it",
                    self.device_serial,
                )
                if not self._tap_ui_node(privacy_node) or stop_event.wait(0.5):
                    return False
                remaining_nodes = self._dump_ui_nodes()
            remaining = self._find_audit_node(remaining_nodes, options)
            if remaining is not None or self._find_privacy_accept_node(remaining_nodes, options) is not None:
                logger.warning(
                    "Audit/privacy gate remained visible after recovery on %s",
                    self.device_serial,
                )
                return False
            self._audit_recovery_count += 1
            logger.info(
                "Audit selection recovery completed on %s",
                self.device_serial,
            )
            return True
        finally:
            resumed_pids, unresolved = self._resume_paused_remote_pids(paused_pids)
            not_resumed = sorted(unresolved)
            if not_resumed:
                logger.warning(
                    "Unable to resume pressure-engine PIDs on %s: %s",
                    self.device_serial,
                    ", ".join(not_resumed),
                )

    def _grant_runtime_permission_dialog(self, stop_event: threading.Event) -> bool:
        """Tap the positive button on a narrowly identified permission prompt."""
        paused_pids = self._signal_remote_pids(
            self._remote_system_monkey_pids(), 19  # Android SIGSTOP
        )
        try:
            node = self._find_permission_allow_node(self._dump_ui_nodes())
            if node is None:
                return False
            if not self._tap_ui_node(node):
                return False
            logger.info(
                "Granted runtime permission dialog on %s",
                self.device_serial,
            )
            stop_event.wait(0.5)
            return True
        finally:
            if paused_pids:
                resumed_pids, unresolved = self._resume_paused_remote_pids(paused_pids)
                not_resumed = sorted(unresolved)
                if not_resumed:
                    logger.warning(
                        "Unable to resume pressure-engine PIDs on %s: %s",
                        self.device_serial,
                        ", ".join(not_resumed),
                    )

    def _permission_recovery_timeout_seconds(
        self, timeout: Optional[float] = None
    ) -> float:
        if timeout is not None:
            try:
                return min(60.0, max(3.0, float(timeout)))
            except (TypeError, ValueError):
                pass
        test_options = self.config.get("test", {})
        configured = (
            test_options.get("permission_recovery_timeout_seconds")
            if isinstance(test_options, Mapping)
            else None
        )
        if configured is None:
            configured = max(
                8.0, float(self._permission_dialog_settle_seconds()) + 5.0
            )
        try:
            return min(60.0, max(3.0, float(configured)))
        except (TypeError, ValueError):
            return 8.0

    def _permission_recovery_attempt_limit(self) -> int:
        test_options = self.config.get("test", {})
        configured = (
            test_options.get("permission_recovery_max_attempts")
            if isinstance(test_options, Mapping)
            else None
        )
        try:
            value = (
                int(configured)
                if configured is not None
                else self.PERMISSION_RECOVERY_MAX_ATTEMPTS
            )
        except (TypeError, ValueError):
            value = self.PERMISSION_RECOVERY_MAX_ATTEMPTS
        return min(50, max(1, value))

    @staticmethod
    def _permission_node_metadata(node: Optional[ET.Element]) -> Dict[str, Any]:
        if node is None:
            return {}
        return {
            "resource_id": str(node.attrib.get("resource-id", "")),
            "text": str(node.attrib.get("text", "")),
            "content_desc": str(node.attrib.get("content-desc", "")),
            "class": str(node.attrib.get("class", "")),
            "bounds": str(node.attrib.get("bounds", "")),
        }

    def recover_runtime_permission_dialogs(
        self,
        *,
        device: Any = None,
        timeout: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
        output_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle all visible runtime permission prompts after a safe handoff.

        This method is called only after Fastbot has stopped and the mutex has
        restored uiautomator2. It never sends a negative action and verifies
        that each positive tap either dismisses the current prompt or exposes
        the next permission in the same request chain. A prompt that triggered
        the handoff may already be gone when we take control.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + self._permission_recovery_timeout_seconds(timeout)
        max_attempts = self._permission_recovery_attempt_limit()
        attempts: List[Dict[str, Any]] = []
        dialogs_seen = 0
        initial_component = self.permission_recovery_component
        handoff_from_permission = bool(
            initial_component and self._is_permission_dialog_component(initial_component)
        )
        last_component = initial_component
        saw_permission = False
        target_foreground_since: Optional[float] = None
        completion_reason: Optional[str] = None
        terminal_error: Optional[str] = None
        probe_count = 0
        last_missing_component: Optional[str] = None

        def cancelled() -> bool:
            return bool(stop_event is not None and stop_event.is_set())

        def wait_poll(seconds: float) -> bool:
            if stop_event is not None:
                return bool(stop_event.wait(max(0.0, seconds)))
            time.sleep(max(0.0, seconds))
            return False

        while time.monotonic() < deadline and len(attempts) < max_attempts:
            if cancelled():
                terminal_error = "permission recovery was cancelled"
                break
            try:
                component = self._foreground_component_probe(
                    self.FOREGROUND_PROBE_TIMEOUT_SECONDS
                )
            except Exception as exc:
                component = None
                terminal_error = "foreground probe failed: %s" % exc
            if component:
                last_component = component
            elif not saw_permission and handoff_from_permission:
                # The Activity can briefly disappear from dumpsys while the
                # stopped Fastbot is being torn down. Keep the component that
                # triggered the handoff as the first probe's context.
                component = initial_component

            permission_visible = bool(
                component and self._is_permission_dialog_component(component)
            )
            if permission_visible:
                saw_permission = True
                target_foreground_since = None
                nodes = self._dump_ui_nodes(device)
                node = self._find_permission_allow_node(nodes)
                attempt: Dict[str, Any] = {
                    "attempt": len(attempts) + 1,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "component": component,
                    "ui_dump_succeeded": bool(self._last_audit_ui_dump_succeeded),
                    "ui_dump_error": self._last_ui_dump_error,
                    "button": self._permission_node_metadata(node),
                    "clicked": False,
                    "dismissed": False,
                }
                if node is None:
                    attempt["error"] = "positive permission button was not found"
                    probe_count += 1
                    # Do not exhaust the tap-attempt budget while Android is
                    # still drawing the vendor dialog. Retain one record per
                    # component/dump state and keep polling until the bounded
                    # recovery deadline.
                    if (
                        component != last_missing_component
                        or probe_count == 1
                        or probe_count % 5 == 0
                    ):
                        attempts.append(attempt)
                        last_missing_component = component
                    terminal_error = attempt["error"]
                    wait_poll(self.PERMISSION_RECOVERY_POLL_SECONDS)
                    continue

                if not self._tap_ui_node(node):
                    attempt["error"] = (
                        "ADB tap of the positive permission button failed"
                    )
                    attempts.append(attempt)
                    terminal_error = attempt["error"]
                    wait_poll(self.PERMISSION_RECOVERY_POLL_SECONDS)
                    continue

                attempt["clicked"] = True
                dialogs_seen += 1
                verify_deadline = min(
                    deadline,
                    time.monotonic() + self.PERMISSION_RECOVERY_VERIFY_SECONDS,
                )
                next_permission = False
                dismissed = False
                while time.monotonic() < verify_deadline:
                    if cancelled():
                        terminal_error = "permission recovery was cancelled"
                        break
                    next_component = self._foreground_component_probe(
                        self.FOREGROUND_PROBE_TIMEOUT_SECONDS
                    )
                    if next_component:
                        last_component = next_component
                    if next_component and self._is_permission_dialog_component(
                        next_component
                    ):
                        # The same Activity is reused for a second permission;
                        # let the outer loop locate its new positive control.
                        next_permission = True
                        break
                    if next_component and next_component.split("/", 1)[0] == self.package_name:
                        dismissed = True
                        break
                    if not next_component:
                        # A short ActivityManager transition is expected after
                        # a grant. Keep probing instead of treating it as a
                        # successful dismissal.
                        wait_poll(self.PERMISSION_RECOVERY_POLL_SECONDS)
                        continue
                    wait_poll(self.PERMISSION_RECOVERY_POLL_SECONDS)
                attempt["dismissed"] = dismissed or next_permission
                attempt["next_permission"] = next_permission
                attempts.append(attempt)
                if terminal_error == "permission recovery was cancelled":
                    break
                if not dismissed and not next_permission:
                    terminal_error = (
                        "permission dialog remained visible after the positive tap"
                    )
                    break
                target_foreground_since = time.monotonic() if dismissed else None
                continue

            if component and component.split("/", 1)[0] == self.package_name:
                now = time.monotonic()
                if target_foreground_since is None:
                    target_foreground_since = now
                if (
                    now - target_foreground_since
                    >= self.PERMISSION_RECOVERY_TARGET_STABLE_SECONDS
                ):
                    if dialogs_seen > 0:
                        completion_reason = "target_returned_after_tap"
                        break
                    if handoff_from_permission:
                        completion_reason = "dialog_disappeared_without_confirmed_tap"
                        break
                    if saw_permission:
                        terminal_error = (
                            "permission Activity disappeared before its positive button "
                            "could be clicked"
                        )
                        break
            else:
                # Missing or external foreground is not evidence that the
                # app has returned. Require another full window on the target.
                target_foreground_since = None
            wait_poll(self.PERMISSION_RECOVERY_POLL_SECONDS)

        success = completion_reason is not None
        if not success and terminal_error is None:
            if len(attempts) >= max_attempts:
                terminal_error = "permission recovery attempt limit reached"
            elif time.monotonic() >= deadline:
                terminal_error = "permission recovery timed out"
            elif not saw_permission:
                terminal_error = (
                    "permission Activity disappeared before its button rendered"
                )
            else:
                terminal_error = "permission recovery did not reach a stable target page"

        result: Dict[str, Any] = {
            "attempted": True,
            "success": bool(success),
            "status": "completed" if success else "failed",
            "component": initial_component or last_component,
            "last_component": last_component,
            "dialogs_seen": dialogs_seen,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if success:
            result["completion_reason"] = completion_reason
        elif terminal_error:
            result["error"] = terminal_error

        # Always retain a compact handoff record. On failure the evidence
        # writer adds the raw hierarchy/screenshot; on success the attempted
        # locators still make the control path auditable.
        if output_dir is not None or not success:
            evidence_dir = output_dir or str(self.report_dir / "permission_recovery")
            evidence = self.capture_permission_recovery_evidence(
                evidence_dir, recovery=result
            )
            result["evidence"] = evidence
            self.record_permission_recovery_evidence(evidence)
        with self._permission_recovery_lock:
            self._permission_recovery_history.append(dict(result))
        if success:
            self._permission_recovery_count += 1
            logger.info(
                "Runtime permission recovery completed on %s: %s (%s confirmed taps)",
                self.device_serial,
                completion_reason,
                dialogs_seen,
            )
        else:
            logger.error(
                "Runtime permission recovery failed on %s: %s",
                self.device_serial,
                terminal_error or "unknown error",
            )
        return result

    # Keep a short alias for integrations that use the singular wording.
    recover_runtime_permission_dialog = recover_runtime_permission_dialogs

    def capture_permission_recovery_evidence(
        self,
        output_dir: str,
        *,
        recovery: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist diagnostics for a permission handoff or failed button tap."""
        directory = self._local_path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        evidence: Dict[str, Any] = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "device_serial": self.device_serial,
            "package_name": self.package_name,
            "artifacts": {},
            "errors": [],
        }
        try:
            component = self._current_foreground_component(
                timeout=self.FOREGROUND_PROBE_TIMEOUT_SECONDS
            )
        except TypeError:
            try:
                component = self._current_foreground_component()
            except Exception as exc:
                component = None
                evidence["errors"].append("foreground: %s" % exc)
        except Exception as exc:
            component = None
            evidence["errors"].append("foreground: %s" % exc)
        evidence["foreground_component"] = component

        # Always take a fresh hierarchy at the evidence boundary.  The last
        # successful recovery attempt may describe a prompt that has already
        # disappeared (or a different permission in a chained request), which
        # is exactly the state needed to diagnose a failed handoff.  Retain the
        # previous dump only when the fresh probe itself is unavailable.
        previous_hierarchy = self._last_ui_dump_xml or ""
        try:
            self._dump_ui_nodes()
        except Exception as exc:
            evidence["errors"].append("ui dump: %s" % exc)
        hierarchy = self._last_ui_dump_xml or previous_hierarchy
        hierarchy_path = directory / "ui_hierarchy.xml"
        try:
            hierarchy_path.write_text(hierarchy, encoding="utf-8")
            evidence["artifacts"]["ui_hierarchy"] = str(hierarchy_path)
        except OSError as exc:
            evidence["errors"].append("write ui hierarchy: %s" % exc)
        if self._last_ui_dump_error:
            evidence["ui_dump_error"] = self._last_ui_dump_error
            error_path = directory / "ui_dump_error.txt"
            try:
                error_path.write_text(str(self._last_ui_dump_error), encoding="utf-8")
                evidence["artifacts"]["ui_dump_error"] = str(error_path)
            except OSError as exc:
                evidence["errors"].append("write ui dump error: %s" % exc)

        screenshot_path = directory / "screen.png"
        try:
            screenshot = subprocess.run(
                [
                    self.adb_path,
                    "-s",
                    self.device_serial,
                    "exec-out",
                    "screencap",
                    "-p",
                ],
                capture_output=True,
                timeout=self.SCREENSHOT_CAPTURE_TIMEOUT_SECONDS,
                check=False,
            )
            if screenshot.returncode == 0 and screenshot.stdout:
                screenshot_path.write_bytes(bytes(screenshot.stdout))
                evidence["artifacts"]["screenshot"] = str(screenshot_path)
            else:
                raw_error = screenshot.stderr or b""
                if isinstance(raw_error, bytes):
                    raw_error = raw_error.decode("utf-8", errors="replace")
                evidence["errors"].append("screenshot: %s" % str(raw_error).strip())
        except (OSError, subprocess.SubprocessError) as exc:
            evidence["errors"].append("screenshot: %s" % exc)

        try:
            top = self._run_adb(
                ["shell", "dumpsys", "activity", "top"], timeout=10, retries=1
            )
            top_path = directory / "activity_top.txt"
            top_path.write_text(
                "stdout:\n%s\n\nstderr:\n%s\nreturncode: %s\n"
                % (top.stdout or "", top.stderr or "", top.returncode),
                encoding="utf-8",
            )
            evidence["artifacts"]["activity_top"] = str(top_path)
        except Exception as exc:
            evidence["errors"].append("activity top: %s" % exc)

        # Keep the pressure-engine tail next to the UI artifacts.  It records
        # whether a random DOWN/UP was emitted around the prompt and remains
        # useful even when the main phase log is rotated or truncated later.
        fastbot_log = self.report_dir / "fastbot.log"
        try:
            with fastbot_log.open("rb") as stream:
                stream.seek(max(0, fastbot_log.stat().st_size - 128 * 1024))
                fastbot_tail = stream.read().decode("utf-8", errors="replace")
            fastbot_tail_path = directory / "fastbot_log_tail.txt"
            fastbot_tail_path.write_text(fastbot_tail, encoding="utf-8")
            evidence["artifacts"]["fastbot_log_tail"] = str(fastbot_tail_path)
        except OSError as exc:
            evidence["errors"].append("fastbot log tail: %s" % exc)

        evidence["handoff"] = {
            "permission_component": self.permission_recovery_component,
            "permission_requested": self.permission_recovery_requested,
            "permission_stop_requested": self.permission_recovery_stop_requested,
            "last_foreground_component": self._last_foreground_component,
        }

        if recovery is not None:
            evidence["recovery"] = dict(recovery)
        manifest_path = directory / "permission_recovery.json"
        # Add the manifest path before serializing so the JSON is self-
        # describing when copied out of the report directory.
        evidence["artifacts"]["manifest"] = str(manifest_path)
        try:
            manifest_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            evidence["errors"].append("write manifest: %s" % exc)
        return evidence

    def _relaunch_target(self, timeout: int = 15) -> bool:
        """Bring only the configured package's existing task back to the front."""
        component = self._resolve_launch_component()
        if not component:
            logger.warning(
                "Unable to resolve launcher component for %s on %s",
                self.package_name,
                self.device_serial,
            )
            return False
        result = self._run_adb(
            [
                "shell",
                "am",
                "start",
                "--activity-reorder-to-front",
                "-n",
                component,
            ],
            timeout=max(3, int(timeout)),
            retries=1,
        )
        if result.returncode != 0:
            logger.warning(
                "Unable to restore target package %s on %s: %s",
                self.package_name,
                self.device_serial,
                (result.stderr or result.stdout).strip(),
            )
            return False
        self._foreground_restore_count += 1
        return True

    def _resumed_activity_component(self) -> Optional[str]:
        """Return the resumed Activity even when another window holds focus."""
        try:
            result = self._run_adb(
                ["shell", "dumpsys", "activity", "activities"],
                timeout=5,
                retries=1,
            )
        except Exception:
            return None
        for line in (result.stdout or "").splitlines():
            if "topResumedActivity" not in line and "mResumedActivity" not in line:
                continue
            match = re.search(
                r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)", line
            )
            if match:
                return self._normalize_component("%s/%s" % match.groups())
        return None

    def blocking_window_blocked(self) -> bool:
        """Return whether a system/IME window permanently blocks injection."""
        return bool(self._blocking_window_blocked)

    def _dismiss_blocking_window(self, stop_event: threading.Event) -> bool:
        """Close a dialog or IME panel that sits above the target app.

        Only acts when the resumed Activity still belongs to the target: a
        dialog or IME panel on top of our own Activity consumes BACK, while a
        genuinely different app in the foreground is left untouched.
        """
        if stop_event.is_set():
            return False
        resumed = self._resumed_activity_component()
        if not resumed or not resumed.startswith(self.package_name + "/"):
            return False
        focused = self._foreground_component_probe(
            self.FOREGROUND_PROBE_TIMEOUT_SECONDS
        )
        if not focused or focused.split("/", 1)[0] == self.package_name:
            return False
        if self._blocking_window_blocked:
            return False
        logger.warning(
            "Blocking window %s sits above %s on %s; sending one controlled BACK",
            focused,
            self.package_name,
            self.device_serial,
        )
        try:
            self._run_adb(
                ["shell", "input", "keyevent", "BACK"], timeout=5, retries=1
            )
        except Exception as exc:
            logger.warning(
                "BACK for blocking window failed on %s: %s", self.device_serial, exc
            )
        recovered = (
            self._wait_for_target_foreground(stop_event, timeout=1.5) is not None
        )
        if recovered:
            # A dismissal that worked proves the window is transient; keep the
            # counter free so a later, unrelated window still gets its own try.
            self._blocking_window_dismiss_count = 0
            return True
        self._blocking_window_dismiss_count += 1
        if self._blocking_window_dismiss_count >= self.BLOCKING_WINDOW_DISMISS_LIMIT:
            self._blocking_window_blocked = True
            logger.error(
                "Injection stays blocked by a foreign window on %s after %s BACK "
                "attempts; stopping this cycle instead of restarting the engine",
                self.device_serial,
                self._blocking_window_dismiss_count,
            )
        return False

    def _wait_for_target_foreground(
        self, stop_event: threading.Event, timeout: float
    ) -> Optional[str]:
        """Wait for the target without letting a single probe exceed the window."""
        requested = max(0.0, float(timeout))
        # ``timeout=0`` is used for the settle-race check. Keep one cheap
        # confirmation probe for compatibility, but never fall back to the
        # historical unbounded pair of dumpsys calls.
        window = (
            self.FOREGROUND_ZERO_WAIT_PROBE_SECONDS
            if requested <= 0
            else requested
        )
        deadline = time.monotonic() + window
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                component = self._foreground_component_probe(
                    min(self.FOREGROUND_PROBE_TIMEOUT_SECONDS, max(0.1, remaining))
                )
            except Exception:
                component = None
            if component:
                self._last_foreground_component = component
                self._last_foreground_component_at = time.monotonic()
                if component.split("/", 1)[0] == self.package_name:
                    return component
            if requested <= 0:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or stop_event.wait(min(0.2, remaining)):
                break
        return None

    def _restore_target_foreground(
        self,
        stop_event: threading.Event,
        timeout: float = 8.0,
        *,
        allow_ui_dump: bool = True,
    ) -> bool:
        """Safely restore the target after Fastbot navigates to another app.

        Xiaomi and a few other vendor launchers can win the foreground race
        for several hundred milliseconds.  Relaunching while Fastbot is still
        injecting events merely sends the app straight back to the launcher.
        Pause only the validated pressure-engine PIDs, bring the explicit
        launcher component forward, wait until the target owns the foreground,
        and complete an audit gate before releasing input again.
        """
        if stop_event.is_set():
            return False
        if self._system_dialog_above_target():
            logger.info(
                "Transient vendor system dialog above %s on %s; skipping relaunch",
                self.package_name,
                self.device_serial,
            )
            return False
        with self._foreground_restore_lock:
            if self._blocking_window_blocked:
                # A foreign system/IME window keeps focus and BACK did not clear
                # it. Stop this cycle with an explicit reason instead of
                # relaunching the app forever.
                self._foreground_restore_failed = True
                self._foreground_restore_error = (
                    "input injection blocked by a foreign window (IME or system "
                    "dialog); manual device cleanup required"
                )
                self._foreground_recovery_requested = True
                self._foreground_recovery_stop_requested = True
                self._record_pressure_stop_intent("foreground_recovery")
                return False
            now = time.monotonic()
            # Avoid duplicate launches when a vendor reports the launcher for
            # two consecutive probes during the same transition.
            if now - self._last_foreground_restore_at < 0.5:
                return False
            self._last_foreground_restore_at = now
            self._foreground_recovery_requested = True
            paused_pids = self._signal_remote_pids(
                self._foreground_pressure_pids(), 19  # Android SIGSTOP
            )
            audit_handoff_requested = False
            try:
                if stop_event.is_set():
                    return False
                # Vendor gesture navigation can expose Home for only a few
                # frames and then return to the target task by itself. Freeze
                # input immediately, but confirm the loss of foreground before
                # issuing an activity launch. This avoids turning a harmless
                # transition into a killed pressure engine.
                if stop_event.wait(self.FOREGROUND_RESTORE_SETTLE_SECONDS):
                    return False
                target_component = self._wait_for_target_foreground(
                    stop_event, timeout=0.0
                )
                if not target_component and self._dismiss_blocking_window(stop_event):
                    # A system/IME window above our own Activity consumed the
                    # BACK; the target is frontmost again without a relaunch.
                    target_component = self._wait_for_target_foreground(
                        stop_event, timeout=0.0
                    )
                if target_component:
                    self._foreground_restore_failed = False
                    self._foreground_recovery_requested = False
                    self._foreground_restore_error = None
                    return True
                launch_error: Optional[BaseException] = None
                launch_acknowledged = False
                try:
                    launch_acknowledged = bool(
                        self._relaunch_target(
                            timeout=self.FOREGROUND_RELAUNCH_TIMEOUT_SECONDS
                        )
                    )
                except Exception as exc:
                    # ``adb shell am start`` may time out while the
                    # ActivityManager request continues on-device.  A timeout
                    # (or a non-zero acknowledgement) is therefore only a
                    # launch warning until the actual foreground is checked.
                    launch_error = exc

                target_component = self._wait_for_target_foreground(
                    stop_event,
                    timeout=max(
                        self.FOREGROUND_RELAUNCH_VERIFY_SECONDS,
                        self.FOREGROUND_RELAUNCH_FINAL_GRACE_SECONDS,
                    ),
                )
                launched = target_component is not None
                if launched:
                    if not launch_acknowledged:
                        self._foreground_restore_count += 1
                    if launch_error is not None:
                        logger.info(
                            "Target package reached foreground after a timed-out "
                            "relaunch on %s",
                            self.device_serial,
                        )

                if not launched:
                    logger.warning(
                        "Target relaunch remained blocked on %s after foreground "
                        "verification; stopping the paused pressure engine before "
                        "one final retry: %s",
                        self.device_serial,
                        launch_error or "activity manager did not acknowledge the launch",
                    )
                    # A vendor activity manager can remain blocked while the
                    # Fastbot UiAutomation session is alive. Killing only the
                    # already-paused, validated engine PIDs releases that
                    # session and is safer than allowing random input on Home.
                    if paused_pids:
                        # Publish the stop owner before SIGKILL. The detached
                        # wrapper will otherwise surface the intentional kill
                        # as return code 137, indistinguishable from an LMK.
                        self._foreground_recovery_stop_requested = True
                        self._record_pressure_stop_intent(
                            "foreground_recovery"
                        )
                        stopped_pids = self._signal_remote_pids(paused_pids, 9)
                    time.sleep(0.5)
                    launch_error = None
                    launch_acknowledged = False
                    try:
                        launch_acknowledged = bool(
                            self._relaunch_target(
                                timeout=self.FOREGROUND_RELAUNCH_TIMEOUT_SECONDS
                            )
                        )
                    except Exception as exc:
                        launch_error = exc
                    # Always verify the second attempt, including a false or
                    # timed-out ADB return.  The app can become foreground just
                    # after the shell command's host timeout.
                    target_component = self._wait_for_target_foreground(
                        stop_event,
                        timeout=max(
                            self.FOREGROUND_RELAUNCH_VERIFY_SECONDS,
                            self.FOREGROUND_RELAUNCH_FINAL_GRACE_SECONDS,
                            max(1.0, float(timeout)),
                        ),
                    )
                    launched = target_component is not None
                    if launched and not launch_acknowledged:
                        self._foreground_restore_count += 1
                if not launched:
                    self._foreground_restore_failed = True
                    # Do not resume random input on the launcher after the
                    # bounded retries are exhausted.  The phase owner will
                    # stop the engine and perform the normal target-app
                    # recovery path.
                    self._foreground_recovery_stop_requested = True
                    self._record_pressure_stop_intent("foreground_recovery")
                    self._foreground_restore_error = (
                        "target package did not return to foreground after ADB relaunch"
                    )
                    return False
                if target_component is None:
                    target_component = self._wait_for_target_foreground(
                        stop_event, timeout=timeout
                    )
                if target_component:
                    # A later successful handoff clears a stale failure from
                    # an earlier launcher race; otherwise the phase cleanup
                    # can be misclassified even though the app is back.
                    self._foreground_restore_failed = False
                    if not self._foreground_recovery_stop_requested:
                        self._foreground_recovery_requested = False
                    self._foreground_restore_error = None
                    # The target is back. Clear a newly rendered audit page
                    # while input remains paused, closing the
                    # relaunch-to-random-tap race on Xiaomi.
                    if self._audit_recovery_options():
                        if not allow_ui_dump:
                            if self._is_runtime_audit_candidate(target_component):
                                audit_handoff_requested = self.request_audit_recovery(
                                    target_component,
                                    paused_pids=paused_pids,
                                )
                            return True
                        # If the vendor activity manager required the emergency
                        # retry above, the pressure process may already be gone.
                        # In that case ``_recover_audit_page`` cannot pause any
                        # PIDs and would silently leave the gate on screen; use
                        # the synchronous, idempotent prelaunch path instead.
                        active_pids = MutexController(
                            self.device_serial
                        )._remote_processes()
                        if active_pids:
                            self._recover_audit_page(stop_event)
                        else:
                            self.prepare_audit_gate(timeout=8.0)
                    return True
                logger.warning(
                    "Target package did not return to foreground on %s within %.1fs",
                    self.device_serial,
                    max(1.0, float(timeout)),
                )
                self._foreground_restore_failed = True
                self._foreground_restore_error = (
                    "target package did not return to foreground within %.1fs"
                    % max(1.0, float(timeout))
                )
                return False
            finally:
                if (
                    paused_pids
                    and not audit_handoff_requested
                    and not self._foreground_recovery_stop_requested
                ):
                    resumed_pids, unresolved = self._resume_paused_remote_pids(
                        paused_pids
                    )
                    not_resumed = sorted(unresolved)
                    if not_resumed:
                        logger.warning(
                            "Unable to resume pressure-engine PIDs on %s: %s",
                            self.device_serial,
                            ", ".join(not_resumed),
                        )

    def _pressure_input_text(self) -> str:
        scenario = self.config.get("scenario", {})
        data = scenario.get("random_input_data")
        if data:
            case = scenario["input_cases"][data]
            values = case if isinstance(case, list) else case["values"]
            return values[(max(1, self._active_cycle or 1) - 1) % len(values)]
        return input_safety_options(self.config).get("default_text", "test123")

    def _request_input_recovery(self, reason: str, pids: Optional[List[str]] = None) -> None:
        self.input_recovery_reason = reason
        self._foreground_recovery_requested = True
        self._foreground_recovery_stop_requested = True
        self._foreground_restore_error = self.input_recovery_reason
        # Record intent before killing a frozen process so exit 137 is a
        # recoverable page interruption, not an application crash/failure.
        self._record_pressure_stop_intent("foreground_recovery")
        self._signal_remote_pids(pids or self._foreground_pressure_pids(), 9)

    def _start_foreground_guard(
        self,
        process: subprocess.Popen,
        *,
        recover_audit: bool = False,
        interval_seconds: Optional[float] = None,
        allow_ui_dump: bool = True,
        startup_grace_seconds: float = 0.0,
        audit_startup_grace_seconds: float = 0.0,
    ) -> None:
        """Keep pressure input scoped to the target package's foreground page."""
        if not self._stop_foreground_guard():
            raise RuntimeError(
                "Previous foreground guard is still active on %s"
                % self.device_serial
            )
        stop_event = threading.Event()
        interval = (
            self.FOREGROUND_GUARD_INTERVAL_SECONDS
            if interval_seconds is None
            else max(0.2, float(interval_seconds))
        )
        audit_candidate_not_before = time.monotonic() + max(
            0.0, float(audit_startup_grace_seconds)
        )

        def worker() -> None:
            last_permission_component: Optional[str] = None
            last_privacy_component: Optional[str] = None
            last_privacy_recovery_at = 0.0
            last_audit_probe_component: Optional[str] = None
            last_audit_probe_at = 0.0
            last_non_target_component: Optional[str] = None
            non_target_since = 0.0
            non_target_probes = 0
            blocking_focus_probes = 0
            # A cached gate Activity is only a candidate, not proof that the
            # blocking controls are rendered. Arm the runtime handoff after
            # observing a normal target Activity; this prevents an app whose
            # steady-state page happens to share its launcher Activity from
            # being stopped repeatedly at the beginning of every phase.
            audit_candidate_armed = False
            audit_candidate_component: Optional[str] = None
            audit_candidate_since = 0.0
            audit_candidate_probes = 0

            def clear_audit_candidate_observation() -> None:
                nonlocal audit_candidate_component, audit_candidate_since
                nonlocal audit_candidate_probes
                audit_candidate_component = None
                audit_candidate_since = 0.0
                audit_candidate_probes = 0

            def clear_non_target_observation() -> None:
                nonlocal last_non_target_component, non_target_since, non_target_probes
                last_non_target_component = None
                non_target_since = 0.0
                non_target_probes = 0

            if stop_event.wait(max(0.0, float(startup_grace_seconds))):
                return
            while not stop_event.wait(interval):
                if process.poll() is not None:
                    return
                try:
                    now = time.monotonic()
                    component = self._foreground_component_probe(
                        self.FOREGROUND_PROBE_TIMEOUT_SECONDS
                    )
                    if not component:
                        if input_safety_options(self.config).get("enabled"):
                            self._request_input_recovery("foreground ownership could not be verified")
                            return
                        clear_non_target_observation()
                        clear_audit_candidate_observation()
                        continue
                    self._last_foreground_component = component
                    self._last_foreground_component_at = time.monotonic()
                    current = component.split("/", 1)[0]
                    if self._is_app_crash_page_component(component):
                        clear_non_target_observation()
                        self._record_app_crash_page(component)
                        continue
                    if recover_audit and self._is_privacy_dialog_component(component):
                        # MIUI's consent page is owned by SecurityCenter, so it
                        # would otherwise fall through to the generic
                        # non-target restore path.  That path relaunches the
                        # app and can leave the page half-rendered.  Treat it
                        # as a first-class gate and use the same freeze-before-
                        # UI handoff as the audit page.
                        clear_non_target_observation()
                        if component != last_privacy_component:
                            logger.info(
                                "Privacy-consent dialog is in foreground on %s: %s",
                                self.device_serial,
                                component,
                            )
                        if not allow_ui_dump:
                            request_audit = getattr(
                                self, "request_audit_recovery", None
                            )
                            if callable(request_audit):
                                request_audit(component)
                        elif (
                            component != last_privacy_component
                            or now - last_privacy_recovery_at >= 0.5
                        ):
                            self._recover_audit_page(stop_event)
                            last_privacy_recovery_at = now
                        last_privacy_component = component
                        continue
                    last_privacy_component = None
                    if self._is_permission_dialog_component(component):
                        clear_non_target_observation()
                        if component != last_permission_component:
                            self._permission_dialog_count += 1
                            logger.info(
                                "Runtime permission dialog is in foreground on %s: %s",
                                self.device_serial,
                                component,
                            )
                        if not allow_ui_dump:
                            # Custom Fastbot owns UiAutomation and has its own
                            # activity controller. Freeze it immediately and
                            # let the phase owner restore uiautomator2 before
                            # inspecting or tapping the dialog. Continuing
                            # here would allow a transition-time random event
                            # to dismiss the prompt or trigger an app restart.
                            request_permission = getattr(
                                self, "request_permission_recovery", None
                            )
                            if callable(request_permission):
                                request_permission(component)
                            else:
                                # Compatibility for older adapters that do not
                                # expose the handoff API: retain the bounded
                                # pause rather than injecting another event.
                                self._settle_permission_dialog(stop_event)
                            last_permission_component = component
                            continue
                        # The built-in Monkey does not hold the custom
                        # UiAutomation registration, so it can use the legacy
                        # in-place grant path. It still freezes input while
                        # the hierarchy is read and falls back to a settle
                        # window when the vendor UI is not yet rendered.
                        if self._grant_runtime_permission_dialog(stop_event):
                            last_permission_component = None
                        else:
                            self._settle_permission_dialog(stop_event)
                            last_permission_component = component
                        continue
                    last_permission_component = None
                    # System permission/consent Activities have their own
                    # packages. Hand them off above. Other external windows
                    # need an exclusive handoff: am start can hang while a
                    # frozen Fastbot still owns UiAutomation on vendor devices.
                    if input_safety_options(self.config).get("enabled") and current != self.package_name:
                        clear_non_target_observation()
                        logger.warning(
                            "External foreground %s on %s; stopping pressure input "
                            "before recovering %s",
                            component,
                            self.device_serial,
                            self.package_name,
                        )
                        self._request_input_recovery(
                            "foreground moved to external window: %s" % component
                        )
                        return
                    if current == self.package_name:
                        clear_non_target_observation()
                    if recover_audit and current == self.package_name:
                        options = self._audit_recovery_options()
                        if options:
                            if not allow_ui_dump:
                                # Custom Fastbot owns Android's UiAutomation
                                # registration. Never start uiautomator dump
                                # beside it. An in-package process restart or a
                                # cached gate Activity requests a clean phase
                                # handoff; the hierarchy is read only after
                                # Fastbot has fully stopped.
                                is_candidate = self._is_runtime_audit_candidate(
                                    component
                                )
                                if not is_candidate:
                                    audit_candidate_armed = True
                                    clear_audit_candidate_observation()
                                elif now < audit_candidate_not_before:
                                    # Fastbot launches the package's launcher
                                    # Activity at the beginning of every phase.
                                    # That short SplashActivity transition is
                                    # not evidence that the blocking gate has
                                    # returned; allow it to settle back to the
                                    # normal target Activity before arming a
                                    # handoff.
                                    clear_audit_candidate_observation()
                                elif audit_candidate_armed:
                                    if component == audit_candidate_component:
                                        audit_candidate_probes += 1
                                    else:
                                        audit_candidate_component = component
                                        audit_candidate_since = now
                                        audit_candidate_probes = 1
                                    # The probe count is the actual debounce
                                    # signal.  Requiring an additional wall
                                    # clock interval makes this handoff flaky
                                    # when the guard thread is briefly delayed
                                    # and Fastbot exits between two samples;
                                    # consecutive samples already provide the
                                    # required transient-page protection.
                                    candidate_confirmed = bool(
                                        audit_candidate_probes
                                        >= self.AUDIT_RUNTIME_CANDIDATE_PROBES
                                    )
                                    if candidate_confirmed and self.request_audit_recovery(
                                        component
                                    ):
                                        logger.warning(
                                            "Confirmed startup gate candidate %s appeared "
                                            "during pressure on %s; requesting a safe handoff",
                                            component,
                                            self.device_serial,
                                        )
                                # Hard isolation boundary: custom Fastbot owns
                                # UiAutomation for its complete lifetime. Never
                                # fall through to the legacy hierarchy-based
                                # recovery path, even when this sample is a
                                # normal target Activity or the candidate has
                                # not been armed yet.
                                continue
                            # ADB hierarchy dumps can be stale or unavailable
                            # while Fastbot owns UiAutomation.  For the
                            # generic (activity-less) rule, pause Fastbot
                            # first and let _recover_audit_page perform the
                            # authoritative dump/click sequence.  Probe on an
                            # Activity transition immediately, then at a
                            # bounded cadence while the same page remains in
                            # front so a relaunch cannot be missed.
                            now = time.monotonic()
                            should_probe = bool(options["activity"])
                            if not should_probe:
                                should_probe = (
                                    component != last_audit_probe_component
                                    or now - last_audit_probe_at >= 0.5
                                )
                            if should_probe and (
                                not options["activity"]
                                or self._is_audit_activity(component)
                            ):
                                last_audit_probe_component = component
                                last_audit_probe_at = now
                                self._recover_audit_page(stop_event)
                                continue
                    if current and current != self.package_name:
                        if self._system_dialog_above_target():
                            # Vendor permission/consent popups are windows, not
                            # Activities: dumpsys reports the launcher for a
                            # frame while the prompt is up.  Do not tear down
                            # pressure for it; Fastbot's allow-list may answer
                            # the prompt on its own.
                            clear_non_target_observation()
                            continue
                        if component == last_non_target_component:
                            non_target_probes += 1
                        else:
                            last_non_target_component = component
                            non_target_since = now
                            non_target_probes = 1
                        if (
                            non_target_probes < self.FOREGROUND_LOSS_CONFIRM_PROBES
                            and now - non_target_since
                            < self.FOREGROUND_LOSS_CONFIRM_SECONDS
                        ):
                            # A single launcher frame is common during Android
                            # task transitions. Do not tear down Fastbot until
                            # the loss is observed consistently.
                            continue
                        clear_non_target_observation()
                        logger.warning(
                            "Foreground component changed to %s; restoring %s on %s",
                            component,
                            self.package_name,
                            self.device_serial,
                        )
                        self._restore_target_foreground(
                            stop_event, allow_ui_dump=allow_ui_dump
                        )
                    elif current == self.package_name:
                        # The resumed Activity is ours, but window focus can sit
                        # on an IME panel or a system dialog. Fastbot's guarded
                        # source then blocks every event while the resumed-page
                        # probe above sees a healthy foreground. Check focus on
                        # a slower cadence and close such a window with one
                        # controlled BACK.
                        blocking_focus_probes += 1
                        if self._blocking_window_blocked:
                            self._restore_target_foreground(
                                stop_event, allow_ui_dump=allow_ui_dump
                            )
                        elif blocking_focus_probes % 5 == 0:
                            self._dismiss_blocking_window(stop_event)
                except Exception:
                    logger.debug(
                        "Foreground guard probe failed on %s",
                        self.device_serial,
                        exc_info=True,
                    )

        thread = threading.Thread(
            target=worker,
            name="foreground-guard-%s" % self.device_serial,
            daemon=True,
        )
        thread.start()
        self._foreground_guard = (stop_event, thread)

    def _stop_foreground_guard(self) -> bool:
        job = self._foreground_guard
        if job is None:
            return True
        stop_event, thread = job
        stop_event.set()
        thread.join(timeout=self.FOREGROUND_GUARD_STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            self._foreground_guard_stop_failed = True
            logger.error(
                "Foreground guard did not stop on %s within %s seconds",
                self.device_serial,
                self.FOREGROUND_GUARD_STOP_TIMEOUT_SECONDS,
            )
            return False
        self._foreground_guard = None
        return True

    @property
    def foreground_guard_stop_failed(self) -> bool:
        """Return whether a foreground guard missed its shutdown deadline."""
        return self._foreground_guard_stop_failed

    @property
    def foreground_restore_failed(self) -> bool:
        """Return whether the target could not be restored after a foreground loss."""
        return self._foreground_restore_failed

    @property
    def foreground_recovery_requested(self) -> bool:
        """Return whether a foreground handoff is active or failed.

        This remains set when the Fastbot process disappears in the middle of
        the handoff, allowing the phase owner to retry the control-plane
        recovery instead of treating the generic exit code as an app failure.
        """
        return self._foreground_recovery_requested

    @property
    def foreground_restore_error(self) -> Optional[str]:
        """Return the bounded diagnostic for the most recent restore failure."""
        return self._foreground_restore_error

    def mark_pressure_deadline_reached(self) -> None:
        """Mark an intentional wall-clock stop as expected process termination."""
        self._deadline_stop_requested = True
        self._record_pressure_stop_intent("deadline")

    def _start_system_monkey(
        self, duration_minutes: int, throttle_ms: int
    ) -> subprocess.Popen:
        """Run Android's built-in Monkey when a local Fastbot bundle is unavailable."""
        extra_args = self._fastbot_options().get("extra_args", [])
        if not isinstance(extra_args, list):
            raise ValueError("fastbot.extra_args must be a JSON array")
        values = [str(value) for value in extra_args]
        blocked_overrides = {
            "-p",
            "--pkg",
            "--package",
            "--target-package",
            "--component",
            "--throttle",
            "--pct-touch",
            "--pct-motion",
            "--pct-trackball",
            "--pct-syskeys",
            "--pct-nav",
            "--pct-majornav",
            "--pct-appswitch",
            "--pct-flip",
            "--pct-anyevent",
            "--pct-pinchzoom",
            "--pct-permission",
        }
        for value in values:
            option_name = value.split("=", 1)[0]
            if option_name in blocked_overrides:
                raise ValueError(
                    "fastbot.extra_args cannot override the configured system-monkey target"
                )
            if not value.startswith("-"):
                raise ValueError(
                    "system-monkey extra_args may contain options only; event count is managed by the runner"
                )
        if not self._resolve_launch_component():
            raise RuntimeError(
                "Unable to resolve the configured package's launcher activity"
            )
        permission_controllers = self._permission_controller_packages_for_monkey()
        if not permission_controllers:
            logger.warning(
                "Unable to resolve the runtime permission controller on %s; "
                "system Monkey may reject permission requests",
                self.device_serial,
            )
        event_percentages = self._random_event_percentages()
        event_count = self._system_monkey_event_count(duration_minutes, throttle_ms)
        arguments = [
            "monkey",
            "-p",
            self.package_name,
        ]
        for permission_controller in permission_controllers:
            # Monkey installs an activity controller and otherwise rejects every
            # package outside its -p allowlist, including Android's own runtime
            # permission prompt. The foreground guard pauses input as soon as
            # this narrowly resolved system activity appears.
            arguments.extend(["-p", permission_controller])
        arguments.extend([
            "--ignore-crashes",
            "--ignore-timeouts",
            "--ignore-security-exceptions",
            "--pct-touch",
            str(event_percentages["touch"]),
            "--pct-motion",
            str(event_percentages["motion"]),
            "--pct-trackball",
            "0",
            "--pct-syskeys",
            "0",
            "--pct-nav",
            "0",
            "--pct-majornav",
            "0",
            "--pct-appswitch",
            "0",
            "--pct-flip",
            "0",
            "--pct-anyevent",
            "0",
            "--pct-pinchzoom",
            str(event_percentages["pinchzoom"]),
            "--pct-permission",
            "0",
            "--throttle",
            str(int(throttle_ms)),
            "-v",
            "-v",
        ])
        arguments += values + [str(event_count)]
        self._prepare_pressure_engine_start()
        remote_command = " ".join(shlex.quote(value) for value in arguments)
        process = self._spawn_logged_remote(remote_command, "System Monkey")
        self._start_foreground_guard(process, recover_audit=True)
        self._pressure_active.set()
        return process

    def dismiss_keyboard_before_pressure(self) -> None:
        """Send at most one BACK when the IME covers the app before pressure.

        The continuous input-safety guard was removed because vendor IMEs report
        stale visibility flags.  A single dismissal is still needed: apps that
        auto-focus an input keep the keyboard on top and injected touches then
        land on the IME, which looks like a frozen page to the stall watchdog.
        """
        try:
            state = probe_input_method(
                lambda arguments, timeout: self._run_adb(
                    arguments, timeout=timeout, retries=1
                )
            )
        except Exception:
            logger.debug(
                "Unable to read the input-method state before pressure on %s",
                self.device_serial,
                exc_info=True,
            )
            return
        if not state.get("known") or state.get("visible") is not True:
            return
        try:
            self._run_adb(
                ["shell", "input", "keyevent", "BACK"], timeout=5, retries=0
            )
            logger.info(
                "Dismissed the on-screen keyboard before pressure on %s",
                self.device_serial,
            )
        except Exception:
            logger.debug(
                "Unable to dismiss the keyboard before pressure on %s",
                self.device_serial,
                exc_info=True,
            )

    def start_fastbot(
        self, duration_minutes: int, throttle_ms: int
    ) -> Any:
        """Start the configured local input engine with the target package."""
        if duration_minutes <= 0 or throttle_ms < 0:
            raise ValueError("Fastbot duration must be positive and throttle non-negative")
        self.dismiss_keyboard_before_pressure()

        if self.pressure_engine == "system-monkey":
            return self._start_system_monkey(duration_minutes, throttle_ms)

        main_class = str(
            self._fastbot_options().get(
                "main_class", "com.android.commands.monkey.Monkey"
            )
        )
        if not re.fullmatch(
            r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)*[A-Za-z_$][A-Za-z0-9_$]*",
            main_class,
        ):
            raise ValueError("fastbot.main_class is not a valid Java class name")
        extra_args = self._fastbot_options().get("extra_args", [])
        if not isinstance(extra_args, list):
            raise ValueError("fastbot.extra_args must be a JSON array")
        blocked_overrides = {
            "-p",
            "--pkg",
            "--package",
            "--target-package",
            "--component",
            "--agent",
            "--running-minutes",
            "--throttle",
            "--pct-touch",
            "--pct-motion",
            "--pct-trackball",
            "--pct-rotation",
            "--pct-syskeys",
            "--pct-nav",
            "--pct-majornav",
            "--pct-appswitch",
            "--pct-flip",
            "--pct-anyevent",
            "--pct-pinchzoom",
            "--pct-permission",
            "--output-directory",
        }
        for value in extra_args:
            text_value = str(value)
            option_name = text_value.split("=", 1)[0]
            if input_safety_options(self.config).get("enabled") and option_name in {"-f", "--setup", "--port"}:
                raise ValueError("strict input safety requires the guarded event source")
            # Config values are commonly supplied either as two list items
            # (``--output-directory``, ``/sdcard/...``) or as one ``--foo=bar``
            # token.  Block both forms so the executor always owns the safe
            # output location.
            if option_name in blocked_overrides or text_value.strip().startswith(
                "--output-directory "
            ):
                raise ValueError(
                    "fastbot.extra_args cannot override executor-managed Fastbot options"
                )
        event_percentages = self._random_event_percentages()
        # The ADB client can disappear while its remote shell keeps running.
        # Refuse a new launch until the previous device-side engine has been
        # actively cleared and then observed absent.
        self._prepare_pressure_engine_start()
        self._verify_fastbot_files()
        self._push_fastbot_config()
        permission_controllers = self._permission_controller_packages_for_fastbot()
        arguments = [
            "app_process",
            "/system/bin",
            main_class,
            "-p",
            self.package_name,
        ]
        if input_safety_options(self.config).get("enabled"):
            # Only ApeNative installs the synchronous window guard. The
            # bundled default otherwise selects Google's random source.
            arguments[1:1] = [
                "-Dapp.press.inputText=" + self._pressure_input_text(),
                "-Dapp.press.targetPackage=" + self.package_name,
            ]
            arguments.extend(["--agent", "reuseq"])
        for permission_controller in permission_controllers:
            # Fastbot's activity controller otherwise treats the vendor
            # permission package as an out-of-scope resume and immediately
            # restarts the target while the dialog is still rendering.
            arguments.extend(["-p", permission_controller])
        # Always pass this explicitly.  Without it Fastbot concatenates every
        # allowed package into one filename and Android can reject mkdirs
        # before the engine starts (especially on vendor permission UIs).
        arguments.extend(["--output-directory", self._fastbot_output_directory()])
        arguments.extend([
            "--pct-touch",
            str(event_percentages["touch"]),
            "--pct-motion",
            str(event_percentages["motion"]),
            "--pct-trackball",
            "0",
            "--pct-rotation",
            "0",
            "--pct-syskeys",
            "0",
            "--pct-nav",
            "0",
            "--pct-majornav",
            "0",
            "--pct-appswitch",
            "0",
            "--pct-flip",
            "0",
            "--pct-anyevent",
            "0",
            "--pct-pinchzoom",
            str(event_percentages["pinchzoom"]),
            "--pct-permission",
            "0",
            "--running-minutes",
            str(int(duration_minutes)),
            "--throttle",
            str(int(throttle_ms)),
            "-v",
            "-v",
        ] + [str(value) for value in extra_args])
        remote_command = "CLASSPATH=%s exec %s" % (
            shlex.quote(self._fastbot_classpath()),
            " ".join(shlex.quote(value) for value in arguments),
        )

        startup_error: Optional[str] = None
        guard_started = False
        process: Optional[Any] = None
        for attempt in range(1, self.FASTBOT_STARTUP_RETRY_ATTEMPTS + 1):
            self._remove_stale_fastbot_output(duration_minutes)
            pressure_log_path = self.report_dir / "fastbot.log"
            log_offset = pressure_log_path.stat().st_size if pressure_log_path.exists() else 0
            try:
                process = self._spawn_detached_fastbot(remote_command, "Fastbot")
            except BaseException as exc:
                # The launch shell may have reached the device even when ADB
                # returned no PID.  Probe and clear it, but preserve the
                # original launch exception for the caller/report.
                self._cleanup_failed_fastbot_start(None, exc)
                raise
            # Start the guard before the startup verification window.  Custom
            # Fastbot can inject immediately, and Xiaomi may send the app to
            # the launcher before the old post-startup guard was scheduled.
            try:
                self._start_foreground_guard(
                    process,
                    recover_audit=bool(self._audit_recovery_options()),
                    interval_seconds=self.FASTBOT_FOREGROUND_GUARD_INTERVAL_SECONDS,
                    allow_ui_dump=False,
                    startup_grace_seconds=0.0,
                    audit_startup_grace_seconds=(
                        self.FASTBOT_FOREGROUND_GUARD_STARTUP_GRACE_SECONDS
                    ),
                )
                guard_started = True
                startup_error = self._verify_fastbot_startup(process, log_offset)
            except BaseException as exc:
                self._cleanup_failed_fastbot_start(process, exc)
                guard_started = False
                raise
            if self._foreground_recovery_stop_requested and self.input_recovery_reason:
                break
            if startup_error is None:
                break
            retry_reason = self._fastbot_startup_retry_reason(startup_error)
            # Snapshot a concurrent web-console stop before cleanup. Cleanup
            # consumes the device marker after classifying the process exit;
            # checking only afterwards could relaunch despite an operator stop.
            manual_stop_before_cleanup = bool(
                retry_reason
                and MutexController(self.device_serial).has_stop_request()
            )
            cleanup_ok = self._cleanup_failed_fastbot_start(process, startup_error)
            guard_started = False
            process = None
            if not cleanup_ok:
                raise PressureEngineError(
                    "%s; failed startup could not be cleaned safely: %s"
                    % (
                        startup_error,
                        self._last_pressure_stop_error
                        or "device-side Fastbot state is unknown",
                    )
                )
            if retry_reason is None:
                raise PressureEngineError(startup_error)
            handoff = MutexController(self.device_serial)
            # A web-console stop can race the startup verification.  Never
            # clear or bypass that durable marker by launching a retry.
            if manual_stop_before_cleanup or handoff.has_stop_request():
                raise PressureEngineError(
                    "%s; retry cancelled because a manual stop was requested"
                    % startup_error
                )
            if attempt >= self.FASTBOT_STARTUP_RETRY_ATTEMPTS:
                raise PressureEngineError(
                    "Fastbot startup did not recover after %s attempts on %s "
                    "(%s): %s"
                    % (attempt, self.device_serial, retry_reason, startup_error)
                )
            if not handoff._stop_remote_uiautomator_processes():
                raise PressureEngineError(
                    "%s; stale uiautomator2 processes could not be stopped"
                    % startup_error
                )
            if not handoff._wait_for_uiautomation_release():
                raise PressureEngineError(
                    "%s; stale UiAutomation registration did not release"
                    % startup_error
                )
            delay = self.FASTBOT_STARTUP_RETRY_DELAYS[attempt - 1]
            logger.warning(
                "Fastbot startup handoff failed on %s (%s); retrying in %ss "
                "(%s/%s)",
                self.device_serial,
                retry_reason,
                delay,
                attempt,
                self.FASTBOT_STARTUP_RETRY_ATTEMPTS,
            )
            self._last_pressure_stop_error = None
            self._last_pressure_stop_remote_stopped = None
            time.sleep(delay)
        # The guard remains active for the full custom-Fastbot lifetime.  It
        # restores the target package even when no audit configuration exists;
        # audit recovery is enabled independently when the generic gate rule
        # is available.
        if process is None:
            raise PressureEngineError(
                startup_error or "Fastbot startup returned no process handle"
            )
        if not guard_started:
            self._start_foreground_guard(
                process,
                recover_audit=bool(self._audit_recovery_options()),
                interval_seconds=self.FASTBOT_FOREGROUND_GUARD_INTERVAL_SECONDS,
                allow_ui_dump=False,
                startup_grace_seconds=0.0,
                audit_startup_grace_seconds=(
                    self.FASTBOT_FOREGROUND_GUARD_STARTUP_GRACE_SECONDS
                ),
            )
        self._pressure_active.set()
        return process

    @staticmethod
    def _send_interrupt(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            process.terminate()

    def _close_process_file(self, process: subprocess.Popen) -> None:
        handle = self._process_files.pop(id(process), None)
        if handle is not None and not handle.closed:
            handle.flush()
            handle.close()

    def _wait_for_detached_fastbot_exit(
        self, process: _DetachedRemoteProcess
    ) -> bool:
        """Ensure the tracked detached wrapper has observed its final exit."""
        pid = str(process.remote_pid or "")
        if not re.fullmatch(r"[1-9][0-9]*", pid):
            return False
        deadline = time.monotonic() + self.DETACHED_EXIT_SETTLE_SECONDS
        probe = (
            "if [ -r /proc/{pid}/stat ]; then "
            "read proc_pid proc_name proc_state proc_rest < /proc/{pid}/stat; "
            "echo \"$proc_state\"; else echo EXITED; fi"
        ).format(pid=pid)
        while True:
            try:
                result = self._run_adb(["shell", probe], timeout=2, retries=1)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                result = None
            if result is not None and result.returncode == 0:
                token = (result.stdout or "").strip().splitlines()
                if token and token[-1].strip() in {"EXITED", "Z", "X"}:
                    return True
            if time.monotonic() >= deadline:
                logger.warning(
                    "Detached Fastbot wrapper PID %s did not settle on %s",
                    pid,
                    self.device_serial,
                )
                return False
            time.sleep(0.2)

    def stop_fastbot(self, process: subprocess.Popen) -> bool:
        """Stop host and device engines; success requires device-side absence."""
        clean_exit = True
        self._last_pressure_stop_remote_stopped = None
        stop_errors: List[str] = []
        host_stop_escalated = False
        detached_process = isinstance(process, _DetachedRemoteProcess)
        unresolved_resume_pids: Set[str] = set()
        self._pressure_active.clear()
        controller = MutexController(self.device_serial)
        manual_stop_requested = controller.has_stop_request()
        if manual_stop_requested:
            self._record_pressure_stop_intent("manual", process)
        if not self._stop_foreground_guard():
            clean_exit = False
            stop_errors.append("foreground guard did not stop within its timeout")
        # The guard can request or complete a foreground handoff while the
        # caller is joining it. Snapshot all owners only after that join.
        stall_recovery_stop = self._stall_recovery_stop_requested
        audit_recovery_stop = self._audit_recovery_stop_requested
        crash_stop = self._crash_stop_requested
        foreground_recovery_stop = self._foreground_recovery_stop_requested
        intent_reason = self.pressure_stop_intent_reason
        if detached_process and not intent_reason:
            intent_reason = self._read_remote_pressure_stop_intent(process)
        if detached_process and not intent_reason:
            # The device-side intent file can disappear with the wrapper
            # before the host reaches this classification point. The local
            # append-only record is the last bounded source of ownership.
            intent_reason = self._read_local_pressure_stop_intent(process)
        with self._audit_recovery_lock:
            # Cancellation or a manual stop may race with the main wait loop
            # before it claims the gate request. Always release an engine
            # frozen by the guard so a queued stop signal can take effect.
            paused_for_audit = list(self._audit_recovery_paused_pids)
        with self._permission_recovery_lock:
            # Permission recovery uses the same freeze-before-stop handoff as
            # the startup-gate path. Include those PIDs here as well; leaving
            # them stopped can make the subsequent cleanup appear hung and
            # prevents a later Fastbot launch from receiving input.
            paused_for_permission = list(self._permission_recovery_paused_pids)
        paused_for_recovery = list(
            dict.fromkeys(paused_for_audit + paused_for_permission)
        )
        recovery_pids_resumed = False
        try:
            if process.poll() is None:
                if detached_process:
                    process.send_signal(2)
                else:
                    self._send_interrupt(process)
                # A gate request freezes the engine before the wait loop can
                # stop it. Queue the interrupt while it is frozen, then resume
                # only so the process can consume that signal. This prevents a
                # random event from landing on the blocking gate between the
                # guard request and the intentional stop.
                if paused_for_recovery:
                    _resumed, unresolved = self._resume_paused_remote_pids(
                        paused_for_recovery
                    )
                    recovery_pids_resumed = True
                    if unresolved:
                        unresolved_resume_pids.update(unresolved)
                        logger.warning(
                            "Paused Fastbot PIDs were not immediately resumed on %s: %s",
                            self.device_serial,
                            ", ".join(sorted(unresolved)),
                        )
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    host_stop_escalated = True
                    try:
                        process.terminate()
                    except Exception as exc:
                        stop_errors.append("Fastbot terminate failed: %s" % exc)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except Exception as exc:
                            stop_errors.append("Fastbot kill failed: %s" % exc)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            clean_exit = False
                            stop_errors.append(
                                "Fastbot process did not exit after kill"
                            )
                except Exception as exc:
                    clean_exit = False
                    stop_errors.append("Fastbot stop wait failed: %s" % exc)
        except Exception as exc:
            clean_exit = False
            stop_errors.append("Fastbot stop signal failed: %s" % exc)
        finally:
            if paused_for_recovery and not recovery_pids_resumed:
                _resumed, unresolved = self._resume_paused_remote_pids(
                    paused_for_recovery
                )
                if unresolved:
                    unresolved_resume_pids.update(unresolved)
                    logger.warning(
                        "Paused Fastbot PIDs were not immediately resumed on %s: %s",
                        self.device_serial,
                        ", ".join(sorted(unresolved)),
                    )
            try:
                if detached_process:
                    # The detached wrapper owns the real app_process session;
                    # verify that tracked PID directly before falling back to
                    # the package/ps scan for stale engines from older runs.
                    fallback_stopped = controller._kill_fastbot_if_running()
                    # The device-side probe is authoritative.  The detached
                    # wrapper can lag behind its status file while ADB is
                    # settling, even after the remote process is gone.
                    wrapper_stopped = self._wait_for_detached_fastbot_exit(process)
                    remote_stopped = bool(fallback_stopped and wrapper_stopped)
                    if not wrapper_stopped:
                        clean_exit = False
                        stop_errors.append(
                            "detached Fastbot wrapper did not settle after stop"
                        )
                    if remote_stopped and process.returncode is None:
                        process.returncode = 0
                else:
                    remote_stopped = controller._kill_fastbot_if_running()
            except Exception:
                logger.exception(
                    "Unable to verify device-side Fastbot stop on %s",
                    self.device_serial,
                )
                stop_errors.append("device-side Fastbot stop verification failed")
                remote_stopped = False
            self._last_pressure_stop_remote_stopped = bool(remote_stopped)
            if unresolved_resume_pids and not remote_stopped:
                clean_exit = False
                stop_errors.append(
                    "unable to resume paused pressure-engine PIDs: %s"
                    % ", ".join(sorted(unresolved_resume_pids))
                )
            elif unresolved_resume_pids:
                logger.info(
                    "Fastbot had already exited before paused PID handoff completed "
                    "on %s; remote cleanup verified",
                    self.device_serial,
                )
            if not remote_stopped:
                clean_exit = False
                stop_errors.append("device-side Fastbot process is still running")
            if not self._collect_detached_fastbot_log(process):
                stop_errors.append("detached Fastbot log collection failed")
            # A manual marker may arrive while the foreground guard, host
            # process, or device-side Fastbot is being stopped. Classify the
            # host exit only after this final read so a late manual stop is not
            # reported as an engine failure or overwritten by recovery state.
            final_manual_stop_requested = controller.has_stop_request()
            manual_stop_requested = (
                manual_stop_requested or final_manual_stop_requested
            )
            if manual_stop_requested:
                intent_reason, _changed = self._remember_pressure_stop_intent(
                    "manual",
                    datetime.now(timezone.utc).isoformat(),
                    process,
                )
            expected_stop = (
                intent_reason in self.PRESSURE_STOP_REASONS
                or self._deadline_stop_requested
                or manual_stop_requested
                or stall_recovery_stop
                or audit_recovery_stop
                or crash_stop
                or foreground_recovery_stop
            )
            if (
                host_stop_escalated or process.returncode not in (0, None)
            ) and not expected_stop:
                clean_exit = False
                stop_errors.append(
                    "Fastbot exited with return code %s" % process.returncode
                )
                diagnostic = self._fastbot_failure_excerpt()
                if diagnostic:
                    stop_errors.append("Fastbot diagnostic: %s" % diagnostic)
            if manual_stop_requested:
                self._last_pressure_stop_reason = "manual"
                if not controller.clear_stop_request():
                    clean_exit = False
            elif intent_reason:
                self._last_pressure_stop_reason = intent_reason
            elif self._deadline_stop_requested:
                self._last_pressure_stop_reason = "deadline"
            elif stall_recovery_stop:
                self._last_pressure_stop_reason = "stall_recovery"
            elif audit_recovery_stop:
                self._last_pressure_stop_reason = "audit_recovery"
            elif crash_stop:
                self._last_pressure_stop_reason = "crash"
            elif foreground_recovery_stop:
                self._last_pressure_stop_reason = "foreground_recovery"
            self._close_process_file(process)
            if self._fastbot_process is process:
                self._fastbot_process = None
            self._deadline_stop_requested = False
            self._stall_recovery_stop_requested = False
            self._audit_recovery_stop_requested = False
            with self._audit_recovery_lock:
                self._audit_recovery_paused_pids = []
            with self._permission_recovery_lock:
                self._permission_recovery_paused_pids = []
            self._crash_stop_requested = False
            self._last_pressure_stop_error = (
                "; ".join(dict.fromkeys(stop_errors)) if stop_errors else None
            )
        return clean_exit

    @staticmethod
    def _find_logcat_boundary_offset(
        path: Path, marker: str, start_offset: int
    ) -> Optional[int]:
        """Return the byte immediately after a captured logcat marker line."""
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, int(start_offset)))
                content = stream.read()
        except OSError:
            return None
        marker_bytes = marker.encode("utf-8")
        marker_index = content.find(marker_bytes)
        if marker_index < 0:
            return None
        line_end = content.find(b"\n", marker_index + len(marker_bytes))
        relative_end = (
            line_end + 1 if line_end >= 0 else marker_index + len(marker_bytes)
        )
        return max(0, int(start_offset)) + relative_end

    def _wait_for_logcat_boundary(
        self, path: Path, marker: str, start_offset: int
    ) -> Optional[int]:
        """Wait briefly until the device marker reaches the local log artifact."""
        deadline = time.monotonic() + self.LOGCAT_BOUNDARY_WAIT_SECONDS
        while time.monotonic() < deadline:
            offset = self._find_logcat_boundary_offset(path, marker, start_offset)
            if offset is not None:
                return offset
            time.sleep(0.05)
        return self._find_logcat_boundary_offset(path, marker, start_offset)

    def get_logcat_capture_offset(
        self, process: subprocess.Popen, fallback: int = 0
    ) -> int:
        """Return the current-run boundary for one active logcat collector."""
        return int(self._logcat_capture_offsets.get(id(process), max(0, int(fallback))))

    def start_logcat_collection(self, log_file: str) -> subprocess.Popen:
        """Start full or crash-focused logcat collection for the target app."""
        path = self._local_path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Clearing logcat improves artifact readability, but it is not a
        # prerequisite for running the pressure engine. Some vendor builds
        # can leave ``logcat -c`` blocked while their logging service restarts.
        # Treat this operation as best effort so it cannot block Fastbot.
        try:
            clear_result = self._run_adb(
                ["logcat", "-c"], timeout=10, retries=1
            )
            if clear_result.returncode != 0:
                logger.warning(
                    "Unable to clear logcat buffer on %s; continuing with "
                    "existing buffer: %s",
                    self.device_serial,
                    clear_result.stderr.strip()
                    or clear_result.stdout.strip()
                    or "ADB returned a non-zero status",
                )
        except Exception as exc:
            logger.warning(
                "Unable to clear logcat buffer on %s; continuing with "
                "existing buffer: %s",
                self.device_serial,
                exc,
            )
        output_handle = path.open("ab")
        output_handle.write(
            ("\n=== Logcat started %s ===\n" % datetime.now(timezone.utc).isoformat()).encode(
                "utf-8"
            )
        )
        output_handle.flush()
        marker_search_offset = path.stat().st_size
        diagnostics = self.config.get("diagnostics", {})
        crash_only = bool(
            isinstance(diagnostics, dict)
            and diagnostics.get("logcat_mode", "full") == "crash_only"
        )
        arguments = [
            self.adb_path,
            "-s",
            self.device_serial,
            "logcat",
            "-b",
            "all",
            "-v",
            "time",
            # Some vendor builds keep returning the old ring buffer after
            # ``logcat -c``. Start at the newest record, then establish an
            # explicit marker below before the pressure engine can launch.
            "-T",
            "1",
        ]
        if crash_only:
            # These tags retain target Java crashes, ANRs, native fatal signals,
            # and system crash reports while avoiding continuous app/system noise.
            arguments.extend(
                [
                    "AndroidRuntime:E",
                    "ActivityManager:E",
                    "am_anr:E",
                    "DEBUG:E",
                    "libc:F",
                    "libc:E",
                    "%s:I" % self.LOGCAT_BOUNDARY_TAG,
                    "*:S",
                ]
            )
        try:
            process = subprocess.Popen(
                arguments,
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
        except Exception:
            output_handle.close()
            raise
        self._process_files[id(process)] = output_handle
        self._logcat_processes[id(process)] = process
        marker = "app_press_test_%s" % uuid.uuid4().hex
        boundary_offset: Optional[int] = None
        try:
            marker_result = self._run_adb(
                [
                    "shell",
                    "log",
                    "-p",
                    "i",
                    "-t",
                    self.LOGCAT_BOUNDARY_TAG,
                    marker,
                ],
                timeout=5,
                retries=1,
            )
            if marker_result.returncode == 0:
                boundary_offset = self._wait_for_logcat_boundary(
                    path, marker, marker_search_offset
                )
            else:
                logger.warning(
                    "Unable to write logcat boundary marker on %s: %s",
                    self.device_serial,
                    marker_result.stderr.strip()
                    or marker_result.stdout.strip()
                    or "ADB returned a non-zero status",
                )
        except Exception as exc:
            logger.warning(
                "Unable to establish a logcat boundary on %s: %s",
                self.device_serial,
                exc,
            )
        if boundary_offset is None:
            # No pressure events have started yet, so dropping everything
            # already drained by this collector is the safe fallback.
            try:
                boundary_offset = path.stat().st_size
            except OSError:
                boundary_offset = marker_search_offset
            logger.warning(
                "Logcat boundary marker was not observed on %s; using byte offset %s",
                self.device_serial,
                boundary_offset,
            )
        self._logcat_capture_offsets[id(process)] = max(
            marker_search_offset, int(boundary_offset)
        )
        return process

    def stop_logcat_collection(self, process: subprocess.Popen) -> bool:
        """Stop logcat collection and close its artifact file."""
        self._logcat_processes.pop(id(process), None)
        self._logcat_capture_offsets.pop(id(process), None)
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            logger.exception("Unable to stop logcat collection")
            return False
        finally:
            self._close_process_file(process)

    def supports_screen_record(self) -> bool:
        """Return whether this device exposes Android's screenrecord binary."""
        try:
            result = self._run_adb(
                [
                    "shell",
                    "sh",
                    "-c",
                    "test -x /system/bin/screenrecord || command -v screenrecord >/dev/null 2>&1",
                ],
                timeout=10,
                retries=1,
            )
        except RuntimeError:
            return False
        return result.returncode == 0

    def start_screen_record(
        self, output_file: str, max_duration: int
    ) -> subprocess.Popen:
        """Start Android screenrecord and remember where to pull the result."""
        if not self.supports_screen_record():
            raise RuntimeError(
                "screenrecord is not available on device %s" % self.device_serial
            )
        local_path = self._local_path(output_file)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        requested = max(1, int(max_duration))
        duration = min(requested, self.MAX_SCREEN_RECORD_SECONDS)
        if requested > duration:
            logger.warning(
                "Android screenrecord is limited to %s seconds; recording will be partial",
                duration,
            )
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]", "_", self.device_serial)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        remote_path = "/sdcard/app_press_test_%s_%s.mp4" % (safe_serial, stamp)
        process = subprocess.Popen(
            [
                self.adb_path,
                "-s",
                self.device_serial,
                "shell",
                "screenrecord",
                "--time-limit",
                str(duration),
                remote_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=self._creation_flags(),
        )
        self._screen_records[id(process)] = (remote_path, local_path)
        self._screen_processes[id(process)] = process
        return process

    def stop_screen_record(self, process: subprocess.Popen) -> bool:
        """Finalize a remote screen recording, pull it, and remove the temp file."""
        metadata = self._screen_records.pop(id(process), None)
        self._screen_processes.pop(id(process), None)
        if metadata is None:
            return False
        remote_path, local_path = metadata
        try:
            if process.poll() is None:
                self._send_interrupt(process)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
            time.sleep(1)
            result = self._run_adb(
                ["pull", remote_path, str(local_path)], timeout=120, retries=2
            )
            if result.returncode != 0:
                logger.error("Unable to pull screen recording: %s", result.stderr.strip())
                return False
            return local_path.is_file() and local_path.stat().st_size > 0
        except (OSError, subprocess.SubprocessError, RuntimeError):
            logger.exception("Unable to finalize screen recording")
            return False
        finally:
            try:
                self._run_adb(
                    ["shell", "rm", "-f", remote_path], retries=1, timeout=10
                )
            except RuntimeError:
                logger.warning(
                    "Unable to remove remote screen-recording temp file %s",
                    remote_path,
                )

    def start_screenshot_collection(
        self, output_dir: str, interval_seconds: int
    ) -> threading.Event:
        """Capture screenshots and optionally observe static pages without UiAutomation."""
        directory = self._local_path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stop_event = threading.Event()
        artifact_interval = max(1, int(interval_seconds))
        screenshot_options = self.config.get("screenshot", {})
        save_artifacts = bool(
            isinstance(screenshot_options, dict)
            and screenshot_options.get("enabled", False)
        )
        watchdog = self._stall_watchdog_options()
        watchdog_mode = str(watchdog.get("mode", "observe"))
        watch_stalls = bool(
            watchdog.get("enabled", False)
            and watchdog_mode in {"observe", "recover_once", "recover"}
        )
        recover = watchdog_mode in {"recover_once", "recover"}
        max_probe_failures = max(
            1, int(watchdog.get("max_probe_failures", 3))
        )
        if watch_stalls:
            with self._stall_events_lock:
                self._stall_events.clear()
                self._stall_detection_total = 0
                self._stall_recovery_requested = False
                self._stall_recovery_claimed = False
                self._stall_watchdog_status = {
                    "enabled": True,
                    "state": "idle",
                    "max_probe_failures": max_probe_failures,
                    "total_probe_failures": 0,
                    "consecutive_probe_failures": 0,
                    "degraded_periods": [],
                }
        else:
            with self._stall_events_lock:
                self._stall_watchdog_status = {
                    "enabled": False,
                    "state": "disabled",
                    "total_probe_failures": 0,
                    "consecutive_probe_failures": 0,
                    "degraded_periods": [],
                }
        sample_interval = (
            max(5, int(watchdog.get("sample_interval_seconds", 10)))
            if watch_stalls
            else artifact_interval
        )
        if save_artifacts:
            sample_interval = min(sample_interval, artifact_interval)

        def pressure_log_progress(path: Path) -> Dict[str, Any]:
            """Read bounded Fastbot progress for stall evidence."""
            return self._read_fastbot_progress(path)

        def worker() -> None:
            def record_probe_failure(stage: str, error: str = "") -> bool:
                detected_at = datetime.now(timezone.utc).isoformat()
                with self._stall_events_lock:
                    status = self._stall_watchdog_status
                    total = int(status.get("total_probe_failures", 0)) + 1
                    consecutive = int(
                        status.get("consecutive_probe_failures", 0)
                    ) + 1
                    status["total_probe_failures"] = total
                    status["consecutive_probe_failures"] = consecutive
                    status["last_probe_failure"] = {
                        "detected_at": detected_at,
                        "stage": stage,
                        "error": error,
                    }
                    if consecutive >= max_probe_failures:
                        if status.get("state") != "degraded":
                            status["state"] = "degraded"
                            status.setdefault("degraded_periods", []).append(
                                {
                                    "started_at": detected_at,
                                    "stage": stage,
                                    "error": error,
                                    "probe_failures": consecutive,
                                }
                            )
                        return True
                return False

            def record_probe_success() -> None:
                recovered_at = datetime.now(timezone.utc).isoformat()
                with self._stall_events_lock:
                    status = self._stall_watchdog_status
                    if status.get("state") == "degraded":
                        periods = status.get("degraded_periods", [])
                        if periods and isinstance(periods[-1], dict):
                            periods[-1]["recovered_at"] = recovered_at
                    status["state"] = "healthy"
                    status["consecutive_probe_failures"] = 0
                    status["last_success_at"] = recovered_at

            sequence = 0
            last_artifact_at: Optional[float] = None
            detector = (
                StallDetector(
                    unchanged_seconds=float(watchdog.get("unchanged_seconds", 150)),
                    max_hash_distance=int(watchdog.get("max_hash_distance", 6)),
                )
                if watch_stalls
                else None
            )
            was_pressure_active = False
            pressure_started_at: Optional[float] = None
            baseline_log_size = 0
            last_progress_marker: Optional[Tuple[Any, ...]] = None
            while not stop_event.is_set():
                pressure_active = self._pressure_active.is_set()
                if not save_artifacts and not pressure_active:
                    if detector is not None:
                        detector.reset()
                        with self._stall_events_lock:
                            self._stall_watchdog_status["state"] = "idle"
                            self._stall_watchdog_status[
                                "consecutive_probe_failures"
                            ] = 0
                    was_pressure_active = False
                    last_progress_marker = None
                    if stop_event.wait(sample_interval):
                        break
                    continue

                content: Optional[bytes] = None
                try:
                    result = subprocess.run(
                        [
                            self.adb_path,
                            "-s",
                            self.device_serial,
                            "exec-out",
                            "screencap",
                            "-p",
                        ],
                        capture_output=True,
                        timeout=self.SCREENSHOT_CAPTURE_TIMEOUT_SECONDS,
                        check=False,
                    )
                    if result.returncode == 0 and result.stdout:
                        content = bytes(result.stdout)
                except (OSError, subprocess.SubprocessError):
                    logger.exception("Screenshot capture failed on %s", self.device_serial)
                if stop_event.is_set():
                    break
                pressure_active = self._pressure_active.is_set()
                now = time.monotonic()
                if content is None:
                    if detector is not None and pressure_active:
                        if record_probe_failure("screenshot_capture"):
                            detector.reset()
                    elif detector is not None:
                        detector.reset()
                    stop_event.wait(sample_interval)
                    continue

                artifact_due = bool(
                    save_artifacts
                    and (
                        last_artifact_at is None
                        or now - last_artifact_at >= artifact_interval
                    )
                )
                if artifact_due:
                    sequence += 1
                    path = directory / ("screenshot_%05d.png" % sequence)
                    try:
                        path.write_bytes(content)
                        last_artifact_at = now
                    except OSError:
                        logger.exception(
                            "Unable to save screenshot on %s", self.device_serial
                        )

                if stop_event.is_set():
                    break
                if detector is not None and pressure_active:
                    if not was_pressure_active:
                        detector.reset()
                        pressure_started_at = now
                        baseline_log_size = 0
                        last_progress_marker = None
                        with self._stall_events_lock:
                            self._stall_watchdog_status["state"] = "warming_up"
                    was_pressure_active = True
                    grace = float(watchdog.get("startup_grace_seconds", 30))
                    if pressure_started_at is None or now - pressure_started_at < grace:
                        detector.reset()
                        stop_event.wait(sample_interval)
                        continue

                    component = self._last_foreground_component
                    component_at = self._last_foreground_component_at
                    if (
                        component
                        and component_at is not None
                        and now - component_at > max(2.0, min(10.0, sample_interval))
                    ):
                        component = None
                    component_probe_recorded = False
                    if not component:
                        try:
                            component = self._current_foreground_component()
                        except RuntimeError as exc:
                            if record_probe_failure(
                                "foreground_component", str(exc)
                            ):
                                detector.reset()
                            component_probe_recorded = True
                            component = None
                    if not component:
                        if (
                            not component_probe_recorded
                            and record_probe_failure("foreground_component")
                        ):
                            detector.reset()
                        stop_event.wait(sample_interval)
                        continue
                    if component.split("/", 1)[0] != self.package_name:
                        record_probe_success()
                        logger.debug(
                            "Stall watchdog ignored non-target foreground on %s: %s",
                            self.device_serial,
                            component,
                        )
                        detector.reset()
                        stop_event.wait(sample_interval)
                        continue

                    try:
                        fingerprint = fingerprint_png(content)
                    except ValueError as exc:
                        logger.debug(
                            "Unable to fingerprint screenshot on %s",
                            self.device_serial,
                            exc_info=True,
                        )
                        if record_probe_failure("screenshot_fingerprint", str(exc)):
                            detector.reset()
                        stop_event.wait(sample_interval)
                        continue

                    record_probe_success()

                    log_path = self.report_dir / "fastbot.log"
                    progress = pressure_log_progress(log_path)
                    log_size = int(progress.get("log_size", 0) or 0)
                    progress_marker = progress.get("progress_marker")
                    if last_progress_marker is None:
                        baseline_log_size = log_size
                    progress_changed = bool(
                        progress_marker is not None
                        and last_progress_marker is not None
                        and progress_marker != last_progress_marker
                    )
                    if progress_marker is not None:
                        last_progress_marker = tuple(progress_marker)
                    if progress_changed:
                        # Fastbot event growth is evidence about the input
                        # engine, not proof that the rendered page progressed.
                        # A device can accept thousands of taps while showing
                        # the same frame, so keep the screenshot/activity
                        # detector running and record the engine activity only
                        # as stall evidence.
                        with self._stall_events_lock:
                            self._stall_watchdog_status["last_progress_at"] = (
                                datetime.now(timezone.utc).isoformat()
                            )
                            self._stall_watchdog_status["last_event_id"] = progress.get(
                                "last_event_id"
                            )
                            self._stall_watchdog_status["progress_log_size"] = log_size
                    observation = detector.observe(component, fingerprint, now)
                    logger.debug(
                        "Stall watchdog sample on %s: component=%s distance=%s "
                        "unchanged=%.1fs changed=%s",
                        self.device_serial,
                        observation.component,
                        observation.hash_distance,
                        observation.unchanged_seconds,
                        observation.state_changed,
                    )
                    if observation.state_changed:
                        baseline_log_size = log_size
                    if observation.detected:
                        if stop_event.is_set():
                            break
                        detected_at = datetime.now(timezone.utc)
                        stamp = detected_at.strftime("%Y%m%dT%H%M%S%fZ")
                        evidence_dir = self.report_dir / "stall_events"
                        evidence_path = evidence_dir / ("stall_%s.png" % stamp)
                        event = {
                            "detected_at": detected_at.isoformat(),
                            "mode": watchdog_mode,
                            "component": observation.component,
                            "unchanged_seconds": round(
                                observation.unchanged_seconds, 3
                            ),
                            "hash": observation.fingerprint.as_hex(),
                            "hash_distance": observation.hash_distance,
                            "engine_log_growth_bytes": max(
                                0, log_size - baseline_log_size
                            ),
                            "evidence": str(evidence_path),
                            "evidence_saved": False,
                            "recovery_action": None,
                            "recovery_success": None,
                        }
                        event.update(progress)
                        with self._stall_events_lock:
                            if stop_event.is_set():
                                break
                            store_evidence = len(self._stall_events) < 20
                            request_recovery = bool(
                                recover
                                and not self._stall_recovery_requested
                                and not self._stall_recovery_claimed
                            )
                        if store_evidence:
                            try:
                                evidence_dir.mkdir(parents=True, exist_ok=True)
                                evidence_path.write_bytes(content)
                                event["evidence_saved"] = True
                            except OSError:
                                logger.exception(
                                    "Unable to save stall evidence on %s",
                                    self.device_serial,
                                )
                                event["evidence"] = None
                        else:
                            # A recovery request is a control-plane signal, not
                            # an artifact-storage decision.  Keep its event
                            # metadata even when the bounded evidence list is
                            # full, so a failed/omitted screenshot cannot
                            # prevent the requested app restart.
                            event["evidence"] = None
                            event["evidence_warning"] = (
                                "stall evidence retention limit reached"
                            )
                        with self._stall_events_lock:
                            if stop_event.is_set():
                                break
                            self._stall_detection_total += 1
                            # Preserve the historical ordering for normal
                            # evidence writes.  When the evidence cap is hit,
                            # append the pending recovery event immediately so
                            # the request still has safe target metadata.
                            if store_evidence or request_recovery:
                                self._stall_events.append(event)
                                if len(self._stall_events) > 20:
                                    del self._stall_events[:-20]
                            if request_recovery:
                                self._stall_recovery_requested = True
                        logger.warning(
                            "Observed a static page for %.1f seconds on %s: %s",
                            observation.unchanged_seconds,
                            self.device_serial,
                            observation.component,
                        )
                else:
                    if detector is not None:
                        detector.reset()
                    was_pressure_active = False

                stop_event.wait(sample_interval)

        thread = threading.Thread(
            target=worker,
            name="screenshots-%s" % self.device_serial,
            daemon=True,
        )
        self._screenshot_collection_failed = False
        self._screenshot_collection_error = None
        thread.start()
        self._screenshot_jobs[id(stop_event)] = (stop_event, thread)
        return stop_event

    def stop_screenshot_collection(self, stop_event: threading.Event) -> bool:
        """Stop a periodic screenshot collector."""
        job = self._screenshot_jobs.get(id(stop_event))
        if job is None:
            return False
        event, thread = job
        with self._stall_events_lock:
            event.set()
        thread.join(timeout=self.SCREENSHOT_STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            self.mark_screenshot_collection_failed(
                "screenshot collector did not stop on %s within %s seconds"
                % (self.device_serial, self.SCREENSHOT_STOP_TIMEOUT_SECONDS)
            )
            logger.error(
                "Screenshot collector did not stop on %s within %s seconds",
                self.device_serial,
                self.SCREENSHOT_STOP_TIMEOUT_SECONDS,
            )
            return False
        self._screenshot_jobs.pop(id(stop_event), None)
        return True

    @staticmethod
    def _read_structured_events(path: Path) -> List[Dict[str, Any]]:
        if path.stat().st_size > 20 * 1024 * 1024:
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        try:
            value = json.loads(text)
            if isinstance(value, list):
                return [event for event in value if isinstance(event, dict)]
            if isinstance(value, dict) and isinstance(value.get("events"), list):
                return [
                    event for event in value["events"] if isinstance(event, dict)
                ]
        except json.JSONDecodeError:
            pass

        events: List[Dict[str, Any]] = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return []
            if isinstance(event, dict):
                events.append(event)
        return events

    def collect_trajectory(
        self,
        *,
        log_path: Optional[Path] = None,
        log_start_offset: int = 0,
        log_end_offset: Optional[int] = None,
        max_events: int = 10000,
    ) -> Dict[str, Any]:
        """Pull Fastbot model/trace files and expose structured events when present.

        A stock ``.fbm`` file may be a binary model rather than a replayable event
        stream. Such files are preserved as artifacts.  Fastbot's text log is
        parsed for the same phase as a fallback, so a binary-only model no
        longer makes an otherwise replayable run appear empty.
        """
        output_directory = self._fastbot_output_directory()
        # New launches write under the explicit short directory.  ``find`` is
        # needed because crash/ANR reports are placed in timestamped child
        # directories by Fastbot.  Keep the old root-level globs as a
        # compatibility fallback for artifacts produced before this change.
        command = (
            "find %s -type f \\( -name '*.fbm' -o -name '*.jsonl' \\) 2>/dev/null; "
            "ls -1 /sdcard/fastbot_%s*.fbm /sdcard/fastbot_%s*.jsonl 2>/dev/null"
            % (shlex.quote(output_directory), self.package_name, self.package_name)
        )
        result = self._run_adb(["shell", command], retries=1, timeout=15)
        remote_files = sorted(
            {
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith("/sdcard/")
            }
        )
        model_dir = self.report_dir / "fastbot_models"
        model_dir.mkdir(parents=True, exist_ok=True)
        file_info: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        for remote_file in remote_files:
            local_file = model_dir / Path(remote_file).name
            # Two timestamped Fastbot subdirectories can contain the same
            # basename (for example ``events.jsonl``). Preserve both instead
            # of silently overwriting the first artifact.
            if local_file.exists():
                stem = local_file.stem
                suffix = local_file.suffix
                counter = 2
                while local_file.exists():
                    local_file = model_dir / ("%s_%02d%s" % (stem, counter, suffix))
                    counter += 1
            pull = self._run_adb(
                ["pull", remote_file, str(local_file)], timeout=120, retries=2
            )
            if pull.returncode != 0 or not local_file.is_file():
                logger.warning("Unable to collect Fastbot file %s", remote_file)
                continue
            parsed = self._read_structured_events(local_file)
            events.extend(parsed)
            file_info.append(
                {
                    "remote_path": remote_file,
                    "local_path": str(local_file),
                    "size_bytes": local_file.stat().st_size,
                    "structured_event_count": len(parsed),
                }
            )

        # Fastbot versions used by the device farm do not emit a JSON event
        # stream, but they do print every gesture in ``fastbot.log``.  Parse
        # only the current phase range to keep collection bounded for overnight
        # runs.  A caller may pass a fake executor in older integrations; the
        # optional arguments keep the no-argument API compatible.
        trace: Dict[str, Any] = {}
        phase_log_path = Path(log_path) if log_path is not None else self.report_dir / "fastbot.log"
        if phase_log_path.is_file():
            phase_end = log_end_offset
            if phase_end is None:
                try:
                    phase_end = phase_log_path.stat().st_size
                except OSError:
                    phase_end = None
            trace = parse_fastbot_log(
                phase_log_path,
                start_offset=max(0, int(log_start_offset)),
                end_offset=phase_end,
                max_events=max_events,
            )
        log_events = [
            dict(event)
            for event in trace.get("events", [])
            if isinstance(event, Mapping)
        ]

        # Prefer a native JSONL stream when one is available, while retaining
        # log-derived actions that are absent from it.  Event ids are scoped to
        # one Fastbot invocation, so include the action in the de-duplication
        # key and preserve the first source's richer payload.
        merged: List[Dict[str, Any]] = []
        seen_event_keys = set()
        for event in events + log_events:
            key = (
                str(event.get("event_id")) if event.get("event_id") is not None else "",
                str(event.get("action") or event.get("type") or ""),
                str(event.get("x", event.get("x1", ""))),
                str(event.get("y", event.get("y1", ""))),
                str(event.get("text", "")),
            )
            if key[0] and key in seen_event_keys:
                continue
            if key[0]:
                seen_event_keys.add(key)
            merged.append(event)
        max_events = max(1, int(max_events))
        events_truncated = bool(trace.get("truncated"))
        if len(merged) > max_events:
            merged = merged[-max_events:]
            events_truncated = True

        source = "jsonl" if events and not log_events else "fastbot.log" if log_events else ""
        if events and log_events:
            source = "jsonl+fastbot.log"
        replayable = bool(merged)
        note = (
            "Structured events are available for replay%s."
            % (" (tail retained because the phase exceeded the replay limit)" if events_truncated else "")
            if replayable
            else (
                "No structured event stream was found for system Monkey."
                if self.pressure_engine == "system-monkey"
                else "No structured event stream was found; the stock FBM model is binary and cannot be replayed directly."
            )
        )
        return {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "device_serial": self.device_serial,
            "package_name": self.package_name,
            "engine": self.pressure_engine,
            "output_directory": output_directory,
            "model_files": file_info,
            "events": merged,
            "operation_count": len(merged),
            "replayable": replayable,
            "event_source": source or None,
            "event_count": len(merged),
            "events_truncated": events_truncated,
            "trace": trace,
            "note": note,
        }

    def collect_fastbot_statistics(self) -> Dict[str, Any]:
        """Extract explicitly reported operation/coverage counters from Fastbot.

        Fastbot builds differ in their log wording. Only unambiguous labelled
        counters are accepted; absent counters remain unknown instead of being
        inferred from binary model size.
        """
        path = self.report_dir / "fastbot.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[-5 * 1024 * 1024 :]
        except OSError:
            return {}
        marker = max(
            text.rfind("=== Fastbot started "),
            text.rfind("=== System Monkey started "),
        )
        fastbot_session_marker = "=== Fastbot started " in text
        if marker >= 0:
            text = text[marker:]
        operation_values = [
            int(value)
            for value in re.findall(
                r"(?:events?\s+injected|total\s+number\s+of\s+events|event\s+count)"
                r"\s*[:=]\s*(\d+)",
                text,
                flags=re.IGNORECASE,
            )
        ]
        # A detached/interrupted Monkey run may leave only these progress
        # lines behind.  Fastbot's own session log uses the same wording for
        # heartbeat output, so do not treat it as injected input once the
        # explicit Fastbot session marker is present.
        if self.pressure_engine == "system-monkey" or not fastbot_session_marker:
            operation_values.extend(
                int(value)
                for value in re.findall(
                    r"Sending\s+event\s+#(\d+)",
                    text,
                    flags=re.IGNORECASE,
                )
            )
        operation_values.extend(
            int(value)
            for value in re.findall(
                r"\bEvent\s+id\s*:\s*(\d+)",
                text,
                flags=re.IGNORECASE,
            )
        )
        coverage_values = [
            float(value)
            for value in re.findall(
                r"(?:coverage|covered)\s*[:=]\s*(\d+(?:\.\d+)?)\s*%",
                text,
                flags=re.IGNORECASE,
            )
        ]
        result: Dict[str, Any] = {}
        if operation_values:
            result["operation_count"] = max(operation_values)
        if coverage_values:
            result["coverage_rate"] = min(100.0, max(0.0, max(coverage_values)))
        return result

    def is_fastbot_running(self) -> bool:
        """Return whether the local or remote Fastbot process is running."""
        if self._fastbot_process is not None and self._fastbot_process.poll() is None:
            return True
        try:
            processes = MutexController(self.device_serial)._remote_processes()
            # An unavailable probe is unsafe to treat as stopped.
            return processes is None or bool(processes)
        except Exception:
            return True

    def cleanup(self) -> None:
        """Best-effort cleanup for all collectors and Fastbot processes."""
        for event, _thread in list(self._screenshot_jobs.values()):
            try:
                self.stop_screenshot_collection(event)
            except Exception:
                logger.exception("Unable to stop screenshot collector")
        for process in list(self._logcat_processes.values()):
            try:
                self.stop_logcat_collection(process)
            except Exception:
                logger.exception("Unable to stop logcat collector")
        for process in list(self._screen_processes.values()):
            try:
                self.stop_screen_record(process)
            except Exception:
                logger.exception("Unable to stop screen recorder")
        if self._fastbot_process is not None:
            try:
                self.stop_fastbot(self._fastbot_process)
            except Exception:
                logger.exception("Unable to stop Fastbot during cleanup")
        self._stop_foreground_guard()
        for handle in list(self._process_files.values()):
            if not handle.closed:
                handle.close()
        self._process_files.clear()
