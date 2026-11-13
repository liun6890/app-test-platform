import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, call, patch

import main
from PIL import Image
from core.crash_handler import CrashHandler as RealCrashHandler


class _ImmediateProcess:
    returncode = 0

    def poll(self):
        return self.returncode


def _fingerprint_png(reverse: bool = False) -> bytes:
    image = Image.new("L", (80, 80))
    values = []
    for _row in range(80):
        columns = range(79, -1, -1) if reverse else range(80)
        values.extend(int(column * 255 / 79) for column in columns)
    image.putdata(values)
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _stall_event(path: Path, component: str, content: bytes) -> dict:
    path.write_bytes(content)
    return {
        "mode": "recover_once",
        "component": component,
        "hash": main.fingerprint_png(content).as_hex(),
        "evidence": str(path),
        "evidence_saved": True,
        "recovery_action": None,
    }


class MainHelperTests(unittest.TestCase):
    def test_permission_recovery_timeout_ignores_malformed_configuration(self):
        self.assertEqual(
            8.0,
            main._permission_recovery_timeout(
                {"test": {"permission_recovery_timeout_seconds": "not-a-number"}}
            ),
        )
        self.assertEqual(8.0, main._permission_recovery_timeout({"test": None}))
        self.assertEqual(
            60.0,
            main._permission_recovery_timeout(
                {"test": {"permission_recovery_timeout_seconds": 999}}
            ),
        )

    def test_failure_manifest_write_error_has_storage_guidance(self):
        error = (
            "[Errno 2] No such file or directory: "
            "'C:/runs/report/runtime_failures/cycle_001/phase_001/"
            ".failure.json.16968.tmp'"
        )

        issue_type = main._pressure_issue_type(error)

        self.assertEqual("artifact_persistence_error", issue_type)
        self.assertIn("路径长度", main._issue_suggestion(issue_type))
        self.assertNotIn("同步并校验 Fastbot", main._issue_suggestion(issue_type))

    def test_successful_monkey_fallback_is_a_record_not_an_app_issue(self):
        report = {
            "device_serial": "serial-1",
            "device_info": {"manufacturer": "Google", "model": "Pixel"},
            "cycles": [{
                "cycle": 1,
                "status": "passed",
                "stall_detection_count": 1,
                "stall_recovery_count": 1,
                "stall_recovery_failure_count": 0,
                "stall_events": [{
                    "component": "com.example/.TerminalPage",
                    "unchanged_seconds": 150,
                    "recovery_attempted": True,
                    "recovery_success": True,
                    "recovery": {"attempted": True, "success": True},
                }],
            }],
            "crashes": [],
            "errors": [],
        }

        main._synthesize_issues(report)
        statistics = main._statistics(report)

        self.assertEqual([], report["issues"])
        self.assertEqual("fallback_recovery", report["stall_records"][0]["type"])
        self.assertIn("不等同于应用卡死", report["stall_records"][0]["problem_description"])
        self.assertEqual(0, statistics["issue_count"])
        self.assertEqual(1, statistics["fallback_recovery_success_count"])

    def test_failed_monkey_fallback_remains_a_reviewable_test_risk(self):
        report = {
            "device_serial": "serial-1",
            "device_info": {"manufacturer": "Google", "model": "Pixel"},
            "cycles": [{
                "cycle": 1,
                "status": "failed",
                "stall_detection_count": 1,
                "stall_recovery_count": 1,
                "stall_recovery_failure_count": 1,
                "stall_events": [{
                    "component": "com.example/.TerminalPage",
                    "unchanged_seconds": 150,
                    "recovery_attempted": True,
                    "recovery_success": False,
                    "recovery": {
                        "attempted": True,
                        "success": False,
                        "error": "target did not return",
                    },
                }],
            }],
            "crashes": [],
            "errors": [],
        }

        main._synthesize_issues(report)

        self.assertEqual([], report["stall_records"])
        self.assertEqual("stall_recovery_failed", report["issues"][0]["type"])
        self.assertIn("保底恢复未成功", report["issues"][0]["problem_description"])

    def test_device_data_clear_override_wins_over_default(self):
        config = {
            "test": {
                "clear_app_data_each_cycle": True,
                "clear_app_data_each_cycle_by_device": {"oppo": False},
            }
        }
        self.assertFalse(main._clear_app_data_for_device(config, "oppo"))
        self.assertTrue(main._clear_app_data_for_device(config, "xiaomi"))

    def test_continuous_run_falls_back_to_force_stop_and_relaunch(self):
        class State:
            device_serial = "supported-device"

            def __init__(self):
                self.clear_requests = []

            def reset_app(self, clear_data=True, stop_packages=None):
                del stop_packages
                self.clear_requests.append(clear_data)
                return not clear_data

        state = State()
        success, details = main._reset_app_for_cycle(
            state,
            {
                "test": {
                    "clear_app_data_each_cycle": True,
                    "continue_without_data_clear_on_failure": True,
                }
            },
        )
        self.assertTrue(success)
        self.assertEqual(state.clear_requests, [True, False])
        self.assertTrue(details["continued_without_data_clear"])

    def test_wait_for_fastbot_accepts_completed_process(self):
        class Executor:
            def stop_fastbot(self, process):
                return True

        main._wait_for_fastbot(Executor(), _ImmediateProcess(), 1, 1)

    def test_wait_for_fastbot_preserves_foreground_stop_intent(self):
        class Executor:
            pressure_stop_intent_reason = "foreground_recovery"

        outcome = main._wait_for_fastbot(
            Executor(), _ImmediateProcess(), 1, 1
        )

        self.assertEqual(main._WAIT_FOREGROUND_RECOVERY, outcome)

    def test_wait_for_fastbot_stops_when_app_crash_page_is_detected(self):
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        class Executor:
            app_crash_page_detection = {
                "component": (
                    "com.example.app/"
                    "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
                )
            }

            def __init__(self):
                self.calls = []

            def mark_crash_stop_requested(self):
                self.calls.append("mark_crash")

            def stop_fastbot(self, _process):
                self.calls.append("stop")
                return True

        executor = Executor()
        outcome = main._wait_for_fastbot(
            executor,
            RunningProcess(),
            duration_minutes=1,
            grace_seconds=1,
        )

        self.assertEqual(outcome, main._WAIT_CRASH)
        self.assertEqual(executor.calls, ["mark_crash", "stop"])

    def test_crash_page_evidence_saves_screenshot_hierarchy_and_text(self):
        component = (
            "com.example.app/"
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
        )
        hierarchy = """<hierarchy>
<node text="崩溃信息" content-desc="" />
<node text="java.lang.IllegalStateException: boom" content-desc="" />
</hierarchy>"""

        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

            @staticmethod
            def current_component():
                return component

            @staticmethod
            def capture_screenshot_png():
                return b"fake-png"

            @staticmethod
            def dump_hierarchy_text():
                return hierarchy

            class Device:
                clipboard = "java.lang.IllegalStateException: copied boom"

                def __call__(self, **locator):
                    class Selector:
                        @staticmethod
                        def wait(timeout=0):
                            return locator.get("text") == "复制"

                        @staticmethod
                        def click():
                            return None

                    return Selector()

            d = Device()

        class Mutex:
            def __init__(self):
                self.released = False

            @staticmethod
            def acquire(_tool, _device):
                return True

            def release(self, _tool, _device):
                self.released = True
                return True

        mutex = Mutex()
        with tempfile.TemporaryDirectory() as temporary:
            evidence = main._capture_app_crash_page_evidence(
                State(),
                mutex,
                main.CrashHandler("offline-device", "com.example.app"),
                Path(temporary),
                2,
                {
                    "component": component,
                    "detected_at": "2026-08-12T00:00:00+00:00",
                    "detection_source": "foreground_component",
                },
            )

            self.assertIsNotNone(evidence)
            self.assertEqual(evidence["copy_status"], "captured")
            self.assertIn("copied boom", evidence["copied_text"])
            self.assertIn("崩溃信息", evidence["page_text"])
            for path in evidence["artifacts"].values():
                self.assertTrue(Path(path).is_file())
            manifest = json.loads(
                Path(evidence["artifacts"]["crash_page_evidence"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["component"], component)
        self.assertTrue(mutex.released)

    def test_runtime_failure_evidence_copies_foreground_app_crash_page(self):
        component = (
            "com.example.app/"
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
        )
        hierarchy = """<hierarchy>
<node text="崩溃信息" content-desc="" />
<node text="java.lang.IllegalStateException: runtime boom" content-desc="" />
</hierarchy>"""

        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

            @staticmethod
            def current_component():
                return component

            @staticmethod
            def capture_screenshot_png():
                return b"fake-png"

            @staticmethod
            def dump_hierarchy_text():
                return hierarchy

            class Device:
                clipboard = "java.lang.IllegalStateException: copied runtime boom"

                def __call__(self, **locator):
                    class Selector:
                        @staticmethod
                        def wait(timeout=0):
                            return locator.get("text") == "复制"

                        @staticmethod
                        def click():
                            return None

                    return Selector()

            d = Device()

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            evidence = main._capture_runtime_failure_evidence(
                State(),
                Mock(),
                Mutex(),
                Path(temporary),
                2,
                1,
                {"exception_type": "java.lang.IllegalStateException"},
                {"events": []},
                "FATAL EXCEPTION",
                "Fastbot tail",
            )

        self.assertEqual("app_crash_page", evidence["page_type"])
        self.assertEqual("captured", evidence["copy_status"])
        self.assertIn("copied runtime boom", evidence["copied_text"])
        self.assertIn("runtime boom", evidence["page_text"])

    def test_pressure_log_recognizes_ahzy_crash_page(self):
        detection = main._detect_app_crash_page_in_pressure_log(
            "// the top activity is "
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity\n"
            "Intent { cmp=com.example.app/"
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity }",
            "com.example.app",
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection["detection_source"], "pressure_engine_log")

    def test_pressure_log_uses_configured_crash_activity(self):
        handler = RealCrashHandler(
            "offline",
            "com.example.app",
            {"crash_page_activity_patterns": [r"CustomFatalActivity$"]},
        )
        detection = main._detect_app_crash_page_in_pressure_log(
            "Intent { cmp=com.example.app/com.vendor.CustomFatalActivity }",
            "com.example.app",
            handler,
        )
        self.assertEqual(
            detection["component"],
            "com.example.app/com.vendor.CustomFatalActivity",
        )

    def test_wait_for_fastbot_stops_intentionally_for_stall_recovery(self):
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        class Executor:
            pressure_stop_reason = "stall_recovery"

            def __init__(self):
                self.calls = []
                self.recovery_claimed = False

            def claim_stall_recovery_request(self):
                self.calls.append("claim")
                if self.recovery_claimed:
                    return False
                self.recovery_claimed = True
                return True

            def mark_stall_recovery_stop_requested(self):
                self.calls.append("intentional_stop")

            def stop_fastbot(self, _process):
                self.calls.append("stop")
                return True

        executor = Executor()

        outcome = main._wait_for_fastbot(
            executor, RunningProcess(), duration_minutes=1, grace_seconds=1
        )

        self.assertEqual(outcome, main._WAIT_STALL_RECOVERY)
        self.assertEqual(executor.calls, ["claim", "intentional_stop", "stop"])

    def test_wait_for_fastbot_stops_intentionally_for_audit_recovery(self):
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        class Executor:
            pressure_stop_reason = "audit_recovery"

            def __init__(self):
                self.calls = []
                self.claimed = False

            def claim_audit_recovery_request(self):
                self.calls.append("claim_audit")
                if self.claimed:
                    return False
                self.claimed = True
                return True

            def mark_audit_recovery_stop_requested(self):
                self.calls.append("intentional_audit_stop")

            def stop_fastbot(self, _process):
                self.calls.append("stop")
                return True

        executor = Executor()
        outcome = main._wait_for_fastbot(
            executor, RunningProcess(), duration_minutes=1, grace_seconds=1
        )

        self.assertEqual(outcome, main._WAIT_AUDIT_RECOVERY)
        self.assertEqual(
            executor.calls,
            ["claim_audit", "intentional_audit_stop", "stop"],
        )

    def test_manual_stop_wins_recovery_race_without_requesting_a_second_stop(self):
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        class Executor:
            pressure_stop_reason = None

            @staticmethod
            def claim_stall_recovery_request():
                return True

            @staticmethod
            def mark_stall_recovery_stop_requested():
                return None

            def stop_fastbot(self, _process):
                self.pressure_stop_reason = "manual"
                return True

        executor = Executor()

        outcome = main._wait_for_fastbot(
            executor, RunningProcess(), duration_minutes=1, grace_seconds=1
        )

        self.assertEqual(outcome, main._WAIT_MANUAL_STOP)

    def test_unclean_stall_stop_is_returned_for_safe_handoff_failure(self):
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        class Executor:
            pressure_stop_reason = "stall_recovery"

            @staticmethod
            def claim_stall_recovery_request():
                return True

            @staticmethod
            def mark_stall_recovery_stop_requested():
                return None

            @staticmethod
            def stop_fastbot(_process):
                return False

        outcome = main._wait_for_fastbot(
            Executor(), RunningProcess(), duration_minutes=1, grace_seconds=1
        )

        self.assertEqual(outcome, main._WAIT_STALL_RECOVERY_UNCLEAN)

    def test_stall_recovery_relaunches_only_target_without_page_verification(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, screenshot):
                self.device = object()
                self.reset_calls = []
                self.screenshot = screenshot

            def get_device(self):
                return self.device

            def reset_app(
                self,
                clear_data=True,
                stop_packages=None,
                clear_task=False,
            ):
                self.reset_calls.append((clear_data, stop_packages, clear_task))
                return True

            def install_app(self, _path):
                raise AssertionError("stall recovery must not reinstall the app")

            @staticmethod
            def current_component():
                return "com.example.app/com.example.app.NewActivity"

            def capture_screenshot_png(self):
                return self.screenshot

        class Mutex:
            def __init__(self):
                self.calls = []

            def acquire(self, tool, device):
                self.calls.append(("acquire", tool, device))
                return True

            def release(self, tool, device):
                self.calls.append(("release", tool, device))
                return True

        trigger_png = _fingerprint_png()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/com.example.app.StuckActivity",
                trigger_png,
            )
            state = State(trigger_png)
            mutex = Mutex()
            result = main._recover_stalled_page(
                state,
                mutex,
                {"scenario": {"stop_packages": ["com.example.other"]}},
                event,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["attempted"])
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")
        self.assertEqual(result["action"], "force_stop_clear_task_relaunch")
        self.assertFalse(result["data_cleared"])
        self.assertFalse(result["reinstalled"])
        self.assertEqual(state.reset_calls, [(False, None, True)])
        self.assertEqual(
            mutex.calls,
            [
                ("acquire", "uiautomator2", state.device),
                ("release", "uiautomator2", state.device),
            ],
        )

    def test_stall_recovery_prefers_executor_audit_privacy_chain(self):
        class State:
            device_serial = "xiaomi-device"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            @staticmethod
            def recover_audit_gate(*_args, **_kwargs):
                raise AssertionError("StateManager fallback must not run")

        class Executor:
            def __init__(self):
                self.calls = []

            def prepare_audit_gate(self, timeout, *, device):
                self.calls.append((timeout, device))
                return True

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        state = State()
        executor = Executor()
        result = main._recover_stalled_page(
            state,
            Mutex(),
            {
                "scenario": {
                    "audit_recovery": {
                        "enabled": True,
                        "audit_resource_id": "com.example.app:id/auditRadioButton",
                        "confirm_text": "confirm",
                    }
                }
            },
            {
                "mode": "recover_once",
                "component": "com.example.app/.SplashActivity",
                "evidence_saved": False,
            },
            executor=executor,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["audit_gate_completed"])
        self.assertEqual("executor", result["audit_gate_method"])
        self.assertEqual([(30.0, state.device)], executor.calls)

    def test_stall_recovery_refreshes_ui_before_audit_fallback(self):
        class State:
            device_serial = "xiaomi-device"
            package_name = "com.example.app"

            def __init__(self):
                self.devices = ["stale-device", "refreshed-device"]
                self.index = 0
                self.fallback_calls = []

            def get_device(self):
                return self.devices[self.index]

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            def refresh_device(self):
                self.index = 1
                return self.get_device()

            def recover_audit_gate(self, audit, confirm, timeout, **kwargs):
                self.fallback_calls.append((audit, confirm, timeout, kwargs))
                return True

        class Executor:
            def __init__(self):
                self.calls = []

            def prepare_audit_gate(self, timeout, *, device):
                self.calls.append((timeout, device))
                return False

        class Mutex:
            def __init__(self):
                self.calls = []

            def acquire(self, tool, device):
                self.calls.append(("acquire", tool, device))
                return True

            def release(self, tool, device):
                self.calls.append(("release", tool, device))
                return True

        state = State()
        executor = Executor()
        mutex = Mutex()
        result = main._recover_stalled_page(
            state,
            mutex,
            {
                "scenario": {
                    "audit_recovery": {
                        "enabled": True,
                        "audit_resource_id": "com.example.app:id/auditRadioButton",
                        "confirm_text": "confirm",
                    }
                }
            },
            {
                "mode": "recover_once",
                "component": "com.example.app/.SplashActivity",
                "evidence_saved": False,
            },
            executor=executor,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["audit_gate_completed"])
        self.assertTrue(result["audit_gate_fallback_used"])
        self.assertEqual("state_manager", result["audit_gate_method"])
        self.assertEqual(
            [(30.0, "stale-device"), (12.0, "refreshed-device")],
            executor.calls,
        )
        self.assertEqual(1, len(state.fallback_calls))
        self.assertEqual(
            [
                ("acquire", "uiautomator2", "stale-device"),
                ("release", "uiautomator2", "refreshed-device"),
            ],
            mutex.calls,
        )

    def test_stall_recovery_tolerates_a_transient_screenshot_probe_failure(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, screenshot):
                self.device = object()
                self.screenshot = screenshot
                self.capture_count = 0

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            @staticmethod
            def current_component():
                return "com.example.app/com.example.app.HomeActivity"

            def capture_screenshot_png(self):
                self.capture_count += 1
                if self.capture_count == 1:
                    raise RuntimeError("ADB screenshot capture returned no data")
                return self.screenshot

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        screenshot = _fingerprint_png()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/com.example.app.StuckActivity",
                screenshot,
            )
            state = State(screenshot)
            result = main._recover_stalled_page(
                state,
                Mutex(),
                {"stall_watchdog": {"sample_interval_seconds": 5}},
                event,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")
        self.assertEqual(state.capture_count, 0)

    def test_stall_recovery_accepts_changed_activity_for_a_secure_window(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            @staticmethod
            def current_component():
                return "com.example.app/com.example.app.HomeActivity"

            @staticmethod
            def capture_screenshot_png():
                raise RuntimeError("ADB screenshot capture returned no data")

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/com.example.app.StuckActivity",
                _fingerprint_png(),
            )
            result = main._recover_stalled_page(
                State(),
                Mutex(),
                {"stall_watchdog": {"sample_interval_seconds": 5}},
                event,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")

    def test_stall_recovery_accepts_restart_even_when_activity_is_unchanged(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, screenshot):
                self.device = object()
                self.screenshot = screenshot

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            @staticmethod
            def current_component():
                return "com.example.app/com.example.app.StuckActivity"

            def capture_screenshot_png(self):
                return self.screenshot

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        screenshot = _fingerprint_png()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/.StuckActivity",
                screenshot,
            )
            result = main._recover_stalled_page(
                State(screenshot),
                Mutex(),
                {"stall_watchdog": {"max_hash_distance": 6}},
                event,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["attempted"])
        self.assertEqual(
            result["trigger_component"],
            "com.example.app/com.example.app.StuckActivity",
        )
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")

    def test_stall_recovery_accepts_changed_hash_on_same_activity(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, screenshot):
                self.device = object()
                self.screenshot = screenshot

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            @staticmethod
            def current_component():
                return "com.example.app/com.example.app.StuckActivity"

            def capture_screenshot_png(self):
                return self.screenshot

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/com.example.app.StuckActivity",
                _fingerprint_png(),
            )
            result = main._recover_stalled_page(
                State(_fingerprint_png(reverse=True)),
                Mutex(),
                {"stall_watchdog": {"max_hash_distance": 6}},
                event,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")

    def test_stall_recovery_accepts_dynamic_images_when_activity_is_stable(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, first, second):
                self.device = object()
                self.images = (first, second)
                self.capture_count = 0

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            @staticmethod
            def current_component():
                return "com.example.app/com.example.app.HomeActivity"

            def capture_screenshot_png(self):
                value = self.images[self.capture_count % 2]
                self.capture_count += 1
                return value

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        first = _fingerprint_png()
        second = _fingerprint_png(reverse=True)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/com.example.app.StuckActivity",
                first,
            )
            result = main._recover_stalled_page(
                State(first, second),
                Mutex(),
                {"stall_watchdog": {"max_hash_distance": 6}},
                event,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")

    def test_stall_recovery_waits_past_transient_page_before_deciding(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, transient, stuck):
                self.device = object()
                self.transient = transient
                self.stuck = stuck
                self.capture_count = 0

            def get_device(self):
                return self.device

            @staticmethod
            def reset_app(clear_data=True, stop_packages=None, clear_task=False):
                return True

            def current_component(self):
                if self.capture_count < 2:
                    return "com.example.app/com.example.app.SplashActivity"
                return "com.example.app/com.example.app.StuckActivity"

            def capture_screenshot_png(self):
                content = self.transient if self.capture_count < 2 else self.stuck
                self.capture_count += 1
                return content

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        stuck = _fingerprint_png()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main.time.sleep", return_value=None
        ):
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/.StuckActivity",
                stuck,
            )
            state = State(_fingerprint_png(reverse=True), stuck)
            result = main._recover_stalled_page(
                state,
                Mutex(),
                {
                    "stall_watchdog": {
                        "sample_interval_seconds": 5,
                        "max_hash_distance": 6,
                    }
                },
                event,
            )

        self.assertEqual(state.capture_count, 0)
        self.assertTrue(result["success"])
        self.assertEqual(result["verification_mode"], "target_foreground_after_relaunch")

    def test_manual_stop_during_relaunch_interrupts_recovery(self):
        class Mutex:
            def __init__(self):
                self.stop_requested = False
                self.cleared = False

            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

            def has_stop_request(self):
                return self.stop_requested

            def clear_stop_request(self):
                self.cleared = True
                self.stop_requested = False
                return True

        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self, mutex):
                self.device = object()
                self.mutex = mutex

            def get_device(self):
                return self.device

            def reset_app(self, clear_data=True, stop_packages=None, clear_task=False):
                self.mutex.stop_requested = True
                return True

        screenshot = _fingerprint_png()
        mutex = Mutex()
        with tempfile.TemporaryDirectory() as temporary:
            event = _stall_event(
                Path(temporary) / "stall.png",
                "com.example.app/.StuckActivity",
                screenshot,
            )
            result = main._recover_stalled_page(State(mutex), mutex, {}, event)

        self.assertFalse(result["success"])
        self.assertTrue(result["attempted"])
        self.assertTrue(result["interrupted"])
        self.assertTrue(result["manual_stop_requested"])
        self.assertTrue(mutex.cleared)
        trajectory = {
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ]
        }
        main._apply_stall_recovery_result(trajectory, result)
        self.assertEqual(trajectory["stall_recovery_count"], 1)
        self.assertEqual(trajectory["stall_recovery_skipped_count"], 0)
        self.assertEqual(trajectory["stall_recovery_failure_count"], 0)

    def test_stall_recovery_relaunches_without_trigger_evidence(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()
                self.reset_calls = []

            def get_device(self):
                return self.device

            def reset_app(self, clear_data=True, stop_packages=None, clear_task=False):
                self.reset_calls.append((clear_data, stop_packages, clear_task))
                return True

            @staticmethod
            def install_app(_path):
                raise AssertionError("stall recovery must not reinstall the app")

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        state = State()
        result = main._recover_stalled_page(
            state,
            Mutex(),
            {},
            {
                "mode": "recover_once",
                "component": "com.example.app/com.example.app.StuckActivity",
                "hash": "00",
                "evidence_saved": False,
            },
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["attempted"])
        self.assertFalse(result["skipped"])
        self.assertIn("evidence", result["evidence_warning"])
        self.assertEqual(state.reset_calls, [(False, None, True)])

    def test_stall_recovery_relaunches_when_trigger_event_is_missing(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()
                self.reset_calls = []

            def get_device(self):
                return self.device

            def current_component(self):
                return "com.example.app/com.example.app.StuckActivity"

            def reset_app(self, clear_data=True, stop_packages=None, clear_task=False):
                self.reset_calls.append((clear_data, stop_packages, clear_task))
                return True

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        state = State()
        result = main._recover_stalled_page(state, Mutex(), {}, None)

        self.assertTrue(result["success"])
        self.assertTrue(result["attempted"])
        self.assertTrue(result["trigger_metadata_fallback"])
        self.assertEqual(state.reset_calls, [(False, None, True)])

    def test_late_manual_stop_skips_stall_relaunch_and_is_reported(self):
        class Mutex:
            def __init__(self):
                self.cleared = False

            @staticmethod
            def has_stop_request():
                return True

            def clear_stop_request(self):
                self.cleared = True
                return True

        trajectory = {
            "stop_reason": main._WAIT_STALL_RECOVERY,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
        }
        mutex = Mutex()
        with patch("main._recover_stalled_page") as recover:
            main._perform_stall_recovery(
                trajectory,
                Mock(),
                mutex,
                {},
            )

        recover.assert_not_called()
        self.assertTrue(mutex.cleared)
        self.assertEqual(trajectory["stop_reason"], "manual")
        self.assertEqual(trajectory["stall_recovery_requested_count"], 1)
        self.assertEqual(trajectory["stall_recovery_count"], 0)
        self.assertEqual(trajectory["stall_recovery_skipped_count"], 1)
        self.assertEqual(trajectory["stall_recovery_failure_count"], 0)

    def test_manual_stop_that_wins_claimed_recovery_is_still_counted(self):
        trajectory = {
            "stop_reason": "manual",
            "stall_recovery_requested": True,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
        }

        with patch("main._recover_stalled_page") as recover:
            main._perform_stall_recovery(
                trajectory,
                Mock(),
                Mock(),
                {},
            )

        recover.assert_not_called()
        self.assertEqual(trajectory["stall_recovery_requested_count"], 1)
        self.assertEqual(trajectory["stall_recovery_count"], 0)
        self.assertEqual(trajectory["stall_recovery_skipped_count"], 1)
        self.assertEqual(trajectory["stall_recovery_failure_count"], 0)

    def test_unclean_stall_stop_still_requests_target_only_relaunch(self):
        trajectory = {
            "stop_reason": main._WAIT_STALL_RECOVERY_UNCLEAN,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
        }
        recovery_result = {
            "attempted": True,
            "skipped": False,
            "success": True,
            "data_cleared": False,
            "reinstalled": False,
        }
        with patch(
            "main._recover_stalled_page", return_value=recovery_result
        ) as recover:
            main._perform_stall_recovery(
                trajectory,
                Mock(),
                Mock(),
                {},
            )

        recover.assert_called_once()
        self.assertEqual(trajectory["stall_recovery"], recovery_result)
        self.assertEqual(trajectory["stall_recovery_count"], 1)
        self.assertEqual(trajectory["stall_recovery_failure_count"], 0)

    def test_verified_stall_stop_ignores_host_warning_and_relaunches_target(self):
        trajectory = {
            "stop_reason": main._WAIT_STALL_RECOVERY_UNCLEAN,
            "stall_recovery_requested": True,
            "stall_recovery_device_stopped": True,
            "stall_recovery_precondition_error": "ADB screenshot capture returned no data",
            "stall_events": [{"mode": "recover_once", "recovery_action": None}],
        }
        recovery_result = {
            "attempted": True,
            "skipped": False,
            "success": True,
            "data_cleared": False,
            "reinstalled": False,
        }
        with patch(
            "main._recover_stalled_page", return_value=recovery_result
        ) as recover:
            main._perform_stall_recovery(
                trajectory,
                Mock(),
                Mock(),
                {},
            )

        recover.assert_called_once()
        self.assertTrue(trajectory["stall_recovery"]["success"])
        self.assertFalse(trajectory["stall_recovery"]["data_cleared"])
        self.assertNotIn("stall_recovery_abort_run", trajectory)

    def test_unverified_stall_stop_does_not_relaunch_target(self):
        trajectory = {
            "stop_reason": main._WAIT_STALL_RECOVERY_UNCLEAN,
            "stall_recovery_requested": True,
            "stall_recovery_device_stopped": False,
            "stall_events": [{"mode": "recover_once", "recovery_action": None}],
        }
        with patch("main._recover_stalled_page") as recover:
            main._perform_stall_recovery(
                trajectory,
                Mock(),
                Mock(),
                {},
            )

        recover.assert_not_called()
        self.assertFalse(trajectory["stall_recovery"]["success"])
        self.assertIn("not verified stopped", trajectory["stall_recovery"]["error"])

    @patch("main.DeviceManager.is_device_ready", return_value=True)
    @patch("main.DeviceManager.get_connected_devices", return_value=["one", "two"])
    def test_select_devices_honors_explicit_request(self, _connected, _ready):
        config = {"devices": [{"serial": "auto"}]}
        self.assertEqual(main.select_devices(config, ["two"]), ["two"])

    def test_fastbot_phase_releases_mutex_when_collector_start_fails(self):
        class State:
            @staticmethod
            def get_device():
                return object()

        class Executor:
            MAX_SCREEN_RECORD_SECONDS = 180

            @staticmethod
            def start_logcat_collection(_path):
                raise RuntimeError("offline failure")

        class Mutex:
            def __init__(self):
                self.released = False

            @staticmethod
            def acquire(_tool, _device):
                return True

            def release(self, _tool, _device):
                self.released = True
                return True

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": False},
        }
        mutex = Mutex()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                main._run_fastbot_phase(
                    State(), Executor(), mutex, config, Path(temporary), 1
                )
        self.assertTrue(mutex.released)

    def test_fastbot_phase_does_not_stop_twice_after_manual_recovery_race(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Executor:
            MAX_SCREEN_RECORD_SECONDS = 180
            pressure_engine = "fastbot"
            pressure_stop_reason = "manual"

            def __init__(self):
                self.stop_calls = 0

            @staticmethod
            def start_logcat_collection(_path):
                return object()

            @staticmethod
            def supports_screen_record():
                return False

            @staticmethod
            def start_fastbot(_duration, _throttle):
                return object()

            def stop_fastbot(self, _process):
                self.stop_calls += 1
                return True

            @staticmethod
            def stop_logcat_collection(_process):
                return True

            @staticmethod
            def collect_trajectory():
                return {}

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        executor = Executor()
        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._wait_for_fastbot",
            side_effect=lambda target, process, *_args: (
                target.stop_fastbot(process) and main._WAIT_MANUAL_STOP
            ),
        ):
            trajectory, _log, _artifacts = main._run_fastbot_phase(
                State(), executor, Mutex(), config, Path(temporary), 1
            )

        self.assertEqual(executor.stop_calls, 1)
        self.assertEqual(trajectory["stop_reason"], "manual")

    def test_phase_keeps_trajectory_when_foreground_cleanup_has_host_warning(self):
        """A verified remote stop must not discard the phase as an engine error."""

        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Process:
            returncode = 1

        class Executor:
            pressure_engine = "fastbot"
            foreground_restore_failed = True
            foreground_restore_error = "launcher handoff timed out"
            pressure_stop_verified = True
            pressure_stop_error = "Fastbot exited with return code 1"

            @staticmethod
            def start_logcat_collection(_path):
                return object()

            @staticmethod
            def start_fastbot(_duration, _throttle):
                return Process()

            @staticmethod
            def stop_fastbot(_process):
                return False

            @staticmethod
            def stop_logcat_collection(_process):
                return True

            @staticmethod
            def collect_trajectory(**_kwargs):
                return {
                    "events": [{"action": "click", "x": 10, "y": 20}],
                    "event_count": 1,
                    "operation_count": 1,
                }

        class Mutex:
            def __init__(self):
                self.released = False

            @staticmethod
            def acquire(_tool, _device):
                return True

            def release(self, _tool, _device):
                self.released = True
                return True

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": False},
        }
        mutex = Mutex()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._wait_for_fastbot", return_value=main._WAIT_COMPLETED
        ):
            trajectory, _log, _artifacts = main._run_fastbot_phase_once(
                State(), Executor(), mutex, config, Path(temporary), 1
            )

        self.assertTrue(mutex.released)
        self.assertEqual(trajectory["stop_reason"], main._WAIT_FOREGROUND_RECOVERY)
        self.assertEqual(trajectory["event_count"], 1)


    def test_phase_returns_verified_engine_exit_for_same_cycle_recovery(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Process:
            returncode = 1

        class Executor:
            pressure_engine = "fastbot"
            pressure_stop_verified = True
            pressure_stop_error = "Fastbot exited with return code 1"
            foreground_guard_stop_failed = False

            @staticmethod
            def start_logcat_collection(_path):
                return object()

            @staticmethod
            def start_fastbot(_duration, _throttle):
                return Process()

            @staticmethod
            def stop_fastbot(_process):
                return False

            @staticmethod
            def stop_logcat_collection(_process):
                return True

            @staticmethod
            def collect_trajectory(**_kwargs):
                return {
                    "events": [{"action": "click", "x": 10, "y": 20}],
                    "event_count": 1,
                    "operation_count": 1,
                }

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._wait_for_fastbot", return_value=main._WAIT_COMPLETED
        ):
            trajectory, _log, _artifacts = main._run_fastbot_phase_once(
                State(), Executor(), Mutex(), config, Path(temporary), 1
            )

        self.assertEqual(
            trajectory["stop_reason"], main._WAIT_PRESSURE_ENGINE_EXIT
        )
        self.assertIn("return code 1", trajectory["pressure_engine_error"])
        self.assertEqual(trajectory["event_count"], 1)

    def test_phase_keeps_verified_stall_stop_recoverable_after_host_warning(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Process:
            returncode = 1

        class Executor:
            pressure_engine = "fastbot"
            pressure_stop_verified = True
            pressure_stop_error = "host cleanup warning"
            stall_recovery_claimed = True

            @staticmethod
            def start_logcat_collection(_path):
                return object()

            @staticmethod
            def start_fastbot(_duration, _throttle):
                return Process()

            @staticmethod
            def stop_logcat_collection(_process):
                return True

            @staticmethod
            def collect_trajectory(**_kwargs):
                return {
                    "events": [{"action": "click", "x": 10, "y": 20}],
                    "event_count": 1,
                    "operation_count": 1,
                }

            @staticmethod
            def consume_stall_events():
                return []

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._wait_for_fastbot", return_value=main._WAIT_STALL_RECOVERY_UNCLEAN
        ):
            trajectory, _log, _artifacts = main._run_fastbot_phase_once(
                State(), Executor(), Mutex(), config, Path(temporary), 1
            )

        self.assertEqual(trajectory["stop_reason"], main._WAIT_STALL_RECOVERY)
        self.assertNotIn("stall_recovery_abort_run", trajectory)

    def test_fastbot_phase_restores_mutex_when_collector_will_not_stop(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Executor:
            MAX_SCREEN_RECORD_SECONDS = 180
            pressure_engine = "fastbot"

            @staticmethod
            def start_logcat_collection(_path):
                return object()

            @staticmethod
            def supports_screen_record():
                return False

            @staticmethod
            def start_screenshot_collection(_path, _interval):
                return object()

            @staticmethod
            def start_fastbot(_duration, _throttle):
                return _ImmediateProcess()

            @staticmethod
            def stop_fastbot(_process):
                return True

            @staticmethod
            def stop_screenshot_collection(_event):
                return False

            @staticmethod
            def stop_logcat_collection(_process):
                return True

        class Mutex:
            def __init__(self):
                self.released = False

            @staticmethod
            def acquire(_tool, _device):
                return True

            def release(self, _tool, _device):
                self.released = True
                return True

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": True, "interval_seconds": 10},
        }
        mutex = Mutex()
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            RuntimeError, "Screenshot collector did not stop cleanly"
        ):
            main._run_fastbot_phase(
                State(), Executor(), mutex, config, Path(temporary), 1
            )
        self.assertTrue(mutex.released)

    def test_stall_recovery_continues_when_collector_stop_fails(self):
        """A stuck screenshot probe must not suppress the target-app restart."""

        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Executor:
            pressure_engine = "fastbot"
            stall_recovery_claimed = True

            @staticmethod
            def start_logcat_collection(_path):
                return object()

            @staticmethod
            def start_screenshot_collection(_path, _interval):
                return object()

            @staticmethod
            def start_fastbot(_duration, _throttle):
                return _ImmediateProcess()

            @staticmethod
            def stop_fastbot(_process):
                return True

            @staticmethod
            def stop_screenshot_collection(_event):
                return False

            @staticmethod
            def stop_logcat_collection(_process):
                return True

            @staticmethod
            def collect_trajectory(**_kwargs):
                return {"operation_count": 2}

            @staticmethod
            def consume_stall_events():
                return []

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": True, "interval_seconds": 10},
            "stall_watchdog": {
                "enabled": True,
                "mode": "recover_once",
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._wait_for_fastbot", return_value=main._WAIT_STALL_RECOVERY
        ):
            trajectory, _log, _artifacts = main._run_fastbot_phase_once(
                State(), Executor(), Mutex(), config, Path(temporary), 1
            )

        self.assertEqual(trajectory["stop_reason"], main._WAIT_STALL_RECOVERY)
        self.assertNotIn("stall_recovery_precondition_error", trajectory)
        self.assertNotIn("stall_recovery_abort_run", trajectory)
        self.assertTrue(
            any(
                "collector" in warning.lower()
                for warning in trajectory.get("collection_warnings", [])
            )
        )

    def test_fastbot_phase_preserves_disconnect_error_when_mutex_restore_fails(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

        class Executor:
            @staticmethod
            def start_logcat_collection(_path):
                raise RuntimeError("device disconnected during pressure testing")

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                raise RuntimeError(
                    "Fastbot control was released but uiautomator2 could not be restored"
                )

        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "fastbot_throttle_ms": 1,
                "fastbot_timeout_grace_seconds": 1,
            },
            "screenshot": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            RuntimeError, "device disconnected during pressure testing"
        ):
            main._run_fastbot_phase_once(
                State(), Executor(), Mutex(), config, Path(temporary), 1
            )

    def test_failed_cycle_keeps_current_phase_stall_observations(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

            @staticmethod
            def save_snapshot(_name):
                return {}

        class Executor:
            stall_detection_count = 23
            screenshot_collection_failed = True

            @staticmethod
            def consume_stall_events():
                return [
                    {"component": "com.example.app/.Page%s" % index}
                    for index in range(20)
                ]

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        report = {"device_serial": "offline-device", "crashes": []}
        config = {"scenario": {}}
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._reset_app_for_cycle", return_value=(True, {})
        ), patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ), patch("main._run_fastbot_phase", side_effect=RuntimeError("engine failed")):
            cycle = main._run_cycle(
                State(),
                Executor(),
                Mock(),
                Mutex(),
                config,
                report,
                Path(temporary),
                1,
            )

        self.assertEqual(cycle["status"], "failed")
        self.assertTrue(cycle["abort_run"])
        self.assertEqual(cycle["stall_detection_count"], 23)
        self.assertEqual(len(cycle["stall_events"]), 20)

    def test_cycle_records_runtime_recovery_cancellation_as_interrupted(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

            @staticmethod
            def save_snapshot(_name):
                return {}

        class Executor:
            stall_detection_count = 0

            @staticmethod
            def consume_stall_events():
                return []

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._reset_app_for_cycle", return_value=(True, {})
        ), patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ), patch(
            "main._run_fastbot_phase",
            side_effect=InterruptedError("runtime recovery cancelled"),
        ):
            cycle = main._run_cycle(
                State(),
                Executor(),
                Mock(),
                Mutex(),
                {"scenario": {}},
                {"device_serial": "offline-device", "crashes": []},
                Path(temporary),
                1,
            )

        self.assertEqual("interrupted", cycle["status"])
        self.assertFalse(cycle.get("abort_run", False))
        self.assertEqual(["test run cancelled"], cycle["errors"])

    def test_cycle_attaches_successful_stall_recovery_before_writing_trajectory(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            @staticmethod
            def save_snapshot(_name):
                return {}

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        class CrashHandler:
            @staticmethod
            def detect_crash(_content):
                return None

        state = State()
        mutex = Mutex()
        config = {"scenario": {}}
        report = {"device_serial": "offline-device", "crashes": []}
        trajectory = {
            "operation_count": 7,
            "stop_reason": "stall_recovery",
            "stall_detection_count": 1,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
        }
        recovery = {
            "action": "force_stop_clear_task_relaunch",
            "attempted": True,
            "skipped": False,
            "data_cleared": False,
            "reinstalled": False,
            "success": True,
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._reset_app_for_cycle", return_value=(True, {})
        ), patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ), patch(
            "main._run_fastbot_phase", return_value=(trajectory, "", {})
        ), patch(
            "main._recover_stalled_page", return_value=recovery
        ) as recover:
            cycle = main._run_cycle(
                state,
                Mock(),
                CrashHandler(),
                mutex,
                config,
                report,
                Path(temporary),
                1,
            )
            artifact = Path(cycle["artifacts"]["trajectory"])
            written_trajectory = json.loads(artifact.read_text(encoding="utf-8"))

        recover.assert_called_once()
        self.assertEqual(recover.call_args.args[:3], (state, mutex, config))
        self.assertIs(recover.call_args.args[3], trajectory["stall_events"][0])
        self.assertIsNone(recover.call_args.args[4])
        self.assertEqual(cycle["status"], "passed")
        self.assertEqual(cycle["stall_recovery"], recovery)
        self.assertEqual(cycle["stall_recovery_requested_count"], 1)
        self.assertEqual(cycle["stall_recovery_count"], 1)
        self.assertEqual(cycle["stall_recovery_skipped_count"], 0)
        self.assertEqual(cycle["stall_recovery_failure_count"], 0)
        self.assertTrue(cycle["stall_events"][0]["recovery_success"])
        self.assertEqual(
            written_trajectory["stall_recovery"]["action"], recovery["action"]
        )

    def test_cycle_fails_when_stall_recovery_fails(self):
        class State:
            device_serial = "offline-device"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            @staticmethod
            def save_snapshot(_name):
                return {}

        class Mutex:
            @staticmethod
            def acquire(_tool, _device):
                return True

            @staticmethod
            def release(_tool, _device):
                return True

        class CrashHandler:
            @staticmethod
            def detect_crash(_content):
                return None

        state = State()
        mutex = Mutex()
        config = {"scenario": {}}
        report = {"device_serial": "offline-device", "crashes": []}
        trajectory = {
            "stop_reason": "stall_recovery",
            "stall_detection_count": 1,
            "stall_recovery_abort_run": True,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
        }
        recovery = {
            "action": "force_stop_clear_task_relaunch",
            "attempted": True,
            "skipped": False,
            "data_cleared": False,
            "reinstalled": False,
            "success": False,
            "error": "force-stop and clean relaunch failed",
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._reset_app_for_cycle", return_value=(True, {})
        ), patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ), patch(
            "main._run_fastbot_phase", return_value=(trajectory, "", {})
        ), patch(
            "main._recover_stalled_page", return_value=recovery
        ) as recover:
            cycle = main._run_cycle(
                state,
                Mock(),
                CrashHandler(),
                mutex,
                config,
                report,
                Path(temporary),
                1,
            )

        recover.assert_called_once()
        self.assertEqual(recover.call_args.args[:3], (state, mutex, config))
        self.assertIs(recover.call_args.args[3], trajectory["stall_events"][0])
        self.assertIsNone(recover.call_args.args[4])
        self.assertEqual(cycle["status"], "failed")
        self.assertEqual(cycle["stall_recovery"], recovery)
        self.assertEqual(cycle["stall_recovery_requested_count"], 1)
        self.assertEqual(cycle["stall_recovery_count"], 1)
        self.assertEqual(cycle["stall_recovery_skipped_count"], 0)
        self.assertEqual(cycle["stall_recovery_failure_count"], 1)
        self.assertTrue(cycle["abort_run"])
        self.assertTrue(cycle["errors"])

    def test_live_logcat_crash_probe_reads_current_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "logcat.log"
            path.write_text(
                "old\nFATAL EXCEPTION: main\nProcess: com.example.app, PID: 7\n",
                encoding="utf-8",
            )
            handler = Mock()
            handler.detect_crash.return_value = {"signature": "boom"}

            self.assertTrue(main._detect_live_crash(handler, path, 4))

        handler.detect_crash.assert_called_once()
        self.assertIn("FATAL EXCEPTION", handler.detect_crash.call_args.args[0])

    def test_process_exit_probe_ignores_records_before_capture_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "logcat.log"
            path.write_text(
                "08-17 15:46:09.139 I/am_kill: "
                "[0,29388,com.example.app,0,excessive cpu]\n",
                encoding="utf-8",
            )
            boundary = path.stat().st_size
            with path.open("a", encoding="utf-8") as stream:
                stream.write("08-17 18:12:49.000 I/Test: pressure started\n")

            detection = main._detect_live_process_exit(
                RealCrashHandler("offline", "com.example.app"),
                path,
                boundary,
            )

        self.assertIsNone(detection)

    def test_activity_manager_process_death_stops_runtime_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "logcat.log"
            path.write_text(
                "08-17 10:00:00.000 I/am_proc_died: [0,321,com.example.app]\n",
                encoding="utf-8",
            )

            class Executor:
                config = {"process_monitor": {"enabled": True}}

                def __init__(self):
                    self.detection = None

                def record_app_process_exit(self, detection):
                    self.detection = detection

            executor = Executor()
            outcome = main._probe_runtime_state(
                executor,
                RealCrashHandler("offline", "com.example.app"),
                path,
                0,
                {"started_at": 0, "next_probe_at": 0, "known_pids": set()},
            )

        self.assertEqual(outcome, main._WAIT_PROCESS_EXIT)
        self.assertEqual(executor.detection["process_event"], "am_proc_died")

    def test_pid_monitor_confirms_missing_process_before_stopping(self):
        class Executor:
            config = {
                "process_monitor": {
                    "enabled": True,
                    "sample_interval_seconds": 1,
                    "startup_grace_seconds": 0,
                    "missing_confirmations": 2,
                    "disconnect_confirmations": 2,
                    "foreground_probe_enabled": False,
                    "require_exit_evidence": False,
                }
            }

            def __init__(self):
                self.observations = iter(
                    [
                        {"connected": True, "pids": [10]},
                        {"connected": True, "pids": []},
                        {"connected": True, "pids": []},
                    ]
                )
                self.detection = None

            def probe_target_process(self):
                return next(self.observations)

            def record_app_process_exit(self, detection):
                self.detection = detection

        executor = Executor()
        state = {
            "started_at": 0,
            "next_probe_at": 0,
            "known_pids": set(),
            "missing": 0,
            "disconnects": 0,
        }
        with patch("main.time.monotonic", side_effect=[0, 2, 4]):
            outcomes = [
                main._probe_runtime_state(executor, None, None, 0, state)
                for _ in range(3)
            ]

        self.assertEqual(outcomes, [None, None, main._WAIT_PROCESS_EXIT])
        self.assertEqual(executor.detection["process_event"], "pid_missing")
        self.assertEqual(executor.detection["previous_pids"], [10])

    def test_pid_replacement_is_classified_as_unexpected_restart(self):
        class Executor:
            config = {
                "process_monitor": {
                    "enabled": True,
                    "sample_interval_seconds": 1,
                    "startup_grace_seconds": 0,
                    "foreground_probe_enabled": False,
                    "require_exit_evidence": False,
                }
            }

            def __init__(self):
                self.observations = iter(
                    [
                        {"connected": True, "pids": [10]},
                        {"connected": True, "pids": [11]},
                    ]
                )
                self.detection = None

            def probe_target_process(self):
                return next(self.observations)

            def record_app_process_exit(self, detection):
                self.detection = detection

        executor = Executor()
        state = {"started_at": 0, "next_probe_at": 0, "known_pids": set()}
        with patch("main.time.monotonic", side_effect=[0, 2]):
            self.assertIsNone(
                main._probe_runtime_state(executor, None, None, 0, state)
            )
            outcome = main._probe_runtime_state(executor, None, None, 0, state)

        self.assertEqual(outcome, main._WAIT_PROCESS_EXIT)
        self.assertEqual(executor.detection["process_event"], "pid_replaced")

    def test_pid_replacement_without_log_evidence_is_not_a_crash(self):
        class Executor:
            config = {
                "process_monitor": {
                    "enabled": True,
                    "sample_interval_seconds": 1,
                    "startup_grace_seconds": 0,
                    "foreground_probe_enabled": False,
                }
            }

            def __init__(self):
                self.observations = iter(
                    [
                        {"connected": True, "pids": [10]},
                        {"connected": True, "pids": [11]},
                    ]
                )
                self.detection = None

            def probe_target_process(self):
                return next(self.observations)

            def record_app_process_exit(self, detection):
                self.detection = detection

        executor = Executor()
        state = {"started_at": 0, "next_probe_at": 0, "known_pids": set()}
        with patch("main.time.monotonic", side_effect=[0, 2]):
            self.assertIsNone(
                main._probe_runtime_state(executor, None, None, 0, state)
            )
            self.assertIsNone(
                main._probe_runtime_state(executor, None, None, 0, state)
            )

        self.assertIsNone(executor.detection)
        self.assertEqual(state["known_pids"], {11})
        self.assertEqual(state["unconfirmed_pid_replacements"], 1)

    def test_permission_handoffs_continue_beyond_eight_when_pressure_progresses(self):
        state = Mock(device_serial="offline-device", package_name="com.example.app")
        executor = Mock(permission_recovery_count=0)
        executor.recover_runtime_permission_dialogs.return_value = {"success": True}
        handoffs = [
            ({
                "operation_count": 10,
                "stop_reason": main._WAIT_PERMISSION_RECOVERY,
                "phase": {"engine_elapsed_seconds": 5.0},
            }, "", {})
            for _ in range(9)
        ]
        complete = ({
            "operation_count": 30,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 15.0},
        }, "", {})
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once", side_effect=[*handoffs, complete]
        ) as phase:
            result, _log, _artifacts = main._run_fastbot_phase(
                state, executor, Mock(), {"test": {"fastbot_duration_minutes": 1}},
                Path(temporary), 1,
            )

        self.assertEqual(result["stop_reason"], main._WAIT_COMPLETED)
        self.assertEqual(result["permission_recovery_success_count"], 9)
        self.assertEqual(result["operation_count"], 120)
        self.assertEqual(phase.call_args.kwargs["remaining_seconds"], 15.0)

    def test_permission_handoffs_stop_when_only_engine_time_advances(self):
        state = Mock(device_serial="offline-device", package_name="com.example.app")
        executor = Mock(permission_recovery_count=0)
        executor.recover_runtime_permission_dialogs.return_value = {"success": True}
        handoffs = [
            ({
                "operation_count": 0,
                "stop_reason": main._WAIT_PERMISSION_RECOVERY,
                "phase": {"engine_elapsed_seconds": 2.0},
            }, "", {})
            for _ in range(9)
        ]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once", side_effect=handoffs
        ):
            result, _log, _artifacts = main._run_fastbot_phase(
                state, executor, Mock(), {"test": {"fastbot_duration_minutes": 1}},
                Path(temporary), 1,
            )

        self.assertTrue(result["permission_recovery_failed"])
        self.assertIn("without pressure progress", result["permission_recovery_error"])
        self.assertEqual(executor.recover_runtime_permission_dialogs.call_count, 8)

    def test_permission_handoff_adb_loss_preserves_cycle_for_reconnect(self):
        state = Mock(device_serial="offline-device", package_name="com.example.app")
        state.save_snapshot.return_value = {}
        executor = Mock(permission_recovery_count=0)
        executor.recover_runtime_permission_dialogs.side_effect = RuntimeError(
            "device 'offline-device' not found"
        )
        config = {"scenario": {}, "test": {"fastbot_duration_minutes": 1}}
        handoff = {
            "operation_count": 23,
            "stop_reason": main._WAIT_PERMISSION_RECOVERY,
            "phase": {"engine_elapsed_seconds": 12.0},
            "pressure_stop_verified": True,
            "control_handoff_safe": True,
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._reset_app_for_cycle", return_value=(True, {})
        ), patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ), patch("main._run_fastbot_phase_once", return_value=(handoff, "", {})) as phase, patch(
            "main._remote_transport_available", return_value=False
        ), patch("main._detect_cycle_crash", return_value=None):
            cycle = main._run_cycle(
                state, executor, Mock(), Mock(), config,
                {"device_serial": state.device_serial, "crashes": []}, Path(temporary), 1,
            )

        phase.assert_called_once()
        self.assertEqual(cycle["stop_reason"], main._WAIT_DEVICE_DISCONNECTED)
        self.assertEqual(main.classify_recovery(cycle)["state"], "waiting_for_device")
        self.assertFalse(cycle["continue_run"])
        self.assertEqual(cycle["pressure_elapsed_seconds"], 12.0)
        self.assertEqual(cycle["operation_count"], 23)

    def test_runtime_failure_restarts_and_uses_remaining_cycle_budget(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        first = {
            "operation_count": 3,
            "stop_reason": main._WAIT_PROCESS_EXIT,
            "app_process_exit": {
                "process_event": "pid_missing",
                "detected_at": "2026-08-17T00:00:00+00:00",
            },
            "phase": {"engine_elapsed_seconds": 2.0},
        }
        second = {
            "operation_count": 4,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 4.0},
        }
        config = {
            "test": {
                "fastbot_duration_minutes": 1,
                "stop_on_crash": False,
            },
            "crash_recovery": {
                "enabled": True,
                "max_restarts_per_cycle": 2,
                "minimum_remaining_seconds": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(first, "phase-one", {}), (second, "phase-two", {})],
        ) as phase, patch(
            "main._detect_cycle_crash",
            return_value={"signature": "exit", "crash_type": "process_exit"},
        ), patch(
            "main._capture_runtime_failure_evidence", return_value={"artifacts": {}}
        ), patch(
            "main._recover_after_runtime_failure",
            return_value={"attempted": True, "success": True},
        ) as recover:
            trajectory, log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 2)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(trajectory["operation_count"], 7)
        self.assertEqual(trajectory["runtime_failure_count"], 1)
        self.assertEqual(trajectory["runtime_recovery_count"], 1)
        self.assertEqual(len(trajectory["pressure_phases"]), 2)
        self.assertEqual(log_content, "phase-one\nphase-two")
        self.assertAlmostEqual(
            phase.call_args_list[1].kwargs["remaining_seconds"], 58.0, places=2
        )
        self.assertTrue(
            trajectory["runtime_failures"][0]["recovery"]["continued_pressure"]
        )

    def test_pressure_engine_exit_restarts_and_uses_remaining_cycle_budget(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        first = {
            "operation_count": 3,
            "stop_reason": main._WAIT_PRESSURE_ENGINE_EXIT,
            "pressure_engine_error": "Fastbot exited with return code 1",
            "phase": {
                "engine_elapsed_seconds": 2.0,
                "process_returncode": 1,
            },
        }
        second = {
            "operation_count": 4,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 58.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {
                "enabled": True,
                "max_restarts_per_cycle": 2,
                "minimum_remaining_seconds": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(first, "phase-one", {}), (second, "phase-two", {})],
        ) as phase, patch(
            "main._recover_after_runtime_failure",
            return_value={"attempted": True, "success": True},
        ) as recover:
            trajectory, log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 2)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(
            recover.call_args.args[3], main._WAIT_PRESSURE_ENGINE_EXIT
        )
        self.assertAlmostEqual(
            phase.call_args_list[1].kwargs["remaining_seconds"], 58.0, places=2
        )
        self.assertEqual(trajectory["operation_count"], 7)
        self.assertEqual(trajectory["pressure_engine_failure_count"], 1)
        self.assertEqual(trajectory["pressure_engine_recovery_count"], 1)
        self.assertTrue(
            trajectory["pressure_engine_failures"][0]["recovery"][
                "continued_pressure"
            ]
        )
        self.assertEqual(log_content, "phase-one\nphase-two")

    def test_stall_recovery_restarts_and_uses_remaining_cycle_budget(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        first = {
            "operation_count": 3,
            "stop_reason": main._WAIT_STALL_RECOVERY,
            "stall_recovery_requested": True,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
            "phase": {"engine_elapsed_seconds": 10.0},
        }
        second = {
            "operation_count": 4,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 50.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "stall_watchdog": {
                "enabled": True,
                "mode": "recover_once",
                "max_recoveries_per_cycle": 1,
            },
        }

        def recover(trajectory, *_args):
            trajectory["stall_recovery"] = {
                "attempted": True,
                "skipped": False,
                "success": True,
            }

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(first, "phase-one", {}), (second, "phase-two", {})],
        ) as phase, patch(
            "main._perform_stall_recovery", side_effect=recover
        ) as recovery:
            trajectory, log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 2)
        recovery.assert_called_once()
        self.assertAlmostEqual(
            phase.call_args_list[1].kwargs["remaining_seconds"], 50.0, places=2
        )
        self.assertEqual(trajectory["operation_count"], 7)
        self.assertEqual(trajectory["pressure_elapsed_seconds"], 60.0)
        self.assertEqual(len(trajectory["pressure_phases"]), 2)
        self.assertEqual(trajectory["stall_recovery_count"], 1)
        self.assertTrue(trajectory["stall_recoveries"][0]["continued_pressure"])
        self.assertEqual(log_content, "phase-one\nphase-two")

    def test_verified_unclean_stall_stop_is_allowed_to_continue(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        first = {
            "operation_count": 2,
            "stop_reason": main._WAIT_STALL_RECOVERY_UNCLEAN,
            "stall_recovery_requested": True,
            "stall_events": [
                {"mode": "recover_once", "recovery_action": None}
            ],
            "phase": {"engine_elapsed_seconds": 10.0},
        }
        second = {
            "operation_count": 5,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 50.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "stall_watchdog": {
                "enabled": True,
                "mode": "recover_once",
                "max_recoveries_per_cycle": 1,
            },
        }

        def recover(trajectory, *_args):
            trajectory["stall_recovery"] = {
                "attempted": True,
                "skipped": False,
                "success": True,
            }

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(first, "phase-one", {}), (second, "phase-two", {})],
        ) as phase, patch(
            "main._perform_stall_recovery", side_effect=recover
        ):
            trajectory, _log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 2)
        self.assertEqual(trajectory["operation_count"], 7)
        self.assertEqual(len(trajectory["pressure_phases"]), 2)
        self.assertTrue(trajectory["stall_recoveries"][0]["continued_pressure"])

    def test_repeated_stall_recoveries_continue_the_same_cycle(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        stall_phases = [
            {
                "operation_count": 2,
                "stop_reason": main._WAIT_STALL_RECOVERY,
                "stall_recovery_requested": True,
                "stall_events": [
                    {"mode": "recover_once", "recovery_action": None}
                ],
                "phase": {"engine_elapsed_seconds": 10.0},
            },
            {
                "operation_count": 3,
                "stop_reason": main._WAIT_STALL_RECOVERY,
                "stall_recovery_requested": True,
                "stall_events": [
                    {"mode": "recover_once", "recovery_action": None}
                ],
                "phase": {"engine_elapsed_seconds": 10.0},
            },
        ]
        completed = {
            "operation_count": 4,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 40.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            # This legacy value must not disable a later phase's recovery.
            "stall_watchdog": {
                "enabled": True,
                "mode": "recover_once",
                "max_recoveries_per_cycle": 1,
            },
        }

        def recover(trajectory, *_args):
            trajectory["stall_recovery"] = {
                "attempted": True,
                "skipped": False,
                "success": True,
            }

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[
                *((phase, "stall", {}) for phase in stall_phases),
                (completed, "completed", {}),
            ],
        ) as phase, patch("main._perform_stall_recovery", side_effect=recover):
            trajectory, _log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 3)
        self.assertEqual(trajectory["stall_recovery_count"], 2)
        self.assertEqual(len(trajectory["stall_recoveries"]), 2)
        self.assertEqual(trajectory["pressure_elapsed_seconds"], 60.0)

    def test_foreground_recovery_restarts_with_remaining_cycle_budget(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        first = {
            "operation_count": 7,
            "stop_reason": main._WAIT_FOREGROUND_RECOVERY,
            "foreground_restore_failed": True,
            "foreground_restore_error": "launcher handoff timed out",
            "phase": {"engine_elapsed_seconds": 2.0},
        }
        second = {
            "operation_count": 9,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 58.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {"minimum_remaining_seconds": 1},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(first, "phase-one", {}), (second, "phase-two", {})],
        ) as phase, patch(
            "main._recover_after_runtime_failure",
            return_value={
                "attempted": True,
                "success": True,
                "reason": main._WAIT_FOREGROUND_RECOVERY,
            },
        ) as recover:
            trajectory, log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 2)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(
            recover.call_args.args[3], main._WAIT_FOREGROUND_RECOVERY
        )
        self.assertAlmostEqual(
            phase.call_args_list[1].kwargs["remaining_seconds"], 58.0, places=2
        )
        self.assertEqual(trajectory["operation_count"], 16)
        self.assertEqual(trajectory["foreground_recovery_count"], 1)
        self.assertEqual(log_content, "phase-one\nphase-two")

    def test_cycle_foreground_deadline_uses_cycle_test_options(self):
        """A deferred foreground handoff must not fail with an undefined local."""
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return object()

            @staticmethod
            def save_snapshot(_name):
                return {}

        trajectory = {
            "stop_reason": main._WAIT_FOREGROUND_RECOVERY,
            "pressure_stop_intent": main._WAIT_PHASE_DEADLINE,
            "pressure_elapsed_seconds": 60.0,
            "foreground_recovery_error": "handoff completed at deadline",
            "operation_count": 4,
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "scenario": {},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._reset_app_for_cycle", return_value=(True, {})
        ), patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ), patch(
            "main._run_fastbot_phase", return_value=(trajectory, "", {})
        ), patch("main._cycle_control_handoff_safe", return_value=True):
            cycle = main._run_cycle(
                State(),
                Mock(),
                Mock(),
                Mock(),
                config,
                {"device_serial": "serial", "crashes": []},
                Path(temporary),
                1,
            )

        self.assertEqual("passed", cycle["status"])
        self.assertNotIn("test_options", " ".join(cycle.get("errors", [])))

    def test_input_recovery_continues_same_cycle_without_failure(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        for elapsed in (2.0, 58.0):
            with self.subTest(elapsed=elapsed):
                state = State()
                executor = Mock(input_recovery_reason="keyboard state unknown")
                first = {
                    "operation_count": 7,
                    "stop_reason": main._WAIT_FOREGROUND_RECOVERY,
                    "phase": {"engine_elapsed_seconds": elapsed},
                }
                second = {
                    "operation_count": 9,
                    "stop_reason": main._WAIT_COMPLETED,
                    "phase": {"engine_elapsed_seconds": 60.0 - elapsed},
                }
                config = {
                    "test": {"fastbot_duration_minutes": 1},
                    "input_safety": {"enabled": True, "recovery_attempts": 3},
                    "crash_recovery": {"minimum_remaining_seconds": 5},
                }
                with tempfile.TemporaryDirectory() as temporary, patch(
                    "main._run_fastbot_phase_once",
                    side_effect=[(first, "interrupted", {}), (second, "completed", {})],
                ) as phase, patch(
                    "main._recover_after_runtime_failure", return_value={"success": True}
                ) as recover:
                    trajectory, _, _ = main._run_fastbot_phase(
                        state, executor, Mock(), config, Path(temporary), 2
                    )
                self.assertEqual(2, state._input_cycle)
                self.assertEqual(2, phase.call_count)
                self.assertEqual(1, recover.call_count)
                self.assertEqual(60.0 - elapsed, phase.call_args.kwargs["remaining_seconds"])
                self.assertEqual(60.0, trajectory["pressure_elapsed_seconds"])
                self.assertEqual(main._WAIT_COMPLETED, trajectory["stop_reason"])
                self.assertNotIn("foreground_recovery_failed", trajectory)
                self.assertEqual("keyboard state unknown", trajectory["foreground_recoveries"][0]["input_safety_reason"])

    def test_input_recovery_stops_after_repeated_no_progress(self):
        state = Mock(device_serial="serial", package_name="com.example.app")
        executor = Mock(input_recovery_reason="keyboard state unknown")
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "input_safety": {"enabled": True, "recovery_attempts": 2},
        }
        phases = [({
            "operation_count": 0,
            "stop_reason": main._WAIT_FOREGROUND_RECOVERY,
            "phase": {"engine_elapsed_seconds": 0.1},
        }, "interrupted", {}) for _ in range(3)]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once", side_effect=phases
        ) as phase, patch(
            "main._recover_after_runtime_failure", return_value={"success": True}
        ) as recover:
            trajectory, _, _ = main._run_fastbot_phase(
                state, executor, Mock(), config, Path(temporary), 1
            )
        self.assertEqual(3, phase.call_count)
        self.assertEqual(2, recover.call_count)
        self.assertTrue(trajectory["input_recovery_exhausted"])
        self.assertNotIn("crash_detected", trajectory)

    def test_repeated_successful_foreground_recoveries_keep_the_cycle_running(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        foreground_phases = [
            {
                "operation_count": phase,
                "stop_reason": main._WAIT_FOREGROUND_RECOVERY,
                "foreground_restore_failed": True,
                "foreground_restore_error": "launcher handoff timed out",
                "phase": {"engine_elapsed_seconds": 10.0},
            }
            for phase in range(1, 4)
        ]
        completed = {
            "operation_count": 4,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 30.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {"minimum_remaining_seconds": 1},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[
                *((phase, "foreground", {}) for phase in foreground_phases),
                (completed, "completed", {}),
            ],
        ) as run_phase, patch(
            "main._recover_after_runtime_failure",
            return_value={
                "attempted": True,
                "success": True,
                "reason": main._WAIT_FOREGROUND_RECOVERY,
            },
        ) as recover:
            trajectory, _log_content, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(run_phase.call_count, 4)
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(trajectory["foreground_recovery_count"], 3)
        self.assertEqual(trajectory["pressure_elapsed_seconds"], 60.0)
        self.assertNotIn("foreground_recovery_failed", trajectory)

    def test_audit_recovery_handoff_continues_with_remaining_cycle_budget(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return "device"

        first = {
            "operation_count": 3,
            "stop_reason": main._WAIT_AUDIT_RECOVERY,
            "phase": {"engine_elapsed_seconds": 2.0},
        }
        second = {
            "operation_count": 4,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 58.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {},
        }
        executor = Mock()
        executor.prepare_audit_gate.return_value = True
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(first, "phase-one", {}), (second, "phase-two", {})],
        ) as phase:
            trajectory, log_content, _artifacts = main._run_fastbot_phase(
                State(), executor, mutex, config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 2)
        self.assertAlmostEqual(
            phase.call_args_list[1].kwargs["remaining_seconds"], 58.0, places=2
        )
        mutex.acquire.assert_called_once_with("uiautomator2", "device")
        mutex.release.assert_called_once_with("uiautomator2", "device")
        executor.prepare_audit_gate.assert_called_once_with(
            timeout=20.0, device="device"
        )
        executor.mark_audit_recovery_completed.assert_called_once_with()
        self.assertEqual(trajectory["operation_count"], 7)
        self.assertEqual(trajectory["pressure_elapsed_seconds"], 60.0)
        self.assertEqual(len(trajectory["pressure_phases"]), 2)
        self.assertEqual(log_content, "phase-one\nphase-two")

    def test_repeated_audit_recoveries_with_pressure_progress_do_not_abort_cycle(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return "device"

        audit_phases = [
            {
                "operation_count": 10,
                "stop_reason": main._WAIT_AUDIT_RECOVERY,
                "phase": {"engine_elapsed_seconds": 6.0},
            }
            for _index in range(4)
        ]
        completed = {
            "operation_count": 20,
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {"engine_elapsed_seconds": 36.0},
        }
        executor = Mock()
        executor.prepare_audit_gate.return_value = True
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {},
        }

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[
                *((phase, "audit", {}) for phase in audit_phases),
                (completed, "completed", {}),
            ],
        ) as run_phase:
            trajectory, _log_content, _artifacts = main._run_fastbot_phase(
                State(), executor, mutex, config, Path(temporary), 1
            )

        self.assertEqual(5, run_phase.call_count)
        self.assertEqual(4, executor.prepare_audit_gate.call_count)
        self.assertEqual(60.0, trajectory["pressure_elapsed_seconds"])
        self.assertNotIn("audit_recovery_failed", trajectory)

    def test_audit_recovery_storm_without_progress_is_bounded(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            @staticmethod
            def get_device():
                return "device"

        phase = {
            "operation_count": 0,
            "stop_reason": main._WAIT_AUDIT_RECOVERY,
            "phase": {"engine_elapsed_seconds": 1.0},
        }
        executor = Mock()
        executor.prepare_audit_gate.return_value = True
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {},
        }

        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            side_effect=[(dict(phase), "audit", {}) for _index in range(4)],
        ) as run_phase:
            trajectory, _log_content, _artifacts = main._run_fastbot_phase(
                State(), executor, mutex, config, Path(temporary), 1
            )

        self.assertEqual(4, run_phase.call_count)
        self.assertEqual(3, executor.prepare_audit_gate.call_count)
        self.assertTrue(trajectory["audit_recovery_failed"])
        self.assertIn("without pressure progress", trajectory["audit_recovery_error"])

    def test_runtime_recovery_retries_after_optional_startup_control(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.navigation_calls = []

            def get_device(self):
                return object()

            def reset_app(self, **_kwargs):
                return True

            def navigate_to(self, target, steps):
                self.navigation_calls.append((target, steps))
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        state = State()
        first = {"login_success": True, "navigation_success": False}
        second = {"login_success": True, "navigation_success": True}
        config = {
            "crash_recovery": {"restart_backoff_seconds": 0},
            "scenario": {
                "login_enabled": True,
                "pre_login_steps_by_device": {
                    "serial": [
                        {
                            "action": "optional_click",
                            "locator": {
                                "resourceId": "com.example.app:id/pre_login_accept"
                            },
                        }
                    ]
                },
                "navigation_steps": [
                    {"action": "click", "locator": {"text": "required"}},
                    {
                        "action": "click_if_exists",
                        "locator": {"resourceId": "com.example.app:id/accept"},
                    },
                ]
            },
        }
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True

        with patch(
            "main.prepare_test_scenario", side_effect=[first, second]
        ) as prepare:
            recovery = main._recover_after_runtime_failure(
                state, mutex, config, main._WAIT_PROCESS_EXIT
            )

        self.assertTrue(recovery["success"])
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(state.navigation_calls[0][0], "RecoveryOptionalPreparation")
        self.assertEqual(
            ["optional_click", "click_if_exists"],
            [step["action"] for step in state.navigation_calls[0][1]],
        )
        self.assertTrue(recovery["optional_preparation"]["success"])

    def test_startup_gate_retry_refreshes_stale_uiautomator_session(self):
        class State:
            device_serial = "serial"

            def __init__(self):
                self.devices = ["old-device", "new-device"]
                self.refresh_count = 0

            def get_device(self):
                return self.devices[self.refresh_count]

            def refresh_device(self):
                self.refresh_count += 1
                return self.devices[self.refresh_count]

        state = State()
        executor = Mock()
        executor.prepare_audit_gate.side_effect = [False, True]

        ready, device = main._prepare_audit_gate_with_retry(
            state, executor, state.get_device(), timeout=20
        )

        self.assertTrue(ready)
        self.assertEqual("new-device", device)
        self.assertEqual(1, state.refresh_count)
        self.assertEqual(
            [
                call(timeout=20.0, device="old-device"),
                call(timeout=12.0, device="new-device"),
            ],
            executor.prepare_audit_gate.call_args_list,
        )

    def test_runtime_recovery_completes_audit_gate_before_navigation(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.calls = []

            def get_device(self):
                return object()

            def reset_app(self, **_kwargs):
                self.calls.append("reset")
                return True

            def recover_audit_gate(self, audit, confirm, timeout, **_kwargs):
                self.calls.append(("audit", audit, confirm, timeout))
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        state = State()
        config = {
            "app": {"package_name": "com.example.app"},
            "crash_recovery": {"restart_backoff_seconds": 0},
            "scenario": {
                "audit_recovery": {
                    "enabled": True,
                    "audit_resource_id": "com.example.app:id/auditRadioButton",
                    "confirm_text": "\u786e\u5b9a",
                }
            },
        }
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        with patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ):
            recovery = main._recover_after_runtime_failure(
                state, mutex, config, main._WAIT_PROCESS_EXIT
            )

        self.assertTrue(recovery["success"])
        self.assertEqual("reset", state.calls[0])
        self.assertEqual("audit", state.calls[1][0])

    def test_runtime_recovery_does_not_repeat_completed_audit_navigation(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def get_device(self):
                return object()

            def reset_app(self, **_kwargs):
                return True

            def recover_audit_gate(self, *_args, **_kwargs):
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        config = {
            "app": {"package_name": "com.example.app"},
            "crash_recovery": {"restart_backoff_seconds": 0},
            "scenario": {
                "navigation_steps": [
                    {"action": "launch_app"},
                    {"action": "complete_audit_gate"},
                    {"action": "verify_foreground_stable"},
                ],
                "navigation_steps_by_device": {
                    "serial": [
                        {"action": "audit_gate"},
                        {"action": "click_if_exists", "text": "Allow"},
                    ]
                },
                "audit_recovery": {
                    "enabled": True,
                    "audit_resource_id": "com.example.app:id/auditRadioButton",
                    "confirm_text": "\u786e\u5b9a",
                },
            },
        }
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        with patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ) as prepare:
            recovery = main._recover_after_runtime_failure(
                State(), mutex, config, main._WAIT_FOREGROUND_RECOVERY
            )

        self.assertTrue(recovery["success"])
        self.assertEqual(2, recovery["skipped_completed_audit_steps"])
        prepared = prepare.call_args.args[1]["scenario"]
        self.assertEqual(
            ["launch_app", "verify_foreground_stable"],
            [step["action"] for step in prepared["navigation_steps"]],
        )
        self.assertEqual(
            ["click_if_exists"],
            [
                step["action"]
                for step in prepared["navigation_steps_by_device"]["serial"]
            ],
        )
        self.assertEqual(
            "complete_audit_gate",
            config["scenario"]["navigation_steps"][1]["action"],
        )

    def test_completed_audit_config_filters_legacy_click_pair(self):
        config = {
            "app": {"package_name": "com.example.app"},
            "scenario": {
                "audit_recovery": {"enabled": True},
                "navigation_steps": [
                    {
                        "action": "click",
                        "resourceId": "com.example.app:id/auditRadioButton",
                    },
                    {"action": "click", "text": "\u786e\u5b9a"},
                    {"action": "click_if_exists", "locator": {"text": "Allow"}},
                    {"action": "click", "locator": {"text": "\u5ba1\u6838\u8bb0\u5f55"}},
                    {"action": "click", "locator": {"text": "\u786e\u5b9a"}},
                ]
            }
        }

        prepared, skipped = main._config_after_completed_audit_gate(config)

        self.assertEqual(2, skipped)
        self.assertEqual(
            ["click_if_exists", "click", "click"],
            [
                step["action"]
                for step in prepared["scenario"]["navigation_steps"]
            ],
        )
        self.assertEqual(
            "\u5ba1\u6838\u8bb0\u5f55",
            prepared["scenario"]["navigation_steps"][1]["locator"]["text"],
        )
        self.assertEqual(5, len(config["scenario"]["navigation_steps"]))

    def test_completed_audit_config_filters_custom_legacy_click_pair(self):
        config = {
            "scenario": {
                "audit_recovery": {
                    "enabled": True,
                    "audit_resource_id": "pkg:id/consentCheckbox",
                    "confirm_text": "\u540c\u610f",
                },
                "navigation_steps": [
                    {
                        "action": "optional_click",
                        "locator": {"resourceId": "pkg:id/consentCheckbox"},
                    },
                    {"action": "tap", "locator": {"text": "\u540c\u610f"}},
                    {"action": "verify_foreground_stable"},
                ],
            }
        }

        prepared, skipped = main._config_after_completed_audit_gate(config)

        self.assertEqual(2, skipped)
        self.assertEqual(
            ["verify_foreground_stable"],
            [
                step["action"]
                for step in prepared["scenario"]["navigation_steps"]
            ],
        )

    def test_completed_audit_config_filters_identical_regex_locator(self):
        pattern = r"pkg:id/consent(Check|Toggle)"
        config = {
            "scenario": {
                "audit_recovery": {
                    "enabled": True,
                    "audit_locator": {"resourceIdMatches": pattern},
                    "confirm_text": "\u540c\u610f",
                },
                "navigation_steps": [
                    {
                        "action": "click",
                        "locator": {"resourceIdMatches": pattern},
                    },
                    {"action": "click", "text": "\u540c\u610f"},
                ],
            }
        }

        prepared, skipped = main._config_after_completed_audit_gate(config)

        self.assertEqual(2, skipped)
        self.assertEqual([], prepared["scenario"]["navigation_steps"])

    def test_completed_audit_config_filters_exact_text_fallback(self):
        config = {
            "app": {"package_name": "com.example.app"},
            "scenario": {
                "audit_recovery": {"enabled": True},
                "navigation_steps": [
                    {"action": "click_if_exists", "text": "\u5ba1\u6838\u4e2d"},
                    {"action": "click", "text": "\u786e\u5b9a"},
                ],
            },
        }

        prepared, skipped = main._config_after_completed_audit_gate(config)

        self.assertEqual(2, skipped)
        self.assertEqual([], prepared["scenario"]["navigation_steps"])

    def test_completed_audit_config_carries_confirmation_across_device_steps(self):
        config = {
            "app": {"package_name": "com.example.app"},
            "scenario": {
                "audit_recovery": {"enabled": True},
                "navigation_steps_by_device": {
                    "serial": [
                        {
                            "action": "click",
                            "resourceId": "com.example.app:id/auditRadioButton",
                        }
                    ]
                },
                "navigation_steps": [
                    {"action": "click", "text": "\u786e\u5b9a"},
                    {"action": "verify_foreground_stable"},
                ],
            },
        }

        prepared, skipped = main._config_after_completed_audit_gate(
            config, "serial"
        )

        self.assertEqual(2, skipped)
        self.assertEqual(
            [], prepared["scenario"]["navigation_steps_by_device"]["serial"]
        )
        self.assertEqual(
            ["verify_foreground_stable"],
            [
                step["action"]
                for step in prepared["scenario"]["navigation_steps"]
            ],
        )

    def test_runtime_recovery_retries_the_complete_transaction(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.reset_count = 0

            def get_device(self):
                return object()

            def reset_app(self, **_kwargs):
                self.reset_count += 1
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        state = State()
        config = {
            "crash_recovery": {"restart_backoff_seconds": 0},
            "scenario": {},
        }
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        with patch(
            "main._complete_configured_audit_gate", side_effect=[False, True]
        ) as complete_gate, patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ) as prepare:
            recovery = main._recover_after_runtime_failure(
                state, mutex, config, main._WAIT_PRESSURE_ENGINE_EXIT
            )

        self.assertTrue(recovery["success"])
        self.assertEqual(2, recovery["attempt_count"])
        self.assertEqual(2, state.reset_count)
        self.assertEqual(2, complete_gate.call_count)
        prepare.assert_called_once()
        self.assertEqual(2, mutex.acquire.call_count)
        self.assertEqual(2, mutex.release.call_count)
        self.assertEqual("complete_audit_gate", recovery["attempts"][0]["stage"])
        self.assertFalse(recovery["attempts"][0]["success"])
        self.assertTrue(recovery["attempts"][1]["success"])

    def test_runtime_recovery_retries_when_control_release_is_unverified(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()
                self.reset_count = 0

            def get_device(self):
                return self.device

            def reset_app(self, **_kwargs):
                self.reset_count += 1
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        state = State()
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.side_effect = [False, True]
        with patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ):
            recovery = main._recover_after_runtime_failure(
                state,
                mutex,
                {"crash_recovery": {"restart_backoff_seconds": 0}},
                main._WAIT_FOREGROUND_RECOVERY,
            )

        self.assertTrue(recovery["success"])
        self.assertEqual(2, recovery["attempt_count"])
        self.assertEqual(2, state.reset_count)
        self.assertEqual(
            "release_uiautomator2", recovery["attempts"][0]["stage"]
        )
        self.assertFalse(recovery["attempts"][0]["success"])
        self.assertNotIn("audit_gate_completed", recovery)

    def test_runtime_recovery_exhausts_unverified_control_releases(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            def reset_app(self, **_kwargs):
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = False
        with patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ):
            recovery = main._recover_after_runtime_failure(
                State(),
                mutex,
                {"crash_recovery": {"restart_backoff_seconds": 0}},
                main._WAIT_FOREGROUND_RECOVERY,
            )

        self.assertFalse(recovery["success"])
        self.assertEqual(3, recovery["attempt_count"])
        self.assertEqual(3, mutex.release.call_count)
        self.assertTrue(
            all(
                attempt["stage"] == "release_uiautomator2"
                and not attempt["success"]
                for attempt in recovery["attempts"]
            )
        )

    def test_failed_recovery_does_not_publish_stale_success_fields(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            def reset_app(self, **_kwargs):
                return True

            def recover_audit_gate(self, *_args, **_kwargs):
                return True

            def current_component(self):
                return "com.example.app/.MainActivity"

        mutex = Mock()
        mutex.acquire.side_effect = [True, False, False]
        mutex.release.return_value = False
        config = {
            "app": {"package_name": "com.example.app"},
            "crash_recovery": {"restart_backoff_seconds": 0},
            "scenario": {
                "audit_recovery": {
                    "enabled": True,
                    "audit_resource_id": "com.example.app:id/auditRadioButton",
                    "confirm_text": "\u786e\u5b9a",
                }
            },
        }
        with patch(
            "main.prepare_test_scenario",
            return_value={"login_success": True, "navigation_success": True},
        ):
            recovery = main._recover_after_runtime_failure(
                State(), mutex, config, main._WAIT_FOREGROUND_RECOVERY
            )

        self.assertFalse(recovery["success"])
        self.assertEqual(3, recovery["attempt_count"])
        for key in (
            "audit_gate_completed",
            "optional_preparation",
            "preparation",
            "foreground_component",
        ):
            self.assertNotIn(key, recovery)
        self.assertIn("control unavailable", recovery["error"])

    def test_runtime_recovery_cancellation_releases_control_and_propagates(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

        state = State()
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True

        with patch("main._reset_app", side_effect=InterruptedError("cancelled")):
            with self.assertRaises(InterruptedError):
                main._recover_after_runtime_failure(
                    state,
                    mutex,
                    {"crash_recovery": {"restart_backoff_seconds": 0}},
                    main._WAIT_PRESSURE_ENGINE_EXIT,
                )

        mutex.release.assert_called_once_with("uiautomator2", state.device)

    def test_runtime_recovery_detects_cancel_after_final_gate_probe(self):
        class CancelEvent:
            def __init__(self):
                self.cancelled = False

            def is_set(self):
                return self.cancelled

        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self, cancel_event):
                self.cancel_event = cancel_event
                self.device = object()
                self.gate_attempts = 0

            def get_device(self):
                return self.device

            def reset_app(self, **_kwargs):
                return True

            def recover_audit_gate(self, *_args, **_kwargs):
                self.gate_attempts += 1
                if self.gate_attempts == 3:
                    self.cancel_event.cancelled = True
                return False

        cancel_event = CancelEvent()
        state = State(cancel_event)
        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        config = {
            "app": {"package_name": "com.example.app"},
            "crash_recovery": {"restart_backoff_seconds": 0},
            "scenario": {
                "audit_recovery": {
                    "enabled": True,
                    "audit_resource_id": "com.example.app:id/auditRadioButton",
                    "confirm_text": "\u786e\u5b9a",
                }
            },
        }

        with self.assertRaises(InterruptedError):
            main._recover_after_runtime_failure(
                state,
                mutex,
                config,
                main._WAIT_PRESSURE_ENGINE_EXIT,
                cancel_event,
            )

        self.assertEqual(3, state.gate_attempts)
        self.assertEqual(3, mutex.acquire.call_count)
        self.assertEqual(3, mutex.release.call_count)

    def test_runtime_recovery_detects_cancel_after_final_scenario_attempt(self):
        class CancelEvent:
            def __init__(self):
                self.cancelled = False

            def is_set(self):
                return self.cancelled

        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def __init__(self):
                self.device = object()

            def get_device(self):
                return self.device

            def reset_app(self, **_kwargs):
                return True

        cancel_event = CancelEvent()
        scenario_attempts = 0

        def prepare(_state, _config):
            nonlocal scenario_attempts
            scenario_attempts += 1
            if scenario_attempts == 3:
                cancel_event.cancelled = True
            return {"login_success": True, "navigation_success": False}

        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        with patch("main.prepare_test_scenario", side_effect=prepare):
            with self.assertRaises(InterruptedError):
                main._recover_after_runtime_failure(
                    State(),
                    mutex,
                    {
                        "app": {"package_name": "com.example.app"},
                        "crash_recovery": {"restart_backoff_seconds": 0},
                    },
                    main._WAIT_FOREGROUND_RECOVERY,
                    cancel_event,
                )

        self.assertEqual(3, scenario_attempts)
        self.assertEqual(3, mutex.acquire.call_count)
        self.assertEqual(3, mutex.release.call_count)

    def test_runtime_recovery_preserves_the_navigation_error(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

            def get_device(self):
                return object()

            def reset_app(self, **_kwargs):
                return True

        mutex = Mock()
        mutex.acquire.return_value = True
        mutex.release.return_value = True
        scenario = {
            "login_success": True,
            "navigation_success": False,
            "navigation_error": "step 3 (verify_foreground_stable): ADB timed out",
        }
        with patch.object(main, "_RUNTIME_RECOVERY_ATTEMPTS", 1), patch(
            "main._complete_configured_audit_gate", return_value=True
        ), patch("main.prepare_test_scenario", return_value=scenario):
            recovery = main._recover_after_runtime_failure(
                State(),
                mutex,
                {"crash_recovery": {"restart_backoff_seconds": 0}},
                main._WAIT_FOREGROUND_RECOVERY,
            )

        self.assertFalse(recovery["success"])
        self.assertIn("verify_foreground_stable", recovery["error"])
        self.assertEqual(
            recovery["error"], recovery["attempts"][0]["error"]
        )

    def test_stop_on_crash_skips_same_cycle_restart(self):
        class State:
            device_serial = "serial"
            package_name = "com.example.app"

        trajectory = {
            "operation_count": 1,
            "stop_reason": main._WAIT_PROCESS_EXIT,
            "app_process_exit": {"process_event": "pid_missing"},
            "phase": {"engine_elapsed_seconds": 1.0},
        }
        config = {
            "test": {"fastbot_duration_minutes": 1, "stop_on_crash": True},
            "crash_recovery": {
                "enabled": True,
                "max_restarts_per_cycle": 2,
                "minimum_remaining_seconds": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once", return_value=(trajectory, "log", {})
        ) as phase, patch(
            "main._detect_cycle_crash",
            return_value={"signature": "exit", "crash_type": "process_exit"},
        ), patch(
            "main._capture_runtime_failure_evidence", return_value={"artifacts": {}}
        ), patch("main._recover_after_runtime_failure") as recover:
            result, _log, _artifacts = main._run_fastbot_phase(
                State(), Mock(), Mock(), config, Path(temporary), 1
            )

        self.assertEqual(phase.call_count, 1)
        recover.assert_not_called()
        self.assertEqual(
            result["runtime_failures"][0]["recovery"]["reason"],
            "stop_on_crash is enabled",
        )

    def test_run_devices_reports_every_unavailable_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = {
                "app": {"package_name": "com.example.app"},
                "paths": {"reports_dir": str(Path(temporary) / "reports")},
                "parallel": {"max_workers": 2},
            }
            states = {
                "unauthorized": {"adb_state": "unauthorized"},
                "offline": {"adb_state": "offline"},
                "locked": {"adb_state": "device", "model": "Phone"},
            }
            with patch.object(
                main.DeviceManager, "is_device_ready", return_value=False
            ) as ready:
                results, interrupted = main.run_devices(
                    ["missing", "unauthorized", "offline", "locked"],
                    config,
                    run_id="coverage-run",
                    device_states=states,
                )

            summary_path = (
                Path(results["__run_summary__"]["report_dir"])
                / "run_summary.json"
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertFalse(interrupted)
        self.assertEqual(results["missing"]["coverage_status"], "missing")
        self.assertEqual(
            results["unauthorized"]["coverage_status"], "unauthorized"
        )
        self.assertEqual(results["offline"]["coverage_status"], "not_ready")
        self.assertEqual(results["locked"]["coverage_status"], "not_ready")
        ready.assert_called_once_with("locked")
        self.assertEqual(summary["expected_device_count"], 4)
        self.assertEqual(summary["coverage_counts"]["not_ready"], 2)

    def test_shutdown_device_workers_terminates_and_joins_active_processes(self):
        class Event:
            def __init__(self):
                self.set_called = False

            def set(self):
                self.set_called = True

        class Process:
            pid = 123
            exitcode = None

            def __init__(self):
                self.alive = True
                self.terminated = False
                self.join_calls = []
                self.closed = False

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminated = True
                self.alive = False
                self.exitcode = -15

            def join(self, timeout=None):
                self.join_calls.append(timeout)

            def close(self):
                self.closed = True

        event = Event()
        process = Process()
        results = {}
        with patch.object(main, "_WORKER_SHUTDOWN_GRACE_SECONDS", 0), patch.object(
            main, "_best_effort_remote_worker_cleanup"
        ) as remote_cleanup:
            active = {"serial-1": process}
            main._shutdown_device_workers(
                active,
                event,
                results,
                None,
                status="failed",
                reason="scheduler failed",
            )

        self.assertTrue(event.set_called)
        self.assertTrue(process.terminated)
        self.assertTrue(process.join_calls)
        self.assertTrue(process.closed)
        self.assertEqual({}, active)
        self.assertEqual("failed", results["serial-1"]["status"])
        remote_cleanup.assert_called_once_with("serial-1")

    def test_worker_summary_includes_preflight_and_cycle_errors(self):
        result = {
            "device_serial": "offline-device",
            "status": "completed_with_errors",
            "report": {
                "errors": ["run-level failure"],
                "preflight": {"success": False, "error": "jar missing"},
                "cycles": [
                    {"cycle": 1, "errors": ["preparation scenario failed"]},
                    {"cycle": 2, "errors": "uiautomator restore failed"},
                ],
                "statistics": {},
                "coverage": {"status": "failed"},
            },
        }
        result_queue = Mock()

        with patch("main.run_test_on_device", return_value=result):
            main._device_worker_entry(
                "offline-device", {}, "run-id", None, result_queue
            )

        row = result_queue.put.call_args.args[0]
        self.assertIn("run-level failure", row["error"])
        self.assertIn("preflight: jar missing", row["error"])
        self.assertIn("cycle 1: preparation scenario failed", row["error"])
        self.assertIn("cycle 2: uiautomator restore failed", row["error"])

    def test_worker_result_falls_back_to_terminal_report_when_ipc_is_lost(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            report_dir = reports_root / "run-id_serial-1"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "device_serial": "serial-1",
                        "status": "completed",
                        "coverage": {
                            "status": "complete",
                            "duration_ratio": "not-a-number",
                        },
                        "statistics": {
                            "total_operations": "broken",
                            "pressure_elapsed_seconds": "NaN",
                        },
                        "device_info": {"model": "Phone"},
                    }
                ),
                encoding="utf-8",
            )
            result = main._worker_result_from_report(
                "serial-1",
                {"paths": {"reports_dir": str(reports_root)}},
                "run-id",
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("complete", result["coverage_status"])
        self.assertEqual(0, result["operation_count"])
        self.assertEqual(0.0, result["pressure_elapsed_seconds"])
        self.assertEqual(0.0, result["duration_ratio"])
        self.assertEqual("report_fallback", result["result_source"])

    def test_worker_result_ignores_running_or_partial_terminal_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            report_dir = reports_root / "run-id_serial-1"
            report_dir.mkdir(parents=True)
            report_path = report_dir / "report.json"
            report_path.write_text(
                json.dumps({"device_serial": "serial-1", "status": "running"}),
                encoding="utf-8",
            )
            result = main._worker_result_from_report(
                "serial-1",
                {"paths": {"reports_dir": str(reports_root)}},
                "run-id",
            )

        self.assertIsNone(result)

    def test_terminal_disk_report_wins_over_late_recovering_ipc(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            report_dir = reports_root / "run-id_serial-1"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "device_serial": "serial-1",
                        "run_id": "run-id",
                        "status": "completed_with_crashes",
                        "resume_state": "completed",
                        "finished_at": "2026-09-04T10:00:00+00:00",
                        "remaining_seconds": 19,
                        "coverage": {"status": "complete"},
                        "statistics": {
                            "total_operations": 20,
                            "pressure_elapsed_seconds": 120,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (report_dir / "checkpoint.json").write_text(
                json.dumps(
                    {
                        "job_id": "run-id",
                        "device_serial": "serial-1",
                        "resume_state": "recovering",
                        "cycle": 2,
                        "elapsed_seconds": 100,
                        "operation_count": 18,
                        "remaining_seconds": 20,
                        "saved_at": "2026-09-04T10:00:01+00:00",
                    }
                ),
                encoding="utf-8",
            )
            config = {"paths": {"reports_dir": str(reports_root)}}
            merged = main._merge_worker_result_with_disk(
                "serial-1",
                None,
                {
                    "device_serial": "serial-1",
                    "status": "recovering",
                    "resume_state": "recovering",
                    "coverage_status": "complete",
                    "remaining_seconds": 20,
                    "last_heartbeat": "2026-09-04T10:00:02+00:00",
                },
                config,
                "run-id",
            )

        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual("completed_with_crashes", merged["status"])
        self.assertEqual("completed_with_crashes", merged["execution_status"])
        self.assertEqual("completed_with_crashes", merged["resume_state"])
        self.assertEqual(0.0, merged["remaining_seconds"])
        self.assertFalse(merged["recoverable"])
        self.assertFalse(merged["automatic_resume"])

    def test_wait_for_worker_results_refreshes_existing_row_from_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            report_dir = reports_root / "run-id_serial-1"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "device_serial": "serial-1",
                        "run_id": "run-id",
                        "status": "completed",
                        "resume_state": "completed",
                        "finished_at": "2026-09-04T10:00:00+00:00",
                        "coverage": {"status": "complete"},
                        "statistics": {},
                    }
                ),
                encoding="utf-8",
            )
            results = {
                "serial-1": {
                    "device_serial": "serial-1",
                    "status": "recovering",
                    "resume_state": "recovering",
                    "coverage_status": "complete",
                }
            }
            main._wait_for_worker_results(
                results,
                ["serial-1"],
                lambda: None,
                timeout=0,
                config={"paths": {"reports_dir": str(reports_root)}},
                run_id="run-id",
            )

        self.assertEqual("completed", results["serial-1"]["status"])
        self.assertEqual("completed", results["serial-1"]["resume_state"])

    def test_run_summary_requires_terminal_rows_even_when_coverage_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            report_dir = reports_root / "run-id_serial-1"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "device_serial": "serial-1",
                        "run_id": "run-id",
                        "status": "running",
                        "resume_state": "recovering",
                        "remaining_seconds": 10,
                        "last_heartbeat": "2026-09-04T10:00:00+00:00",
                        "coverage": {"status": "complete"},
                        "statistics": {},
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "app": {"package_name": "com.example.app"},
                "paths": {"reports_dir": str(reports_root)},
                "resume": {"enabled": False},
            }
            results, interrupted = main.run_devices(
                ["serial-1"],
                config,
                run_id="run-id",
                device_states={},
            )
            summary = json.loads(
                (
                    Path(results["__run_summary__"]["report_dir"])
                    / "run_summary.json"
                ).read_text(encoding="utf-8")
            )

        self.assertFalse(interrupted)
        self.assertNotEqual("completed", summary["status"])
        self.assertIn(
            summary["devices"][0]["status"],
            {"running", "recovering"},
        )

    def test_worker_exit_without_auto_resume_is_closed_as_manual_resume(self):
        class Event:
            def __init__(self):
                self.value = False

            def set(self):
                self.value = True

            def is_set(self):
                return self.value

        class Queue:
            def __init__(self):
                self.values = []

            def put(self, value):
                self.values.append(value)

            def get_nowait(self):
                if not self.values:
                    raise main.queue.Empty
                return self.values.pop(0)

            def cancel_join_thread(self):
                return None

            def close(self):
                return None

            def join_thread(self):
                return None

        class Process:
            def __init__(self, target, args, name):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = False
                self.pid = None
                self.exitcode = None

            def start(self):
                self.pid = 123
                self.target(*self.args)
                self.exitcode = 0

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

            def close(self):
                return None

        class Context:
            def Event(self):
                return Event()

            def Queue(self):
                return Queue()

            def Process(self, target, args, name):
                return Process(target, args, name)

        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            config = {
                "app": {"package_name": "com.example.app"},
                "paths": {"reports_dir": str(reports_root)},
                "parallel": {"max_workers": 1},
                "resume": {"enabled": False, "mode": "manual"},
            }

            def worker(serial, worker_config, run_id, _cancel_event, result_queue):
                report_dir = main.report_directory(worker_config, run_id, serial)
                report_dir.mkdir(parents=True, exist_ok=True)
                (report_dir / "report.json").write_text(
                    json.dumps(
                        {
                            "device_serial": serial,
                            "run_id": run_id,
                            "status": "running",
                            "resume_state": "recovering",
                            "remaining_seconds": 30,
                            "last_heartbeat": "2026-09-04T10:00:00+00:00",
                            "coverage": {"status": "incomplete"},
                            "statistics": {},
                        }
                    ),
                    encoding="utf-8",
                )
                result_queue.put(
                    {
                        "device_serial": serial,
                        "status": "recovering",
                        "resume_state": "recovering",
                        "recoverable": True,
                        "automatic_resume": True,
                        "coverage_status": "incomplete",
                    }
                )

            with patch.object(main.mp, "get_context", return_value=Context()), patch.object(
                main.DeviceManager, "is_device_ready", return_value=True
            ), patch.object(main, "_device_worker_entry", side_effect=worker):
                results, interrupted = main.run_devices(
                    ["serial-1"],
                    config,
                    run_id="run-id",
                    device_states={"serial-1": {"adb_state": "device"}},
                )
            durable = json.loads(
                (
                    reports_root / "run-id_serial-1" / "report.json"
                ).read_text(encoding="utf-8")
            )

        self.assertFalse(interrupted)
        self.assertEqual("needs_manual_resume", results["serial-1"]["status"])
        self.assertEqual("needs_manual_resume", durable["resume_state"])
        self.assertNotEqual("completed", results["__run_summary__"]["status"])


if __name__ == "__main__":
    unittest.main()
