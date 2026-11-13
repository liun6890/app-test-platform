"""Application state preparation through uiautomator2."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .vendor_install import is_installer_package
from .input_safety import (
    InputRecoveryRequired, input_safety_options,
    is_system_dialog_package, touch_path_is_owned,
)

try:
    import uiautomator2 as u2
except ImportError:  # pragma: no cover - exercised on hosts before installation
    u2 = None  # type: ignore


logger = logging.getLogger(__name__)

# Packages that host the OEM install confirmation UI.  MIUI/HyperOS shows a
# bottom sheet with "继续安装" and a "拒绝" button whose countdown rejects the
# install after ~10s; vivo/HONOR show a full install page.
VENDOR_INSTALLER_PACKAGES = (
    "com.miui.packageinstaller",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.android.permissioncontroller",
    "com.vivo.packageinstaller",
    "com.vivo.appstore",
    "com.huawei.packageinstaller",
    "com.hihonor.packageinstaller",
    "com.oppo.packageinstaller",
    "com.coloros.packageinstaller",
    "com.oplus.appdetail",
)

# Only ever press the "continue" side of an install prompt.  "拒绝"/"取消" must
# never be clicked: on MIUI the reject button is what ends the install.
VENDOR_INSTALL_CONFIRM_TEXTS = (
    "继续安装",
    "立即安装",
    "仍要安装",
    "安装",
    "允许",
    "继续",
)
VENDOR_INSTALL_REJECT_TEXTS = ("拒绝", "取消", "取消安装", "放弃")


class StateManager:
    """Reset, prepare, snapshot, and restore one application on one device."""

    # These model families belong to the local Oplus fleet.  Some ColorOS
    # builds report the model code as ``brand`` through uiautomator2, so model
    # recognition must not depend on a separate manufacturer round trip.
    OPLUS_MODEL_PREFIXES = ("PFFM", "PGBM", "PJR", "PHM")
    # Vivo X80 variants seen in the device fleet.  ``V2144``/``V2183`` are
    # regional hardware codes, while ``PD2183`` is the product-code form.
    # Keep the aliases here rather than relying on a single marketing-model
    # string because uiautomator2 reports different fields across Android
    # releases.
    VIVO_X80_MODEL_ALIASES = frozenset(
        {
            "x80",
            "v2144",
            "v2183",
            "v2183a",
            "pd2183",
            "pd2183a",
        }
    )
    VIVO_X80_FOREGROUND_STABLE_SECONDS = 1.0
    LAUNCHER_PACKAGES = frozenset(
        {
            "com.miui.home",
            "com.android.launcher",
            "com.android.launcher2",
            "com.android.launcher3",
            "com.google.android.apps.nexuslauncher",
            "com.huawei.android.launcher",
            "com.oppo.launcher",
            "com.sec.android.app.launcher",
            "com.vivo.launcher",
        }
    )
    FOREGROUND_LAUNCHER_RETRY_ATTEMPTS = 3
    FOREGROUND_LAUNCHER_RETRY_SECONDS = 1.0
    AUDIT_GATE_ABSENT_STABLE_PROBES = 3
    AUDIT_GATE_ABSENT_STABLE_SECONDS = 3.0

    PERMISSION_CONTROLLER_PACKAGES = frozenset(
        {
            "com.android.permissioncontroller",
            "com.google.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.google.android.packageinstaller",
            "com.lbe.security.miui",
            "com.hihonor.systemmanager",
            "com.huawei.systemmanager",
            "com.vivo.permissionmanager",
            "com.vivo.permissionmanager2",
            "com.coloros.securitypermission",
            "com.oplus.permissioncontroller",
            "com.samsung.android.permissioncontroller",
            "com.miui.securitycenter",
        }
    )
    PRIVACY_DIALOG_COMPONENTS = frozenset(
        {
            "com.miui.securitycenter/com.miui.permcenter.permissions.systemapppermissiondialogactivity",
        }
    )
    PRIVACY_DIALOG_ACTIVITY_MARKERS = ("systemapppermissiondialogactivity",)
    PRIVACY_ACCEPT_LOCATORS = (
        {"resourceId": "android:id/button1"},
        {"resourceId": "com.miui.securitycenter:id/button1"},
        {"text": "同意并继续"},
        {"text": "同意并使用"},
        {"text": "同意并进入"},
        {"text": "我同意"},
        {"text": "同意"},
    )
    PERMISSION_ALLOW_LOCATORS = (
        # Prefer the explicit "allow all" action on Android 14/15 vendor
        # pages.  A broad regex can otherwise select Honor's
        # ``permission_allow_selected_button`` and open PhotoPicker.
        {"resourceIdMatches": r".*:id/permission_allow_all_button"},
        {"resourceIdMatches": r".*:id/permission_allow_foreground_only_button"},
        {"resourceIdMatches": r".*:id/permission_allow_one_time_button"},
        {"resourceIdMatches": r".*:id/permission_allow_always_button"},
        {"resourceIdMatches": r".*:id/permission_allow.*button"},
        {"resourceIdMatches": r".*:id/.*allow.*button"},
        {"resourceIdMatches": r".*:id/(permission_allow|allow|btn_allow).*"},
        {"text": "仅在使用中允许"},
        {"text": "仅使用期间允许"},
        {"text": "在使用时允许"},
        {"text": "仅在使用该应用时允许"},
        {"text": "仅在使用该应用时允许访问"},
        {"text": "使用应用时允许"},
        {"text": "使用时允许"},
        {"text": "使用期间允许"},
        {"text": "本次使用允许"},
        {"text": "仅此一次"},
        {"text": "本次运行允许"},
        {"text": "本次允许"},
        {"text": "始终允许"},
        {"text": "允许"},
        {"text": "允許"},
        {"text": "允许访问全部照片和视频"},
        # MIUI 14/Android 14 uses the shorter media-permission label and an
        # empty resource-id, so this must remain an explicit text locator.
        {"text": "允许访问全部"},
        {"text": "允许所有照片和视频"},
        {"text": "允许全部照片和视频"},
        {"text": "Allow"},
        {"textContains": "While using"},
        {"textContains": "Only this time"},
        {"textContains": "Allow while using"},
        {"textContains": "Allow all photos and videos"},
        {"textContains": "ALLOW"},
    )

    def __init__(self, device_serial: str, package_name: str):
        """Initialize the manager and connect to the requested device."""
        if u2 is None:
            raise RuntimeError(
                "uiautomator2 is not installed; run: pip install -r requirements.txt"
            )
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            raise ValueError("Invalid Android package name: %s" % package_name)
        self.device_serial = device_serial
        self.package_name = package_name
        self._device_manufacturer: Optional[str] = None
        self._launcher_activity: Optional[str] = None
        self._last_known_app: Dict[str, Any] = {}
        self._audit_gate_known_complete = False
        # Startup gates (audit page and privacy consent) only appear on the first
        # launch after an install or a data clear.  Once this cycle completed
        # them, a recovery relaunch must not wait for them again.
        self._startup_gates_passed = False
        self._data_clear_capability: Optional[Dict[str, Any]] = None
        self._apk_path: Optional[Path] = None
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self.last_navigation_error: Optional[str] = None
        self.last_reset_error: Optional[str] = None
        self._input_safety: Dict[str, Any] = {}
        self.input_recovery_required = False
        self.d = self._connect_with_retry()

    def _connect_with_retry(self):
        last_error: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                device = u2.connect(self.device_serial)
                _ = device.info
                return device
            except Exception as exc:  # uiautomator2 exposes transport-specific errors
                last_error = exc
                if attempt < 3:
                    logger.warning(
                        "uiautomator2 connection to %s failed (%s/3): %s",
                        self.device_serial,
                        attempt,
                        exc,
                    )
                    time.sleep(attempt)
        raise RuntimeError(
            "Unable to connect uiautomator2 to %s: %s"
            % (self.device_serial, last_error)
        )

    def reset_app(
        self,
        clear_data: bool = True,
        stop_packages: Optional[Iterable[str]] = None,
        clear_task: bool = False,
    ) -> bool:
        """Optionally clear data, then launch the initial application state.

        Some vendor builds reject ``pm clear`` even for an authorized shell.
        Callers that already performed a one-time clean reset can pass
        ``clear_data=False`` and still get a deterministic force-stop/start.
        """
        self.last_reset_error = None
        if clear_data:
            self._audit_gate_known_complete = False
            # Clearing data re-arms the first-launch screens, so the next start
            # must handle the audit gate and privacy consent again.
            self._startup_gates_passed = False
        try:
            use_direct_lifecycle = self._uses_direct_adb_lifecycle()
            packages = [self.package_name]
            for package in stop_packages or []:
                value = str(package).strip()
                if value and value not in packages:
                    packages.append(value)
            for package in packages:
                if use_direct_lifecycle:
                    if not self._stop_package_via_direct_adb(package):
                        self.last_reset_error = (
                            "force-stop failed for %s" % package
                        )
                        return False
                else:
                    self.d.app_stop(package)
            if clear_data and not self._clear_app_data():
                logger.error("Application data clear did not return Success")
                capability = self.data_clear_capability()
                self.last_reset_error = (
                    "application data clear failed: %s"
                    % str(capability.get("reason") or "unknown error")
                )
                return False
            start_with_clean_task = clear_task
            if (
                clear_data
                and not start_with_clean_task
                and self._current_app().get("package") == "com.android.settings"
            ):
                # ColorOS can leave the App info subpage as the active task
                # after a Settings-based data clear. A normal app_start may
                # report success without displacing that task.
                start_with_clean_task = True
                logger.info(
                    "Android Settings is foreground after reset on %s; "
                    "using a clean task launch for %s",
                    self.device_serial,
                    self.package_name,
                )
            if self._start_target_app(
                # Oplus devices were already stopped through direct ADB above.
                # Avoid a second lifecycle command during a USB re-enumeration.
                stop=not use_direct_lifecycle,
                wait=True,
                clear_task=start_with_clean_task,
            ) is False:
                logger.error("Application start returned false")
                self.last_reset_error = "target application launch failed"
                return False
            if not self._wait_for_target_foreground():
                self.last_reset_error = "target application did not reach foreground"
                return False
            if not use_direct_lifecycle:
                self._dismiss_transient_system_dialogs()
            return True
        except Exception as exc:
            self.last_reset_error = (
                "%s: %s" % (type(exc).__name__, str(exc) or "unknown error")
            )[:2000]
            logger.exception("Failed to reset %s", self.package_name)
            return False

    def data_clear_capability(self) -> Dict[str, Any]:
        """Return the cached outcome of this device's actual clear attempt."""
        cached = getattr(self, "_data_clear_capability", None)
        if isinstance(cached, dict):
            return dict(cached)
        return {"supported": None, "reason": "not yet attempted"}

    def _wait_for_target_foreground(self, timeout: float = 45.0) -> bool:
        """Wait for the target and recover from bounded vendor-page races."""
        if self._is_vivo_x80_device():
            return self._wait_for_vivo_x80_foreground(timeout)
        if self._is_oplus_device():
            # A direct ``am start`` has already acknowledged the exact launcher
            # component.  Polling uiautomator2.app_current immediately afterwards
            # makes ColorOS run its shell wrapper repeatedly while the ADB transport
            # is settling, which is both redundant and prone to a false failure.
            time.sleep(min(0.75, max(0.0, float(timeout))))
            return True

        app_current = getattr(self.d, "app_current", None)
        # Kept for compatibility with minimal test doubles and older clients;
        # real uiautomator2 devices always expose this probe.
        if not callable(app_current):
            return True
        deadline = time.monotonic() + max(0.1, min(60.0, float(timeout)))
        last_current: Dict[str, Any] = {}
        settings_relaunch_attempted = False
        launcher_relaunch_attempts = 0
        next_launcher_relaunch_at = 0.0
        # A fresh install leaves the package installer page in front for a
        # while; failing the cycle here made a device drop out right after a
        # successful install.  Wait for the page to close before judging.
        installer_wait_deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            try:
                last_current = self._foreground_app()
                if last_current.get("package") == self.package_name:
                    return True
                if is_installer_package(str(last_current.get("package") or "")):
                    if time.monotonic() < installer_wait_deadline:
                        logger.info(
                            "Installer window %s is still in front on %s; "
                            "waiting for it to close",
                            last_current.get("package"),
                            self.device_serial,
                        )
                        deadline = max(deadline, time.monotonic() + 5.0)
                        time.sleep(1.0)
                        continue
                if (
                    not settings_relaunch_attempted
                    and last_current.get("package") == "com.android.settings"
                ):
                    settings_relaunch_attempted = True
                    logger.info(
                        "Android Settings remained in foreground after reset on %s; "
                        "using a clean task launch for %s",
                        self.device_serial,
                        self.package_name,
                    )
                    if self._start_target_app(
                        stop=False, wait=False, clear_task=True
                    ) is False:
                        logger.warning(
                            "Clean task launch failed after Settings reset on %s; "
                            "retrying the standard launch",
                            self.device_serial,
                        )
                        self._start_target_app(stop=False, wait=False)
                elif (
                    str(last_current.get("package") or "")
                    in self.LAUNCHER_PACKAGES
                    and launcher_relaunch_attempts
                    < self.FOREGROUND_LAUNCHER_RETRY_ATTEMPTS
                    and time.monotonic() >= next_launcher_relaunch_at
                ):
                    launcher_relaunch_attempts += 1
                    next_launcher_relaunch_at = (
                        time.monotonic() + self.FOREGROUND_LAUNCHER_RETRY_SECONDS
                    )
                    logger.warning(
                        "Launcher %s remained in foreground after reset on %s; "
                        "retrying the explicit target component (%s/%s)",
                        last_current.get("package"),
                        self.device_serial,
                        launcher_relaunch_attempts,
                        self.FOREGROUND_LAUNCHER_RETRY_ATTEMPTS,
                    )
                    launched = self._start_target_app_via_direct_adb(
                        stop=False,
                        clear_task=True,
                        activity=str(
                            getattr(self, "_launcher_activity", "") or ""
                        ).strip()
                        or None,
                        retry_attempts=1,
                    )
                    if launched is None:
                        launched = self._start_target_app(
                            stop=False, wait=False, clear_task=True
                        )
                    if launched is False:
                        logger.warning(
                            "Explicit launcher retry %s/%s failed on %s",
                            launcher_relaunch_attempts,
                            self.FOREGROUND_LAUNCHER_RETRY_ATTEMPTS,
                            self.device_serial,
                        )
            except Exception:
                logger.debug(
                    "Foreground probe failed while waiting for %s on %s",
                    self.package_name,
                    self.device_serial,
                    exc_info=True,
                )
            time.sleep(0.25)
        logger.error(
            "Target package did not reach foreground after reset on %s: %r",
            self.device_serial,
            last_current,
        )
        return False

    def _wait_for_vivo_x80_foreground(self, timeout: float = 45.0) -> bool:
        """Wait for Vivo X80's launcher transition to settle before success.

        Vivo firmware can report the target package for one probe while the
        launcher is still winning the task transition.  Require two target
        observations separated by a real settle interval; this also keeps a
        timed-out ``am start`` from being treated as a terminal task failure.
        """
        app_current = getattr(self.d, "app_current", None)
        if not callable(app_current):
            time.sleep(
                min(
                    self.VIVO_X80_FOREGROUND_STABLE_SECONDS,
                    max(0.0, float(timeout)),
                )
            )
            return True
        deadline = time.monotonic() + max(0.1, min(60.0, float(timeout)))
        stable_observed = False
        launcher_relaunch_attempts = 0
        next_launcher_relaunch_at = 0.0
        last_current: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                last_current = self._foreground_app()
                if last_current.get("package") == self.package_name:
                    if stable_observed:
                        return True
                    stable_observed = True
                    # The sleep is intentional: a second probe after this
                    # interval is the stability confirmation, rather than a
                    # cached first ``am start`` acknowledgement.
                    remaining = max(0.0, deadline - time.monotonic())
                    time.sleep(
                        min(self.VIVO_X80_FOREGROUND_STABLE_SECONDS, remaining)
                    )
                    continue
                stable_observed = False
                if (
                    str(last_current.get("package") or "") in self.LAUNCHER_PACKAGES
                    and launcher_relaunch_attempts
                    < self.FOREGROUND_LAUNCHER_RETRY_ATTEMPTS
                    and time.monotonic() >= next_launcher_relaunch_at
                ):
                    launcher_relaunch_attempts += 1
                    next_launcher_relaunch_at = (
                        time.monotonic() + self.FOREGROUND_LAUNCHER_RETRY_SECONDS
                    )
                    logger.warning(
                        "Vivo launcher remained in foreground after reset on %s; "
                        "retrying the explicit target component (%s/%s)",
                        self.device_serial,
                        launcher_relaunch_attempts,
                        self.FOREGROUND_LAUNCHER_RETRY_ATTEMPTS,
                    )
                    self._start_target_app_via_direct_adb(
                        stop=False,
                        clear_task=True,
                        activity=str(
                            getattr(self, "_launcher_activity", "") or ""
                        ).strip()
                        or None,
                        retry_attempts=1,
                    )
            except Exception:
                stable_observed = False
                logger.debug(
                    "Vivo X80 foreground probe failed while waiting for %s on %s",
                    self.package_name,
                    self.device_serial,
                    exc_info=True,
                )
            time.sleep(0.25)
        logger.error(
            "Vivo X80 target package did not remain foreground on %s: %r",
            self.device_serial,
            last_current,
        )
        return False

    @staticmethod
    def _process_output(result: subprocess.CompletedProcess) -> str:
        return "\n".join(
            value.strip()
            for value in (result.stdout or "", result.stderr or "")
            if value and value.strip()
        )

    @classmethod
    def _is_direct_adb_transport_failure(
        cls, result: Optional[subprocess.CompletedProcess]
    ) -> bool:
        if result is None:
            return True
        output = cls._process_output(result).lower()
        return any(
            marker in output
            for marker in (
                "device not found",
                "device offline",
                "no devices/emulators found",
                "cannot connect to daemon",
                "transport endpoint is not connected",
                "connection reset",
                "connection refused",
                "remote end closed connection",
            )
        )

    def _run_direct_adb(
        self,
        arguments: List[str],
        timeout: int,
        transport_retries: int = 0,
    ) -> Optional[subprocess.CompletedProcess]:
        serial = str(getattr(self, "device_serial", "")).strip()
        if not serial:
            return None
        attempts = max(1, min(5, int(transport_retries) + 1))
        last_result: Optional[subprocess.CompletedProcess] = None
        for attempt in range(1, attempts + 1):
            try:
                result = subprocess.run(
                    [os.environ.get("ADB_PATH", "adb"), "-s", serial, *arguments],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
                logger.warning(
                    "Direct ADB command failed on %s (%s/%s): %s",
                    serial,
                    attempt,
                    attempts,
                    " ".join(arguments),
                    exc_info=True,
                )
            last_result = result
            if not self._is_direct_adb_transport_failure(result):
                return result
            if attempt < attempts:
                time.sleep(min(0.5 * attempt, 2.0))
        return last_result

    def _target_foreground_stable_without_relaunch(
        self, timeout: float = 3.0
    ) -> bool:
        """Check whether a timed-out launch actually reached the app.

        Vivo X80 firmware can complete ``am start`` after the host-side ADB
        call times out.  Retrying immediately can push the task back to the
        Launcher and turn a recoverable transport race into a false failure.
        Probe the existing foreground state twice before issuing another start.
        This helper deliberately never launches anything, so it is safe to use
        from the direct-launch retry loop.
        """
        if not callable(getattr(self.d, "app_current", None)):
            return False
        deadline = time.monotonic() + max(0.1, min(6.0, float(timeout)))
        observed = False
        probes = 0
        while time.monotonic() < deadline and probes < 8:
            probes += 1
            try:
                value = self._foreground_app()
            except Exception:
                value = {}
            if str(value.get("package") or "") == self.package_name:
                if observed:
                    self._last_known_app = value
                    return True
                observed = True
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(self.VIVO_X80_FOREGROUND_STABLE_SECONDS, remaining))
                continue
            observed = False
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return False

    @staticmethod
    def _normalize_component(package_name: str, activity: str) -> str:
        if activity.startswith("."):
            activity = package_name + activity
        return "%s/%s" % (package_name, activity)

    @classmethod
    def _foreground_app_from_output(cls, output: str) -> Dict[str, Any]:
        """Parse the resumed app from modern and legacy dumpsys formats."""
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
                if not match:
                    continue
                package_name, activity = match.groups()
                if activity.startswith("."):
                    activity = package_name + activity
                return {"package": package_name, "activity": activity}
        return {}

    def _foreground_app_from_adb(self) -> Dict[str, Any]:
        """Read the real resumed app without adbutils' task-list fallback."""
        for arguments in (
            ["shell", "dumpsys", "activity", "activities"],
            ["shell", "dumpsys", "window"],
        ):
            result = self._run_direct_adb(arguments, timeout=5)
            if result is None or result.returncode != 0:
                continue
            current = self._foreground_app_from_output(result.stdout or "")
            if current:
                return current
        return {}

    def _foreground_app(self) -> Dict[str, Any]:
        """Return the foreground app, preferring Android's resumed activity."""
        try:
            result = self._foreground_app_from_adb()
        except Exception:
            logger.debug(
                "Direct foreground probe failed on %s",
                getattr(self, "device_serial", "unknown-device"),
                exc_info=True,
            )
            result = {}
        if not result:
            return self._current_app()
        self._last_known_app = dict(result)
        if str(result.get("package") or "").strip() in self.LAUNCHER_PACKAGES:
            self._launcher_observed = True
        return result

    def _current_app(self) -> Dict[str, Any]:
        try:
            current = self.d.app_current()
            result = dict(current) if isinstance(current, dict) else dict(current or {})
            if result:
                self._last_known_app = dict(result)
                if str(result.get("package") or "").strip() in self.LAUNCHER_PACKAGES:
                    # Keep this edge visible to recovery code even when a
                    # transient-dialog probe reads the foreground before the
                    # main audit loop does.
                    self._launcher_observed = True
            return result
        except Exception:
            logger.debug(
                "Unable to inspect current foreground app on %s",
                getattr(self, "device_serial", "unknown-device"),
                exc_info=True,
            )
            return {}

    def current_component(self) -> Optional[str]:
        """Return the normalized foreground component, if it can be observed."""
        current = self._foreground_app()
        package_name = str(current.get("package") or "").strip()
        activity = str(current.get("activity") or "").strip()
        if not package_name or not activity:
            return None
        return self._normalize_component(package_name, activity)

    def capture_screenshot_png(self, timeout: int = 20) -> bytes:
        """Capture one PNG through ADB without querying the UI hierarchy."""
        serial = str(getattr(self, "device_serial", "")).strip()
        if not serial:
            raise RuntimeError("device serial is unavailable for screenshot capture")
        timeout_seconds = max(1, min(60, int(timeout)))
        deadline = time.monotonic() + timeout_seconds
        last_error = "ADB screenshot capture returned no data"
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = subprocess.run(
                    [
                        os.environ.get("ADB_PATH", "adb"),
                        "-s",
                        serial,
                        "exec-out",
                        "screencap",
                        "-p",
                    ],
                    capture_output=True,
                    timeout=max(1, min(60, int(remaining))),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = "ADB screenshot capture failed: %s" % exc
            else:
                if result.returncode != 0:
                    error = bytes(result.stderr or b"").decode(
                        "utf-8", errors="replace"
                    ).strip()
                    last_error = "ADB screenshot capture failed: %s" % (
                        error or "unknown error"
                    )
                else:
                    content = bytes(result.stdout or b"")
                    if content:
                        return content
                    last_error = "ADB screenshot capture returned no data"
            if attempt < 2 and time.monotonic() < deadline:
                logger.debug(
                    "Retrying ADB screenshot capture on %s after attempt %d/3: %s",
                    serial,
                    attempt + 1,
                    last_error,
                )
                time.sleep(0.25)
        raise RuntimeError(last_error)

    def _click_if_exists(self, locator: Dict[str, Any], timeout: float = 0.5) -> bool:
        """Click a UI node if present, recovering a short U2 transport drop."""
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            try:
                selector, _ = self._selector(locator)
                remaining = max(0.1, deadline - time.monotonic())
                if not selector.wait(timeout=min(0.5, remaining)):
                    return False
                self._assert_safe_control(selector)
                selector.click()
                return True
            except InputRecoveryRequired:
                raise
            except Exception as exc:
                if not (
                    self._is_stale_object_error(exc)
                    or self._is_transient_ui_transport_error(exc)
                ):
                    logger.debug(
                        "Unable to click locator %r on %s",
                        locator,
                        self.device_serial,
                        exc_info=True,
                    )
                    return False
                if self._is_transient_ui_transport_error(exc):
                    try:
                        self.d = self._connect_with_retry()
                    except Exception:
                        logger.debug(
                            "Unable to reconnect uiautomator2 before optional click retry",
                            exc_info=True,
                        )
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
        logger.debug(
            "Optional locator was not clickable on %s: %r",
            self.device_serial,
            locator,
        )
        return False

    def _dump_hierarchy_text(self) -> str:
        """Return the current UI hierarchy as XML text, best effort."""
        try:
            return str(self.d.dump_hierarchy(compressed=False))
        except TypeError:
            try:
                return str(self.d.dump_hierarchy())
            except Exception:
                logger.debug(
                    "Unable to dump UI hierarchy on %s",
                    getattr(self, "device_serial", "unknown-device"),
                    exc_info=True,
                )
        except Exception:
            logger.debug(
                "Unable to dump UI hierarchy on %s",
                getattr(self, "device_serial", "unknown-device"),
                exc_info=True,
            )
        return ""

    def dump_hierarchy_text(self) -> str:
        """Return the current hierarchy for read-only artifact collection."""
        return self._dump_hierarchy_text()

    @staticmethod
    def _hierarchy_matches_locator(
        hierarchy: str, locator: Dict[str, Any]
    ) -> Optional[bool]:
        """Return whether hierarchy XML matches a locator, or None if unknown."""
        if not hierarchy or not locator:
            return None
        # uiautomator2 evaluates XPath with lxml after normalizing node tags to
        # their Android class names.  ElementTree does neither, so an offline
        # True/False here could contradict the selector used for the real UI
        # operation.  Let the live selector probe remain authoritative.
        if "xpath" in locator:
            return None
        try:
            root = ET.fromstring(hierarchy)
        except (ET.ParseError, ValueError):
            return None

        exact_attributes = {
            "text": "text",
            "resourceId": "resource-id",
            "description": "content-desc",
            "className": "class",
        }
        contains_attributes = {
            "textContains": "text",
            "descriptionContains": "content-desc",
        }
        selector_keys = {
            *exact_attributes,
            *contains_attributes,
            "resourceIdMatches",
            "instance",
        }
        supported_values = {
            key: locator[key]
            for key in selector_keys
            if key in locator
        }
        if not supported_values:
            return None
        if any(
            value is None or (isinstance(value, str) and not value)
            for key, value in supported_values.items()
            if key != "instance"
        ):
            return None

        instance_value = locator.get("instance")
        try:
            expected_instance = (
                int(instance_value) if instance_value is not None else None
            )
        except (TypeError, ValueError):
            return None
        if expected_instance is not None and expected_instance < 0:
            return None
        resource_pattern = locator.get("resourceIdMatches")
        try:
            compiled_resource_pattern = (
                re.compile(str(resource_pattern))
                if resource_pattern is not None
                else None
            )
        except re.error:
            return None

        matched_instance = 0
        for node in root.iter("node"):
            if any(
                node.attrib.get(attribute, "") != str(locator[key])
                for key, attribute in exact_attributes.items()
                if key in locator
            ):
                continue
            if any(
                str(locator[key]) not in node.attrib.get(attribute, "")
                for key, attribute in contains_attributes.items()
                if key in locator
            ):
                continue
            if (
                compiled_resource_pattern is not None
                and compiled_resource_pattern.fullmatch(
                    node.attrib.get("resource-id", "")
                )
                is None
            ):
                continue
            if expected_instance is None or matched_instance == expected_instance:
                return True
            matched_instance += 1
        return False

    @staticmethod
    def _hierarchy_contains_package(hierarchy: str, package_name: str) -> bool:
        if not hierarchy or not package_name:
            return False
        try:
            root = ET.fromstring(hierarchy)
        except (ET.ParseError, ValueError):
            return False
        return any(
            node.attrib.get("package") == package_name for node in root.iter("node")
        )

    def _dismiss_transient_system_dialogs(self) -> bool:
        """Close known system overlays that block the app's preparation UI."""
        if getattr(self, "_input_safety", {}).get("enabled"):
            # Strict runs never tap system-owned permission, USB or IME UI.
            return False
        hierarchy = self._dump_hierarchy_text()
        if not hierarchy:
            return False

        if self._accept_privacy_dialog(hierarchy):
            return True
        if self._allow_runtime_permission_dialog(hierarchy):
            return True

        usb_dialog_visible = (
            "USB 连接方式" in hierarchy
            or "USB connection" in hierarchy
            or (
                "com.android.settings" in hierarchy
                and "android:id/alertTitle" in hierarchy
                and "android:id/button2" in hierarchy
            )
        )
        if not usb_dialog_visible:
            return False

        dismissed = self._click_if_exists(
            {"resourceId": "android:id/button2"}, timeout=0.2
        ) or self._click_if_exists({"text": "取消"}, timeout=0.2)
        if dismissed:
            logger.info(
                "Dismissed USB connection mode dialog on %s",
                getattr(self, "device_serial", "unknown-device"),
            )
            time.sleep(0.5)
            return True
        return False

    @classmethod
    def _is_privacy_dialog_component(cls, package: str, activity: str) -> bool:
        """Match MIUI's first-run privacy page without broad permission markers."""
        package_name = str(package or "").strip().lower()
        activity_name = str(activity or "").strip().lower()
        if activity_name.startswith("."):
            activity_name = package_name + activity_name
        normalized = "%s/%s" % (package_name, activity_name)
        if normalized in cls.PRIVACY_DIALOG_COMPONENTS:
            return True
        return package_name == "com.miui.securitycenter" and any(
            marker in activity_name for marker in cls.PRIVACY_DIALOG_ACTIVITY_MARKERS
        )

    @classmethod
    def _hierarchy_has_privacy_consent(cls, hierarchy: str) -> bool:
        return any(
            marker in hierarchy
            for marker in ("用户须知", "隐私政策", "用户协议", "个人信息")
        ) and any(
            cls._hierarchy_matches_locator(hierarchy, locator) is True
            for locator in cls.PRIVACY_ACCEPT_LOCATORS
        )

    def _accept_privacy_dialog(self, hierarchy: str = "") -> bool:
        """Tap the explicit positive control of a visible startup consent dialog."""
        component_is_privacy = False
        try:
            current = self._current_app()
            component_is_privacy = self._is_privacy_dialog_component(
                str(current.get("package") or ""),
                str(current.get("activity") or ""),
            )
        except Exception:
            logger.debug(
                "Unable to inspect foreground while checking privacy dialog on %s",
                getattr(self, "device_serial", "unknown-device"),
                exc_info=True,
            )
        if not component_is_privacy and not self._hierarchy_has_privacy_consent(hierarchy):
            return False
        for locator in self.PRIVACY_ACCEPT_LOCATORS:
            if self._click_if_exists(locator, timeout=0.2):
                logger.info(
                    "Accepted privacy-consent dialog on %s",
                    getattr(self, "device_serial", "unknown-device"),
                )
                time.sleep(0.5)
                return True
        return False

    def _allow_runtime_permission_dialog(self, hierarchy: str = "") -> bool:
        """Grant a narrowly-scoped Android runtime permission prompt if present."""
        # During normal app navigation the hierarchy does not contain an Android
        # permission affordance.  Avoid an app_current shell call in that common
        # case; on some ColorOS builds it can briefly reset the ADB transport.
        hierarchy_lower = hierarchy.lower()
        hierarchy_indicates_permission = any(
            marker in hierarchy_lower
            for marker in (
                "permission_allow",
                "permissioncontroller",
                "grantpermissionsactivity",
                "permissiondialog",
                "permissionrequestactivity",
                "servicepermissionactivity",
            )
        )
        if self._is_oplus_device() and not hierarchy_indicates_permission:
            return False

        current = self._current_app()
        package = str(current.get("package") or "").lower()
        activity = str(current.get("activity") or "").lower()
        permission_activity = (
            package in self.PERMISSION_CONTROLLER_PACKAGES
            or package.endswith(".permissioncontroller")
            or package.endswith(".packageinstaller")
        ) and (
            "grantpermissionsactivity" in activity
            or "permissiondialog" in activity
            or "permissionrequestactivity" in activity
            or "servicepermissionactivity" in activity
        )
        if self._is_privacy_dialog_component(package, activity):
            return False
        if not permission_activity and not hierarchy_indicates_permission:
            return False

        for locator in self.PERMISSION_ALLOW_LOCATORS:
            if self._click_if_exists(locator, timeout=0.2):
                logger.info(
                    "Granted runtime permission dialog on %s",
                    getattr(self, "device_serial", "unknown-device"),
                )
                time.sleep(0.5)
                return True
        return False

    def _dismiss_oppo_install_flow(self) -> bool:
        """Dismiss the OPPO install guide or finish page if it is visible."""
        if getattr(self, "_input_safety", {}).get("enabled"):
            return False
        try:
            current = self.d.app_current()
            current = dict(current) if isinstance(current, dict) else dict(current or {})
        except Exception:
            logger.debug(
                "Unable to inspect current UI while handling OPPO install flow",
                exc_info=True,
            )
            return False

        package = str(current.get("package") or "")
        activity = str(current.get("activity") or "")
        if package.lower() != "com.oplus.appdetail":
            return False

        # The completion page can also be exposed under a vendor-specific
        # Activity name. Its stable resource ids let us recognize it without
        # pressing BACK on an unrelated Oplus page.
        hierarchy = self._dump_hierarchy_text()
        finish_page = (
            "InstallFinishActivity" in activity
            or "launch_done_layout" in hierarchy
            or "launch_button" in hierarchy
        )
        if not finish_page:
            # ColorOS has shipped several installer Activity names. The stable
            # contract is the package and resource id, so prefer ids and only
            # use text as a compatibility fallback for older builds.
            for locator in (
                {"resourceId": "com.oplus.appdetail:id/btn_left"},
                {"resourceId": "com.oplus.appdetail:id/btn_install"},
                {"text": "继续安装"},
                {"text": "安装"},
            ):
                if self._click_if_exists(locator, timeout=0.25):
                    logger.info(
                        "Confirmed OPPO install action on %s (activity=%s)",
                        self.device_serial,
                        activity or "unknown",
                    )
                    return True

        if finish_page:
            try:
                self.d.press("back")
            except Exception:
                try:
                    self.d.shell(["input", "keyevent", "4"], timeout=10)
                except Exception:
                    logger.debug(
                        "Unable to dismiss OPPO install finish page",
                        exc_info=True,
                    )
                    return False
            return True

        return False

    def _is_oplus_device(self) -> bool:
        """Return whether the connected device uses the Oplus installer."""
        cached = getattr(self, "_device_manufacturer", None)
        if cached is None:
            values: List[str] = []
            info = getattr(self.d, "info", None)
            if isinstance(info, dict):
                values = [
                    str(info.get(key) or "").strip().lower()
                    for key in (
                        "brand",
                        "manufacturer",
                        "model",
                        "productName",
                        "product_name",
                    )
                ]
            normalized = " ".join(value for value in values if value)
            detected = any(
                marker in normalized
                for marker in ("oppo", "oneplus", "realme", "coloros", "oplus")
            ) or any(
                value.upper().startswith(self.OPLUS_MODEL_PREFIXES)
                for value in values
            )
            # Minimal test doubles and older transports may not expose usable
            # identity fields. Probe only in that case so normal Oplus startup
            # does not add another ADB command.
            if not detected and not normalized and callable(getattr(self.d, "shell", None)):
                result = self._run_direct_adb(
                    ["shell", "getprop", "ro.product.manufacturer"], timeout=10
                )
                manufacturer = (
                    self._process_output(result).strip().lower() if result else ""
                )
                normalized = manufacturer
                detected = manufacturer in {
                    "oppo",
                    "oneplus",
                    "realme",
                    "coloros",
                    "oplus",
                }
            self._device_manufacturer = "oplus" if detected else normalized
        return cached == "oplus" or getattr(self, "_device_manufacturer", "") == "oplus"

    @staticmethod
    def _normalize_device_identity(value: Any) -> str:
        """Normalize vendor identity strings across uiautomator2 variants."""
        return re.sub(r"[\s._-]+", "", str(value or "").casefold())

    def _device_identity_values(self) -> Dict[str, List[str]]:
        """Return normalized identity fields without issuing needless probes."""
        fields: Dict[str, List[str]] = {
            "brand": [],
            "manufacturer": [],
            "model": [],
            "product": [],
        }
        info = getattr(self.d, "info", None)
        if isinstance(info, dict):
            for key, bucket in (
                ("brand", "brand"),
                ("manufacturer", "manufacturer"),
                ("model", "model"),
                ("productName", "product"),
                ("product_name", "product"),
                ("device", "model"),
            ):
                value = self._normalize_device_identity(info.get(key))
                if value and value not in fields[bucket]:
                    fields[bucket].append(value)
        return fields

    def _is_vivo_x80_device(self) -> bool:
        """Return whether this device is a Vivo X80 family handset."""
        cached = str(getattr(self, "_device_manufacturer", "") or "")
        if cached == "vivo_x80":
            return True
        fields = self._device_identity_values()
        vendor_values = fields["brand"] + fields["manufacturer"] + fields["product"]
        model_values = fields["model"] + fields["product"]
        has_vivo = any("vivo" in value for value in vendor_values)
        aliases = self.VIVO_X80_MODEL_ALIASES
        model_match = any(
            value in aliases
            or value.startswith("vivox80")
            or value.startswith("x80")
            or value in {"v2144", "v2183", "v2183a", "pd2183", "pd2183a"}
            for value in model_values
        )
        # A combined ``Vivo X80`` string may be reported only in ``model``;
        # the explicit vendor check prevents unrelated X80 products from being
        # classified as Vivo.
        detected = bool(has_vivo and model_match)
        if not detected:
            combined = "".join(vendor_values + model_values)
            detected = "vivox80" in combined
        if detected:
            self._device_manufacturer = "vivo_x80"
        return detected

    def _uses_direct_adb_lifecycle(self) -> bool:
        """Use the direct ADB lifecycle for Oplus and Vivo X80 devices."""
        return self._is_oplus_device() or self._is_vivo_x80_device()

    @property
    def foreground_recovery_strategy(self) -> str:
        """Stable marker persisted with reports and recovery evidence."""
        if self._is_vivo_x80_device():
            return "vivo_x80_foreground_recovery"
        return "standard_foreground_recovery"

    def get_foreground_recovery_strategy(self) -> str:
        """Method form kept for external adapters that avoid properties."""
        return self.foreground_recovery_strategy

    @staticmethod
    def _terminate_install_process(process: subprocess.Popen) -> Tuple[str, str]:
        """Stop a host-side install once the package is visible on-device."""
        if process.poll() is None:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except (OSError, subprocess.SubprocessError):
                    logger.debug(
                        "Unable to terminate completed install process",
                        exc_info=True,
                    )
            wait = getattr(process, "wait", None)
            if callable(wait):
                try:
                    wait(timeout=3)
                except (
                    OSError,
                    subprocess.SubprocessError,
                    subprocess.TimeoutExpired,
                ):
                    kill = getattr(process, "kill", None)
                    if callable(kill):
                        try:
                            kill()
                        except (OSError, subprocess.SubprocessError):
                            logger.debug(
                                "Unable to kill completed install process",
                                exc_info=True,
                            )
        try:
            stdout, stderr = process.communicate()
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "Unable to collect install process output",
                exc_info=True,
            )
            stdout, stderr = "", ""
        return str(stdout or ""), str(stderr or "")

    def _install_apk_with_vendor_flow(
        self, apk_path: Path, timeout: int = 300
    ) -> Optional[subprocess.CompletedProcess]:
        """Install an APK while handling vendor confirmation pages."""
        serial = str(getattr(self, "device_serial", "")).strip()
        if not serial:
            return None
        command = [
            os.environ.get("ADB_PATH", "adb"),
            "-s",
            serial,
            "install",
            "-r",
            "-t",
            "-g",
            str(apk_path),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.warning(
                "Unable to start interactive ADB install on %s",
                serial,
                exc_info=True,
            )
            return None

        deadline = time.monotonic() + max(1, int(timeout))
        # Give the installer UI one turn to process the confirmation and
        # completion pages before probing pm. This also avoids racing the
        # vendor transaction immediately after the first click.
        next_install_probe = time.monotonic() + 1.0
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(
                    command,
                    int(process.returncode or 0),
                    stdout,
                    stderr,
                )
            now = time.monotonic()
            if now >= next_install_probe:
                # On ColorOS the package transaction can complete before the
                # host-side ``adb install`` process exits. Treat pm path as
                # authoritative and release the blocked host process.
                if self._is_package_installed_direct():
                    stdout, stderr = self._terminate_install_process(process)
                    logger.info(
                        "Package %s became visible before interactive ADB "
                        "install exited on %s",
                        self.package_name,
                        serial,
                    )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout,
                        stderr,
                    )
                next_install_probe = now + 1.0
            if now >= deadline:
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                stdout, stderr = process.communicate()
                logger.warning("Interactive ADB install timed out on %s", serial)
                return subprocess.CompletedProcess(
                    command,
                    int(process.returncode or -1),
                    stdout,
                    stderr,
                )
            self._dismiss_oppo_install_flow()
            self._dismiss_vendor_install_flow()
            self._dismiss_transient_system_dialogs()
            time.sleep(0.5)

    def _dismiss_vendor_install_flow(self) -> bool:
        """Confirm an OEM install prompt without ever pressing reject.

        MIUI/HyperOS renders a bottom sheet whose reject button auto-fires after
        a short countdown; vivo/HONOR render a full install page.  The prompt
        blocks a headless ``adb install`` until answered, so the install loop
        polls this method every half second.
        """
        if getattr(self, "_input_safety", {}).get("enabled"):
            # Strict runs never tap vendor/system-owned UI.
            return False
        package = ""
        try:
            current = self.d.app_current()
            package = str(dict(current or {}).get("package") or "").lower()
        except Exception:
            logger.debug(
                "Unable to read the current window while handling an install prompt",
                exc_info=True,
            )
        installer_focused = any(
            package == candidate or package.startswith(candidate + ".")
            for candidate in VENDOR_INSTALLER_PACKAGES
        )
        if package and package == str(getattr(self, "package_name", "")).lower():
            # The target app is foreground: this is a normal launch, not an
            # install prompt.  Skipping the hierarchy dump keeps the 0.5s poll
            # cheap on devices that never show a confirmation page.
            return False
        if not installer_focused:
            hierarchy = self._dump_hierarchy_text()
            if not hierarchy:
                return False
            # Outside a known installer package only a prompt that visibly offers
            # both sides is safe to answer; a lone "安装" could be app content.
            has_confirm = any(
                text in hierarchy
                for text in ("继续安装", "立即安装", "仍要安装")
            )
            has_reject = any(
                text in hierarchy for text in VENDOR_INSTALL_REJECT_TEXTS
            )
            if not (has_confirm and has_reject):
                return False
        for text in VENDOR_INSTALL_CONFIRM_TEXTS:
            if self._click_if_exists({"text": text}, timeout=0.3):
                logger.info(
                    "Confirmed vendor install prompt on %s (text=%s, package=%s)",
                    self.device_serial,
                    text,
                    package or "unknown",
                )
                return True
        return False

    def _is_package_installed_direct(self) -> bool:
        installed = self._run_direct_adb(
            ["shell", "pm", "path", self.package_name], timeout=30
        )
        if installed is None or installed.returncode != 0:
            return False
        return any(
            line.startswith("package:")
            for line in self._process_output(installed).splitlines()
        )

    def _clear_app_data_via_settings(self, timeout: float = 60.0) -> bool:
        """Clear target data through the Android Settings UI without reinstalling."""
        if getattr(self, "_input_safety", {}).get("enabled"):
            return False
        started = self._run_direct_adb(
            [
                "shell",
                "am",
                "start",
                "-W",
                "-a",
                "android.settings.APPLICATION_DETAILS_SETTINGS",
                "-d",
                "package:%s" % self.package_name,
            ],
            timeout=30,
        )
        if started is None or started.returncode != 0:
            logger.error(
                "Unable to open application settings on %s: %s",
                self.device_serial,
                self._process_output(started) if started is not None else "ADB unavailable",
            )
            return False

        deadline = time.monotonic() + max(10.0, min(120.0, float(timeout)))
        storage_locators = (
            {"text": "存储占用"},
            {"text": "存储和缓存"},
            {"text": "存储空间"},
            {"text": "Storage & cache"},
            {"text": "Storage usage"},
            {"text": "Storage"},
        )
        storage_opened = False
        while time.monotonic() < deadline and not storage_opened:
            for locator in storage_locators:
                if self._click_if_exists(locator, timeout=0.25):
                    storage_opened = True
                    break
            if not storage_opened:
                time.sleep(0.25)
        if not storage_opened:
            logger.error("Application storage page was not found on %s", self.device_serial)
            return False

        clear_locators = (
            {"resourceId": "com.android.settings:id/button", "text": "清除数据"},
            {"resourceId": "com.android.settings:id/button", "text": "清除存储空间"},
            {"text": "清除数据"},
            {"text": "清除存储空间"},
            {"text": "Clear data"},
            {"text": "Clear storage"},
        )
        clear_selector = None
        while time.monotonic() < deadline and clear_selector is None:
            for locator in clear_locators:
                try:
                    selector, _ = self._selector(locator)
                    if selector.wait(timeout=0.25):
                        clear_selector = selector
                        break
                except Exception:
                    logger.debug(
                        "Unable to inspect Settings clear-data control %r",
                        locator,
                        exc_info=True,
                    )
            if clear_selector is None:
                time.sleep(0.25)
        if clear_selector is None:
            logger.error("Clear-data control was not found on %s", self.device_serial)
            return False

        try:
            clear_info = dict(getattr(clear_selector, "info", {}) or {})
        except Exception:
            clear_info = {}
        if clear_info.get("enabled") is False:
            logger.info("Application data is already empty on %s", self.device_serial)
            return self._is_package_installed_direct()

        try:
            if getattr(self, "_input_safety", {}).get("enabled"):
                clear_info = dict(getattr(clear_selector, "info", {}) or {})
                if clear_info.get("packageName") != self.package_name:
                    logger.error(
                        "Refusing to click external Settings clear-data control on %s",
                        self.device_serial,
                    )
                    return False
            clear_selector.click()
        except Exception:
            logger.exception("Unable to click clear-data control on %s", self.device_serial)
            return False

        confirm_locators = (
            {"resourceId": "android:id/button3"},
            {"resourceId": "android:id/button1"},
            {"text": "删除"},
            {"text": "确定"},
            {"text": "确认"},
            {"text": "Delete"},
            {"text": "OK"},
        )
        confirmed = False
        while time.monotonic() < deadline and not confirmed:
            for locator in confirm_locators:
                if self._click_if_exists(locator, timeout=0.25):
                    confirmed = True
                    break
            if not confirmed:
                time.sleep(0.25)
        if not confirmed:
            logger.error("Clear-data confirmation was not found on %s", self.device_serial)
            return False

        while time.monotonic() < deadline:
            if not self._is_package_installed_direct():
                logger.error(
                    "Package %s disappeared while clearing data on %s",
                    self.package_name,
                    self.device_serial,
                )
                return False
            try:
                info = dict(getattr(clear_selector, "info", {}) or {})
                if info.get("enabled") is False:
                    logger.info(
                        "Cleared %s data through Android Settings on %s",
                        self.package_name,
                        self.device_serial,
                    )
                    return True
            except Exception:
                logger.debug("Waiting for Settings data-clear completion", exc_info=True)
            time.sleep(0.5)

        logger.error("Settings did not confirm data clear on %s", self.device_serial)
        return False

    def _clear_app_data(self) -> bool:
        details: List[str] = []
        shell = getattr(self.d, "shell", None)
        if callable(shell):
            try:
                result = shell(["pm", "clear", self.package_name], timeout=30)
                output = str(getattr(result, "output", "")).strip()
                exit_code = int(getattr(result, "exit_code", -1))
                if exit_code == 0 and output == "Success":
                    self._data_clear_capability = {
                        "supported": True,
                        "method": "uiautomator_pm_clear",
                    }
                    return True
                details.append(output)
            except Exception as exc:
                details.append(str(exc))
        else:
            try:
                if self.d.app_clear(self.package_name) is not False:
                    self._data_clear_capability = {
                        "supported": True,
                        "method": "uiautomator_app_clear",
                    }
                    return True
            except Exception as exc:
                details.append(str(exc))

        direct = self._run_direct_adb(
            ["shell", "pm", "clear", self.package_name],
            timeout=30,
            transport_retries=3,
        )
        if direct is not None:
            output = self._process_output(direct)
            if direct.returncode == 0 and output == "Success":
                self._data_clear_capability = {
                    "supported": True,
                    "method": "adb_pm_clear",
                }
                return True
            details.append(output)

        failure = "\n".join(value for value in details if value)
        logger.warning(
            "Command-line data clear failed on %s: %s; trying Android Settings",
            getattr(self, "device_serial", "unknown-device"),
            failure or "unknown error",
        )
        if self._clear_app_data_via_settings():
            self._data_clear_capability = {
                "supported": True,
                "method": "settings_ui_clear",
            }
            return True
        reason = failure or "unknown error"
        lowered = reason.lower()
        denied = any(
            marker in lowered
            for marker in (
                "clear_app_user_data",
                "permission denial",
                "securityexception",
                "not allowed",
                "not permitted",
            )
        )
        self._data_clear_capability = {
            "supported": not denied,
            "method": "clear_failed",
            "reason": reason,
        }
        logger.error(
            "Application data clear failed without uninstalling or reinstalling: %s",
            reason,
        )
        return False

    def _start_target_app(
        self,
        stop: bool,
        wait: bool,
        clear_task: bool = False,
    ):
        """Start the launcher activity without relying on vendor Monkey output."""
        if self._uses_direct_adb_lifecycle():
            direct_start = self._start_target_app_via_direct_adb(
                stop=stop,
                clear_task=clear_task,
                retry_attempts=5,
            )
            if direct_start is None:
                logger.warning(
                    "Unable to resolve a direct launcher component for ColorOS device %s",
                    self.device_serial,
                )
                return False
            # Do not fall through to uiautomator2.app_start here. Its launcher
            # path adds Monkey, process-list, and wait commands after a direct
            # ADB transport failure, which can turn one transient reconnect into
            # a command burst on affected ColorOS devices.
            return direct_start

        activity: Optional[str] = None
        try:
            result = self.d.shell(
                [
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
                timeout=30,
            )
            output = str(getattr(result, "output", ""))
            for line in reversed(output.splitlines()):
                component = line.strip()
                if not component.startswith(self.package_name + "/"):
                    continue
                candidate = component.split("/", 1)[1]
                if re.fullmatch(r"[A-Za-z0-9_.$]+", candidate):
                    activity = candidate
                    self._launcher_activity = candidate
                    break
        except Exception:
            logger.debug(
                "Unable to resolve launcher activity for %s",
                self.package_name,
                exc_info=True,
            )

        if clear_task:
            if not activity:
                direct_start = self._start_target_app_via_direct_adb(
                    stop=stop,
                    clear_task=True,
                )
                if direct_start is not None:
                    return direct_start
                logger.error(
                    "Unable to resolve launcher activity for clear-task start of %s",
                    self.package_name,
                )
                return False
            component = "%s/%s" % (self.package_name, activity)
            try:
                result = self.d.shell(
                    [
                        "am",
                        "start",
                        "-W",
                        "-f",
                        "0x10008000",
                        "-n",
                        component,
                    ],
                    timeout=30,
                )
            except Exception:
                logger.warning(
                    "uiautomator2 clean-task launch failed for %s; "
                    "trying direct ADB",
                    self.device_serial,
                    exc_info=True,
                )
                direct_start = self._start_target_app_via_direct_adb(
                    # reset_app already stopped the target before reaching
                    # this fallback. Repeating force-stop can make some
                    # ColorOS USB transports temporarily disappear.
                    stop=False,
                    clear_task=True,
                    activity=activity,
                )
                if direct_start is not None:
                    return direct_start
                return False
            output = str(getattr(result, "output", ""))
            exit_code = getattr(result, "exit_code", 0)
            if exit_code not in (0, None) or re.search(
                r"\b(?:Error|Exception):", output, re.IGNORECASE
            ):
                logger.error(
                    "Clear-task launch failed for %s: %s",
                    self.package_name,
                    output.strip() or "unknown error",
                )
                direct_start = self._start_target_app_via_direct_adb(
                    stop=False,
                    clear_task=True,
                    activity=activity,
                )
                if direct_start is not None:
                    return direct_start
                return False
            return True

        if activity:
            try:
                started = self.d.app_start(
                    self.package_name,
                    activity=activity,
                    stop=stop,
                    wait=wait,
                )
                if started is not False:
                    return started
            except TypeError as exc:
                if "activity" not in str(exc):
                    raise
            except Exception:
                logger.warning(
                    "uiautomator2 launcher start failed for %s; trying direct ADB",
                    self.device_serial,
                    exc_info=True,
                )

            direct_start = self._start_target_app_via_direct_adb(
                stop=False,
                clear_task=False,
                activity=activity,
            )
            if direct_start is not None:
                return direct_start

        direct_start = self._start_target_app_via_direct_adb(
            stop=stop,
            clear_task=False,
        )
        if direct_start is not None:
            return direct_start
        return self.d.app_start(self.package_name, stop=stop, wait=wait)

    def _start_target_app_via_direct_adb(
        self,
        *,
        stop: bool,
        clear_task: bool,
        activity: Optional[str] = None,
        retry_attempts: int = 5,
    ) -> Optional[bool]:
        """Use direct ADB when a uiautomator2 transport briefly disconnects.

        A ColorOS device can remain visible to ADB while uiautomator2's cached
        transport returns ``device not found``.  Resolve and start the launcher
        through the same ADB server before allowing the legacy Monkey fallback.
        ``None`` means no launch component could be resolved, so callers may
        retain their existing fallback behavior.
        """
        cached_activity = str(getattr(self, "_launcher_activity", "") or "").strip()
        resolved_activity = activity or cached_activity or None
        attempts = max(1, min(5, int(retry_attempts)))
        stop_completed = not stop
        for attempt in range(attempts):
            if not resolved_activity:
                try:
                    resolved = self._run_direct_adb(
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
                        timeout=30,
                    )
                except Exception:
                    resolved = None
                    logger.debug(
                        "Direct ADB launcher resolution failed on %s",
                        self.device_serial,
                        exc_info=True,
                    )
                if resolved is not None and resolved.returncode == 0:
                    output = self._process_output(resolved)
                    for line in reversed(output.splitlines()):
                        component = line.strip()
                        if not component.startswith(self.package_name + "/"):
                            continue
                        candidate = component.split("/", 1)[1]
                        if re.fullmatch(r"[A-Za-z0-9_.$]+", candidate):
                            resolved_activity = candidate
                            self._launcher_activity = candidate
                            break

            if resolved_activity:
                if not stop_completed:
                    try:
                        stopped = self._run_direct_adb(
                            ["shell", "am", "force-stop", self.package_name],
                            timeout=30,
                        )
                    except Exception:
                        stopped = None
                    if stopped is None or stopped.returncode != 0:
                        logger.warning(
                            "Direct ADB force-stop failed on %s while launching %s",
                            self.device_serial,
                            self.package_name,
                        )
                        if attempt < attempts - 1:
                            time.sleep(0.75 * (attempt + 1))
                            continue
                        return False
                    stop_completed = True

                # ColorOS can print a valid "Starting: Intent" response yet
                # return a non-zero status for ``am start -W``. The caller
                # verifies foreground state separately, so avoid waiting in
                # the command and accept that response as a launch request.
                command = ["shell", "am", "start"]
                if clear_task:
                    command.extend(["-f", "0x10008000"])
                command.extend(
                    ["-n", "%s/%s" % (self.package_name, resolved_activity)]
                )
                try:
                    started = self._run_direct_adb(command, timeout=30)
                except Exception:
                    started = None
                    logger.debug(
                        "Direct ADB launcher start raised on %s",
                        self.device_serial,
                        exc_info=True,
                    )
                output = self._process_output(started) if started is not None else ""
                launch_acknowledged = "Starting: Intent" in output
                if started is not None and (
                    (
                        started.returncode == 0
                        and not re.search(
                            r"\b(?:Error|Exception):", output, re.IGNORECASE
                        )
                    )
                    or launch_acknowledged
                ):
                    self._last_known_app = {
                        "package": self.package_name,
                        "activity": resolved_activity,
                    }
                    return True
                # A host-side timeout/non-zero response is ambiguous on Vivo
                # X80: ActivityManager may have completed the launch already.
                # Confirm a stable target foreground before sending another
                # launch command, which avoids bouncing the task back to the
                # Launcher and preserves the current pressure cycle.
                if self._is_vivo_x80_device() and self._target_foreground_stable_without_relaunch():
                    return True
                logger.warning(
                    "Direct ADB launcher start failed on %s: %s",
                    self.device_serial,
                    output.strip() or "ADB unavailable",
                )
                if attempt < attempts - 1:
                    time.sleep(0.75 * (attempt + 1))
                    continue
                return False

            if attempt < attempts - 1:
                time.sleep(0.75 * (attempt + 1))

        return None

    def _stop_package_via_direct_adb(self, package_name: str) -> bool:
        """Stop one Oplus package without depending on a cached U2 transport."""
        # Once a ColorOS USB/ADB session drops, sending several force-stop
        # commands in quick succession delays its recovery. One bounded retry
        # keeps the lifecycle deterministic without flooding the transport.
        for attempt in range(4):
            try:
                stopped = self._run_direct_adb(
                    ["shell", "am", "force-stop", package_name],
                    timeout=30,
                    transport_retries=1,
                )
            except Exception:
                stopped = None
            if stopped is not None and stopped.returncode == 0:
                return True
            if attempt < 3:
                time.sleep(min(1.0 + attempt * 0.5, 2.0))
        logger.warning(
            "Direct ADB force-stop could not reach %s on %s",
            package_name,
            self.device_serial,
        )
        return False

    def _install_apk_via_shell_pm(self, apk_path: Path, timeout: int = 240) -> bool:
        """Install one missing package through device-side Package Manager."""
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", apk_path.name)
        remote_path = "/data/local/tmp/app_press_test_%s_%s" % (
            os.getpid(),
            safe_name,
        )
        pushed = self._run_direct_adb(
            ["push", str(apk_path), remote_path], timeout=min(120, timeout)
        )
        if pushed is None or pushed.returncode != 0:
            logger.warning(
                "Unable to stage APK on %s: %s",
                self.device_serial,
                self._process_output(pushed) if pushed is not None else "ADB unavailable",
            )
            return False
        try:
            installed = self._run_direct_adb(
                [
                    "shell",
                    "pm",
                    "install",
                    "-r",
                    "-d",
                    "-t",
                    "-g",
                    remote_path,
                ],
                timeout=timeout,
            )
            output = self._process_output(installed) if installed is not None else ""
            if installed is None or installed.returncode != 0 or "Success" not in output:
                logger.warning(
                    "Device-side Package Manager install failed on %s: %s",
                    self.device_serial,
                    output or "ADB unavailable",
                )
                return False
            if not self._is_package_installed_direct():
                logger.error(
                    "Package Manager reported success but %s is absent on %s",
                    self.package_name,
                    self.device_serial,
                )
                return False
            logger.info(
                "Installed missing package %s through device Package Manager on %s",
                self.package_name,
                self.device_serial,
            )
            return True
        finally:
            self._run_direct_adb(["shell", "rm", "-f", remote_path], timeout=30)

    def apply_device_permissions(
        self, options: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Grant the configured runtime/appops permissions and audit the result.

        Stability runs start from a fully-permissioned app: ``pm install -g``
        covers the dangerous permissions declared at install time, and this
        method covers everything else (permissions revoked later, appops-only
        switches such as overlays or all-files access).  Failures are recorded
        instead of being swallowed so a missing grant cannot look like coverage.
        """
        from .device_permissions import apply_permission_setup

        settings = dict(options) if isinstance(options, Mapping) else {}
        result = apply_permission_setup(
            self._run_direct_adb,
            self.package_name,
            grant_permissions=settings.get("grant_permissions") or [],
            appops=settings.get("appops_permissions") or {},
            disable_verifier=bool(settings.get("disable_package_verifier", False)),
        )
        self.device_permission_report = result
        granted = len(result["grant"]["granted"])
        failed = len(result["grant"]["failed"]) + len(result["appops"]["failed"])
        if failed:
            logger.warning(
                "Device permission setup on %s: %s granted, %s failed — %s",
                self.device_serial,
                granted,
                failed,
                "; ".join(
                    str(item.get("permission") or item.get("op"))
                    for item in (
                        list(result["grant"]["failed"])
                        + list(result["appops"]["failed"])
                    )
                ),
            )
        else:
            logger.info(
                "Device permission setup on %s: %s permission(s) granted, "
                "%s appop(s) applied",
                self.device_serial,
                granted,
                len(result["appops"]["applied"]),
            )
        return result

    def install_app(self, apk_path: str, reinstall: bool = False) -> bool:
        """Install the configured APK without opening the system installer UI.

        ``uiautomator2.app_install`` may launch a vendor package-installer
        activity that waits for a manual "Continue" tap.  A direct ADB
        install is headless on authorized devices, so use it first and only
        fall back to uiautomator2 for older/unsupported transports.
        """
        try:
            raw_path = str(apk_path).strip()
            if (
                re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw_path)
                or raw_path.startswith(("\\\\", "//"))
            ):
                logger.error("Only a local APK filesystem path is allowed")
                return False
            path = Path(raw_path).expanduser().resolve(strict=False)
            if str(path).startswith(("\\\\", "//")):
                logger.error("Network-share APK paths are not allowed")
                return False
            if path.suffix.lower() != ".apk" or not path.is_file():
                logger.error("APK does not exist: %s", path)
                return False

            self._apk_path = path

            installed_packages = set(self.d.app_list())
            if self.package_name in installed_packages and not reinstall:
                logger.info("Package %s is already installed", self.package_name)
                return True
            if self.package_name in installed_packages:
                logger.info(
                    "Reinstalling %s because the requested APK fingerprint changed",
                    self.package_name,
                )

            install_args = ["install", "-r", "-t", "-g", str(path)]
            direct: Optional[subprocess.CompletedProcess] = None
            direct_output = ""
            interactive: Optional[subprocess.CompletedProcess] = None
            if self._is_oplus_device() and self._install_apk_via_shell_pm(path):
                return True
            # Every OEM may put its own confirmation page in front of the install
            # (MIUI bottom sheet with a reject countdown, vivo/HONOR install page,
            # ColorOS guide). That page blocks the headless ``adb install`` until
            # it times out, which used to end as "waiting for manual install".
            # ``_install_apk_with_vendor_flow`` runs the same adb command but
            # dismisses those pages while the host process is blocked.
            interactive = self._install_apk_with_vendor_flow(path, timeout=300)
            if (
                interactive is not None
                and interactive.returncode == 0
                and self._is_package_installed_direct()
            ):
                logger.info(
                    "Installed %s through vendor install flow",
                    self.package_name,
                )
                return True

            direct = self._run_direct_adb(install_args, timeout=300)
            direct_output = self._process_output(direct) if direct is not None else ""
            if (
                direct is not None
                and direct.returncode == 0
                and "Success" in direct_output.splitlines()
            ):
                installed = self._run_direct_adb(
                    ["shell", "pm", "path", self.package_name], timeout=30
                )
                if (
                    installed is not None
                    and installed.returncode == 0
                    and any(
                        line.startswith("package:")
                        for line in self._process_output(installed).splitlines()
                    )
                ):
                    logger.info("Installed %s through headless ADB", self.package_name)
                    return True
                logger.warning(
                    "ADB reported install success but package verification failed on %s",
                    self.device_serial,
                )
            else:
                logger.warning(
                    "Headless ADB install failed on %s: %s; trying interactive vendor flow",
                    self.device_serial,
                    direct_output or "ADB unavailable",
                )

            if interactive is None:
                interactive = self._install_apk_with_vendor_flow(path, timeout=300)
            interactive_output = (
                self._process_output(interactive) if interactive is not None else ""
            )
            if (
                interactive is not None
                and interactive.returncode == 0
                and self._is_package_installed_direct()
            ):
                logger.info("Installed %s through vendor install flow", self.package_name)
                return True
            logger.warning(
                "Interactive vendor install failed on %s: %s; trying uiautomator2 fallback",
                self.device_serial,
                interactive_output or "ADB unavailable",
            )

            try:
                self.d.app_install(str(path))
            except Exception:
                logger.exception("uiautomator2 APK install fallback failed")
                return False
            return self.package_name in set(self.d.app_list())
        except Exception:
            logger.exception("Failed to install APK %s", apk_path)
            return False

    def _selector(self, locator: Dict[str, Any]):
        """Create a uiautomator2 selector from text/resourceId/xpath data."""
        if not locator:
            raise ValueError("A non-empty locator is required")
        xpath = locator.get("xpath")
        if xpath:
            return self.d.xpath(xpath), True

        supported = {
            key: value
            for key, value in locator.items()
            if key
            in {
                "text",
                "textContains",
                "resourceId",
                "resourceIdMatches",
                "description",
                "descriptionContains",
                "className",
                "instance",
                "packageName",
            }
        }
        if not supported:
            raise ValueError("Unsupported locator: %r" % locator)
        if getattr(self, "_input_safety", {}).get("enabled"):
            supported["packageName"] = self.package_name
        return self.d(**supported), False

    def _wait_for_selector(
        self,
        locator: Dict[str, Any],
        timeout: float = 10.0,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Tuple[Any, bool]:
        selector, is_xpath = self._selector(locator)
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                raise InterruptedError("selector wait was cancelled")
            remaining = deadline - time.monotonic()
            self._dismiss_transient_system_dialogs()
            try:
                if selector.wait(timeout=min(0.5, max(0.1, remaining))):
                    return selector, is_xpath
            except Exception as exc:
                logger.debug(
                    "Selector probe failed on %s: %r",
                    getattr(self, "device_serial", "unknown-device"),
                    locator,
                    exc_info=True,
                )
                if self._is_stale_object_error(exc):
                    # A UiObject is a snapshot-backed handle. Once Android
                    # reports it stale, waiting on that same handle can never
                    # recover; query a fresh selector for the next probe.
                    selector, is_xpath = self._selector(locator)
                elif self._is_transient_ui_transport_error(exc):
                    raise
            time.sleep(0.1)
        raise TimeoutError("Element was not found: %r" % locator)

    @staticmethod
    def _is_stale_object_error(exc: BaseException) -> bool:
        """Recognize direct and RPC-wrapped UiAutomator stale-object errors."""
        text = "%s: %s" % (type(exc).__name__, exc)
        normalized = text.replace("_", "").replace(" ", "").lower()
        return "staleobject" in normalized

    @staticmethod
    def _is_transient_ui_transport_error(exc: BaseException) -> bool:
        """Recognize short ADB/HTTP drops that can recover with a fresh U2 session."""
        text = ("%s: %s" % (type(exc).__name__, exc)).lower()
        return any(
            marker in text
            for marker in (
                "remotedisconnected",
                "remote end closed connection",
                "device not found",
                "device offline",
                "connection reset",
                "connection aborted",
                "connection refused",
                "uiautomation not connected",
                "uiautomator server not started",
            )
        )

    def _click_locator_with_retry(
        self,
        locator: Dict[str, Any],
        timeout: float = 10.0,
        should_stop: Optional[Callable[[], bool]] = None,
        attempts: int = 3,
    ) -> None:
        """Click a newly-resolved selector, retrying RPC-wrapped stale handles."""
        deadline = time.monotonic() + max(0.1, float(timeout))
        last_error: Optional[BaseException] = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            if should_stop is not None and should_stop():
                raise InterruptedError("selector click was cancelled")
            remaining = max(0.1, deadline - time.monotonic())
            try:
                selector, _ = self._wait_for_selector(
                    locator, remaining, should_stop=should_stop
                )
                self._assert_safe_control(selector)
                selector.click()
                return
            except Exception as exc:
                last_error = exc
                stale = self._is_stale_object_error(exc)
                transport = self._is_transient_ui_transport_error(exc)
                if (not stale and not transport) or attempt >= attempts:
                    raise
                logger.warning(
                    "UI click failed transiently on %s; resolving it again "
                    "(%s/%s): %s",
                    getattr(self, "device_serial", "unknown-device"),
                    attempt,
                    attempts,
                    exc,
                )
                if transport:
                    try:
                        self.d = self._connect_with_retry()
                    except Exception:
                        logger.warning(
                            "Unable to refresh uiautomator2 on %s before click retry",
                            getattr(self, "device_serial", "unknown-device"),
                            exc_info=True,
                        )
                delay = 0.2 if stale else 0.5 * attempt
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        if last_error is not None:  # pragma: no cover - loop always raises/returns
            raise last_error

    def configure_input_safety(self, config: Dict[str, Any]) -> None:
        self._input_safety = input_safety_options(config)
        self.input_recovery_required = False

    def input_method_state(self) -> Dict[str, Any]:
        """Expose only passive diagnostics; callers must not tap the IME."""
        from .input_safety import probe_input_method
        return probe_input_method(self._run_direct_adb)

    def _require_input_recovery(self, reason: str) -> None:
        self.input_recovery_required = True
        self.last_navigation_error = reason
        raise InputRecoveryRequired(reason) from None

    def ensure_input_safe(self) -> None:
        """Require the target app to own the foreground before we send input.

        The keyboard probe was removed: every test device now uses the clean
        test keyboard, and vendor IMEs left stale visibility flags that made a
        healthy run pause and relaunch the app.  Foreground ownership is still
        checked here because a real foreground loss breaks every click.
        """
        options = getattr(self, "_input_safety", {})
        if not options.get("enabled"):
            return
        foreground = self._foreground_app().get("package")
        if foreground != self.package_name and is_system_dialog_package(foreground):
            # A vendor permission prompt is a transient window above our own
            # task.  Give it a moment to be answered (Fastbot's allow-list may
            # tap it, or the system dismisses it) before demanding recovery;
            # otherwise every prompt becomes a false "the app is gone" event.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                foreground = self._foreground_app().get("package")
                if foreground == self.package_name:
                    break
                if not is_system_dialog_package(foreground):
                    break
        if foreground != self.package_name:
            self._require_input_recovery("target app does not own the foreground before input")

    def _assert_safe_control(self, selector: Any, *, touch: bool = True) -> None:
        if not getattr(self, "_input_safety", {}).get("enabled"):
            return
        self.ensure_input_safe()
        info = getattr(selector, "info", None)
        if info is None and callable(getattr(selector, "get", None)):
            info = selector.get().info
        if not isinstance(info, dict) or info.get("packageName") != self.package_name:
            self._require_input_recovery("input control ownership could not be verified")
        bounds = info.get("bounds", {})
        if touch and not bounds:
            self._require_input_recovery("control bounds could not be verified")
        if touch and bounds:
            self._assert_safe_coordinates(
                (bounds["left"] + bounds["right"]) // 2,
                (bounds["top"] + bounds["bottom"]) // 2,
            )

    def _assert_safe_coordinates(
        self, x: int, y: int, end: Optional[Tuple[int, int]] = None,
    ) -> None:
        if not getattr(self, "_input_safety", {}).get("enabled"):
            return
        self.ensure_input_safe()
        try:
            width, height = self.d.window_size()
            nodes = list(ET.fromstring(self._dump_hierarchy_text()).iter("node"))
        except Exception:
            self._require_input_recovery("touch window bounds could not be verified")
        points = [(x, y)] if end is None else [(x, y), end]
        # Exclude system gesture edges even for full-screen app layouts.
        if not all(
            width * 0.03 < px < width * 0.97 and height * 0.05 < py < height * 0.95
            for px, py in points
        ):
            self._require_input_recovery("coordinate falls in a system gesture area")
        if not touch_path_is_owned(nodes, self.package_name, points):
            self._require_input_recovery("touch path is outside the app or covered by an external window")

    def _native_input_selector(self, selector: Any, is_xpath: bool) -> Any:
        if not is_xpath:
            return selector
        if not getattr(self, "_input_safety", {}).get("enabled"):
            return selector
        # XPathSelector.set_text itself uses click + send_keys in U2. Resolve
        # its exact bounds to a native UiObject and call the setText RPC.
        element = selector.get()
        info = element.info
        if info.get("packageName") != self.package_name:
            self._require_input_recovery("XPath input belongs to an external window")
        query = {"packageName": self.package_name, "className": info["className"]}
        resource_id = info.get("resourceName") or info.get("resourceId")
        if resource_id:
            query["resourceId"] = resource_id
        candidates = self.d(**query)
        for index in range(candidates.count):
            candidate = candidates[index]
            if candidate.info.get("bounds") == info.get("bounds"):
                return candidate
        self._require_input_recovery("XPath input could not be resolved to a native control")

    def _set_text(self, locator: Dict[str, Any], value: str) -> None:
        self.ensure_input_safe()
        selector, is_xpath = self._wait_for_selector(locator)
        selector = self._native_input_selector(selector, is_xpath)
        self._assert_safe_control(selector, touch=False)
        setter = getattr(selector, "set_text", None)
        if not callable(setter):
            self._require_input_recovery("control does not support direct text input")
        try:
            if setter(value) is False:
                self._require_input_recovery("control rejected direct text input")
        except Exception:
            # Do not copy typed credentials into the recovery report.
            self._require_input_recovery("control text input did not complete")
        self.ensure_input_safe()

    def _complete_audit_gate(
        self,
        audit_locator: Dict[str, Any],
        confirm_locator: Dict[str, Any],
        timeout: float,
        audit_activity: str = "",
        should_stop: Optional[Callable[[], bool]] = None,
        allow_stable_foreground_fallback: bool = False,
    ) -> bool:
        """Select the audit state and clear any following startup consent.

        The action is intentionally idempotent.  A cycle can arrive here with
        the gate already completed, especially after a manual intervention or
        when a vendor device cannot clear app data.  In that case the method
        continues instead of failing on a missing startup-only control.
        """
        if not audit_locator:
            raise ValueError("audit gate requires an audit locator")
        if not confirm_locator:
            raise ValueError("audit gate requires a confirm locator")

        # The gate and privacy consent are first-launch screens.  When this cycle
        # already passed them, a recovery relaunch lands directly on the home
        # page and there is nothing to complete; waiting here was what pushed
        # devices to "needs manual resume" after an unrelated recovery.
        if getattr(self, "_startup_gates_passed", False):
            try:
                current = dict(self.d.app_current() or {})
            except Exception:
                current = {}
            if str(current.get("package") or "") == self.package_name:
                logger.info(
                    "Startup gates already completed in this cycle on %s; "
                    "skipping audit gate recovery",
                    self.device_serial,
                )
                self._audit_gate_known_complete = True
                return True

        normalized_audit_activity = ""
        if audit_activity:
            package, separator, activity = str(audit_activity).partition("/")
            if separator:
                normalized_audit_activity = self._normalize_component(package, activity)

        deadline = time.monotonic() + max(0.1, float(timeout))
        # Keep the value observed before this recovery attempt separate from
        # evidence collected while probing the current foreground.  A newly
        # inferred ``known complete`` value must not silently satisfy the
        # recovery-only foreground fallback; it has to be backed by a valid
        # hierarchy (or by the explicit historical marker that predates the
        # relaunch).
        initial_known_gate_complete = bool(
            getattr(self, "_audit_gate_known_complete", False)
        )
        gate_observed = False
        self._launcher_observed = False
        launcher_observed = False
        target_foreground_since: Optional[float] = None
        target_foreground_probes = 0
        target_foreground_component = ""
        audit_probe_failures = 0
        empty_hierarchy_probes = 0
        last_probe_error = ""

        def audit_control_state(
            probe_timeout: float = 0.35,
        ) -> Tuple[bool, Optional[bool], bool]:
            """Return visibility, checked state, and whether the probe completed."""
            nonlocal audit_probe_failures, last_probe_error
            try:
                selector, _ = self._selector(audit_locator)
                if not selector.wait(timeout=max(0.1, probe_timeout)):
                    return False, None, True
                checked: Optional[bool] = None
                try:
                    info = selector.info
                    if callable(info):
                        info = info()
                    if isinstance(info, dict) and "checked" in info:
                        value = info.get("checked")
                        if isinstance(value, bool):
                            checked = value
                        elif str(value).strip().lower() in {"true", "false"}:
                            checked = str(value).strip().lower() == "true"
                except Exception:
                    logger.debug(
                        "Unable to read audit selection state on %s",
                        getattr(self, "device_serial", "unknown-device"),
                        exc_info=True,
                    )
                return True, checked, True
            except Exception as exc:
                audit_probe_failures += 1
                last_probe_error = "%s: %s" % (type(exc).__name__, str(exc))
                logger.debug(
                    "Unable to probe audit control on %s",
                    getattr(self, "device_serial", "unknown-device"),
                    exc_info=True,
                )
                return False, None, False

        def click_audit_control(remaining: float) -> None:
            # The project JSON uses the resource id.  A few vendor builds expose
            # the same RadioButton only by its visible label, so retain the
            # framework locator first and use the label as a bounded fallback.
            try:
                self._click_locator_with_retry(
                    audit_locator,
                    timeout=min(10.0, max(0.5, remaining)),
                    should_stop=should_stop,
                )
                return
            except Exception:
                if audit_locator.get("text") or audit_locator.get("textContains"):
                    raise
                self._click_locator_with_retry(
                    {"text": "审核中"},
                    timeout=min(5.0, max(0.5, remaining)),
                    should_stop=should_stop,
                )

        audit_selected = False
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return False
            self._dismiss_transient_system_dialogs()
            try:
                remaining = max(0.5, deadline - time.monotonic())
                gate_visible, checked, audit_probe_succeeded = audit_control_state(
                    min(0.5, remaining)
                )
                if gate_visible:
                    gate_observed = True
                    self._audit_gate_known_complete = False
                    target_foreground_since = None
                    target_foreground_probes = 0
                    # Xiaomi keeps the RadioButton and confirmation button on
                    # the same page after selection.  Clicking an already
                    # checked RadioButton can toggle it back off, so retries
                    # must go directly to confirmation while it remains set.
                    if checked is False or (checked is None and not audit_selected):
                        click_audit_control(remaining)
                        audit_selected = True
                    elif checked is True:
                        audit_selected = True
                    self._click_locator_with_retry(
                        confirm_locator,
                        timeout=min(10.0, max(0.5, deadline - time.monotonic())),
                        should_stop=should_stop,
                    )
                    self._last_known_app = {
                        "package": self.package_name,
                        "activity": "",
                    }
                    # A confirmation can replace the radio with a privacy
                    # dialog in the same Activity. Re-probe the radio, then
                    # check the current hierarchy before declaring success.
                    time.sleep(0.8)
                    gate_visible, checked, audit_probe_succeeded = (
                        audit_control_state(0.35)
                    )
                    if checked is not None:
                        audit_selected = checked
                    if gate_visible:
                        logger.warning(
                            "Audit gate remained visible after confirmation on %s; "
                            "retrying",
                            getattr(self, "device_serial", "unknown-device"),
                        )
                    elif not audit_probe_succeeded:
                        logger.warning(
                            "Unable to verify audit gate clearance after confirmation "
                            "on %s; retrying",
                            getattr(self, "device_serial", "unknown-device"),
                        )
            except InterruptedError:
                return False
            except Exception as exc:
                last_probe_error = "%s: %s" % (type(exc).__name__, str(exc))
                logger.debug(
                    "Audit gate probe failed on %s",
                    getattr(self, "device_serial", "unknown-device"),
                    exc_info=True,
                )

            current = self._current_app()
            current_package = str(current.get("package") or "")
            current_activity = str(current.get("activity") or "")
            if current_package in self.LAUNCHER_PACKAGES or bool(
                getattr(self, "_launcher_observed", False)
            ):
                # A selector absence while the launcher is foreground is not
                # evidence that the target application's startup gate is gone.
                # Require one extra target probe after this transition before
                # using the recovery fallback below.
                launcher_observed = True
            if current_package == self.package_name:
                current_component = self._normalize_component(
                    current_package, current_activity
                )
                now = time.monotonic()
                if current_component == target_foreground_component:
                    target_foreground_probes += 1
                else:
                    target_foreground_component = current_component
                    target_foreground_since = now
                    target_foreground_probes = 1
                hierarchy = self._dump_hierarchy_text()
                if self._hierarchy_has_privacy_consent(hierarchy):
                    # The next pass handles the visible consent through the
                    # existing dialog path. A successful tap alone does not
                    # prove that the dialog closed.
                    gate_observed = True
                    self._audit_gate_known_complete = False
                    target_foreground_since = None
                    target_foreground_probes = 0
                    time.sleep(0.2)
                    continue
                if (
                    normalized_audit_activity
                    and current_component != normalized_audit_activity
                    and not gate_observed
                    and audit_probe_succeeded
                    and not gate_visible
                ):
                    self._audit_gate_known_complete = True
                    logger.info(
                        "Audit gate is not in the foreground on %s; continuing from %s",
                        getattr(self, "device_serial", "unknown-device"),
                        current_component,
                    )
                    return True
                hierarchy_gate_state = self._hierarchy_matches_locator(
                    hierarchy, audit_locator
                )
                target_hierarchy = self._hierarchy_contains_package(
                    hierarchy, self.package_name
                )
                if hierarchy_gate_state is True:
                    gate_observed = True
                    self._audit_gate_known_complete = False
                    target_foreground_since = None
                    target_foreground_probes = 0
                if (
                    gate_observed
                    and audit_probe_succeeded
                    and not gate_visible
                    and target_hierarchy
                    and hierarchy_gate_state is not True
                ):
                    self._audit_gate_known_complete = True
                    logger.info(
                        "Previously observed audit gate cleared on %s; continuing from %s",
                        getattr(self, "device_serial", "unknown-device"),
                        current_component,
                    )
                    return True
                if (
                    target_hierarchy
                    and not gate_observed
                    and (
                        hierarchy_gate_state is False
                        or (
                            hierarchy_gate_state is None
                            and audit_probe_succeeded
                            and not gate_visible
                        )
                    )
                    and not allow_stable_foreground_fallback
                ):
                    self._audit_gate_known_complete = True
                    logger.info(
                        "Audit gate is already past on %s; continuing from %s",
                        getattr(self, "device_serial", "unknown-device"),
                        current_component,
                    )
                    return True
                if not hierarchy:
                    empty_hierarchy_probes += 1
                stable_since = target_foreground_since
                required_stable_probes = self.AUDIT_GATE_ABSENT_STABLE_PROBES
                if launcher_observed:
                    required_stable_probes = max(2, required_stable_probes)
                historical_known_gate = bool(
                    initial_known_gate_complete and not gate_observed
                )
                hierarchy_gate_evidence = bool(
                    target_hierarchy
                    and hierarchy_gate_state is False
                )
                # A relaunch loses the in-memory "gate already complete" fact, and
                # apps whose gate has no audit locator never produce hierarchy
                # evidence either.  After a stable target foreground and a successful
                # audit probe, the absence of the locator is itself the evidence;
                # without this the recovery relaunch always timed out and the device
                # ended as "needs manual resume".
                gate_locator_absent = bool(
                    audit_probe_succeeded and hierarchy_gate_state is None
                )
                if (
                    allow_stable_foreground_fallback
                    and not gate_observed
                    and audit_probe_succeeded
                    and not gate_visible
                    and stable_since is not None
                    and target_foreground_probes
                    >= max(required_stable_probes, 6)
                    and now - stable_since
                    >= self.AUDIT_GATE_ABSENT_STABLE_SECONDS
                    and (
                        historical_known_gate
                        or hierarchy_gate_evidence
                        or gate_locator_absent
                    )
                ):
                    if historical_known_gate:
                        logger.warning(
                            "Audit gate was known complete before relaunch on %s; "
                            "continuing after the target foreground remained stable "
                            "at %s (%d probes, %d empty hierarchy probes, "
                            "%d control probe failures)",
                            getattr(self, "device_serial", "unknown-device"),
                            current_component,
                            target_foreground_probes,
                            empty_hierarchy_probes,
                            audit_probe_failures,
                        )
                    else:
                        logger.warning(
                            "Audit gate was not observed on %s; continuing after the "
                            "target foreground remained stable at %s "
                            "(%d probes, %d empty hierarchy probes, %d control probe failures)",
                            getattr(self, "device_serial", "unknown-device"),
                            current_component,
                            target_foreground_probes,
                            empty_hierarchy_probes,
                            audit_probe_failures,
                        )
                    return True
            else:
                target_foreground_since = None
                target_foreground_probes = 0
                target_foreground_component = ""
            time.sleep(0.2)
        if gate_observed:
            logger.warning(
                "Audit gate did not clear before timeout on %s "
                "(%d control probe failures; last probe error: %s)",
                getattr(self, "device_serial", "unknown-device"),
                audit_probe_failures,
                last_probe_error or "none",
            )
        else:
            logger.warning(
                "Audit gate state remained inconclusive before timeout on %s: "
                "target foreground was not stable (%d probes at %s; "
                "%d empty hierarchy probes; %d control probe failures; "
                "last probe error: %s)",
                getattr(self, "device_serial", "unknown-device"),
                target_foreground_probes,
                target_foreground_component or "unknown",
                empty_hierarchy_probes,
                audit_probe_failures,
                last_probe_error or "none",
            )
        return False

    def recover_audit_gate(
        self,
        audit_locator: Dict[str, Any],
        confirm_locator: Dict[str, Any],
        timeout: float = 30.0,
        audit_activity: str = "",
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Complete the configured startup audit gate after a clean relaunch."""
        return self._complete_audit_gate(
            audit_locator,
            confirm_locator,
            timeout,
            audit_activity=audit_activity,
            should_stop=should_stop,
            allow_stable_foreground_fallback=True,
        )

    def login(
        self,
        username: str,
        password: str,
        login_locators: Dict[str, Any],
    ) -> bool:
        """Fill configured login controls and submit the login form.

        A login flow may explicitly allow an empty secondary credential, such
        as an SMS verification-code field that the test build accepts blank.
        In that case the control is deliberately left untouched.
        """
        required = {"username", "login_button"}
        if not isinstance(login_locators, dict):
            logger.error("Login locators must be a JSON object")
            return False
        allow_empty_password = bool(login_locators.get("allow_empty_password", False))
        if not allow_empty_password:
            required.add("password")
        missing = required.difference(login_locators)
        if missing:
            logger.error("Login locators are missing: %s", sorted(missing))
            return False
        try:
            self._set_text(login_locators["username"], username)
            password_locator = login_locators.get("password")
            if password or not allow_empty_password:
                if not isinstance(password_locator, dict):
                    raise ValueError("Login password locator must be an object")
                self._set_text(password_locator, password)
            agreement_locator = login_locators.get("agreement")
            if agreement_locator:
                if not isinstance(agreement_locator, dict):
                    raise ValueError("Login agreement locator must be an object")
                agreement, _ = self._wait_for_selector(agreement_locator)
                self._assert_safe_control(agreement)
                agreement.click()
            button, _ = self._wait_for_selector(login_locators["login_button"])
            self._assert_safe_control(button)
            button.click()
            time.sleep(
                min(30.0, max(0.0, float(login_locators.get("wait_after_login", 2))))
            )
            return True
        except InputRecoveryRequired as exc:
            logger.warning("Login page requires input recovery on %s: %s", self.device_serial, exc)
            return False
        except Exception:
            logger.exception("Login scenario failed on %s", self.device_serial)
            return False

    def navigate_to(
        self, target_page: str, navigation_steps: List[Dict[str, Any]]
    ) -> bool:
        """Execute click, input, swipe, key, wait, and sleep navigation steps."""
        index = 0
        action = "unknown"
        self.last_navigation_error = None
        try:
            for index, step in enumerate(navigation_steps, start=1):
                self._dismiss_transient_system_dialogs()
                action = str(step.get("action", "click")).lower()
                if action not in {"launch_app", "start_app", "verify_foreground", "verify_foreground_stable", "wait_for_package"}:
                    self.ensure_input_safe()
                timeout = min(120.0, max(0.1, float(step.get("timeout", 10))))
                locator = step.get("locator") or {
                    key: step[key]
                    for key in (
                        "xpath",
                        "text",
                        "textContains",
                        "resourceId",
                        "description",
                    )
                    if key in step
                }

                if action in {"click", "tap"}:
                    self._click_locator_with_retry(locator, timeout)
                elif action in {"complete_audit_gate", "handle_audit_gate", "audit_gate"}:
                    audit_locator = (
                        step.get("audit_locator")
                        or step.get("locator")
                        or (
                            {"resourceId": str(step.get("audit_resource_id"))}
                            if step.get("audit_resource_id")
                            else {}
                        )
                    )
                    confirm_locator = (
                        step.get("confirm_locator")
                        or (
                            {"text": str(step.get("confirm_text"))}
                            if step.get("confirm_text")
                            else {"text": "确定"}
                        )
                    )
                    if not self._complete_audit_gate(
                        audit_locator,
                        confirm_locator,
                        timeout,
                        audit_activity=str(step.get("audit_activity", "")),
                    ):
                        raise RuntimeError("Audit gate was not completed")
                elif action in {"click_if_exists", "optional_click"}:
                    if not self._click_if_exists(locator, timeout=timeout):
                        logger.info(
                            "Optional element was not present on %s: %r",
                            self.device_serial,
                            locator,
                        )
                elif action in {"input", "set_text"}:
                    self._set_text(locator, str(step.get("text", "")))
                elif action == "swipe":
                    self._assert_safe_coordinates(
                        int(step["x1"]), int(step["y1"]),
                        (int(step["x2"]), int(step["y2"])),
                    )
                    self.d.swipe(
                        int(step["x1"]),
                        int(step["y1"]),
                        int(step["x2"]),
                        int(step["y2"]),
                        min(30.0, max(0.0, float(step.get("duration", 0.3)))),
                    )
                elif action in {"press", "key"}:
                    key = str(step["key"])
                    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", key):
                        raise ValueError("Invalid Android key name: %s" % key)
                    if getattr(self, "_input_safety", {}).get("enabled"):
                        self.ensure_input_safe()
                        if key.lower() not in {"back", "enter", "search", "delete", "del", "4", "66", "67", "84"}:
                            self._require_input_recovery("navigation key is not scoped to the target app")
                    self.d.press(key)
                elif action in {"launch_app", "start_app"}:
                    requested_package = str(
                        step.get("package", self.package_name)
                    ).strip()
                    if requested_package != self.package_name:
                        raise ValueError(
                            "Navigation may launch only the configured target package"
                        )
                    if self._uses_direct_adb_lifecycle():
                        started = self._start_target_app(
                            stop=bool(step.get("stop", False)),
                            wait=True,
                        )
                    else:
                        started = self.d.app_start(
                            self.package_name,
                            stop=bool(step.get("stop", False)),
                            wait=True,
                        )
                    if started is False:
                        raise RuntimeError("Target application did not start")
                elif action in {
                    "verify_foreground",
                    "verify_foreground_stable",
                    "wait_for_package",
                }:
                    deadline = time.monotonic() + timeout
                    current: Dict[str, Any] = {}
                    restore = bool(step.get("restore", False))
                    stable_seconds = (
                        min(30.0, max(0.0, float(step.get("stable_seconds", 2))))
                        if action == "verify_foreground_stable"
                        else 0.0
                    )
                    stable_since: Optional[float] = None
                    use_direct_lifecycle = self._uses_direct_adb_lifecycle()
                    last_restore_attempt = 0.0
                    known_app = dict(getattr(self, "_last_known_app", {}) or {})
                    if (
                        use_direct_lifecycle
                        and known_app.get("package") == self.package_name
                    ):
                        # Direct ``am start`` has acknowledged the launcher, or
                        # the audit control was clicked inside the target app.
                        # Do not issue repeated app_current/ps probes merely to
                        # re-prove that known state on a reconnecting ColorOS
                        # transport.
                        time.sleep(stable_seconds)
                        continue
                    while time.monotonic() < deadline:
                        try:
                            current = self._foreground_app()
                        except Exception:
                            if not use_direct_lifecycle:
                                raise
                            # A failed probe is not proof that the app left the
                            # foreground. In particular, do not turn it into an
                            # immediate uiautomator2.app_start call.
                            logger.debug(
                                "Foreground probe is reconnecting on %s",
                                self.device_serial,
                                exc_info=True,
                            )
                            stable_since = None
                            time.sleep(1.0)
                            continue
                        if current.get("package") == self.package_name:
                            if stable_since is None:
                                stable_since = time.monotonic()
                            if time.monotonic() - stable_since >= stable_seconds:
                                break
                        else:
                            stable_since = None
                        if restore:
                            if use_direct_lifecycle:
                                now = time.monotonic()
                                if now - last_restore_attempt >= 2.0:
                                    self._start_target_app(
                                        stop=False,
                                        wait=False,
                                    )
                                    last_restore_attempt = now
                            else:
                                self.d.app_start(
                                    self.package_name,
                                    stop=False,
                                    wait=True,
                                )
                        time.sleep(1.0 if use_direct_lifecycle else 0.25)
                    else:
                        raise TimeoutError(
                            "Target package did not reach the foreground: %r" % current
                        )
                elif action == "wait":
                    self._wait_for_selector(locator, timeout)
                elif action in {"wait_if_exists", "optional_wait"}:
                    selector, _ = self._selector(locator)
                    selector.wait(timeout=timeout)
                elif action == "sleep":
                    time.sleep(min(30.0, max(0.0, float(step.get("seconds", 1)))))
                else:
                    raise ValueError("Unsupported navigation action: %s" % action)
                time.sleep(min(30.0, max(0.0, float(step.get("after", 0.2)))))
                self.ensure_input_safe()

            self.ensure_input_safe()
            logger.info("Reached target page %s on %s", target_page, self.device_serial)
            # The audit page and privacy consent (if any) are consumed by this
            # first launch.  Later relaunches in the same cycle start on the home
            # page directly, so recovery must not expect them again.
            self._startup_gates_passed = True
            return True
        except InputRecoveryRequired as exc:
            self.last_navigation_error = str(exc)
            logger.warning("Navigation requires input recovery on %s: %s", self.device_serial, exc)
            return False
        except Exception as exc:
            detail = re.sub(r"\s+", " ", str(exc or "")).strip()
            self.last_navigation_error = (
                "step %s (%s): %s%s"
                % (
                    index,
                    action,
                    type(exc).__name__,
                    ": %s" % detail if detail else "",
                )
            )[:2000]
            logger.exception(
                "Navigation to %s failed on step %s", target_page, index
            )
            return False

    def save_snapshot(self, snapshot_name: str) -> Dict[str, Any]:
        """Capture activity, device information, and the current UI hierarchy."""
        # Vivo X80 uses the direct lifecycle for launches, but its snapshot
        # capture still goes through the normal uiautomator2 path.  The
        # lightweight shortcut below is specific to Oplus transports whose UI
        # session is intentionally unavailable during lifecycle handoff.
        if self._is_oplus_device():
            # A snapshot is diagnostic metadata, not an extra prerequisite for
            # an already-prepared app. Keep the known state if the transient U2
            # session is unavailable instead of failing preparation and causing
            # another lifecycle restart.
            current = dict(getattr(self, "_last_known_app", {}) or {})
            if not current:
                current = {"package": self.package_name}
            snapshot: Dict[str, Any] = {
                "name": snapshot_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "device_serial": self.device_serial,
                "package_name": self.package_name,
                "app_current": current,
                "device_info": {},
                "window_size": [],
                "hierarchy": "",
            }
            snapshots = getattr(self, "_snapshots", None)
            if not isinstance(snapshots, dict):
                snapshots = {}
                self._snapshots = snapshots
            snapshots[snapshot_name] = snapshot
            return snapshot
        try:
            info = dict(self.d.info)
            current = self._foreground_app()
            try:
                hierarchy = self.d.dump_hierarchy(compressed=False)
            except TypeError:
                # Older uiautomator2 releases do not expose the keyword.
                hierarchy = self.d.dump_hierarchy()
            snapshot: Dict[str, Any] = {
                "name": snapshot_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "device_serial": self.device_serial,
                "package_name": self.package_name,
                "app_current": current,
                "device_info": info,
                "window_size": list(self.d.window_size()),
                "hierarchy": hierarchy,
            }
            self._snapshots[snapshot_name] = snapshot
            return snapshot
        except Exception as exc:
            logger.exception("Unable to save snapshot %s", snapshot_name)
            return {"name": snapshot_name, "error": str(exc)}

    def restore_snapshot(self, snapshot: Dict[str, Any]) -> bool:
        """Restart the app and return to the activity recorded in a snapshot.

        Android UI XML is descriptive rather than restorable state. This method
        preserves application data, restarts the package, and opens the recorded
        activity. Business data restoration belongs in the scenario extension.
        """
        if snapshot.get("error"):
            return False
        if (
            snapshot.get("device_serial")
            and snapshot.get("device_serial") != self.device_serial
        ):
            logger.error("Snapshot belongs to a different device")
            return False
        if (
            snapshot.get("package_name")
            and snapshot.get("package_name") != self.package_name
        ):
            logger.error("Snapshot belongs to a different package")
            return False
        try:
            current = snapshot.get("app_current", {})
            activity = current.get("activity")
            if activity and not re.fullmatch(r"[A-Za-z0-9_.$/]+", str(activity)):
                logger.error("Snapshot contains an invalid activity name")
                return False
            if activity:
                activity_text = str(activity)
                if not (
                    activity_text.startswith(".")
                    or activity_text == self.package_name
                    or activity_text.startswith(self.package_name + ".")
                    or activity_text.startswith(self.package_name + "/")
                    or ("." not in activity_text and "/" not in activity_text)
                ):
                    logger.error("Snapshot activity is outside the target package")
                    return False
            if self._uses_direct_adb_lifecycle():
                if self._start_target_app(
                    stop=True,
                    wait=False,
                    clear_task=False,
                ) is False:
                    logger.error("Snapshot restore launch returned false")
                    return False
                return self._wait_for_target_foreground()
            self.d.app_stop(self.package_name)
            start_kwargs = {"stop": False, "wait": True}
            if activity:
                start_kwargs["activity"] = activity
            if self.d.app_start(self.package_name, **start_kwargs) is False:
                logger.error("Snapshot activity start returned false")
                return False
            time.sleep(1)
            restored = self._foreground_app().get("package") == self.package_name
            if not restored:
                logger.error("Snapshot restore opened the wrong package")
            return restored
        except Exception:
            logger.exception("Unable to restore snapshot %s", snapshot.get("name"))
            return False

    def get_device(self):
        """Return the connected uiautomator2 device object."""
        return self.d

    def refresh_device(self):
        """Replace a stale uiautomator2 session with a verified connection.

        A process crash can leave the HTTP/session object usable enough for a
        foreground probe while its next hierarchy request is rejected. Callers
        use this bounded refresh only after such a failed operation.
        """
        self.d = self._connect_with_retry()
        return self.d
