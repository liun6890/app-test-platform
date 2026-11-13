import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class _FakeDevice:
    class Watcher:
        def stop(self):
            return None

    watcher = Watcher()


class _FakeStateManager:
    reset_calls = 0

    def __init__(self, serial, package):
        self.device_serial = serial
        self.package_name = package
        self.device = _FakeDevice()

    def get_device(self):
        return self.device

    def install_app(self, _path):
        return True

    def reset_app(self):
        type(self).reset_calls += 1
        return True

    def navigate_to(self, _target, _steps):
        return True

    def save_snapshot(self, name):
        return {
            "name": name,
            "device_serial": self.device_serial,
            "package_name": self.package_name,
            "app_current": {"package": self.package_name, "activity": "MainActivity"},
        }


class _FakeProcess:
    returncode = 0

    def poll(self):
        return 0


class _FakeExecutor:
    MAX_SCREEN_RECORD_SECONDS = 180
    log_content = "logcat\n"
    pressure_stop_reason = None

    def __init__(self, _serial, _package, config):
        self.report_dir = Path(config["_report_dir"])

    def start_logcat_collection(self, path):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(self.log_content)
        return _FakeProcess()

    def start_screen_record(self, _path, _seconds):
        return _FakeProcess()

    def start_fastbot(self, _duration, _throttle):
        return _FakeProcess()

    def stop_fastbot(self, _process):
        return True

    def stop_screen_record(self, _process):
        return True

    def stop_logcat_collection(self, _process):
        return True

    def collect_trajectory(self):
        return {"events": [{"action": "click", "x": 1, "y": 1}], "operation_count": 1}

    def cleanup(self):
        return None


class _FakeMutex:
    def __init__(self, _serial):
        self.holder = None

    def acquire(self, tool, _device):
        if self.holder is not None:
            return False
        self.holder = tool
        return True

    def release(self, tool, _device):
        if self.holder == tool:
            self.holder = None
            return True
        return False


class _FakeCrashHandler:
    def __init__(self, _serial, package):
        self.package_name = package
        self.last_replay_result = {}

    def detect_crash(self, _content):
        if not _FakeExecutor.log_content.startswith("CRASH"):
            return None
        return {
            "signature": "fake-signature",
            "package_name": self.package_name,
            "crash_type": "java",
            "exception_type": "FakeException",
            "class": "com.example.app.Main",
            "method": "run",
            "stacktrace": "FakeException",
        }


