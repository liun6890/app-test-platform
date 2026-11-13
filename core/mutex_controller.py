"""Exclusive control handoff between uiautomator2 and Fastbot."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Dict, Optional


logger = logging.getLogger(__name__)


class MutexController:
    """Track and enforce which automation tool may control a device."""

    VALID_TOOLS = {"uiautomator2", "fastbot"}
    REMOTE_STOP_GRACE_SECONDS = 0.5
    REMOTE_STOP_VERIFY_ATTEMPTS = 4
    REMOTE_PROBE_ATTEMPTS = 6
    REMOTE_PROBE_RETRY_SECONDS = 0.75
    UIAUTOMATION_RELEASE_ATTEMPTS = 20
    UIAUTOMATION_RELEASE_RETRY_SECONDS = 0.5
    UIAUTOMATOR_RESTORE_ATTEMPTS = 4
    UIAUTOMATOR_RESTORE_RETRY_SECONDS = (1.0, 2.0, 3.0)
    UIAUTOMATOR_HEALTH_ATTEMPTS = 3
    UIAUTOMATOR_HEALTH_RETRY_SECONDS = 0.5
    STOP_REQUEST_PREFIX = "/sdcard/.app_press_test_stop_"

    def __init__(self, device_serial: str):
        """Initialize an unowned controller for one device."""
        self.device_serial = device_serial
        self.current_holder: Optional[str] = None
        self._lock = threading.RLock()
        self._adb_path = os.environ.get("ADB_PATH", "adb")
        self._uiautomator_stopped = False

    def acquire(self, tool: str, d=None) -> bool:
        """Acquire device control after stopping the competing automation tool."""
        if tool not in self.VALID_TOOLS:
            raise ValueError("Unknown device-control tool: %s" % tool)

        with self._lock:
            if self.current_holder == tool:
                return True
            if self.current_holder is not None:
                logger.error(
                    "%s cannot acquire %s; control is held by %s",
                    tool,
                    self.device_serial,
                    self.current_holder,
                )
                return False

            if tool == "fastbot":
                # An interrupted host process can leave the device-local
                # app_process alive. Do not start a second input engine on a
                # device until that state is positively cleared.
                if not self._kill_fastbot_if_running():
                    logger.error(
                        "Refusing Fastbot control because a previous engine could "
                        "not be verified stopped on %s",
                        self.device_serial,
                    )
                    return False
                if d is not None:
                    try:
                        # Required invariant: no watcher may inject input while
                        # Fastbot owns the device.
                        stopped = d.watcher.stop()
                        if stopped is False:
                            logger.error(
                                "Refusing Fastbot control because watcher stop "
                                "returned false on %s",
                                self.device_serial,
                            )
                            return False
                        stop_uiautomator = getattr(d, "stop_uiautomator", None)
                        if not callable(stop_uiautomator):
                            logger.error(
                                "Refusing Fastbot control because uiautomator2 "
                                "cannot be stopped on %s",
                                self.device_serial,
                            )
                            return False
                        stop_uiautomator(wait=True)
                        self._uiautomator_stopped = True
                        # Closing uiautomator2's HTTP endpoint does not guarantee
                        # that Android has released the underlying UiAutomation
                        # registration.  Remove a leftover u2 process and verify
                        # the registration itself before starting Fastbot.
                        # Real uiautomator2 devices expose ``shell``.  Keep the
                        # lightweight compatibility path for test doubles and
                        # older adapters that only implement start/stop methods.
                        if callable(getattr(d, "shell", None)):
                            if not self._stop_remote_uiautomator_processes():
                                logger.error(
                                    "Refusing Fastbot control because the "
                                    "uiautomator2 process could not be cleared on %s",
                                    self.device_serial,
                                )
                                self._restore_uiautomator(d)
                                return False
                            if not self._wait_for_uiautomation_release():
                                logger.error(
                                    "Refusing Fastbot control because UiAutomation "
                                    "was not released on %s",
                                    self.device_serial,
                                )
                                self._restore_uiautomator(d)
                                return False
                        else:
                            time.sleep(self.REMOTE_STOP_GRACE_SECONDS)
                    except Exception as exc:
                        logger.error(
                            "Refusing Fastbot control because the watcher could not "
                            "be stopped on %s: %s",
                            self.device_serial,
                            exc,
                        )
                        if self._uiautomator_stopped:
                            self._restore_uiautomator(d)
                        return False
                else:
                    logger.error(
                        "Refusing Fastbot control without a uiautomator2 device; "
                        "watcher state cannot be verified on %s",
                        self.device_serial,
                    )
                    return False
            else:
                if not self._kill_fastbot_if_running():
                    logger.error(
                        "Refusing uiautomator2 control because Fastbot could not "
                        "be verified stopped on %s",
                        self.device_serial,
                    )
                    return False
                if d is not None and not self._ensure_uiautomator_ready(d):
                    logger.error(
                        "Refusing uiautomator2 control because its service is not "
                        "healthy on %s",
                        self.device_serial,
                    )
                    return False

            self.current_holder = tool
            logger.debug("%s acquired %s", tool, self.device_serial)
            return True

    def release(self, tool: str, d=None) -> bool:
        """Release device control when the caller is the current holder."""
        with self._lock:
            if self.current_holder != tool:
                logger.error(
                    "%s cannot release %s; current holder is %s",
                    tool,
                    self.device_serial,
                    self.current_holder,
                )
                return False
            self.current_holder = None
            if tool == "fastbot" and self._uiautomator_stopped:
                if not self._restore_uiautomator(d):
                    return False
            logger.debug("%s released %s", tool, self.device_serial)
            return True

    def _restore_uiautomator(self, d=None) -> bool:
        """Restore uiautomator2 after a completed or failed Fastbot handoff."""
        if not self._uiautomator_stopped:
            return True
        start_uiautomator = getattr(d, "start_uiautomator", None)
        if not callable(start_uiautomator):
            logger.error("uiautomator2 could not be restored on %s", self.device_serial)
            return False
        stop_uiautomator = getattr(d, "stop_uiautomator", None)
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.UIAUTOMATOR_RESTORE_ATTEMPTS + 1):
            try:
                start_uiautomator()
                if not self._wait_for_uiautomator_ready(d):
                    raise RuntimeError(
                        "uiautomator2 started but its hierarchy service is not ready"
                    )
                self._uiautomator_stopped = False
                return True
            except Exception as exc:
                last_error = exc
                # A failed start can leave another partial registration. Clean
                # it before the next bounded retry.
                if callable(stop_uiautomator):
                    try:
                        stop_uiautomator(wait=True)
                        # A failed start can leave Android's UiAutomation
                        # registration alive for a short window.  Wait for the
                        # registration to disappear before retrying so the
                        # next start does not race the previous session.
                        if callable(getattr(d, "shell", None)):
                            self._wait_for_uiautomation_release()
                    except Exception:
                        logger.warning(
                            "Unable to clean a failed uiautomator2 restore on %s "
                            "(attempt %s/%s)",
                            self.device_serial,
                            attempt,
                            self.UIAUTOMATOR_RESTORE_ATTEMPTS,
                            exc_info=True,
                        )
                if attempt >= self.UIAUTOMATOR_RESTORE_ATTEMPTS:
                    break
                delay = self.UIAUTOMATOR_RESTORE_RETRY_SECONDS[attempt - 1]
                logger.warning(
                    "Retrying uiautomator2 restore on %s after attempt %s/%s "
                    "failed: %s",
                    self.device_serial,
                    attempt,
                    self.UIAUTOMATOR_RESTORE_ATTEMPTS,
                    exc,
                )
                time.sleep(delay)
        logger.error(
            "Unable to restore uiautomator2 on %s after %s attempts: %s",
            self.device_serial,
            self.UIAUTOMATOR_RESTORE_ATTEMPTS,
            last_error,
        )
        return False

    def _wait_for_uiautomator_ready(self, d=None) -> bool:
        """Verify that uiautomator2 can serve a real UI hierarchy request.

        ``start_uiautomator`` returning only proves that its process answered a
        ping. Android may still reject the underlying UiAutomation registration
        after Fastbot exits. A bounded hierarchy read catches that half-started
        state before the following test cycle clears or launches the app.
        """
        dump_hierarchy = getattr(d, "dump_hierarchy", None)
        if not callable(dump_hierarchy):
            # Compatibility for minimal adapters and unit-test doubles. Real
            # uiautomator2 Device instances always expose dump_hierarchy.
            return True
        last_error: Optional[BaseException] = None
        for attempt in range(self.UIAUTOMATOR_HEALTH_ATTEMPTS):
            try:
                hierarchy = dump_hierarchy(compressed=True)
                if isinstance(hierarchy, str) and "<hierarchy" in hierarchy:
                    return True
                last_error = RuntimeError("empty or invalid UI hierarchy")
            except Exception as exc:
                last_error = exc
            if attempt < self.UIAUTOMATOR_HEALTH_ATTEMPTS - 1:
                time.sleep(self.UIAUTOMATOR_HEALTH_RETRY_SECONDS)
        logger.warning(
            "uiautomator2 health probe failed on %s after %s attempts: %s",
            self.device_serial,
            self.UIAUTOMATOR_HEALTH_ATTEMPTS,
            last_error,
        )
        return False

    def _ensure_uiautomator_ready(self, d=None) -> bool:
        """Return only after uiautomator2 is usable, with one bounded restart."""
        if self._wait_for_uiautomator_ready(d):
            return True
        stop_uiautomator = getattr(d, "stop_uiautomator", None)
        if not callable(stop_uiautomator):
            return False
        try:
            stop_uiautomator(wait=True)
        except Exception:
            logger.warning(
                "Unable to stop unhealthy uiautomator2 on %s",
                self.device_serial,
                exc_info=True,
            )
        self._uiautomator_stopped = True
        return self._restore_uiautomator(d)

    @staticmethod
    def _is_transient_transport_error(output: str) -> bool:
        text = str(output or "").lower()
        if any(
            marker in text
            for marker in (
                "device not found",
                "device offline",
                "no devices/emulators found",
                "closed",
                "transport error",
            )
        ):
            return True
        # ``adb`` includes the serial in its error, for example:
        # ``device 'TS5TFQ5PFU7PRWLF' not found``.  ColorOS can emit this
        # during a short USB re-enumeration, so treat the quoted form as the
        # same recoverable transport condition.
        return bool(re.search(r"\bdevice\b[^\r\n]*(?:not found|offline)\b", text))

    def _run_remote_probe(
        self, arguments: list[str], timeout: int
    ) -> Optional[subprocess.CompletedProcess]:
        """Run a read-only device probe, tolerating a short USB re-enumeration."""
        command = [self._adb_path, "-s", self.device_serial, *arguments]
        last_error = "unavailable"
        for attempt in range(self.REMOTE_PROBE_ATTEMPTS):
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = str(exc)
            else:
                output = (result.stderr or result.stdout).strip()
                if result.returncode == 0 or not self._is_transient_transport_error(output):
                    return result
                last_error = output or "ADB transport unavailable"
            if attempt < self.REMOTE_PROBE_ATTEMPTS - 1:
                time.sleep(self.REMOTE_PROBE_RETRY_SECONDS)
        logger.warning(
            "Remote Fastbot process probe failed on %s after reconnect retries: %s",
            self.device_serial,
            last_error,
        )
        return None

    def _remote_processes(self) -> Optional[Dict[str, str]]:
        """Return Fastbot-related device PIDs, or ``None`` when probing fails."""
        # ``pidof`` is both cheaper and less disruptive than a full process
        # listing.  Fastbot launched through ``app_process`` is not always
        # reported under the Java package name, so an empty ``pidof`` result
        # must still fall back to a command-line process listing.
        pidof = self._run_remote_probe(
            ["shell", "pidof", "com.android.commands.monkey"], timeout=5
        )
        if pidof is None:
            return None
        if pidof.returncode == 0 and not (pidof.stderr or "").strip():
            processes = {
                pid: "pidof com.android.commands.monkey"
                for pid in pidof.stdout.split()
                if pid.isdigit() and int(pid) > 0
            }
            if processes:
                return processes

        result = self._run_remote_probe(
            [
                "shell",
                "ps -A -o PID,ARGS 2>/dev/null || ps -A 2>/dev/null || ps",
            ],
            timeout=10,
        )
        if result is None or result.returncode != 0:
            logger.warning(
                "Remote Fastbot process fallback probe failed on %s: %s",
                self.device_serial,
                "" if result is None else (result.stderr or result.stdout).strip(),
            )
            return None

        processes: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            lowered = line.lower()
            if not any(
                marker in lowered
                for marker in (
                    "com.android.commands.monkey",
                    "monkeyq.jar",
                    "monkey.apk",
                    "fastbot-runtime.apk",
                    "fastbot-runtime.jar",
                    "fastbot-thirdpart.jar",
                    "fastbot",
                )
            ):
                continue
            columns = line.split()
            # Android toybox ps emits USER PID PPID ..., but vendor builds may
            # add columns. The first entirely numeric field is still the PID.
            pid = next((value for value in columns if value.isdigit()), None)
            if pid:
                processes[pid] = line
        return processes

    def _uiautomator_processes(self) -> Optional[Dict[str, str]]:
        """Return device-local uiautomator2 server PIDs, if they can be probed."""
        result = self._run_remote_probe(
            ["shell", "ps -A -o PID,ARGS 2>/dev/null || ps -A 2>/dev/null || ps"],
            timeout=10,
        )
        if result is None or result.returncode != 0:
            return None
        processes: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            lowered = line.lower()
            if "com.wetest.uia2.main" not in lowered and "u2.jar" not in lowered:
                continue
            columns = line.split()
            pid = next(
                (value for value in columns if value.isdigit() and int(value) > 0),
                None,
            )
            if pid:
                processes[pid] = line
        return processes

    def _stop_remote_uiautomator_processes(self) -> bool:
        """Kill a uiautomator2 process left behind after its ADB shell closes."""
        processes = self._uiautomator_processes()
        if processes is None:
            return False
        if not processes:
            return True
        self._signal_remote_processes(processes, 9)
        for _ in range(self.REMOTE_STOP_VERIFY_ATTEMPTS):
            remaining = self._uiautomator_processes()
            if remaining == {}:
                return True
            if remaining is None:
                return False
            time.sleep(self.REMOTE_STOP_GRACE_SECONDS)
        logger.error(
            "uiautomator2 is still running on %s after cleanup: %s",
            self.device_serial,
            ", ".join(sorted(processes)),
        )
        return False

    def _wait_for_uiautomation_release(self) -> bool:
        """Wait until Android no longer exposes a UiAutomation registration."""
        last_error: Optional[str] = None
        for attempt in range(self.UIAUTOMATION_RELEASE_ATTEMPTS):
            result = self._run_remote_probe(
                ["shell", "dumpsys", "accessibility"], timeout=10
            )
            if result is None or result.returncode != 0:
                last_error = (
                    "probe unavailable"
                    if result is None
                    else (result.stderr or result.stdout or "probe failed").strip()
                )
                logger.debug(
                    "UiAutomation release probe failed on %s (%s/%s): %s",
                    self.device_serial,
                    attempt + 1,
                    self.UIAUTOMATION_RELEASE_ATTEMPTS,
                    last_error,
                )
                if attempt < self.UIAUTOMATION_RELEASE_ATTEMPTS - 1:
                    time.sleep(self.UIAUTOMATION_RELEASE_RETRY_SECONDS)
                continue
            output = (result.stdout or "").lower()
            if not re.search(r"ui\s*automation\s*\[|uiautomationservice", output):
                return True
            if attempt < self.UIAUTOMATION_RELEASE_ATTEMPTS - 1:
                time.sleep(self.UIAUTOMATION_RELEASE_RETRY_SECONDS)
        if last_error:
            logger.warning(
                "UiAutomation release could not be confirmed on %s: %s",
                self.device_serial,
                last_error,
            )
        return False

    def _stop_request_path(self) -> str:
        """Return the device-local marker used to distinguish manual stops."""
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]", "_", self.device_serial)
        return self.STOP_REQUEST_PREFIX + (safe_serial or "unknown")

    def _set_stop_request(self) -> bool:
        """Persist a manual-stop intent before signalling the input engine."""
        try:
            result = subprocess.run(
                [
                    self._adb_path,
                    "-s",
                    self.device_serial,
                    "shell",
                    "touch",
                    self._stop_request_path(),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Unable to set Fastbot stop request on %s: %s", self.device_serial, exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "Unable to set Fastbot stop request on %s: %s",
                self.device_serial,
                (result.stderr or result.stdout).strip(),
            )
            return False
        return True

    def has_stop_request(self) -> bool:
        """Return whether a separate controller requested a manual stop."""
        try:
            result = subprocess.run(
                [
                    self._adb_path,
                    "-s",
                    self.device_serial,
                    "shell",
                    "test",
                    "-e",
                    self._stop_request_path(),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Unable to inspect Fastbot stop request on %s: %s", self.device_serial, exc)
            return False

    def clear_stop_request(self) -> bool:
        """Clear a stale or consumed manual-stop marker on the selected device."""
        try:
            result = subprocess.run(
                [
                    self._adb_path,
                    "-s",
                    self.device_serial,
                    "shell",
                    "rm",
                    "-f",
                    self._stop_request_path(),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Unable to clear Fastbot stop request on %s: %s", self.device_serial, exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "Unable to clear Fastbot stop request on %s: %s",
                self.device_serial,
                (result.stderr or result.stdout).strip(),
            )
            return False
        return True

    def _signal_remote_processes(self, processes: Dict[str, str], signal_number: int) -> None:
        """Send one signal to validated Fastbot PIDs and retain audit logs."""
        for pid in processes:
            if not pid.isdigit() or int(pid) <= 0:
                continue
            try:
                result = subprocess.run(
                    [
                        self._adb_path,
                        "-s",
                        self.device_serial,
                        "shell",
                        "kill",
                        "-%s" % signal_number,
                        pid,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "Unable to signal Fastbot PID %s on %s: %s",
                    pid,
                    self.device_serial,
                    exc,
                )
                continue
            if result.returncode != 0:
                logger.warning(
                    "Fastbot PID %s on %s rejected signal %s: %s",
                    pid,
                    self.device_serial,
                    signal_number,
                    (result.stderr or result.stdout).strip(),
                )

    def _wait_for_no_remote_fastbot(self) -> bool:
        """Verify, with a short bounded delay, that no engine remains on device."""
        for attempt in range(self.REMOTE_STOP_VERIFY_ATTEMPTS):
            processes = self._remote_processes()
            if processes is None:
                return False
            if not processes:
                return True
            if attempt < self.REMOTE_STOP_VERIFY_ATTEMPTS - 1:
                time.sleep(self.REMOTE_STOP_GRACE_SECONDS)
        logger.error(
            "Fastbot is still running on %s after stop verification: %s",
            self.device_serial,
            ", ".join(sorted(processes)),
        )
        return False

    def _kill_fastbot_if_running(self) -> bool:
        """Stop remote Fastbot and return true only after device-side verification."""
        try:
            processes = self._remote_processes()
            if processes is None:
                return False
            if not processes:
                return True
            self._signal_remote_processes(processes, 2)
            if processes:
                time.sleep(self.REMOTE_STOP_GRACE_SECONDS)
            if self._wait_for_no_remote_fastbot():
                return True
            remaining = self._remote_processes()
            if remaining is None:
                return False
            if remaining:
                self._signal_remote_processes(remaining, 9)
            return self._wait_for_no_remote_fastbot()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Unable to stop remote Fastbot process: %s", exc)
            return False

    def stop_remote_fastbot(self) -> bool:
        """Public, verification-backed emergency stop for a selected device."""
        return self._kill_fastbot_if_running()

    def request_stop_remote_fastbot(self) -> bool:
        """Request a manual stop and verify that its device-side engine is gone."""
        if not self._set_stop_request():
            return False
        return self._kill_fastbot_if_running()

    def get_current_holder(self) -> Optional[str]:
        """Return the tool currently holding device control."""
        with self._lock:
            return self.current_holder
