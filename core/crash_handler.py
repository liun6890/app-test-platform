"""Crash detection, signature generation, and UI trajectory replay."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .input_safety import InputRecoveryRequired, hide_input_method, input_safety_options, touch_path_is_owned

try:
    import uiautomator2 as u2
except ImportError:  # pragma: no cover - handled when replay is requested
    u2 = None  # type: ignore


logger = logging.getLogger(__name__)


class CrashHandler:
    """Analyze failures belonging to one authorized target application."""

    APP_CRASH_ACTIVITY = "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
    # Replay is diagnostic work, not another unbounded pressure run.  Keep
    # both the per-command and whole-trajectory limits explicit so a dead
    # UiAutomation/ADB transport cannot leave the HTTP request hanging.
    REPLAY_COMMAND_TIMEOUT_SECONDS = 8.0
    # Foreground verification adds one bounded dumpsys probe per event. A
    # previously valid 493-event crash phase was just over five minutes, so
    # keep enough headroom for those probes and a handful of task recoveries.
    REPLAY_TIMEOUT_SECONDS = 600.0
    REPLAY_MAX_TIMEOUT_SECONDS = 900.0
    REPLAY_UI_DUMP_PATH = "/sdcard/app_press_test_replay_ui.xml"
    REPLAY_GATE_TIMEOUT_SECONDS = 30.0
    REPLAY_GATE_STABLE_SECONDS = 0.8
    REPLAY_FOREGROUND_VERIFY_SECONDS = 3.0
    # Fastbot's ``delay_ms`` is only its throttle (a few hundred milliseconds),
    # but real gaps between recorded actions reach several seconds while a slow
    # swipe or a page reload completes. Replaying at throttle speed compresses
    # that timeline and hides state races. Honour the recorded gap, bounded.
    REPLAY_MAX_EVENT_GAP_SECONDS = 15.0
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
    PERMISSION_DIALOG_ACTIVITY_MARKERS = (
        "grantpermissionsactivity",
        "permissiondialog",
        "permissionrequestactivity",
        "servicepermissionactivity",
    )
    PRIVACY_DIALOG_COMPONENTS = frozenset(
        {
            "com.miui.securitycenter/com.miui.permcenter.permissions.systemapppermissiondialogactivity",
        }
    )
    PRIVACY_DIALOG_ACTIVITY_MARKERS = ("systemapppermissiondialogactivity",)

    _FRAME_PATTERN = re.compile(
        r"\bat\s+([\w.$]+)\.([\w$<>]+)\(([^)]*)\)"
    )
    _EXCEPTION_PATTERN = re.compile(
        r"^\s*(?:Caused by:\s*)?"
        r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:Exception|Error))"
        r"(?::\s*(.*))?$"
    )
    _LOGCAT_PAYLOAD_PATTERN = re.compile(
        r"^(?:\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+)?"
        r"[VDIWEF]/[^:]+:\s*(.*)$"
    )
    # Fastbot prints an Activity inventory during startup/shutdown.  A crash
    # Activity appearing in that inventory is not evidence that it was ever
    # shown.  Only lines carrying an explicit foreground/top/cmp marker may
    # be used as a pressure-log crash-page signal.
    _FOREGROUND_ACTIVITY_MARKER = re.compile(
        r"(?:\btop(?:\s+resumed)?\s+activity\b|\bcurrent\s*activity\b|"
        r"\bcurrentactivity\b|\b(?:mResumedActivity|mFocusedApp|mCurrentFocus)\b|"
        r"\bforeground(?:\s+component)?\b|\btop\s*=\s*ComponentInfo\b|"
        r"\bcmp\s*=)",
        re.IGNORECASE,
    )
    _COMPONENT_TOKEN = re.compile(
        r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)"
    )
    _ACTIVITY_TOKEN = re.compile(
        r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)+"
        r"[A-Za-z_$][A-Za-z0-9_$]*(?:Activity|Dialog)\b"
    )

    def __init__(
        self,
        device_serial: str,
        package_name: str,
        options: Optional[Mapping[str, Any]] = None,
    ):
        """Initialize the crash handler without taking device control."""
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            raise ValueError("Invalid Android package name: %s" % package_name)
        self.device_serial = device_serial
        self.package_name = package_name
        self.options = dict(options or {})
        self.adb_path = os.environ.get("ADB_PATH", "adb")
        self._device = None
        self.last_replay_result: Dict[str, Any] = {}
        self.last_replay_start: Dict[str, Any] = {}
        # New crash-tail plans preserve the existing task by default.  The
        # flag is scoped to ``replay_trajectory`` so a foreground-loss recovery
        # cannot silently destroy the page the operator asked us to replay.
        self._replay_allow_force_stop = True
        # Opt-in: replay a recorded touch even when the current hierarchy does
        # not prove target-package ownership. Off by default.
        self._replay_allow_unowned_touch = False
        # Opt-in: replay through a visible or unconfirmed input method. Needed
        # when the crash itself requires interacting with a text field.
        self._replay_allow_ime = False

    def _configured_crash_page_activities(self) -> List[str]:
        values = self.options.get("crash_page_activities", [])
        configured = [
            str(value).strip()
            for value in values
            if isinstance(value, str) and str(value).strip()
        ] if isinstance(values, list) else []
        if self.APP_CRASH_ACTIVITY not in configured:
            configured.append(self.APP_CRASH_ACTIVITY)
        return configured

    def matches_app_crash_page_component(
        self, component: Optional[str], package_name: Optional[str] = None
    ) -> bool:
        """Match the default Ahzy page plus configured target-app activities."""
        target_package = str(package_name or self.package_name)
        package, separator, activity = str(component or "").partition("/")
        if not separator or package != target_package:
            return False
        if activity.startswith("."):
            activity = package + activity
        for configured in self._configured_crash_page_activities():
            normalized = configured
            if normalized.startswith("."):
                normalized = target_package + normalized
            if activity == normalized or activity.endswith("." + normalized.rsplit(".", 1)[-1]):
                return True
        patterns = self.options.get("crash_page_activity_patterns", [])
        if isinstance(patterns, list):
            for pattern in patterns:
                try:
                    if re.search(str(pattern), activity):
                        return True
                except re.error:
                    continue
        return False

    def find_app_crash_page_component(self, content: str) -> Optional[str]:
        """Find a *foreground* crash Activity in pressure-engine text.

        Fastbot's activity enumeration contains every declared Activity,
        including ``AhzyCrashActivity``.  Parsing the whole blob therefore
        creates a false crash at the end of otherwise healthy runs.  Restrict
        matching to explicit foreground/top/cmp records and keep the latest
        matching record, since a page may have been shown and then replaced by
        the launcher while the engine is shutting down.
        """
        text = str(content or "")
        if not text:
            return None

        found: Optional[str] = None
        for line in text.splitlines():
            if not self._FOREGROUND_ACTIVITY_MARKER.search(line):
                continue
            candidates: List[str] = []
            for match in self._COMPONENT_TOKEN.finditer(line):
                package_name, activity = match.groups()
                candidates.append("%s/%s" % (package_name, activity))
            # Fastbot commonly emits the shared Ahzy Activity without the
            # target package (``top activity is com.ahzy...CrashActivity``).
            # It is safe to associate that token with the target only because
            # the line is an explicit foreground record, unlike the inventory
            # lines filtered above.
            candidates.extend(
                "%s/%s" % (self.package_name, activity)
                for activity in self._ACTIVITY_TOKEN.findall(line)
            )
            for component in dict.fromkeys(candidates):
                if self.matches_app_crash_page_component(component):
                    found = component
                    break
        return found

    def _belongs_to_target(self, block: str, crash_type: str) -> bool:
        # Native tombstones identify their owner with ``>>> package <<<``.
        # Check that marker before generic process metadata because vendor logs
        # may contain fields such as ``keyProcess: 0`` that are not an owner.
        if crash_type == "native":
            native_match = re.search(r">>>\s*([^\s]+)\s*<<<", block)
            if native_match:
                return native_match.group(1) == self.package_name
        process_match = re.search(
            r"(?<![A-Za-z0-9_])Process:\s*([^,\s]+)", block
        )
        if process_match:
            return process_match.group(1) == self.package_name
        if crash_type == "anr":
            return bool(
                re.search(
                    r"\bANR in\s+%s(?:\s|$)" % re.escape(self.package_name),
                    block,
                )
            )
        # A package name in a later log line or stack frame is not process
        # ownership. Java/native crashes without an explicit owner are ignored
        # so a system tool failure cannot stop the target-app pressure run.
        if crash_type == "native":
            first_line = block.splitlines()[0] if block else ""
            return bool(
                re.search(
                    r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])"
                    % re.escape(self.package_name),
                    first_line,
                )
            )
        return False

    @staticmethod
    def _extract_block(lines: List[str], start: int, max_lines: int = 160) -> str:
        selected = []
        for line in lines[start : start + max_lines]:
            if selected and (
                " FATAL EXCEPTION:" in line
                or " ANR in " in line
                or " Fatal signal " in line
            ):
                break
            selected.append(line)
        return "\n".join(selected)

    def detect_crash(self, logcat_content: str) -> Optional[Dict[str, Any]]:
        """Detect a target-app Java crash, ANR, or native fatal signal."""
        if not isinstance(logcat_content, str) or not logcat_content:
            return None
        lines = logcat_content.splitlines()
        markers: List[Tuple[str, str]] = [
            ("java", "FATAL EXCEPTION:"),
            ("anr", "ANR in %s" % self.package_name),
            ("native", "Fatal signal "),
        ]
        candidates: List[Dict[str, Any]] = []
        for index, line in enumerate(lines):
            for crash_type, marker in markers:
                if marker not in line:
                    continue
                block = self._extract_block(lines, index)
                if not self._belongs_to_target(block, crash_type):
                    continue
                parsed = self.parse_stacktrace(block)
                parsed.update(
                    {
                        "crash_type": crash_type,
                        "package_name": self.package_name,
                        "raw_log": block,
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                candidates.append(parsed)
                break

        if not candidates:
            return None
        # Return the last failure, which is normally nearest to the final app state.
        result = candidates[-1]
        result["signature"] = self.generate_signature(result)
        return result

    def detect_process_exit(self, logcat_content: str) -> Optional[Dict[str, Any]]:
        """Detect a target process death even when no crash stack/page exists."""
        if not isinstance(logcat_content, str) or not logcat_content:
            return None
        package_pattern = re.compile(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])"
            % re.escape(self.package_name)
        )
        markers = (
            ("am_crash", "ActivityManagerCrash", re.compile(r"\bam_crash\b", re.I)),
            ("am_proc_died", "ProcessDied", re.compile(r"\bam_proc_died\b", re.I)),
            ("am_kill", "ProcessKilled", re.compile(r"\bam_kill\b", re.I)),
            (
                "process_died",
                "ProcessDied",
                re.compile(r"\bProcess\b.*\bhas died\b", re.I),
            ),
            (
                "low_memory_kill",
                "LowMemoryKill",
                re.compile(r"\b(?:lmkd|lowmemorykiller|low memory kill)\b", re.I),
            ),
        )
        candidates: List[Dict[str, Any]] = []
        intentional_stop_pids = set()
        lines = logcat_content.splitlines()
        for index, line in enumerate(lines):
            if not package_pattern.search(line):
                continue
            pid = self._process_event_pid(line)
            if self._is_intentional_force_stop(line):
                if pid is not None:
                    intentional_stop_pids.add(pid)
                continue
            for event_type, exception_type, marker in markers:
                if not marker.search(line):
                    continue
                # ``am force-stop`` is commonly recorded as an ``am_kill``
                # followed by ``am_proc_died`` without repeating the reason.
                # Ignore both records when they refer to the same PID.
                if event_type == "am_proc_died" and pid in intentional_stop_pids:
                    continue
                context = "\n".join(lines[max(0, index - 2) : index + 3])
                crash = {
                    "crash_type": "process_exit",
                    "exception_type": exception_type,
                    "message": line.strip(),
                    "class": self.package_name,
                    "method": event_type,
                    "source": "ActivityManager",
                    "stack_frames": [],
                    "stacktrace": context,
                    "raw_log": context,
                    "package_name": self.package_name,
                    "pid": pid,
                    "process_event": event_type,
                    "detection_source": "activity_manager_log",
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
                crash["signature"] = self.generate_signature(crash)
                candidates.append(crash)
                break
        return candidates[-1] if candidates else None

    @staticmethod
    def _process_event_pid(line: str) -> Optional[int]:
        """Extract the process PID from Android event-log or textual records."""
        event_match = re.search(r"\[\s*\d+\s*,\s*(\d+)\s*,", str(line))
        if event_match:
            return int(event_match.group(1))
        text_match = re.search(r"\bpid\s*[=: ]\s*(\d+)\b", str(line), re.I)
        return int(text_match.group(1)) if text_match else None

    def _is_intentional_force_stop(self, line: str) -> bool:
        """Recognize framework-initiated force-stop records from vendor logcat."""
        text = re.sub(r"\s+", " ", str(line or "")).lower()
        if re.search(r"\bforce[- ]?stop(?:ping)?\b", text):
            return True
        package = re.escape(self.package_name.lower())
        # MIUI: ``stop <pkg> due to from process:123``
        # MagicOS: ``stop <pkg>from pid 123by app``
        return bool(
            re.search(
                r"\bstop\s+%s\s*(?:(?:due\s+to\s+)?from\s+"
                r"(?:process|pid)\b|by\s+(?:app|shell)\b)" % package,
                text,
            )
        )

    def build_process_exit(
        self, detection: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Normalize a PID-monitor transition into the crash schema."""
        event_type = str(detection.get("process_event") or "pid_missing")
        crash = {
            "crash_type": "process_exit",
            "exception_type": str(
                detection.get("exception_type") or "UnexpectedProcessExit"
            ),
            "message": str(
                detection.get("message")
                or "target application process disappeared during pressure testing"
            ),
            "class": self.package_name,
            "method": event_type,
            "source": str(detection.get("detection_source") or "pid_monitor"),
            "stack_frames": [],
            "stacktrace": str(detection.get("raw_log") or ""),
            "raw_log": str(detection.get("raw_log") or ""),
            "package_name": self.package_name,
            "pid": detection.get("pid"),
            "previous_pids": list(detection.get("previous_pids") or []),
            "current_pids": list(detection.get("current_pids") or []),
            "process_event": event_type,
            "detection_source": str(
                detection.get("detection_source") or "pid_monitor"
            ),
            "detected_at": str(
                detection.get("detected_at")
                or datetime.now(timezone.utc).isoformat()
            ),
        }
        crash["signature"] = self.generate_signature(crash)
        return crash

    @classmethod
    def is_app_crash_page_component(
        cls, component: Optional[str], package_name: str
    ) -> bool:
        """Return whether the target app is showing Ahzy's built-in crash page."""
        package, separator, activity = str(component or "").partition("/")
        if not separator or package != package_name:
            return False
        if activity.startswith("."):
            activity = package + activity
        return activity == cls.APP_CRASH_ACTIVITY or activity.endswith(
            ".AhzyCrashActivity"
        )

    @staticmethod
    def extract_app_crash_page_text(hierarchy: str) -> str:
        """Extract visible crash text from a uiautomator XML hierarchy."""
        if not isinstance(hierarchy, str) or not hierarchy.strip():
            return ""
        try:
            root = ET.fromstring(hierarchy)
        except ET.ParseError:
            return ""
        values: List[str] = []
        seen = set()
        for node in root.iter():
            for attribute in ("text", "content-desc"):
                value = str(node.attrib.get(attribute, "")).strip()
                if value and value not in seen:
                    seen.add(value)
                    values.append(value)
        return "\n".join(values)

    def detect_app_crash_page(
        self,
        component: Optional[str],
        page_text: str,
        detected_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build a crash record when the app exposes its own crash activity."""
        content = str(page_text or "").strip()
        component_matches = self.matches_app_crash_page_component(
            component, self.package_name
        )
        text_matches = False
        patterns = self.options.get("crash_page_text_patterns", [])
        if isinstance(patterns, list) and content:
            for pattern in patterns:
                try:
                    if re.search(str(pattern), content, re.I):
                        text_matches = True
                        break
                except re.error:
                    continue
        component_package = str(component or "").partition("/")[0]
        if not component_matches and not (
            text_matches and component_package == self.package_name
        ):
            return None
        parsed = self.parse_stacktrace(content)
        if parsed["exception_type"] == "UnknownCrash":
            parsed["exception_type"] = "AppCrashPage"
        parsed.update(
            {
                "crash_type": (
                    "java"
                    if parsed["exception_type"].endswith(("Exception", "Error"))
                    else "app_crash_page"
                ),
                "package_name": self.package_name,
                "raw_log": "",
                "detected_at": detected_at
                or datetime.now(timezone.utc).isoformat(),
                "detection_source": "app_crash_page",
                "crash_page_component": str(component),
            }
        )
        parsed["signature"] = self.generate_signature(parsed)
        return parsed

    def _frames_in(self, text: str) -> List[Dict[str, str]]:
        frames = []
        for frame_match in self._FRAME_PATTERN.finditer(text):
            frames.append(
                {
                    "class": frame_match.group(1),
                    "method": frame_match.group(2),
                    "source": frame_match.group(3),
                }
            )
        return frames

    def _exception_blocks(self, crash_log: str) -> List[Dict[str, Any]]:
        """Split a log or crash-page dump into one block per exception header.

        A crash page can show several stacks: the fatal app crash plus caught
        background-thread exceptions (vendor SDKs print their own). Treating the
        last header as "the" crash mislabels the report, so keep every block and
        let ``parse_stacktrace`` choose by ownership and FATAL context.
        """
        lines = str(crash_log or "").splitlines()
        headers: List[Tuple[int, Any]] = []
        for index, line in enumerate(lines):
            payload_match = self._LOGCAT_PAYLOAD_PATTERN.match(line.strip())
            payload = payload_match.group(1) if payload_match else line.strip()
            match = self._EXCEPTION_PATTERN.match(payload)
            if match:
                headers.append((index, match))
        blocks: List[Dict[str, Any]] = []
        for position, (index, match) in enumerate(headers):
            end = (
                headers[position + 1][0]
                if position + 1 < len(headers)
                else len(lines)
            )
            block_text = "\n".join(lines[index:end])
            context = "\n".join(lines[max(0, index - 3) : index])
            block_frames = self._frames_in(block_text)
            blocks.append(
                {
                    "text": block_text,
                    "exception_type": match.group(1),
                    "message": (match.group(2) or "").strip(),
                    "fatal": "FATAL EXCEPTION" in context
                    or "FATAL EXCEPTION" in block_text,
                    "app_frame": any(
                        frame["class"].startswith(self.package_name)
                        for frame in block_frames
                    ),
                    "app_owned": any(
                        frame["class"].startswith(self.package_name)
                        for frame in block_frames
                    )
                    or self.package_name in block_text,
                    "frames": block_frames,
                }
            )
        return blocks

    def parse_stacktrace(self, crash_log: str) -> Dict[str, Any]:
        """Extract exception type, message, location, and call frames.

        When several exceptions are present, prefer one that is both FATAL and
        owned by the target app; a caught background exception from a bundled
        SDK must not become the reported crash.
        """
        text = str(crash_log or "")
        blocks = self._exception_blocks(text)
        frames = self._frames_in(text)

        selected: Optional[Dict[str, Any]] = None
        if blocks:
            ordered = sorted(
                blocks,
                key=lambda block: (
                    1 if (block["fatal"] and block["app_frame"]) else 0,
                    1 if block["app_frame"] else 0,
                    1 if (block["fatal"] and block["app_owned"]) else 0,
                    1 if block["app_owned"] else 0,
                ),
            )
            selected = ordered[-1]

        block_frames = selected["frames"] if selected is not None else frames
        if selected is not None:
            exception_type = str(selected["exception_type"])
            message = str(selected["message"])
        elif "ANR in " in text:
            exception_type = "ApplicationNotResponding"
            reason_match = re.search(r"Reason:\s*(.*)", text)
            message = reason_match.group(1).strip() if reason_match else ""
        elif "Fatal signal " in text:
            signal_match = re.search(r"Fatal signal\s+(\d+)\s+\(([^)]+)\)", text)
            exception_type = (
                "NativeSignal%s" % signal_match.group(1)
                if signal_match
                else "NativeCrash"
            )
            message = signal_match.group(2) if signal_match else ""
        else:
            exception_type = "UnknownCrash"
            message = ""

        primary = next(
            (
                frame
                for frame in block_frames
                if frame["class"].startswith(self.package_name)
            ),
            block_frames[0] if block_frames else {},
        )
        supplementary = [
            {
                "exception_type": block["exception_type"],
                "message": block["message"],
                "fatal": bool(block["fatal"]),
                "app_owned": bool(block["app_owned"]),
            }
            for block in blocks
            if block is not selected
        ]
        return {
            "exception_type": exception_type,
            "message": message,
            "class": primary.get("class", "unknown"),
            "method": primary.get("method", "unknown"),
            "source": primary.get("source", "unknown"),
            "stack_frames": block_frames,
            "supplementary_exceptions": supplementary,
            "stacktrace": text,
        }

    def generate_signature(self, crash_info: Dict[str, Any]) -> str:
        """Generate a stable, readable signature for crash de-duplication."""
        identity = "|".join(
            str(crash_info.get(key, "unknown"))
            for key in ("crash_type", "exception_type", "class", "method")
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        exception_name = str(crash_info.get("exception_type", "UnknownCrash"))
        location = "%s.%s" % (
            crash_info.get("class", "unknown"),
            crash_info.get("method", "unknown"),
        )
        return "%s@%s#%s" % (exception_name, location, digest)

    def _connect(self):
        if self._device is not None:
            try:
                _ = self._device.info
                return self._device
            except Exception:
                self._device = None
        if u2 is None:
            raise RuntimeError(
                "uiautomator2 is not installed; run: pip install -r requirements.txt"
            )
        last_error: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                self._device = u2.connect(self.device_serial)
                _ = self._device.info
                return self._device
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(attempt)
        raise RuntimeError("Unable to connect for replay: %s" % last_error)

    def _adb_command(
        self, arguments: List[str], timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        """Run one bounded command against this device."""
        command = [self.adb_path, "-s", self.device_serial] + [str(item) for item in arguments]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=(
                self.REPLAY_COMMAND_TIMEOUT_SECONDS
                if timeout is None
                else max(0.1, float(timeout))
            ),
            check=False,
        )

    def _screen_size_from_adb(self) -> Tuple[int, int]:
        """Read the physical display size without opening a UiAutomation session."""
        result = self._adb_command(["shell", "wm", "size"])
        if result.returncode != 0:
            raise RuntimeError("unable to read device screen size: %s" % (result.stderr or result.stdout).strip())
        matches = re.findall(r"(\d+)x(\d+)", result.stdout or "")
        if not matches:
            raise RuntimeError("unable to parse device screen size")
        width, height = matches[-1]
        return max(1, int(width)), max(1, int(height))

    @staticmethod
    def _foreground_component_from_output(output: str) -> str:
        """Parse the resumed component from modern and vendor dumpsys output."""
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
                return "%s/%s" % (package_name, activity)
        return ""

    def _foreground_component_from_adb(self) -> str:
        """Return the current foreground component without opening UiAutomation."""
        result = self._adb_command(
            ["shell", "dumpsys", "activity", "activities"], timeout=5
        )
        if result.returncode == 0:
            component = self._foreground_component_from_output(result.stdout or "")
            if component:
                return component
        result = self._adb_command(
            ["shell", "dumpsys", "window", "windows"], timeout=5
        )
        if result.returncode != 0:
            return ""
        return self._foreground_component_from_output(result.stdout or "")

    def _foreground_package_from_adb(self) -> str:
        """Return the package in the resumed activity, or an empty string."""
        component = self._foreground_component_from_adb()
        return component.split("/", 1)[0] if component else ""

    @classmethod
    def _is_privacy_dialog_component(cls, component: str) -> bool:
        """Return whether a component is MIUI's privacy-consent page."""
        package_name, separator, activity = str(component or "").partition("/")
        if not separator:
            return False
        package_name = package_name.strip().lower()
        activity = activity.strip().lower()
        if activity.startswith("."):
            activity = package_name + activity
        normalized = "%s/%s" % (package_name, activity)
        if normalized in cls.PRIVACY_DIALOG_COMPONENTS:
            return True
        return package_name == "com.miui.securitycenter" and any(
            marker in activity for marker in cls.PRIVACY_DIALOG_ACTIVITY_MARKERS
        )

    @classmethod
    def _is_permission_dialog_component(cls, component: str) -> bool:
        """Return whether a system-owned component is a runtime permission prompt."""
        package_name, separator, activity = str(component or "").partition("/")
        if not separator:
            return False
        if cls._is_privacy_dialog_component(component):
            return False
        package_name = package_name.lower()
        activity = activity.lower()
        package_is_controller = package_name in cls.PERMISSION_CONTROLLER_PACKAGES or (
            package_name.endswith(".permissioncontroller")
            or package_name.endswith(".packageinstaller")
        )
        return package_is_controller and any(
            marker in activity for marker in cls.PERMISSION_DIALOG_ACTIVITY_MARKERS
        )

    def _is_replay_foreground_allowed(
        self, component: str, package_name: str
    ) -> bool:
        """Allow the target and its transient runtime-permission prompt."""
        current_package = str(component or "").split("/", 1)[0]
        return current_package == package_name or self._is_permission_dialog_component(
            component
        )

    def _wait_for_replay_foreground(self, package_name: str, timeout: float) -> bool:
        """Wait for a launch request to restore the target-owned foreground."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            component = self._foreground_component_from_adb()
            if self._is_replay_foreground_allowed(component, package_name):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25, remaining))

    def _launch_target_task(self, package_name: str) -> bool:
        """Bring the existing target task forward without clearing its state."""
        launch = self._adb_command(
            [
                "shell",
                "monkey",
                "-p",
                package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=15,
        )
        if launch.returncode != 0:
            return False
        return self._wait_for_replay_foreground(
            package_name, self.REPLAY_FOREGROUND_VERIFY_SECONDS
        )

    def _ensure_target_foreground(self, package_name: str) -> None:
        """Restore the target task, preserving state unless a hard reset is required."""
        component = self._foreground_component_from_adb()
        if self._is_replay_foreground_allowed(component, package_name):
            return

        # Launcher-style activation normally resumes the existing task and
        # preserves the screen reached by the preceding replay events.
        if self._launch_target_task(package_name):
            return

        if not self._replay_allow_force_stop:
            raise RuntimeError(
                "target package did not return to foreground; cold replay reset is "
                "disabled, open the saved page and use current-page replay"
            )

        # A wedged or removed task needs a clean launch. Keep this destructive
        # fallback behind the verified soft-recovery attempt.
        for attempt in range(2):
            stopped = self._adb_command(
                ["shell", "am", "force-stop", package_name], timeout=5
            )
            if stopped.returncode == 0 and self._launch_target_task(package_name):
                return
            if attempt == 0:
                time.sleep(0.5)
        raise RuntimeError(
            "target package did not reach foreground before replay: %s"
            % package_name
        )

    @staticmethod
    def _trajectory_replay_plan(trajectory: Mapping[str, Any]) -> Dict[str, Any]:
        value = trajectory.get("replay_plan", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _trajectory_anchor_component(
        self, trajectory: Mapping[str, Any], package_name: str
    ) -> str:
        plan = self._trajectory_replay_plan(trajectory)
        anchor = plan.get("page_anchor", {})
        if not isinstance(anchor, Mapping):
            return ""
        component = str(anchor.get("component") or "").strip()
        activity = str(anchor.get("activity") or "").strip()
        if not component and activity:
            if activity.startswith("."):
                activity = package_name + activity
            elif "." not in activity:
                activity = package_name + "." + activity
            component = "%s/%s" % (package_name, activity)
        match = re.fullmatch(
            r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/"
            r"(\.?[A-Za-z0-9_.$]+)",
            component,
        )
        if not match or match.group(1) != package_name:
            return ""
        activity = match.group(2)
        if activity.startswith("."):
            activity = package_name + activity
        return "%s/%s" % (package_name, activity)

    def _wait_for_exact_component(self, component: str, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self._foreground_component_from_adb() == component:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25, remaining))

    def _reorder_replay_anchor(self, component: str, timeout: float) -> bool:
        result = self._adb_command(
            [
                "shell",
                "am",
                "start",
                "--activity-reorder-to-front",
                "-n",
                component,
            ],
            timeout=max(3.0, float(timeout)),
        )
        if result.returncode != 0:
            return False
        return self._wait_for_exact_component(component, timeout)

    def prepare_replay_start(
        self,
        trajectory: Mapping[str, Any],
        package_name: str,
        start_mode: str = "auto",
    ) -> Dict[str, Any]:
        """Restore a replay start page without destroying a usable crash task.

        ``auto`` first walks back from the app crash page or reorders the saved
        Activity in the existing task. This lets Android restore its saved
        Fragment/Activity state. A cold Activity start is deliberately disabled
        by default because matching an Activity does not prove that a nested
        Fragment or editor state was restored. ``current`` is the assisted mode:
        an operator positions the device, then this method validates the target
        package and Activity before the short tail is injected.
        """

        if package_name != self.package_name:
            raise ValueError("Replay package does not match the configured target")
        mode = str(start_mode or "auto").strip().lower()
        if mode not in {"auto", "current"}:
            raise ValueError("Unsupported replay start mode: %s" % start_mode)

        plan = self._trajectory_replay_plan(trajectory)
        anchor_component = self._trajectory_anchor_component(
            trajectory, package_name
        )
        try:
            timeout = min(
                30.0,
                max(1.0, float(plan.get("activity_restore_timeout_seconds", 8) or 8)),
            )
        except (TypeError, ValueError, OverflowError):
            timeout = 8.0

        def option_bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
            return default

        require_anchor = option_bool(plan.get("require_page_anchor", True), True)
        allow_cold = option_bool(plan.get("allow_cold_activity_start", False), False)
        initial_component = self._foreground_component_from_adb()
        result: Dict[str, Any] = {
            "success": False,
            "mode": mode,
            "strategy": "",
            "anchor_component": anchor_component,
            "initial_component": initial_component,
            "final_component": initial_component,
            "task_preserved": True,
            "activity_matched": False,
        }

        if mode == "current":
            if not self._is_replay_foreground_allowed(
                initial_component, package_name
            ):
                result["error"] = (
                    "target app is not in foreground; open the saved replay page "
                    "on the device before using current-page replay"
                )
            elif anchor_component and initial_component != anchor_component:
                result["error"] = (
                    "current Activity does not match replay anchor: expected %s, got %s"
                    % (anchor_component, initial_component or "unknown")
                )
            else:
                result.update(
                    {
                        "success": True,
                        "strategy": "operator_positioned_current_page",
                        "activity_matched": bool(anchor_component),
                        "page_state_verified": False,
                        "reason": (
                            "target Activity matched; nested page state is operator-confirmed"
                            if anchor_component
                            else "target app is foreground; no Activity anchor was available"
                        ),
                    }
                )
            self.last_replay_start = dict(result)
            return result

        if anchor_component:
            if (
                initial_component == anchor_component
                and not self.matches_app_crash_page_component(initial_component)
            ):
                result.update(
                    {
                        "success": True,
                        "strategy": "existing_task_anchor",
                        "activity_matched": True,
                        "page_state_verified": False,
                        "reason": "existing task is already on the replay anchor Activity",
                    }
                )
                self.last_replay_start = dict(result)
                return result

            if self.matches_app_crash_page_component(initial_component):
                if input_safety_options(self.options).get("enabled"):
                    self._execute_event(self._connect(), {"action": "back", "delay_ms": 0})
                    back_ok = True
                else:
                    back_ok = self._adb_command(
                        ["shell", "input", "keyevent", "BACK"], timeout=5
                    ).returncode == 0
                if back_ok and self._wait_for_exact_component(
                    anchor_component, timeout
                ):
                    result.update(
                        {
                            "success": True,
                            "strategy": "back_from_crash_page",
                            "final_component": anchor_component,
                            "activity_matched": True,
                            "page_state_verified": False,
                            "reason": "restored the Activity underneath the crash page",
                        }
                    )
                    self.last_replay_start = dict(result)
                    return result

            if self._reorder_replay_anchor(anchor_component, timeout):
                result.update(
                    {
                        "success": True,
                        "strategy": "reordered_existing_task",
                        "final_component": anchor_component,
                        "activity_matched": True,
                        "page_state_verified": False,
                        "reason": "reordered the saved Activity in the existing task",
                    }
                )
                self.last_replay_start = dict(result)
                return result

        if not anchor_component and require_anchor:
            result["error"] = (
                "no page anchor was captured; use current-page replay after opening "
                "the target page manually"
            )
            self.last_replay_start = dict(result)
            return result

        allow_relaunch = option_bool(
            plan.get("relaunch_when_anchor_missing", True), True
        )
        if anchor_component and not allow_cold and allow_relaunch:
            # The recorded anchor can be a transient page (an ad SDK Activity, a
            # stub, a dismissed dialog owner) that no longer exists in the saved
            # task even though the task itself survived the crash.  A
            # launcher-style activation resumes that task without clearing data,
            # so try it before refusing the replay.  The exact Activity cannot
            # be proven, which is reported honestly instead of being treated as
            # a verified page.
            if self._launch_target_task(package_name):
                # A launcher launch usually lands on the splash page first. Give
                # the recorded anchor a bounded chance to appear, otherwise the
                # crash tail is injected onto whatever page happens to be up.
                anchor_ready = self._wait_for_exact_component(
                    anchor_component, timeout
                )
                result.update(
                    {
                        "success": True,
                        "strategy": (
                            "relaunch_wait_anchor"
                            if anchor_ready
                            else "relaunch_existing_task"
                        ),
                        "final_component": self._foreground_component_from_adb(),
                        "activity_matched": bool(anchor_ready),
                        "page_state_verified": False,
                        "reason": (
                            "relaunched the existing task and reached the replay "
                            "anchor Activity"
                            if anchor_ready
                            else "the anchor Activity was not restorable; "
                            "relaunched the existing task without clearing "
                            "application data"
                        ),
                    }
                )
                self.last_replay_start = dict(result)
                return result

        if not allow_cold:
            result["error"] = (
                "the saved task/page is no longer restorable automatically; open "
                "%s manually and use current-page replay"
                % (anchor_component or "the crash page")
            )
            self.last_replay_start = dict(result)
            return result

        result["task_preserved"] = False
        stopped = self._adb_command(
            ["shell", "am", "force-stop", package_name], timeout=5
        )
        if stopped.returncode != 0 or not self._launch_target_task(package_name):
            result["error"] = "unable to cold-launch target package for replay"
            self.last_replay_start = dict(result)
            return result
        if anchor_component and not self._reorder_replay_anchor(
            anchor_component, timeout
        ):
            result["error"] = "cold Activity anchor could not be started: %s" % anchor_component
            self.last_replay_start = dict(result)
            return result
        final_component = self._foreground_component_from_adb()
        result.update(
            {
                "success": True,
                "strategy": "cold_activity_start" if anchor_component else "cold_launcher_start",
                "final_component": final_component,
                "activity_matched": bool(
                    anchor_component and final_component == anchor_component
                ),
                "page_state_verified": False,
                "reason": "cold-start fallback completed",
            }
        )
        self.last_replay_start = dict(result)
        return result

    @staticmethod
    def _is_foreground_injection_error(error: BaseException) -> bool:
        """Identify the Android error raised when the target lost focus.

        Recent Android releases reject shell input aimed at a different
        foreground window with a ``SecurityException``.  The exact wording
        differs between vendors, so keep the check deliberately small and
        case-insensitive.  Other ADB failures must still fail the replay
        instead of blindly repeating an event that may have been delivered.
        """
        detail = str(error or "").lower()
        return (
            "inject_events" in detail
            or "injecting to another application" in detail
            or ("securityexception" in detail and "input" in detail)
        )

    @staticmethod
    def _replay_node_center(node: ET.Element) -> Optional[Tuple[int, int]]:
        match = re.fullmatch(
            r"\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]",
            str(node.attrib.get("bounds", "")),
        )
        if not match:
            return None
        left, top, right, bottom = (int(value) for value in match.groups())
        if right <= left or bottom <= top:
            return None
        return ((left + right) // 2, (top + bottom) // 2)

    def _replay_ui_nodes(self) -> Tuple[List[ET.Element], bool]:
        """Use the replay's UI owner in strict mode, otherwise the legacy ADB dump."""
        if input_safety_options(self.options).get("enabled"):
            try:
                root = ET.fromstring(self._connect().dump_hierarchy(compressed=False))
                return list(root.iter("node")), True
            except Exception:
                return [], False
        dump = self._adb_command(
            ["shell", "uiautomator", "dump", "--compressed", self.REPLAY_UI_DUMP_PATH],
            timeout=12,
        )
        if dump.returncode != 0:
            return [], False
        content = self._adb_command(
            ["shell", "cat", self.REPLAY_UI_DUMP_PATH], timeout=8
        )
        if content.returncode != 0 or not (content.stdout or "").strip():
            return [], False
        try:
            root = ET.fromstring(content.stdout)
        except ET.ParseError:
            return [], False
        return list(root.iter("node")), True

    @staticmethod
    def _replay_find_node(
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

    def _replay_gate_options(self, config: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        """Normalize the saved startup-gate configuration for manual replay."""
        scenario = config.get("scenario", {}) if isinstance(config, Mapping) else {}
        if not isinstance(scenario, Mapping):
            return None
        audit = scenario.get("audit_recovery", {})
        if not isinstance(audit, Mapping):
            audit = {}
        navigation = scenario.get("navigation_steps", [])
        if not isinstance(navigation, list):
            navigation = []
        step: Mapping[str, Any] = {}
        for candidate in navigation:
            if not isinstance(candidate, Mapping):
                continue
            action = str(candidate.get("action") or "").strip().lower()
            if action in {"complete_audit_gate", "handle_audit_gate", "audit_gate"}:
                step = candidate
                break
        enabled = bool(audit.get("enabled", False)) or bool(step)
        if not enabled:
            return None
        audit_locator = audit.get("audit_locator")
        if not isinstance(audit_locator, Mapping):
            audit_locator = step.get("audit_locator")
        if not isinstance(audit_locator, Mapping):
            audit_locator = {"resourceId": "%s:id/auditRadioButton" % self.package_name}
        confirm_locator = audit.get("confirm_locator")
        if not isinstance(confirm_locator, Mapping):
            confirm_locator = step.get("confirm_locator")
        if not isinstance(confirm_locator, Mapping):
            confirm_locator = {"text": str(audit.get("confirm_text") or "确定")}
        resource_id = str(
            audit_locator.get("resourceId") or audit_locator.get("resource-id") or ""
        ).strip()
        confirm_text = str(
            confirm_locator.get("text") or audit.get("confirm_text") or "确定"
        ).strip()
        privacy_resource_id = str(audit.get("privacy_resource_id") or "").strip()
        privacy_texts = audit.get("privacy_accept_texts")
        if not isinstance(privacy_texts, (list, tuple)):
            privacy_texts = ["同意并继续", "同意并使用", "同意并进入", "我同意", "同意"]
        return {
            "audit_resource_id": resource_id,
            "confirm_text": confirm_text,
            "privacy_resource_id": privacy_resource_id,
            "privacy_texts": tuple(dict.fromkeys(str(item).strip() for item in privacy_texts if str(item).strip())),
        }

    def _replay_find_privacy_node(
        self, nodes: List[ET.Element], options: Mapping[str, Any]
    ) -> Optional[ET.Element]:
        context = any(
            marker in (
                str(node.attrib.get("text", ""))
                + " "
                + str(node.attrib.get("content-desc", ""))
            )
            for node in nodes
            for marker in ("隐私政策", "用户协议", "个人信息")
        )
        resource_id = str(options.get("privacy_resource_id") or "").strip()
        if resource_id:
            node = self._replay_find_node(nodes, resource_id=resource_id)
            if node is not None and context:
                return node
        for text in options.get("privacy_texts", ()):
            node = self._replay_find_node(nodes, text=str(text))
            if node is not None and (context or str(text) in {"同意并继续", "同意并使用", "同意并进入"}):
                return node
        return None

    def _replay_tap_node(self, node: ET.Element, label: str) -> None:
        center = self._replay_node_center(node)
        if center is None:
            raise RuntimeError("回放准备控件缺少有效坐标：%s" % label)
        if input_safety_options(self.options).get("enabled"):
            if node.get("package") != self.package_name:
                raise InputRecoveryRequired("replay control belongs to an external window")
            self._execute_event(self._connect(), {
                "action": "click", "x": center[0], "y": center[1], "delay_ms": 0,
            })
            return
        result = self._adb_command(
            ["shell", "input", "tap", str(center[0]), str(center[1])], timeout=8
        )
        if result.returncode != 0:
            raise RuntimeError(
                "回放准备点击失败（%s）：%s"
                % (label, (result.stderr or result.stdout or "ADB failed").strip())
            )

    def prepare_replay_state(
        self, package_name: str, config: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Complete the same startup gates used by pressure runs before replay."""
        if package_name != self.package_name:
            raise ValueError("Replay package does not match the configured target")
        result: Dict[str, Any] = {
            "success": False,
            "auditSelected": False,
            "confirmed": False,
            "privacyAccepted": False,
            "probes": 0,
        }
        options = self._replay_gate_options(config)
        try:
            self._ensure_target_foreground(package_name)
            if options is None:
                result.update({"success": True, "reason": "no startup gate configured"})
                return result
            deadline = time.monotonic() + self.REPLAY_GATE_TIMEOUT_SECONDS
            audit_selected = False
            confirm_pending = False
            clear_since: Optional[float] = None
            saw_gate = False
            while time.monotonic() < deadline:
                nodes, dumped = self._replay_ui_nodes()
                result["probes"] = int(result.get("probes", 0)) + 1
                if not dumped:
                    clear_since = None
                    time.sleep(0.35)
                    continue
                audit_node = self._replay_find_node(
                    nodes, resource_id=str(options.get("audit_resource_id") or "")
                )
                if audit_node is None:
                    audit_node = self._replay_find_node(nodes, text="审核中")
                confirm_node = self._replay_find_node(
                    nodes, text=str(options.get("confirm_text") or "确定")
                )
                privacy_node = self._replay_find_privacy_node(nodes, options)
                if privacy_node is not None:
                    # Some builds leave the replay state waiting for a
                    # confirmation control while already showing the next
                    # privacy page.  The explicit privacy-page evidence wins
                    # over that stale state so replay preparation can advance.
                    saw_gate = True
                    clear_since = None
                    confirm_pending = False
                    self._replay_tap_node(privacy_node, "隐私政策同意")
                    result["privacyAccepted"] = True
                    time.sleep(0.45)
                    continue
                if confirm_pending or (audit_node is not None and audit_node.attrib.get("checked") == "true"):
                    saw_gate = True
                    clear_since = None
                    if confirm_node is not None:
                        self._replay_tap_node(confirm_node, "确定")
                        confirm_pending = False
                        result["confirmed"] = True
                        time.sleep(0.45)
                    else:
                        confirm_pending = True
                        time.sleep(0.25)
                    continue
                if audit_node is not None and not audit_selected:
                    saw_gate = True
                    clear_since = None
                    self._replay_tap_node(audit_node, "审核中")
                    audit_selected = True
                    result["auditSelected"] = True
                    confirm_pending = True
                    time.sleep(0.35)
                    continue
                if clear_since is None:
                    clear_since = time.monotonic()
                if time.monotonic() - clear_since >= self.REPLAY_GATE_STABLE_SECONDS:
                    result["success"] = True
                    result["reason"] = "startup gates cleared" if saw_gate else "no startup gate visible"
                    return result
                time.sleep(0.2)
            result["error"] = "启动审核/隐私门禁在 %.0f 秒内未完成" % self.REPLAY_GATE_TIMEOUT_SECONDS
            return result
        except Exception as exc:
            result["error"] = str(exc)
            return result

    @staticmethod
    def _adb_text_value(value: str) -> str:
        """Encode text for Android's ``input text`` parser."""
        # ``input text`` uses %s for spaces and treats these characters as
        # shell syntax on older Android releases.  The command is still passed
        # as an argument list on the host; this escaping is for the device
        # shell parser itself.
        return (
            str(value)
            .replace("%", "%25")
            .replace(" ", "%s")
            .replace("&", "\\&")
            .replace("|", "\\|")
            .replace(";", "\\;")
            .replace("<", "\\<")
            .replace(">", "\\>")
            .replace("(", "\\(")
            .replace(")", "\\)")
            .replace("'", "\\'")
            .replace('"', '\\"')
        )

    def _execute_event_adb(
        self, event: Dict[str, Any], width: int, height: int
    ) -> None:
        """Execute a structured event through bounded device-local input."""
        if input_safety_options(self.options).get("enabled"):
            self._execute_event(self._connect(), event)
            return
        action = str(event.get("action") or event.get("type") or "").lower()
        command: Optional[List[str]] = None
        if action in {"click", "tap", "touch"}:
            command = [
                "shell",
                "input",
                "tap",
                str(self._coordinate(event, "x", width)),
                str(self._coordinate(event, "y", height)),
            ]
        elif action in {"swipe", "drag"}:
            duration_ms = int(
                min(30000.0, max(1.0, float(event.get("duration", 0.3)) * 1000.0))
            )
            command = [
                "shell",
                "input",
                "swipe",
                str(self._coordinate(event, "x1", width)),
                str(self._coordinate(event, "y1", height)),
                str(self._coordinate(event, "x2", width)),
                str(self._coordinate(event, "y2", height)),
                str(duration_ms),
            ]
        elif action in {"long_click", "longclick", "longpress", "long_press"}:
            duration_ms = int(
                min(30000.0, max(100.0, float(event.get("duration", 1.0)) * 1000.0))
            )
            x = str(self._coordinate(event, "x", width))
            y = str(self._coordinate(event, "y", height))
            command = ["shell", "input", "swipe", x, y, x, y, str(duration_ms)]
        elif action in {"press", "key", "back", "home", "menu", "app_switch", "recents"}:
            key = str(event.get("key") or event.get("keycode") or "")
            if not key and action in {"back", "home", "menu", "app_switch", "recents"}:
                key = {"app_switch": "APP_SWITCH", "recents": "APP_SWITCH"}.get(
                    action, action.upper()
                )
            key = key.upper()
            if not re.fullmatch(r"[A-Z0-9_]{1,32}", key):
                raise ValueError("Invalid replay key: %s" % key)
            command = ["shell", "input", "keyevent", key]
        elif action in {"input", "text", "set_text"}:
            text_value = str(event.get("text", ""))
            if len(text_value) > 4096:
                raise ValueError("Replay text exceeds 4096 characters")
            if text_value and not text_value.isascii():
                raise ValueError(
                    "Replay text contains non-ASCII characters unsupported by adb input"
                )
            command = ["shell", "input", "text", self._adb_text_value(text_value)]
        elif action in {"sleep", "wait"}:
            time.sleep(min(30.0, max(0.0, float(event.get("seconds", event.get("duration", 1))))))
        else:
            raise ValueError("Unsupported replay event: %s" % action)
        if command is not None:
            # ``input swipe`` blocks for the requested gesture duration.  The
            # old fixed eight second transport timeout made valid Fastbot
            # gestures (which can be recorded as 30 seconds) fail before they
            # reached the device, so size the host timeout to the command.
            command_timeout = None
            if action in {"swipe", "drag", "long_click", "longclick", "longpress", "long_press"}:
                try:
                    command_timeout = min(
                        self.REPLAY_MAX_TIMEOUT_SECONDS,
                        max(
                            self.REPLAY_COMMAND_TIMEOUT_SECONDS,
                            float(duration_ms) / 1000.0 + 5.0,
                        ),
                    )
                except (NameError, TypeError, ValueError):
                    command_timeout = self.REPLAY_COMMAND_TIMEOUT_SECONDS
            result = (
                self._adb_command(command)
                if command_timeout is None
                else self._adb_command(command, timeout=command_timeout)
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "command failed").strip()
                raise RuntimeError("ADB replay command failed: %s" % detail)
        delay_ms = min(10000, max(0, int(event.get("delay_ms", 200))))
        if delay_ms:
            time.sleep(delay_ms / 1000.0)

    @staticmethod
    def _coordinate(
        event: Dict[str, Any], name: str, dimension: int
    ) -> int:
        ratio_name = "%s_ratio" % name
        if ratio_name in event:
            ratio = min(1.0, max(0.0, float(event[ratio_name])))
            return min(max(0, dimension - 1), int(ratio * dimension))
        return min(max(0, dimension - 1), max(0, int(event[name])))

    def _verify_replay_input(self, device, points: List[Tuple[int, int]]) -> None:
        options = input_safety_options(self.options)
        if not options.get("enabled"):
            return
        if self._replay_allow_ime:
            # The caller explicitly accepted a visible input method (a crash may
            # require touching a text field). Skip the probing round-trip: it
            # costs up to ``hide_timeout_seconds`` per event and its result is
            # ignored anyway.
            pass
        elif not hide_input_method(
            self._adb_command, options.get("hide_timeout_seconds", 3.0),
            can_dismiss=lambda: device.app_current().get("package") == self.package_name,
        ):
            raise InputRecoveryRequired(
                "replay input method is visible or unknown"
            )
        if device.app_current().get("package") != self.package_name:
            raise InputRecoveryRequired("replay foreground is outside the target app")
        if points:
            try:
                width, height = device.window_size()
                nodes = list(ET.fromstring(device.dump_hierarchy(compressed=False)).iter("node"))
                # Ownership is the precise check. Fastbot's guarded source
                # already delivered these touches inside the target window, and
                # a fixed 3%/5% margin rejected legitimate edge taps (a row by
                # the bezel, the bottom bar, a full-height list) before a single
                # replay event could run. Require the recorded point to be on
                # screen and covered by a target-owned node with no foreign
                # window intersecting it, and drop the margin entirely.
                safe = touch_path_is_owned(nodes, self.package_name, points) and all(
                    0 <= x < width and 0 <= y < height for x, y in points
                )
            except Exception:
                safe = False
            if not safe and self._replay_allow_unowned_touch:
                # Opt-in debugging escape hatch: the recorded point was produced
                # by Fastbot's own guarded source inside this window, the target
                # app is confirmed foreground above, and the point is on screen.
                # A click that only exists on an unowned node is therefore worth
                # attempting once, with an explicit warning, so a single odd
                # coordinate cannot hide the trigger event.
                on_screen = all(0 <= x < width and 0 <= y < height for x, y in points)
                if on_screen:
                    logger.warning(
                        "Replaying an unowned touch on %s at %s (allow_unowned_touch)",
                        self.device_serial,
                        points,
                    )
                    return
            if not safe:
                raise InputRecoveryRequired(
                    "replay touch is outside the app or has unknown ownership"
                )

    def _execute_event(self, device, event: Dict[str, Any]) -> None:
        action = str(event.get("action") or event.get("type") or "").lower()
        width, height = device.window_size()
        strict = input_safety_options(self.options).get("enabled")
        points = []
        if action in {"click", "tap", "touch", "long_click", "longclick", "longpress", "long_press"}:
            points = [(self._coordinate(event, "x", width), self._coordinate(event, "y", height))]
        elif action in {"swipe", "drag"}:
            points = [
                (self._coordinate(event, "x1", width), self._coordinate(event, "y1", height)),
                (self._coordinate(event, "x2", width), self._coordinate(event, "y2", height)),
            ]
        self._verify_replay_input(device, points)
        if action in {"click", "tap", "touch"}:
            device.click(
                self._coordinate(event, "x", width),
                self._coordinate(event, "y", height),
            )
        elif action in {"swipe", "drag"}:
            device.swipe(
                self._coordinate(event, "x1", width),
                self._coordinate(event, "y1", height),
                self._coordinate(event, "x2", width),
                self._coordinate(event, "y2", height),
                min(30.0, max(0.0, float(event.get("duration", 0.3)))),
            )
        elif action in {"long_click", "longclick", "longpress", "long_press"}:
            x = self._coordinate(event, "x", width)
            y = self._coordinate(event, "y", height)
            duration = min(30.0, max(0.1, float(event.get("duration", 1.0))))
            try:
                device.long_click(x, y, duration=duration)
            except TypeError:
                # Older uiautomator2 releases accept the duration only as a
                # positional argument.
                device.long_click(x, y, duration)
        elif action in {"press", "key", "back", "home", "menu", "app_switch", "recents"}:
            key = str(event.get("key") or event.get("keycode") or "")
            if not key and action in {"back", "home", "menu", "app_switch", "recents"}:
                key = {"app_switch": "recent", "recents": "recent"}.get(action, action)
            if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", key):
                raise ValueError("Invalid replay key: %s" % key)
            if strict and key.lower() not in {"back", "enter", "search", "delete", "del", "4", "66", "67", "84"}:
                raise InputRecoveryRequired("replay key is not scoped to the target app")
            device.press(key)
        elif action in {"input", "text", "set_text"}:
            text_value = str(event.get("text", ""))
            if len(text_value) > 4096:
                raise ValueError("Replay text exceeds 4096 characters")
            clear_value = event.get("clear", False)
            clear = (
                clear_value
                if isinstance(clear_value, bool)
                else str(clear_value).strip().lower() in {"1", "true", "yes"}
            )
            if strict:
                locator = dict(event.get("locator") or {"focused": True})
                if "xpath" in locator:
                    raise InputRecoveryRequired("replay text requires a native control locator")
                locator["packageName"] = self.package_name
                selector = device(**locator)
                try:
                    if selector.info.get("packageName") != self.package_name:
                        raise InputRecoveryRequired("replay text control ownership is unknown")
                    if not clear:
                        text_value = str(selector.get_text() or "") + text_value
                    if selector.set_text(text_value) is False:
                        raise InputRecoveryRequired("replay text control rejected the value")
                except Exception:
                    raise InputRecoveryRequired("replay control text input did not complete") from None
            else:
                device.send_keys(text_value, clear=clear)
        elif action in {"sleep", "wait"}:
            seconds = float(event.get("seconds", event.get("duration", 1)))
            time.sleep(min(30.0, max(0.0, seconds)))
        else:
            raise ValueError("Unsupported replay event: %s" % action)
        delay_ms = min(10000, max(0, int(event.get("delay_ms", 200))))
        time.sleep(delay_ms / 1000.0)
        self._verify_replay_input(device, [])

    @staticmethod
    def _event_timestamp(value: Any) -> Optional[float]:
        """Parse Fastbot's device-local ``%Y-%m-%d %H:%M:%S.%f`` clock."""
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _component_activity_key(component: Any, package_name: str) -> str:
        """Return ``package/fully.qualified.Activity`` for comparison.

        Recorded events carry a bare fully-qualified class name while dumpsys
        reports ``package/.Class``; both collapse to the same key.
        """
        text = str(component or "").strip()
        if not text:
            return ""
        if "/" in text:
            package, _, activity = text.partition("/")
        else:
            package, activity = str(package_name or ""), text
        activity = activity.strip()
        package = package.strip()
        if not package or not activity:
            return ""
        if activity.startswith("."):
            activity = package + activity
        elif "." not in activity:
            activity = package + "." + activity
        return "%s/%s" % (package, activity)

    def _track_replay_fidelity(
        self, event: Mapping[str, Any], component: str, fidelity: Dict[str, Any]
    ) -> None:
        """Compare the page reached by replay with the recorded page."""
        recorded = event.get("activity") or event.get("component")
        expected = self._component_activity_key(recorded, self.package_name)
        if not expected:
            return
        actual = self._component_activity_key(component, self.package_name)
        if not actual:
            return
        fidelity["total"] = int(fidelity.get("total", 0)) + 1
        if actual == expected:
            fidelity["matched"] = int(fidelity.get("matched", 0)) + 1
            return
        if not fidelity.get("first_divergence"):
            fidelity["first_divergence"] = {
                "event_id": event.get("event_id"),
                "expected": expected,
                "actual": actual,
            }

    def _logcat(self, clear: bool = False) -> str:
        arguments = [self.adb_path, "-s", self.device_serial, "logcat"]
        arguments.append("-c" if clear else "-d")
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        return result.stdout

    def replay_trajectory(
        self,
        trajectory: Dict[str, Any],
        package_name: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> bool:
        """Replay structured events with bounded ADB input and capture the result."""
        if package_name != self.package_name:
            raise ValueError("Replay package does not match the configured target")
        events = trajectory.get("events", [])
        if not isinstance(events, list) or not events:
            self.last_replay_result = {
                "executed": False,
                "reason": "No structured replay events are available",
                "crash": None,
            }
            return False

        started_clock = time.monotonic()
        replay_plan = self._trajectory_replay_plan(trajectory)
        planned_events = [event for event in events[:10000] if isinstance(event, dict)]
        try:
            repeat_rounds = min(10, max(1, int(replay_plan.get("repeat_rounds", 1) or 1)))
        except (TypeError, ValueError, OverflowError):
            repeat_rounds = 1
        # Data-dependent crashes (an empty question list, an exhausted pager)
        # only surface after the recorded tail is repeated. The page is not
        # restarted between rounds, so the app keeps consuming the same data a
        # real random session would have consumed.
        replay_events = planned_events * repeat_rounds
        total_events = len(replay_events)
        previous_force_stop_policy = self._replay_allow_force_stop
        previous_unowned_policy = self._replay_allow_unowned_touch
        previous_ime_policy = self._replay_allow_ime
        allow_ime_value = replay_plan.get("allow_ime_visible", False)
        if isinstance(allow_ime_value, bool):
            self._replay_allow_ime = allow_ime_value
        elif isinstance(allow_ime_value, str):
            self._replay_allow_ime = (
                allow_ime_value.strip().lower() in {"1", "true", "yes", "on"}
            )
        allow_unowned_value = replay_plan.get("allow_unowned_touch", False)
        if isinstance(allow_unowned_value, bool):
            self._replay_allow_unowned_touch = allow_unowned_value
        elif isinstance(allow_unowned_value, str):
            self._replay_allow_unowned_touch = (
                allow_unowned_value.strip().lower() in {"1", "true", "yes", "on"}
            )
        # A plan produced by the new bounded planner opts into destructive cold
        # recovery explicitly.  Truly legacy trajectories retain their old
        # behavior so existing integrations do not unexpectedly change.
        if replay_plan:
            allow_cold_value = replay_plan.get("allow_cold_activity_start", False)
            if isinstance(allow_cold_value, bool):
                self._replay_allow_force_stop = allow_cold_value
            elif isinstance(allow_cold_value, str):
                self._replay_allow_force_stop = allow_cold_value.strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            else:
                self._replay_allow_force_stop = False
        else:
            self._replay_allow_force_stop = True

        def report_progress(status: str, executed: int, **extra: Any) -> None:
            if not callable(progress_callback):
                return
            payload = {
                "status": status,
                "executed": max(0, int(executed)),
                "total": total_events,
                "skipped": len(extra.get("skipped_events", []))
                if isinstance(extra.get("skipped_events"), list)
                else int(extra.get("skipped", 0) or 0),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            payload.update({key: value for key, value in extra.items() if key != "skipped_events"})
            try:
                progress_callback(payload)
            except Exception:
                logger.debug("Replay progress callback failed", exc_info=True)

        report_progress("running", 0)
        try:
            state = self._adb_command(["get-state"], timeout=5)
            if state.returncode != 0 or (state.stdout or "").strip() != "device":
                raise RuntimeError("device is not ready for replay")
            self._ensure_target_foreground(package_name)
            self._logcat(clear=True)
            width, height = self._screen_size_from_adb()
            replay_options = self.options.get("crash_replay", {})
            if not isinstance(replay_options, Mapping):
                replay_options = {}
            configured_timeout = replay_options.get(
                "replay_timeout_seconds",
                self.options.get(
                    "replay_timeout_seconds", self.REPLAY_TIMEOUT_SECONDS
                ),
            )
            try:
                timeout_seconds = min(
                    self.REPLAY_MAX_TIMEOUT_SECONDS,
                    max(30.0, float(configured_timeout)),
                )
            except (TypeError, ValueError):
                timeout_seconds = self.REPLAY_TIMEOUT_SECONDS
            deadline = time.monotonic() + timeout_seconds
            executed = 0
            foreground_recovery_count = 0
            failed_event_id = None
            failed_event: Optional[Dict[str, Any]] = None
            skipped_events: List[Dict[str, Any]] = []
            round_size = max(1, len(planned_events))
            rounds_completed = 0
            # Page fidelity: whether the app actually sat on the Activity each
            # recorded event was captured on. A low ratio explains a failed
            # reproduction far better than "no crash found".
            fidelity: Dict[str, Any] = {
                "matched": 0,
                "total": 0,
                "ratio": None,
                "first_divergence": None,
            }
            last_event_at: Optional[float] = None
            last_throttle_seconds = 0.0
            for event_index, event in enumerate(replay_events):
                # Keep the recorded cadence: a five second wait before the fatal
                # tap is part of the reproduction, not dead time.
                event_at = self._event_timestamp(event.get("timestamp"))
                if event_at is not None and last_event_at is not None:
                    wait_seconds = min(
                        self.REPLAY_MAX_EVENT_GAP_SECONDS,
                        max(0.0, (event_at - last_event_at) - last_throttle_seconds),
                    )
                    if wait_seconds > 0.05:
                        time.sleep(wait_seconds)
                last_event_at = event_at if event_at is not None else last_event_at
                last_throttle_seconds = min(
                    10.0, max(0, int(event.get("delay_ms", 200) or 0)) / 1000.0
                )
            for event_index, event in enumerate(replay_events):
                if repeat_rounds > 1 and event_index and event_index % round_size == 0:
                    # Stop feeding a tail into an app that already died on the
                    # previous round; the crash is in logcat either way.
                    probe = self.detect_crash(self._logcat(clear=False))
                    rounds_completed = event_index // round_size
                    if probe is not None:
                        logger.info(
                            "Crash reproduced after %s replay rounds on %s",
                            rounds_completed,
                            self.device_serial,
                        )
                        break
                if time.monotonic() >= deadline:
                    failed_event_id = event.get("event_id")
                    failed_event = dict(event)
                    raise TimeoutError(
                        "replay timed out after %.1f seconds" % timeout_seconds
                    )
                try:
                    self._execute_event_adb(event, width, height)
                    executed += 1
                except Exception as exc:
                    # Android 15/16 and several vendor ROMs reject shell input
                    # once Fastbot has navigated to a launcher, ad, or other
                    # external activity.  The command was not delivered in
                    # this case, so restore the target and retry this event
                    # exactly once.  Other ADB failures are not retried: a
                    # timeout may mean that the input already ran.
                    recovered = False
                    if self._is_foreground_injection_error(exc):
                        report_progress(
                            "recovering",
                            executed,
                            message="目标应用离开前台，正在恢复后继续回放",
                            foregroundRecoveryCount=foreground_recovery_count + 1,
                        )
                        try:
                            self._ensure_target_foreground(package_name)
                            self._execute_event_adb(event, width, height)
                        except Exception as recovered_exc:
                            exc = recovered_exc
                        else:
                            executed += 1
                            foreground_recovery_count += 1
                            recovered = True
                    if (
                        not recovered
                        and isinstance(exc, InputRecoveryRequired)
                        and "foreground" in str(exc).lower()
                    ):
                        # A delivered tap can open an external Activity. Every
                        # later event then fails the foreground guard, so resume
                        # the target task once and retry instead of skipping the
                        # rest of the tail.
                        report_progress(
                            "recovering",
                            executed,
                            message="回放过程中应用离开前台，正在恢复后继续",
                            foregroundRecoveryCount=foreground_recovery_count + 1,
                        )
                        try:
                            self._ensure_target_foreground(package_name)
                            self._execute_event_adb(event, width, height)
                        except Exception as recovered_exc:
                            exc = recovered_exc
                        else:
                            executed += 1
                            foreground_recovery_count += 1
                            recovered = True
                    if not recovered and isinstance(exc, InputRecoveryRequired):
                        # One rejected touch (permission dialog, system gesture
                        # area, transient overlay) must not discard the whole
                        # tail before the crash sequence is reached. Record it
                        # and keep injecting the remaining events, bounded so a
                        # systematically wrong page still fails loudly.
                        skipped_events.append(
                            {
                                "event_id": event.get("event_id"),
                                "event": dict(event),
                                "reason": str(exc),
                            }
                        )
                        skip_limit = max(5, total_events // 2)
                        if len(skipped_events) > skip_limit:
                            failed_event_id = event.get("event_id")
                            failed_event = dict(event)
                            raise InputRecoveryRequired(
                                "input guard rejected %d of %d replay events; "
                                "open the saved page and use current-page replay"
                                % (len(skipped_events), total_events)
                            ) from None
                        logger.warning(
                            "Skipping replay event %s rejected by the input guard "
                            "on %s: %s",
                            event.get("event_id"),
                            self.device_serial,
                            exc,
                        )
                        report_progress(
                            "running",
                            executed,
                            skipped_events=skipped_events,
                            foregroundRecoveryCount=foreground_recovery_count,
                        )
                        continue
                    if not recovered:
                        action = str(
                            event.get("action") or event.get("type") or ""
                        ).lower()
                        if action in {"input", "text", "set_text"}:
                            skipped_events.append(
                                {
                                    "event_id": event.get("event_id"),
                                    "event": dict(event),
                                    "reason": str(exc),
                                }
                            )
                            logger.warning(
                                "Skipping unsupported replay text event %s on %s: %s",
                                event.get("event_id"),
                                self.device_serial,
                                exc,
                            )
                            report_progress(
                                "running",
                                executed,
                                skipped_events=skipped_events,
                                foregroundRecoveryCount=foreground_recovery_count,
                            )
                            continue
                        failed_event_id = event.get("event_id")
                        failed_event = dict(event)
                        raise exc

                # Some external activities accept shell input, so successful
                # ADB delivery is not evidence that replay stayed in the app.
                # Probe after every delivered event, including the final one.
                component = self._foreground_component_from_adb()
                if component and not self._is_replay_foreground_allowed(
                    component, package_name
                ):
                    report_progress(
                        "recovering",
                        executed,
                        skipped_events=skipped_events,
                        message="自动化操作离开了目标应用，正在恢复目标应用",
                        foregroundRecoveryCount=foreground_recovery_count + 1,
                        foregroundComponent=component,
                    )
                    self._ensure_target_foreground(package_name)
                    foreground_recovery_count += 1
                if component:
                    # Compare against the page the *next* recorded event ran on:
                    # that is where this action was supposed to land.
                    next_index = event_index + 1
                    expected_event = (
                        replay_events[next_index]
                        if next_index < len(replay_events)
                        else event
                    )
                    self._track_replay_fidelity(expected_event, component, fidelity)
                report_progress(
                    "running",
                    executed,
                    skipped_events=skipped_events,
                    foregroundRecoveryCount=foreground_recovery_count,
                )
            else:
                # The loop was not broken early, so every planned round ran.
                rounds_completed = repeat_rounds
            if fidelity["total"]:
                fidelity["ratio"] = round(
                    int(fidelity["matched"]) / int(fidelity["total"]), 3
                )
            time.sleep(2)
            replay_log = self._logcat(clear=False)
            crash = self.detect_crash(replay_log)
            self.last_replay_result = {
                "executed": executed > 0,
                "event_count": executed,
                "replay_rounds": repeat_rounds,
                "rounds_completed": rounds_completed or (1 if repeat_rounds == 1 else 0),
                "fidelity": dict(fidelity),
                "crash": crash,
                "signature": crash.get("signature") if crash else None,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "transport": "uiautomator2" if input_safety_options(self.options).get("enabled") else "adb",
                "foreground_recovery_count": foreground_recovery_count,
                "elapsed_seconds": round(time.monotonic() - started_clock, 3),
                "replay_plan": replay_plan,
                "replay_start": dict(self.last_replay_start),
            }
            if skipped_events:
                self.last_replay_result["skipped_events"] = skipped_events
                self.last_replay_result["skipped_event_count"] = len(skipped_events)
            report_progress(
                "completed",
                executed,
                skipped_events=skipped_events,
                sameCrash=bool(crash),
                foregroundRecoveryCount=foreground_recovery_count,
            )
            return executed > 0
        except Exception as exc:
            logger.exception("Trajectory replay failed on %s", self.device_serial)
            failure = {
                "executed": False,
                "reason": str(exc),
                "crash": None,
                "event_count": executed if "executed" in locals() else 0,
                "transport": "uiautomator2" if input_safety_options(self.options).get("enabled") else "adb",
                "foreground_recovery_count": (
                    foreground_recovery_count
                    if "foreground_recovery_count" in locals()
                    else 0
                ),
                "elapsed_seconds": round(time.monotonic() - started_clock, 3),
                "fidelity": dict(fidelity) if "fidelity" in locals() else {},
                "replay_plan": replay_plan,
                "replay_start": dict(self.last_replay_start),
            }
            if "skipped_events" in locals() and skipped_events:
                failure["skipped_events"] = skipped_events
                failure["skipped_event_count"] = len(skipped_events)
            if "failed_event_id" in locals() and failed_event_id is not None:
                failure["failed_event_id"] = failed_event_id
            if "failed_event" in locals() and failed_event is not None:
                failure["failed_event"] = failed_event
            self.last_replay_result = failure
            report_progress(
                "failed",
                int(failure.get("event_count", 0) or 0),
                skipped_events=skipped_events if "skipped_events" in locals() else [],
                error=str(exc),
                foregroundRecoveryCount=int(
                    failure.get("foreground_recovery_count", 0) or 0
                ),
            )
            return False
        finally:
            self._replay_allow_force_stop = previous_force_stop_policy
            self._replay_allow_unowned_touch = previous_unowned_policy
            self._replay_allow_ime = previous_ime_policy

    @staticmethod
    def _signature_parts(signature: Any) -> Dict[str, str]:
        exception, _, rest = str(signature or "").partition("@")
        location, _, digest = rest.partition("#")
        return {
            "exception": exception.strip(),
            "location": location.strip(),
            "digest": digest.strip(),
        }

    def classify_reproduction(
        self, crash_signature: str, replay_result: Dict[str, Any]
    ) -> str:
        """Grade a replay outcome as ``exact``, ``likely`` or ``none``.

        The signature hashes the crash type, exception, class and method. A
        reproduced bug can still differ in those details (another thread, a
        different frame order after a library update), which used to be reported
        as "not reproduced" even though the same bug came back. ``likely``
        keeps that case visible instead of discarding it.
        """
        replay_signature = replay_result.get("signature")
        crash = replay_result.get("crash")
        if not replay_signature and isinstance(crash, Mapping):
            replay_signature = self.generate_signature(dict(crash))
        expected = str(crash_signature or "").strip()
        actual = str(replay_signature or "").strip()
        if not expected or not actual:
            return "none"
        if expected == actual:
            return "exact"
        expected_parts = self._signature_parts(expected)
        actual_parts = self._signature_parts(actual)
        if (
            expected_parts["exception"]
            and expected_parts["exception"] == actual_parts["exception"]
            and expected_parts["location"]
            and expected_parts["location"] == actual_parts["location"]
        ):
            return "likely"
        if isinstance(crash, Mapping):
            exception_type = str(crash.get("exception_type") or "").strip()
            location = "%s.%s" % (crash.get("class"), crash.get("method"))
            if (
                exception_type
                and exception_type == expected_parts["exception"]
                and location == expected_parts["location"]
            ):
                return "likely"
        return "none"

    def verify_crash_reproducible(
        self, crash_signature: str, replay_result: Dict[str, Any]
    ) -> bool:
        """Return true when replay reproduced the bug, exactly or probably."""
        return self.classify_reproduction(crash_signature, replay_result) != "none"