class MainFlowTests(unittest.TestCase):
    def test_one_cycle_writes_completed_report_without_devices(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "max_cycles": 1,
                    "crash_replay_attempts": 0,
                    "fastbot_timeout_grace_seconds": 1,
                },
                "logging": {"level": "INFO", "log_dir": str(directory / "logs")},
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
                "scenario": {"login_enabled": False, "navigation_steps": []},
            }
            with patch.object(
                main.DeviceManager, "is_device_ready", return_value=True
            ), patch.object(
                main.DeviceManager,
                "get_device_info",
                return_value={"serial": "offline"},
            ), patch.object(main, "StateManager", _FakeStateManager), patch.object(
                main, "TestExecutor", _FakeExecutor
            ), patch.object(main, "MutexController", _FakeMutex):
                result = main.run_test_on_device("offline", config, "run-offline")
            self.assertEqual(result["status"], "completed")
            report_path = Path(result["report_dir"]) / "report.json"
            self.assertTrue(report_path.is_file())
            self.assertEqual(result["report"]["statistics"]["total_operations"], 1)
            self.assertNotIn(
                "screen_record", result["report"]["cycles"][0]["artifacts"]
            )

    def test_crash_cycle_writes_compatibility_trajectory_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "max_cycles": 1,
                    "crash_replay_attempts": 0,
                    "fastbot_timeout_grace_seconds": 1,
                },
                "logging": {"level": "INFO", "log_dir": str(directory / "logs")},
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
                "scenario": {"login_enabled": False, "navigation_steps": []},
            }
            old_log = _FakeExecutor.log_content
            _FakeExecutor.log_content = "CRASH\n"
            try:
                with patch.object(
                    main.DeviceManager, "is_device_ready", return_value=True
                ), patch.object(
                    main.DeviceManager,
                    "get_device_info",
                    return_value={"serial": "offline"},
                ), patch.object(
                    main, "StateManager", _FakeStateManager
                ), patch.object(
                    main, "TestExecutor", _FakeExecutor
                ), patch.object(
                    main, "MutexController", _FakeMutex
                ), patch.object(main, "CrashHandler", _FakeCrashHandler):
                    result = main.run_test_on_device("offline", config, "run-crash")
            finally:
                _FakeExecutor.log_content = old_log
            self.assertEqual(result["status"], "completed_with_crashes")
            self.assertTrue(
                (Path(result["report_dir"]) / "crash_trajectory.json").is_file()
            )

    def test_crash_is_recorded_then_next_cycle_restarts_and_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "max_cycles": 2,
                    "crash_replay_attempts": 0,
                    "fastbot_timeout_grace_seconds": 1,
                    "stop_on_crash": False,
                },
                "logging": {"level": "INFO", "log_dir": str(directory / "logs")},
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
                "scenario": {"login_enabled": False, "navigation_steps": []},
            }
            old_log = _FakeExecutor.log_content
            old_resets = _FakeStateManager.reset_calls
            _FakeExecutor.log_content = "CRASH\n"
            _FakeStateManager.reset_calls = 0
            try:
                with patch.object(
                    main.DeviceManager, "is_device_ready", return_value=True
                ), patch.object(
                    main.DeviceManager,
                    "get_device_info",
                    return_value={"serial": "offline"},
                ), patch.object(
                    main, "StateManager", _FakeStateManager
                ), patch.object(
                    main, "TestExecutor", _FakeExecutor
                ), patch.object(
                    main, "MutexController", _FakeMutex
                ), patch.object(main, "CrashHandler", _FakeCrashHandler):
                    result = main.run_test_on_device(
                        "offline", config, "run-two-crashes"
                    )
            finally:
                _FakeExecutor.log_content = old_log
                reset_calls = _FakeStateManager.reset_calls
                _FakeStateManager.reset_calls = old_resets

            report = result["report"]
            self.assertEqual(result["status"], "completed_with_crashes")
            self.assertEqual(reset_calls, 2)
            self.assertEqual(len(report["cycles"]), 2)
            self.assertEqual(
                [cycle["status"] for cycle in report["cycles"]],
                ["crash", "crash"],
            )
            self.assertEqual(report["crashes"][0]["occurrences"], [1, 2])

    def test_manual_stop_still_records_crash_without_resuming(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "fastbot_timeout_grace_seconds": 1,
                },
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
            }
            old_log = _FakeExecutor.log_content
            old_reason = _FakeExecutor.pressure_stop_reason
            _FakeExecutor.log_content = "CRASH\n"
            _FakeExecutor.pressure_stop_reason = "manual"
            try:
                with patch.object(
                    main.DeviceManager, "is_device_ready", return_value=True
                ), patch.object(
                    main.DeviceManager,
                    "get_device_info",
                    return_value={"serial": "offline"},
                ), patch.object(main, "StateManager", _FakeStateManager), patch.object(
                    main, "TestExecutor", _FakeExecutor
                ), patch.object(main, "MutexController", _FakeMutex), patch.object(
                    main, "CrashHandler", _FakeCrashHandler
                ):
                    result = main.run_fastbot_only("offline", config, 1)
            finally:
                _FakeExecutor.log_content = old_log
                _FakeExecutor.pressure_stop_reason = old_reason

            report = json.loads(
                (Path(result["report_dir"]) / "report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(report["statistics"]["crash_count"], 1)
            self.assertEqual(report["cycles"][0]["status"], "interrupted")
            self.assertEqual(report["crashes"][0]["replay_attempts"], [])

    def test_fastbot_only_writes_standard_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "fastbot_timeout_grace_seconds": 1,
                },
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
            }
            with patch.object(
                main.DeviceManager, "is_device_ready", return_value=True
            ), patch.object(
                main.DeviceManager,
                "get_device_info",
                return_value={"serial": "offline"},
            ), patch.object(main, "StateManager", _FakeStateManager), patch.object(
                main, "TestExecutor", _FakeExecutor
            ), patch.object(main, "MutexController", _FakeMutex), patch.object(
                main, "CrashHandler", _FakeCrashHandler
            ):
                result = main.run_fastbot_only("offline", config, 1)
            self.assertEqual(result["status"], "completed")
            self.assertTrue((Path(result["report_dir"]) / "report.json").is_file())
            self.assertTrue((Path(result["report_dir"]) / "report.html").is_file())

    def test_fastbot_only_reports_manual_stop_as_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "fastbot_timeout_grace_seconds": 1,
                },
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
            }
            old_reason = _FakeExecutor.pressure_stop_reason
            _FakeExecutor.pressure_stop_reason = "manual"
            try:
                with patch.object(
                    main.DeviceManager, "is_device_ready", return_value=True
                ), patch.object(
                    main.DeviceManager,
                    "get_device_info",
                    return_value={"serial": "offline"},
                ), patch.object(main, "StateManager", _FakeStateManager), patch.object(
                    main, "TestExecutor", _FakeExecutor
                ), patch.object(main, "MutexController", _FakeMutex):
                    result = main.run_fastbot_only("offline", config, 1)
            finally:
                _FakeExecutor.pressure_stop_reason = old_reason
            self.assertEqual(result["status"], "interrupted")

    def test_fastbot_only_runs_stall_recovery_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "fastbot_throttle_ms": 1,
                    "fastbot_timeout_grace_seconds": 1,
                },
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
            }
            trajectory = {
                "operation_count": 2,
                "stop_reason": "stall_recovery",
            }
            recovery_result = {
                "action": "force_stop_clear_task_relaunch",
                "attempted": True,
                "skipped": False,
                "data_cleared": False,
                "reinstalled": False,
                "success": False,
                "error": "audit-gate recovery failed",
            }
            with patch.object(
                main.DeviceManager, "is_device_ready", return_value=True
            ), patch.object(
                main.DeviceManager,
                "get_device_info",
                return_value={"serial": "offline"},
            ), patch.object(main, "StateManager", _FakeStateManager), patch.object(
                main, "TestExecutor", _FakeExecutor
            ), patch.object(main, "MutexController", _FakeMutex), patch.object(
                main,
                "_run_fastbot_phase",
                return_value=(trajectory, "", {}),
            ), patch.object(
                main, "_recover_stalled_page", return_value=recovery_result
            ) as recovery:
                result = main.run_fastbot_only("offline", config, 1)

            report = json.loads(
                (Path(result["report_dir"]) / "report.json").read_text(
                    encoding="utf-8"
                )
            )

        recovery.assert_called_once()
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(report["cycles"][0]["status"], "failed")
        self.assertEqual(report["cycles"][0]["stall_recovery_requested_count"], 1)
        self.assertEqual(report["cycles"][0]["stall_recovery_count"], 1)
        self.assertEqual(report["cycles"][0]["stall_recovery_skipped_count"], 0)
        self.assertEqual(report["cycles"][0]["stall_recovery_failure_count"], 1)
        self.assertEqual(report["cycles"][0]["stall_recovery"], recovery_result)

    def test_preflight_failure_is_persisted_before_install(self):
        class PreflightExecutor(_FakeExecutor):
            def preflight_pressure_engine(self):
                raise FileNotFoundError(
                    "Fastbot files are missing on offline: /sdcard/monkeyq.jar"
                )

        class NoInstallState(_FakeStateManager):
            def install_app(self, _path):
                raise AssertionError("APK installation must not run after failed preflight")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {
                    "fastbot_duration_minutes": 1,
                    "max_cycles": 1,
                },
                "paths": {"reports_dir": str(directory / "reports")},
                "logging": {"log_dir": str(directory / "logs")},
            }
            with patch.object(
                main.DeviceManager, "is_device_ready", return_value=True
            ), patch.object(
                main.DeviceManager,
                "get_device_info",
                return_value={"serial": "offline", "model": "Fixture"},
            ), patch.object(main, "StateManager", NoInstallState), patch.object(
                main, "TestExecutor", PreflightExecutor
            ), patch.object(main, "MutexController", _FakeMutex):
                result = main.run_test_on_device(
                    "offline", config, "run-preflight-failure"
                )

            report = json.loads(
                (Path(result["report_dir"]) / "report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(report["preflight"]["success"])
        self.assertEqual(report["coverage"]["status"], "fastbot_files_missing")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["issues"])
        self.assertIn("Fastbot files are missing", report["issues"][0]["key_log_excerpt"])

    def test_fastbot_only_exception_closes_running_report_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "local.apk"
            apk.write_bytes(b"offline fixture")
            config = {
                "app": {"package_name": "com.example.app", "apk_path": str(apk)},
                "test": {"fastbot_duration_minutes": 1},
                "paths": {"reports_dir": str(directory / "reports")},
                "screenshot": {"enabled": False, "interval_seconds": 10},
            }
            with patch.object(
                main.DeviceManager, "is_device_ready", return_value=True
            ), patch.object(
                main.DeviceManager,
                "get_device_info",
                return_value={"serial": "offline"},
            ), patch.object(main, "StateManager", _FakeStateManager), patch.object(
                main, "TestExecutor", _FakeExecutor
            ), patch.object(main, "MutexController", _FakeMutex), patch.object(
                main, "_run_fastbot_phase", side_effect=RuntimeError("engine failed")
            ):
                result = main.run_fastbot_only("offline", config, 1)

            report = json.loads(
                (Path(result["report_dir"]) / "report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["coverage"]["status"], "failed")
        self.assertIn("engine failed", report["errors"])


if __name__ == "__main__":
    unittest.main()
