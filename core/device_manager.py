"""ADB device discovery and health checks."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional


logger = logging.getLogger(__name__)

# Keep every project-owned ADB client on the single primary server.  Allowing
# callers to inherit another port can split USB transports across daemons and
# make a connected device disappear from the web console.
os.environ["ANDROID_ADB_SERVER_PORT"] = "5037"


class DeviceManager:
    """Discover Android devices and query their runtime information."""

    adb_path = os.environ.get("ADB_PATH", "adb")
    retry_count = 3

    @classmethod
    def _run_adb(
        cls,
        arguments: List[str],
        serial: Optional[str] = None,
        timeout: int = 15,
        retries: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run an ADB command, retrying transient connection failures."""
        attempts = retries if retries is not None else cls.retry_count
        command = [cls.adb_path]
        if serial:
            command.extend(["-s", serial])
        command.extend(arguments)

        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
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
                if result.returncode == 0:
                    return result
                last_error = RuntimeError(
                    result.stderr.strip() or result.stdout.strip() or "ADB failed"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc

            if attempt < attempts:
                logger.warning(
                    "ADB command failed for %s (attempt %s/%s): %s",
                    serial or "host",
                    attempt,
                    attempts,
                    last_error,
                )
                time.sleep(attempt)

        raise RuntimeError(
            "ADB command failed after %s attempts: %s" % (attempts, last_error)
        )

    @classmethod
    def get_connected_devices(cls) -> List[str]:
        """Return serial numbers for all ADB-authorized online devices."""
        states = cls.get_device_states()
        devices = [
            serial
            for serial, details in states.items()
            if details.get("adb_state") == "device"
        ]
        for serial, details in states.items():
            state = str(details.get("adb_state") or "unknown")
            if state in {"offline", "unauthorized", "no_permissions"}:
                logger.warning("Ignoring %s device: %s", state, serial)
        return devices

    @classmethod
    def get_device_states(cls) -> Dict[str, Dict[str, object]]:
        """Return every device listed by ADB, including unusable states."""
        try:
            result = cls._run_adb(["devices", "-l"], retries=1)
        except RuntimeError as exc:
            logger.error("Unable to list Android devices: %s", exc)
            return {}

        devices: Dict[str, Dict[str, object]] = {}
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            if "no permissions" in line.lower():
                state = "no_permissions"
            properties: Dict[str, object] = {}
            for token in parts[2:]:
                key, separator, value = token.partition(":")
                if separator and key and value:
                    properties[key] = value
            devices[serial] = {
                "serial": serial,
                "adb_state": state,
                "listed": True,
                "connected": state == "device",
                "authorized": state == "device",
                **properties,
            }
        return devices

    @classmethod
    def is_device_ready(cls, serial: str) -> bool:
        """Check ADB authorization, input permission, boot, and lock state."""
        if not str(serial).strip():
            return False
        try:
            state = cls._run_adb(["get-state"], serial=serial).stdout.strip()
            if state != "device":
                return False

            booted = cls._run_adb(
                ["shell", "getprop", "sys.boot_completed"], serial=serial
            ).stdout.strip()
            if booted != "1":
                return False

            input_issue = cls.input_injection_block_reason(serial)
            if input_issue:
                logger.warning("Device %s is not ready: %s", serial, input_issue)
                return False

            # The exact keyguard fields differ across Android 5-14. Only reject
            # explicit locked states; absent fields do not imply a failure.
            policy = cls._run_adb(
                ["shell", "dumpsys", "window", "policy"],
                serial=serial,
                timeout=20,
            ).stdout
            locked_patterns = (
                r"isStatusBarKeyguard=true",
                r"mShowingLockscreen=true",
                r"showing=true\s+occluded=false",
            )
            return not any(re.search(pattern, policy) for pattern in locked_patterns)
        except RuntimeError as exc:
            logger.warning("Device %s is not ready: %s", serial, exc)
            return False

    @classmethod
    def input_injection_block_reason(cls, serial: str) -> Optional[str]:
        """Return a clear reason when vendor security blocks shell input."""
        value = cls._run_adb(
            ["shell", "getprop", "persist.security.adbinput"],
            serial=serial,
            timeout=5,
            retries=1,
        ).stdout.strip()
        if value == "0":
            return "USB 调试（安全设置）未开启，ADB/Fastbot 无法注入点击事件"
        return None

    @classmethod
    def get_device_info(cls, serial: str) -> Dict[str, object]:
        """Return model, Android version, SDK, and physical screen size."""

        def property_value(name: str) -> str:
            return cls._run_adb(
                ["shell", "getprop", name], serial=serial
            ).stdout.strip()

        size_output = cls._run_adb(
            ["shell", "wm", "size"], serial=serial
        ).stdout
        size_match = re.search(r"Physical size:\s*(\d+x\d+)", size_output)
        if size_match is None:
            size_match = re.search(r"(\d+x\d+)", size_output)

        return {
            "serial": serial,
            "manufacturer": property_value("ro.product.manufacturer"),
            "model": property_value("ro.product.model"),
            "android_version": property_value("ro.build.version.release"),
            "sdk": property_value("ro.build.version.sdk"),
            "screen_size": size_match.group(1) if size_match else "unknown",
        }
