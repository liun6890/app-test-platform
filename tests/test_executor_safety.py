import itertools
import json
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, call, patch

from core.executor import PressureEngineError, TestExecutor, _DetachedRemoteProcess
from core.fastbot_assets import runtime_asset_md5, runtime_remote_path
from core.stall_monitor import ImageFingerprint, StallObservation


class _PermissionRecoveryClock:
    def __init__(self):
        self.now = 0.0

    def is_set(self):
        return False

    def wait(self, seconds):
        self.now += max(0.0, float(seconds))
        return False


class ExecutorSafetyTests(unittest.TestCase):
    def _executor(self, classpath):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return TestExecutor(
            "offline-device",
            "com.example.app",
            {
                "_report_dir": temporary.name,
                "fastbot": {"classpath": classpath},
            },
        )

    def test_classpath_must_stay_in_allowed_device_directories(self):
        executor = self._executor("/sdcard/../system/framework.jar")
        with self.assertRaises(ValueError):
            executor._fastbot_classpath()

    def test_valid_classpath_is_accepted(self):
        executor = self._executor("/sdcard/monkeyq.jar:/data/local/tmp/framework.jar")
        self.assertEqual(
            executor._fastbot_classpath(),
            "/sdcard/monkeyq.jar:/data/local/tmp/framework.jar",
        )

    def test_expected_fastbot_jar_checksum_must_match_device_file(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["fastbot"]["expected_monkeyq_md5"] = "a" * 32
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=0, stdout="/sdcard/monkeyq.jar\n", stderr=""),
                Mock(returncode=0, stdout="b" * 32 + "  /sdcard/monkeyq.jar\n", stderr=""),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                executor._verify_fastbot_files()

    def test_fastbot_preflight_verifies_abi_native_library_and_checksum(self):
        executor = self._executor(runtime_remote_path())
        expected = runtime_asset_md5()
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=0, stdout="/data/local/tmp/monkey.apk\n", stderr=""),
                Mock(returncode=0, stdout="%s  /data/local/tmp/monkey.apk\n" % expected, stderr=""),
                Mock(returncode=0, stdout="arm64-v8a\n", stderr=""),
            ],
        ):
            result = executor._verify_fastbot_files()

        self.assertEqual("arm64-v8a", result["native"]["abi"])
        self.assertTrue(result["native"]["bundled"])
        self.assertEqual(
            "/data/local/tmp/monkey.apk!/lib/arm64-v8a/libfastbot_native.so",
            result["native"]["path"],
        )

    def test_fastbot_preflight_reports_missing_native_library_before_launch(self):
        executor = self._executor(runtime_remote_path())
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=1, stdout="", stderr="No such file or directory"),
                Mock(returncode=0, stdout="arm64-v8a\n", stderr=""),
            ],
        ):
            with self.assertRaisesRegex(FileNotFoundError, "Fastbot files are missing"):
                executor._verify_fastbot_files()

    def test_fastbot_audit_xpath_uses_current_package_and_configured_activity(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "com.example.app/.AuditActivity",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确认",
            }
        }
        executor._cached_audit_activity = "com.example.app/.CachedActivity"

        with patch.object(executor, "_resolve_launch_component") as resolve:
            actions = executor._fastbot_audit_xpath_actions()

        resolve.assert_not_called()
        self.assertEqual(actions[0]["activity"], "com.example.app.AuditActivity")
        self.assertEqual(
            actions[0]["actions"],
            [
                {
                    "xpath": "//*[@resource-id='com.example.app:id/auditRadioButton']",
                    "action": "CLICK",
                    "throttle": 500,
                },
                {
                    "xpath": "//*[@text='确认']",
                    "action": "CLICK",
                    "throttle": 1500,
                },
            ],
        )

    def test_fastbot_audit_xpath_reuses_activity_observed_before_pressure(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            }
        }
        executor._cached_audit_activity = "com.example.app/.SplashActivity"

        with patch.object(executor, "_resolve_launch_component") as resolve:
            actions = executor._fastbot_audit_xpath_actions()

        resolve.assert_not_called()
        self.assertEqual(actions[0]["activity"], "com.example.app.SplashActivity")

    def test_push_fastbot_config_generates_current_package_actions(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        success = Mock(returncode=0, stdout="", stderr="")
        with patch.object(
            executor,
            "_resolve_launch_component",
            return_value="com.example.app/.SplashActivity",
        ), patch.object(executor, "_run_adb", return_value=success) as adb:
            executor._push_fastbot_config()

        self.assertEqual(adb.call_count, 2)
        self.assertEqual(adb.call_args_list[0].args[0][-1], "/sdcard/max.config")
        xpath_push = adb.call_args_list[1].args[0]
        self.assertEqual(xpath_push[-1], "/sdcard/max.xpath.actions")
        generated = Path(xpath_push[1])
        actions = json.loads(generated.read_text(encoding="utf-8"))
        serialized = json.dumps(actions, ensure_ascii=False)
        self.assertIn("com.example.app:id/auditRadioButton", serialized)
        self.assertNotIn("com.ofuodapz", serialized)
        self.assertNotIn("com.hfyf", serialized)
        self.assertNotIn("com.zetinxcrqby", serialized)

    def test_push_fastbot_config_clears_stale_actions_when_audit_is_disabled(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {"enabled": False, "activity": ""}
        }
        success = Mock(returncode=0, stdout="", stderr="")
        with patch.object(executor, "_resolve_launch_component") as resolve, patch.object(
            executor, "_run_adb", return_value=success
        ) as adb:
            executor._push_fastbot_config()

        resolve.assert_not_called()
        generated = Path(adb.call_args_list[1].args[0][1])
        self.assertEqual(json.loads(generated.read_text(encoding="utf-8")), [])

    def test_screen_record_capability_requires_device_binary(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        with patch.object(
            executor, "_run_adb", return_value=Mock(returncode=127, stdout="", stderr="")
        ):
            self.assertFalse(executor.supports_screen_record())

    def test_passive_stall_observation_records_evidence_without_recovery(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config.update(
            {
                "screenshot": {"enabled": False},
                "stall_watchdog": {
                    "enabled": True,
                    "mode": "observe",
                    "sample_interval_seconds": 5,
                    "unchanged_seconds": 30,
                    "startup_grace_seconds": 0,
                    "max_hash_distance": 6,
                },
            }
        )
        executor._last_foreground_component = "com.example.app/.MainActivity"
        executor._pressure_active.set()
        (Path(executor.report_dir) / "fastbot.log").write_text(
            "Event id: 1\nEvent id: 27\n", encoding="utf-8"
        )
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )
        result = Mock(returncode=0, stdout=b"fake-png", stderr=b"")
        with patch(
            "core.executor.subprocess.run", return_value=result
        ) as capture, patch(
            "core.executor.fingerprint_png", return_value=fingerprint
        ), patch(
            "core.executor.StallDetector.observe", return_value=observation
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            deadline = time.monotonic() + 2
            while executor.stall_detection_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(executor.stop_screenshot_collection(job))

        events = executor.consume_stall_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["mode"], "observe")
        self.assertIsNone(events[0]["recovery_action"])
        self.assertEqual(events[0]["last_event_id"], 27)
        self.assertTrue(Path(events[0]["evidence"]).is_file())
        self.assertEqual(executor.stall_detection_count, 0)
        self.assertIn("screencap", capture.call_args.args[0])

    def test_static_page_with_remote_fastbot_progress_still_requests_recovery(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config.update(
            {
                "screenshot": {"enabled": False},
                "stall_watchdog": {
                    "enabled": True,
                    "mode": "recover_once",
                    "sample_interval_seconds": 5,
                    "unchanged_seconds": 30,
                    "startup_grace_seconds": 0,
                },
            }
        )
        executor._pressure_active.set()
        executor._last_foreground_component = "com.example.app/.MainActivity"
        progress_seen = threading.Event()
        release_third_read = threading.Event()
        read_count = 0
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )
        initial_observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=0,
            state_changed=False,
            detected=False,
        )
        markers = iter([(0, 100, 1), (0, 200, 2)])

        def read_progress(_path):
            nonlocal read_count
            read_count += 1
            if read_count >= 3:
                original_wait(release_third_read, timeout=2)
            marker = next(markers, (0, 200, 2))
            if marker[-1] == 2:
                progress_seen.set()
                time.sleep(0.1)
            return {
                "log_size": marker[1],
                "last_event_id": marker[-1],
                "progress_marker": marker,
            }

        original_wait = threading.Event.wait

        def quick_wait(event, timeout=None):
            return original_wait(event, min(timeout or 0.01, 0.01))

        observations = iter([initial_observation, observation])

        def observe_result(*_args, **_kwargs):
            return next(observations, observation)

        with patch.object(executor, "_read_fastbot_progress", side_effect=read_progress), patch(
            "core.executor.subprocess.run",
            return_value=Mock(returncode=0, stdout=b"fake-png", stderr=b""),
        ), patch("core.executor.fingerprint_png", return_value=fingerprint), patch(
            "core.executor.StallDetector.observe",
            side_effect=observe_result,
        ) as observe, patch(
            "core.executor.threading.Event.wait", autospec=True, side_effect=quick_wait
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            try:
                deadline = time.monotonic() + 2
                while observe.call_count < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(progress_seen.is_set())
                self.assertGreaterEqual(observe.call_count, 2)
                while (
                    executor.stall_detection_count < 1 and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
            finally:
                job.set()
                release_third_read.set()
                self.assertTrue(executor.stop_screenshot_collection(job))

        self.assertGreaterEqual(executor.stall_detection_count, 1)
        self.assertTrue(executor.claim_stall_recovery_request())
        self.assertEqual(executor.stall_watchdog_status["last_event_id"], 2)

    def test_detached_progress_reader_uses_current_remote_log(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._fastbot_process = _DetachedRemoteProcess(
            "4321", "/sdcard/current-fastbot.log"
        )
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=0, stdout="256 /sdcard/current-fastbot.log\n"),
                Mock(returncode=0, stdout="Event id: 42\nSending Touch (ACTION_MOVE)\n"),
            ],
        ) as adb:
            progress = executor._read_fastbot_progress(
                Path(executor.report_dir) / "fastbot.log"
            )
        self.assertEqual(progress["remote_log_size"], 256)
        self.assertEqual(progress["last_event_id"], 42)
        self.assertEqual(adb.call_args_list[1].args[0][:3], ["shell", "tail", "-c"])

    def test_wait_event_log_growth_is_not_input_progress(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._fastbot_process = _DetachedRemoteProcess(
            "4321", "/sdcard/current-fastbot.log"
        )
        log = Path(executor.report_dir) / "fastbot.log"
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=0, stdout="100 /sdcard/current-fastbot.log\n"),
                Mock(returncode=0, stdout="Event id: 12\nWait Event for 150 milliseconds\n"),
                Mock(returncode=0, stdout="900 /sdcard/current-fastbot.log\n"),
                Mock(returncode=0, stdout="Event id: 12\nWait Event for 150 milliseconds\n"),
                Mock(returncode=0, stdout="1200 /sdcard/current-fastbot.log\n"),
                Mock(returncode=0, stdout="Wait Event for 150 milliseconds\n"),
            ],
        ):
            first = executor._read_fastbot_progress(log)
            waiting = executor._read_fastbot_progress(log)
            no_events = executor._read_fastbot_progress(log)

        self.assertGreater(waiting["log_size"], first["log_size"])
        self.assertEqual(first["progress_marker"], waiting["progress_marker"])
        self.assertIsNone(no_events["progress_marker"])

    def test_recover_once_requests_recovery_only_after_event_is_recorded(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config.update(
            {
                "screenshot": {"enabled": False},
                "stall_watchdog": {
                    "enabled": True,
                    "mode": "recover_once",
                    "sample_interval_seconds": 5,
                    "unchanged_seconds": 30,
                    "startup_grace_seconds": 0,
                    "max_hash_distance": 6,
                },
            }
        )
        executor._last_foreground_component = "com.example.app/.MainActivity"
        executor._pressure_active.set()
        requested_when_recorded = []

        class RecordingEvents(list):
            def append(self, event):
                requested_when_recorded.append(executor._stall_recovery_requested)
                super().append(event)

        executor._stall_events = RecordingEvents()
        write_started = threading.Event()
        release_write = threading.Event()
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )

        def write_evidence(_path, content):
            write_started.set()
            release_write.wait(timeout=2)
            return len(content)

        result = Mock(returncode=0, stdout=b"fake-png", stderr=b"")
        with patch("core.executor.subprocess.run", return_value=result), patch(
            "core.executor.fingerprint_png", return_value=fingerprint
        ), patch(
            "core.executor.StallDetector.observe", return_value=observation
        ), patch(
            "pathlib.Path.write_bytes", autospec=True, side_effect=write_evidence
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            try:
                self.assertTrue(write_started.wait(timeout=2))
                self.assertFalse(executor.claim_stall_recovery_request())
                release_write.set()

                deadline = time.monotonic() + 2
                claimed = False
                while time.monotonic() < deadline:
                    claimed = executor.claim_stall_recovery_request()
                    if claimed:
                        break
                    time.sleep(0.01)
                self.assertTrue(claimed)
            finally:
                release_write.set()
                self.assertTrue(executor.stop_screenshot_collection(job))

        events = executor.consume_stall_events()
        self.assertEqual(requested_when_recorded, [False])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["mode"], "recover_once")
        self.assertTrue(executor._stall_recovery_claimed)

    def test_recover_once_requests_recovery_without_saved_evidence(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config.update(
            {
                "screenshot": {"enabled": False},
                "stall_watchdog": {
                    "enabled": True,
                    "mode": "recover_once",
                    "sample_interval_seconds": 5,
                    "unchanged_seconds": 30,
                    "startup_grace_seconds": 0,
                    "max_hash_distance": 6,
                },
            }
        )
        executor._last_foreground_component = "com.example.app/.MainActivity"
        executor._pressure_active.set()
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )

        with patch(
            "core.executor.subprocess.run",
            return_value=Mock(returncode=0, stdout=b"fake-png", stderr=b""),
        ), patch(
            "core.executor.fingerprint_png", return_value=fingerprint
        ), patch(
            "core.executor.StallDetector.observe", return_value=observation
        ), patch(
            "pathlib.Path.write_bytes", side_effect=OSError("disk full")
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            deadline = time.monotonic() + 2
            while executor.stall_detection_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(executor.stop_screenshot_collection(job))

        events = executor.consume_stall_events()
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["evidence"])
        self.assertFalse(events[0]["evidence_saved"])
        self.assertTrue(executor.claim_stall_recovery_request())

    def test_recover_once_requests_recovery_when_evidence_cap_is_reached(self):
        """The bounded evidence list must not disable the control-plane restart."""
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config.update(
            {
                "screenshot": {"enabled": False},
                "stall_watchdog": {
                    "enabled": True,
                    "mode": "recover_once",
                    "sample_interval_seconds": 5,
                    "unchanged_seconds": 30,
                    "startup_grace_seconds": 0,
                    "max_hash_distance": 6,
                },
            }
        )
        executor._last_foreground_component = "com.example.app/.MainActivity"
        executor._pressure_active.set()
        capture_started = threading.Event()
        release_capture = threading.Event()
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )

        def capture(*_args, **_kwargs):
            capture_started.set()
            release_capture.wait(timeout=2)
            return Mock(returncode=0, stdout=b"fake-png", stderr=b"")

        with patch("core.executor.subprocess.run", side_effect=capture), patch(
            "core.executor.fingerprint_png", return_value=fingerprint
        ), patch(
            "core.executor.StallDetector.observe", return_value=observation
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            try:
                self.assertTrue(capture_started.wait(timeout=2))
                executor._stall_events.extend(
                    {"component": "old/.Page%s" % index} for index in range(20)
                )
                release_capture.set()
                deadline = time.monotonic() + 2
                while executor.stall_detection_count < 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(executor.stall_detection_count, 1)
                events = executor.consume_stall_events()
                self.assertEqual(len(events), 20)
                pending = [event for event in events if event.get("mode") == "recover_once"]
                self.assertEqual(len(pending), 1)
                self.assertIsNone(pending[0]["evidence"])
                self.assertTrue(executor.claim_stall_recovery_request())
            finally:
                release_capture.set()
                self.assertTrue(executor.stop_screenshot_collection(job))

    def test_foreground_guard_job_is_retained_when_join_times_out(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        stop_event = Mock()
        thread = Mock()
        thread.is_alive.side_effect = [True, False]
        job = (stop_event, thread)
        executor._foreground_guard = job

        self.assertFalse(executor._stop_foreground_guard())
        self.assertIs(executor._foreground_guard, job)
        self.assertTrue(executor.foreground_guard_stop_failed)

        self.assertTrue(executor._stop_foreground_guard())
        self.assertIsNone(executor._foreground_guard)
        self.assertEqual(stop_event.set.call_count, 2)

    def test_new_stall_collection_discards_previous_phase_events(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["stall_watchdog"] = {
            "enabled": True,
            "mode": "observe",
            "sample_interval_seconds": 5,
            "unchanged_seconds": 30,
            "startup_grace_seconds": 0,
            "max_hash_distance": 6,
        }
        executor._stall_events.append({"component": "stale/.Page"})

        job = executor.start_screenshot_collection(
            str(Path(executor.report_dir) / "screenshots"), 30
        )
        self.assertEqual(executor.stall_detection_count, 0)
        self.assertTrue(executor.stop_screenshot_collection(job))

    def test_stall_watchdog_records_degraded_probe_health(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["stall_watchdog"] = {
            "enabled": True,
            "mode": "observe",
            "sample_interval_seconds": 5,
            "unchanged_seconds": 30,
            "startup_grace_seconds": 0,
            "max_hash_distance": 6,
            "max_probe_failures": 1,
        }
        executor._pressure_active.set()
        with patch(
            "core.executor.subprocess.run", side_effect=OSError("transport failed")
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            deadline = time.monotonic() + 2
            while (
                executor.stall_watchdog_status["state"] != "degraded"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(executor.stop_screenshot_collection(job))

        status = executor.stall_watchdog_status
        self.assertEqual(status["state"], "degraded")
        self.assertGreaterEqual(status["total_probe_failures"], 1)
        self.assertEqual(status["degraded_periods"][0]["stage"], "screenshot_capture")

    def test_pressure_engine_preflight_records_verified_classpath(self):
        executor = self._executor("/sdcard/monkeyq.jar:/sdcard/framework.jar")
        with patch.object(executor, "_verify_fastbot_files") as verify:
            result = executor.preflight_pressure_engine()

        verify.assert_called_once()
        self.assertTrue(result["success"])
        self.assertEqual(result["classpath"], runtime_remote_path())

    def test_stop_during_capture_cannot_record_a_late_stall(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["stall_watchdog"] = {
            "enabled": True,
            "mode": "observe",
            "sample_interval_seconds": 5,
            "unchanged_seconds": 30,
            "startup_grace_seconds": 0,
            "max_hash_distance": 6,
        }
        executor._last_foreground_component = "com.example.app/.MainActivity"
        executor._pressure_active.set()
        capture_started = threading.Event()
        release_capture = threading.Event()
        stop_result = {}
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )

        def capture(*_args, **_kwargs):
            capture_started.set()
            release_capture.wait(timeout=2)
            return Mock(returncode=0, stdout=b"fake-png", stderr=b"")

        with patch("core.executor.subprocess.run", side_effect=capture), patch(
            "core.executor.fingerprint_png", return_value=fingerprint
        ), patch("core.executor.StallDetector.observe", return_value=observation):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            self.assertTrue(capture_started.wait(timeout=2))
            stopper = threading.Thread(
                target=lambda: stop_result.setdefault(
                    "clean", executor.stop_screenshot_collection(job)
                )
            )
            stopper.start()
            self.assertTrue(job.wait(timeout=2))
            release_capture.set()
            stopper.join(timeout=2)

        self.assertFalse(stopper.is_alive())
        self.assertTrue(stop_result.get("clean"))
        self.assertEqual(executor.stall_detection_count, 0)

    def test_stop_signal_is_not_blocked_by_stall_evidence_write(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["stall_watchdog"] = {
            "enabled": True,
            "mode": "observe",
            "sample_interval_seconds": 5,
            "unchanged_seconds": 30,
            "startup_grace_seconds": 0,
            "max_hash_distance": 6,
        }
        executor._last_foreground_component = "com.example.app/.MainActivity"
        executor._pressure_active.set()
        write_started = threading.Event()
        release_write = threading.Event()
        stop_result = {}
        fingerprint = ImageFingerprint(value=1, bit_count=256)
        observation = StallObservation(
            component="com.example.app/.MainActivity",
            fingerprint=fingerprint,
            hash_distance=0,
            unchanged_seconds=30,
            state_changed=False,
            detected=True,
        )

        def write_evidence(_path, content):
            write_started.set()
            release_write.wait(timeout=2)
            return len(content)

        with patch(
            "core.executor.subprocess.run",
            return_value=Mock(returncode=0, stdout=b"fake-png", stderr=b""),
        ), patch(
            "core.executor.fingerprint_png", return_value=fingerprint
        ), patch(
            "core.executor.StallDetector.observe", return_value=observation
        ), patch(
            "pathlib.Path.write_bytes", autospec=True, side_effect=write_evidence
        ):
            job = executor.start_screenshot_collection(
                str(Path(executor.report_dir) / "screenshots"), 30
            )
            self.assertTrue(write_started.wait(timeout=2))
            stopper = threading.Thread(
                target=lambda: stop_result.setdefault(
                    "clean", executor.stop_screenshot_collection(job)
                )
            )
            stopper.start()
            self.assertTrue(job.wait(timeout=0.5))
            release_write.set()
            stopper.join(timeout=2)

        self.assertFalse(stopper.is_alive())
        self.assertTrue(stop_result.get("clean"))
        self.assertEqual(executor.consume_stall_events(), [])

    def test_running_screenshot_job_is_retained_after_stop_timeout(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        event = threading.Event()
        thread = Mock()
        thread.is_alive.return_value = True
        executor._screenshot_jobs[id(event)] = (event, thread)

        self.assertFalse(executor.stop_screenshot_collection(event))
        self.assertTrue(event.is_set())
        self.assertIn(id(event), executor._screenshot_jobs)
        self.assertTrue(executor.screenshot_collection_failed)
        self.assertTrue(executor.screenshot_collection_active)
        thread.join.assert_called_once_with(
            timeout=executor.SCREENSHOT_STOP_TIMEOUT_SECONDS
        )
        executor._screenshot_jobs.clear()

    def test_detection_count_is_not_limited_by_evidence_cap(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._stall_detection_total = 23
        executor._stall_events.extend(
            {"component": "com.example.app/.Page%s" % index}
            for index in range(20)
        )

        self.assertEqual(executor.stall_detection_count, 23)
        self.assertEqual(len(executor.consume_stall_events()), 20)
        self.assertEqual(executor.stall_detection_count, 0)

    def test_explicit_log_statistics_are_parsed(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        (Path(executor.report_dir) / "fastbot.log").write_text(
            "Events injected: 17\ncoverage: 42.5%\n", encoding="utf-8"
        )
        self.assertEqual(
            executor.collect_fastbot_statistics(),
            {"operation_count": 17, "coverage_rate": 42.5},
        )

    def test_interrupted_monkey_progress_is_counted(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        (Path(executor.report_dir) / "fastbot.log").write_text(
            "// Sending event #100\n// Sending event #400\n", encoding="utf-8"
        )
        self.assertEqual(
            executor.collect_fastbot_statistics(), {"operation_count": 400}
        )

    def test_fastbot_event_id_progress_is_counted(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        (Path(executor.report_dir) / "fastbot.log").write_text(
            "Event id: 1\naction type: CLICK\nEvent id: 29\n", encoding="utf-8"
        )
        self.assertEqual(
            executor.collect_fastbot_statistics(), {"operation_count": 29}
        )

    def test_extra_args_cannot_change_target_package(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["fastbot"]["extra_args"] = ["--package=com.other.app"]
        with self.assertRaises(ValueError):
            executor.start_fastbot(1, 10)

    def test_system_monkey_is_explicit_and_targeted(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["fastbot"] = {
            "engine": "system-monkey",
            "extra_args": [],
            "event_percentages": {
                "touch": 70,
                "motion": 25,
                "pinchzoom": 5,
            },
        }

        class FakeProcess:
            returncode = None

            def poll(self):
                return None

        process = FakeProcess()
        try:
            with patch(
                "core.executor.subprocess.Popen", return_value=process
            ) as start, patch.object(
                executor, "_start_foreground_guard"
            ) as guard, patch.object(executor, "_prepare_pressure_engine_start"):
                with patch.object(
                    executor,
                    "_resolve_launch_component",
                    return_value="com.example.app/.MainActivity",
                ), patch.object(
                    executor,
                    "_resolve_permission_controller_package",
                    return_value="com.android.permissioncontroller",
                ), patch.object(
                    executor,
                    "_permission_controller_packages_for_monkey",
                    return_value=[
                        "com.android.permissioncontroller",
                        "com.lbe.security.miui",
                    ],
                ):
                    self.assertIs(executor.start_fastbot(1, 500), process)
            command = start.call_args.args[0]
            remote_command = command[-1]
            self.assertEqual(command[command.index("-s") + 1], "offline-device")
            self.assertIn("monkey", remote_command)
            self.assertIn("com.example.app", remote_command)
            self.assertIn(
                "-p com.example.app -p com.android.permissioncontroller",
                remote_command,
            )
            self.assertIn("-p com.lbe.security.miui", remote_command)
            self.assertIn("--pct-touch 70", remote_command)
            self.assertIn("--pct-motion 25", remote_command)
            self.assertIn("--pct-pinchzoom 5", remote_command)
            self.assertIn("--pct-appswitch 0", remote_command)
            self.assertIn("--throttle 500", remote_command)
            self.assertTrue(remote_command.endswith(" 100000"))
            guard.assert_called_once_with(process, recover_audit=True)
        finally:
            executor._close_process_file(process)

    def test_fastbot_starts_audit_foreground_guard_even_without_scenario_block(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = Mock()
        process.poll.return_value = None
        with patch.object(
            executor, "_prepare_pressure_engine_start"
        ), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(
            executor, "_push_fastbot_config"
        ), patch.object(
            executor, "_spawn_detached_fastbot", return_value=process
        ), patch.object(
            executor, "_start_foreground_guard"
        ) as guard:
            self.assertIs(executor.start_fastbot(1, 250), process)

        guard.assert_called_once_with(
            process,
            recover_audit=True,
            interval_seconds=executor.FASTBOT_FOREGROUND_GUARD_INTERVAL_SECONDS,
            allow_ui_dump=False,
            startup_grace_seconds=0.0,
            audit_startup_grace_seconds=(
                executor.FASTBOT_FOREGROUND_GUARD_STARTUP_GRACE_SECONDS
            ),
        )

    def test_fastbot_permission_allowlist_uses_installed_packages_with_length_budget(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._permission_controller_package = "com.android.permissioncontroller"
        installed = [
            "com.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.google.android.permissioncontroller",
            "com.google.android.packageinstaller",
            "com.lbe.security.miui",
            "com.miui.securitycenter",
            "com.huawei.systemmanager",
            "com.vivo.permissionmanager2",
            "com.coloros.securitypermission",
            "com.oplus.permissioncontroller",
            "com.samsung.android.permissioncontroller",
        ]
        with patch.object(
            executor, "_permission_controller_packages_for_monkey", return_value=installed
        ):
            packages = executor._permission_controller_packages_for_fastbot()

        self.assertIn("com.android.permissioncontroller", packages)
        self.assertIn("com.lbe.security.miui", packages)
        self.assertNotIn("com.miui.securitycenter", packages)
        self.assertNotIn("com.huawei.systemmanager", packages)
        output_component = (
            "/sdcard/fastbot-"
            + executor.package_name
            + "-".join(packages)
            + "-running-minutes-999"
        )
        self.assertLessEqual(len(output_component.rsplit("/", 1)[-1]), 255)

    def test_fastbot_uses_random_touch_gestures_without_reuse_agent(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["fastbot"]["event_percentages"] = {
            "touch": 70,
            "motion": 25,
            "pinchzoom": 5,
        }
        process = Mock()
        process.poll.return_value = None
        with patch.object(
            executor, "_prepare_pressure_engine_start"
        ), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(
            executor, "_push_fastbot_config"
        ), patch.object(
            executor, "_remove_stale_fastbot_output", return_value=True
        ), patch.object(
            executor, "_spawn_detached_fastbot", return_value=process
        ) as spawn, patch.object(
            executor, "_verify_fastbot_startup", return_value=None
        ), patch.object(
            executor, "_start_foreground_guard"
        ):
            self.assertIs(executor.start_fastbot(1, 250), process)

        remote_command = spawn.call_args.args[0]
        self.assertNotIn("--agent", remote_command)
        self.assertNotIn("reuseq", remote_command)
        self.assertIn("--pct-touch 70", remote_command)
        self.assertIn("--pct-motion 25", remote_command)
        self.assertIn("--pct-pinchzoom 5", remote_command)
        self.assertIn("--pct-nav 0", remote_command)
        self.assertIn("--pct-appswitch 0", remote_command)
        self.assertIn("--pct-rotation 0", remote_command)
        self.assertIn("--running-minutes 1", remote_command)
        self.assertIn("--throttle 250", remote_command)

    def test_strict_startup_input_interruption_keeps_recovery_intent(self):
        executor = self._executor(runtime_remote_path())
        executor.config["input_safety"] = {"enabled": True, "default_text": "test text"}
        process = Mock()
        process.poll.return_value = 137

        def interrupted(*_args):
            executor.input_recovery_reason = "keyboard visibility is unknown"
            executor._foreground_recovery_stop_requested = True
            return "Fastbot exited during startup with return code 137"

        with patch.object(executor, "_prepare_pressure_engine_start"), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(executor, "_push_fastbot_config"), patch.object(
            executor, "_remove_stale_fastbot_output", return_value=True
        ), patch.object(executor, "_spawn_detached_fastbot", return_value=process) as spawn, patch.object(
            executor, "_verify_fastbot_startup", side_effect=interrupted
        ), patch.object(executor, "_start_foreground_guard"), patch.object(
            executor, "_cleanup_failed_fastbot_start"
        ) as cleanup:
            self.assertIs(executor.start_fastbot(1, 250), process)
        spawn.assert_called_once()
        cleanup.assert_not_called()
        command = spawn.call_args.args[0]
        self.assertIn("--agent reuseq", command)
        self.assertIn("-Dapp.press.targetPackage=com.example.app", command)
        self.assertIn("-Dapp.press.inputText=test text", command)

    def test_strict_mode_refuses_system_nodes_and_cached_coordinates(self):
        executor = self._executor(runtime_remote_path())
        executor.config["input_safety"] = {"enabled": True}
        executor._cached_audit_tap_center = (300, 300)
        executor._cached_confirm_tap_center = (400, 400)
        executor._cached_privacy_tap_center = (500, 500)
        with patch.object(executor, "_run_adb") as adb:
            self.assertFalse(executor._tap_ui_node(ET.fromstring(
                '<node package="com.keyboard" bounds="[0,100][800,900]"/>'
            )))
            self.assertFalse(executor._tap_cached_audit_controls(threading.Event()))
            self.assertFalse(executor._tap_cached_confirm_control(threading.Event()))
            self.assertFalse(executor._tap_cached_privacy_control(threading.Event()))
        adb.assert_not_called()

    def test_fastbot_is_detached_from_the_adb_shell(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        launch = Mock(returncode=0, stdout="4321\n", stderr="")
        with patch("core.executor.subprocess.run", return_value=launch) as run:
            process = executor._spawn_detached_fastbot(
                "CLASSPATH=/sdcard/monkeyq.jar exec app_process /system/bin "
                "com.android.commands.monkey.Monkey -p com.example.app",
                "Fastbot",
            )

        self.assertEqual(process.remote_pid, "4321")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-s") + 1], "offline-device")
        self.assertIn("setsid sh -c", command[-1])
        self.assertIn("app_process", command[-1])
        self.assertIn("app_press_test_fastbot_offline-device_", command[-1])
        self.assertIn(".status", command[-1])
        self.assertIn(".pid", command[-1])
        self.assertIn(".intent", command[-1])
        self.assertTrue(str(process.remote_intent_path).endswith(".intent"))
        self.assertIn("echo $$", command[-1])
        self.assertIn("rc=$?", command[-1])

    def test_detached_fastbot_poll_reads_remote_exit_status(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = _DetachedRemoteProcess(
            "4321",
            "/sdcard/fastbot.log",
            "/sdcard/fastbot.log.status",
            poller=executor._poll_detached_fastbot,
        )
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=0, stdout="RUNNING\n", stderr=""),
                Mock(returncode=0, stdout="7\n", stderr=""),
            ],
        ):
            self.assertIsNone(process.poll())
            process._next_poll_at = 0
            self.assertEqual(process.poll(), 7)

    def test_detached_fastbot_poll_handles_empty_status_after_kill(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = _DetachedRemoteProcess(
            "4321",
            "/sdcard/fastbot.log",
            "/sdcard/fastbot.log.status",
            poller=executor._poll_detached_fastbot,
        )
        with patch.object(
            executor, "_run_adb", return_value=Mock(returncode=0, stdout="EXITED\n")
        ) as adb:
            self.assertEqual(process.poll(), 1)

        probe = adb.call_args.args[0][-1]
        self.assertIn("[ -s /sdcard/fastbot.log.status ]", probe)
        self.assertIn("/proc/4321/stat", probe)
        self.assertIn("Z|X) echo EXITED", probe)

    def test_fastbot_heartbeat_does_not_count_as_injected_events(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        log = Path(executor.report_dir) / "fastbot.log"
        log.write_text(
            "=== Fastbot started (detached) ===\n"
            "Wait Event for 150 milliseconds\n"
            "// Sending event #100\n"
            "// Sending event #200\n",
            encoding="utf-8",
        )
        self.assertNotIn("operation_count", executor.collect_fastbot_statistics())
        with log.open("a", encoding="utf-8") as output:
            output.write("// Event id: 47\n")
        self.assertEqual(executor.collect_fastbot_statistics()["operation_count"], 47)

    def test_deadline_stop_signals_tracked_detached_process_group(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        signals = []

        def poller(_process):
            return 130 if signals else None

        process = _DetachedRemoteProcess(
            "4321",
            "/sdcard/fastbot.log",
            "/sdcard/fastbot.log.status",
            poller=poller,
            signaler=lambda _process, value: signals.append(value),
        )
        executor.mark_pressure_deadline_reached()
        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=True,
        ), patch.object(executor, "_collect_detached_fastbot_log", return_value=True), patch.object(
            executor, "_wait_for_detached_fastbot_exit", return_value=True
        ):
            self.assertTrue(executor.stop_fastbot(process))

        self.assertEqual(signals, [2])

    def test_detached_fastbot_kill_uses_numeric_sigkill_when_unavailable(self):
        process = _DetachedRemoteProcess("4321", "/sdcard/fastbot.log")
        sent = []
        process._signaler = lambda _process, value: sent.append(value)
        had_sigkill = hasattr(signal, "SIGKILL")
        original_sigkill = getattr(signal, "SIGKILL", None)
        try:
            if had_sigkill:
                delattr(signal, "SIGKILL")
            process.kill()
        finally:
            if had_sigkill:
                setattr(signal, "SIGKILL", original_sigkill)
        self.assertEqual(sent, [9])

    def test_fastbot_startup_retries_only_stale_uiautomation_error(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        first = Mock()
        second = Mock()
        with patch.object(
            executor, "_prepare_pressure_engine_start"
        ), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(
            executor, "_push_fastbot_config"
        ), patch.object(
            executor, "_remove_stale_fastbot_output", return_value=True
        ), patch.object(
            executor, "_spawn_detached_fastbot", side_effect=[first, second]
        ) as spawn, patch.object(
            executor,
            "_verify_fastbot_startup",
            side_effect=["UiAutomationService already registered!", None],
        ), patch(
            "core.executor.MutexController._stop_remote_uiautomator_processes",
            return_value=True,
        ), patch(
            "core.executor.MutexController._wait_for_uiautomation_release",
            return_value=True,
        ), patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch("core.executor.time.sleep"):
            self.assertIs(executor.start_fastbot(1, 300), second)

        self.assertEqual(spawn.call_count, 2)

    def test_fastbot_startup_retries_vendor_sigkill_after_clean_handoff(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        first = Mock()
        second = Mock()
        with patch.object(
            executor, "_prepare_pressure_engine_start"
        ), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(
            executor, "_push_fastbot_config"
        ), patch.object(
            executor, "_remove_stale_fastbot_output", return_value=True
        ), patch.object(
            executor, "_spawn_detached_fastbot", side_effect=[first, second]
        ) as spawn, patch.object(
            executor,
            "_verify_fastbot_startup",
            side_effect=[
                "Fastbot exited during startup with return code 137.\nKilled",
                None,
            ],
        ), patch.object(
            executor, "_cleanup_failed_fastbot_start", return_value=True
        ), patch.object(
            executor, "_start_foreground_guard"
        ), patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._stop_remote_uiautomator_processes",
            return_value=True,
        ), patch(
            "core.executor.MutexController._wait_for_uiautomation_release",
            return_value=True,
        ), patch("core.executor.time.sleep"):
            self.assertIs(executor.start_fastbot(1, 300), second)

        self.assertEqual(spawn.call_count, 2)

    def test_fastbot_startup_sigkill_does_not_retry_after_manual_stop(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = Mock()
        with patch.object(
            executor, "_prepare_pressure_engine_start"
        ), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(
            executor, "_push_fastbot_config"
        ), patch.object(
            executor, "_remove_stale_fastbot_output", return_value=True
        ), patch.object(
            executor, "_spawn_detached_fastbot", return_value=process
        ) as spawn, patch.object(
            executor,
            "_verify_fastbot_startup",
            return_value="Fastbot exited during startup with return code 137.",
        ), patch.object(
            executor, "_cleanup_failed_fastbot_start", return_value=True
        ), patch.object(
            executor, "_start_foreground_guard"
        ), patch(
            "core.executor.MutexController.has_stop_request",
            side_effect=[True, False],
        ), patch(
            "core.executor.MutexController._stop_remote_uiautomator_processes"
        ) as stop_uiautomator:
            with self.assertRaisesRegex(PressureEngineError, "manual stop"):
                executor.start_fastbot(1, 300)

        spawn.assert_called_once()
        stop_uiautomator.assert_not_called()

    def test_fastbot_startup_does_not_retry_unrelated_engine_error(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = Mock()
        with patch.object(
            executor, "_prepare_pressure_engine_start"
        ), patch.object(
            executor, "_verify_fastbot_files"
        ), patch.object(
            executor, "_push_fastbot_config"
        ), patch.object(
            executor, "_remove_stale_fastbot_output", return_value=True
        ), patch.object(
            executor, "_spawn_detached_fastbot", return_value=process
        ) as spawn, patch.object(
            executor,
            "_verify_fastbot_startup",
            return_value="Fastbot exited: cannot load jar",
        ):
            with self.assertRaises(PressureEngineError):
                executor.start_fastbot(1, 300)

        spawn.assert_called_once()

    def test_runtime_permission_controller_is_resolved_from_system_intent(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        result = Mock(
            returncode=0,
            stdout=(
                "priority=1 preferredOrder=0\n"
                "com.android.permissioncontroller/.permission.ui.GrantPermissionsActivity\n"
            ),
            stderr="",
        )
        with patch.object(executor, "_run_adb", return_value=result) as adb:
            self.assertEqual(
                executor._resolve_permission_controller_package(),
                "com.android.permissioncontroller",
            )
        self.assertIn(
            "android.content.pm.action.REQUEST_PERMISSIONS",
            adb.call_args.args[0],
        )

    def test_installed_vendor_permission_controller_is_added(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        def adb_result(arguments, **_kwargs):
            if arguments[-1] == "com.lbe.security.miui":
                return Mock(
                    returncode=0,
                    stdout="package:/system/priv-app/MiuiPackageInstaller.apk\n",
                )
            return Mock(returncode=1, stdout="", stderr="not found")

        with patch.object(
            executor,
            "_resolve_permission_controller_package",
            return_value="com.android.permissioncontroller",
        ), patch.object(executor, "_run_adb", side_effect=adb_result):
            packages = executor._permission_controller_packages_for_monkey()

        self.assertEqual(
            packages,
            ["com.android.permissioncontroller", "com.lbe.security.miui"],
        )

    def test_privileged_vendor_manager_is_excluded_from_pressure_allowlist(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        def adb_result(arguments, **_kwargs):
            if arguments[-1] == "com.lbe.security.miui":
                return Mock(
                    returncode=0,
                    stdout="package:/system/priv-app/MiuiPackageInstaller.apk\n",
                )
            return Mock(returncode=1, stdout="", stderr="not found")

        with patch.object(
            executor,
            "_resolve_permission_controller_package",
            return_value="com.miui.securitycenter",
        ), patch.object(executor, "_run_adb", side_effect=adb_result):
            packages = executor._permission_controller_packages_for_monkey()

        self.assertEqual(packages, ["com.lbe.security.miui"])

    def test_launcher_visible_huawei_manager_is_excluded_from_pressure_allowlist(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._permission_controller_package = "com.huawei.systemmanager"

        with patch.object(
            executor,
            "_permission_controller_packages_for_monkey",
            return_value=[
                "com.huawei.systemmanager",
                "com.android.permissioncontroller",
            ],
        ):
            packages = executor._permission_controller_packages_for_fastbot()

        self.assertEqual(packages, ["com.android.permissioncontroller"])
        self.assertNotIn("com.miui.securitycenter", packages)

    def test_foreground_probe_extracts_top_package(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        result = Mock(
            stdout=(
                "topResumedActivity=ActivityRecord{abc u0 "
                "com.example.app/.MainActivity t1}"
            )
        )
        with patch.object(executor, "_run_adb", return_value=result):
            self.assertEqual(executor._current_foreground_package(), "com.example.app")

    def test_foreground_probe_extracts_top_component(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        result = Mock(
            stdout=(
                "topResumedActivity=ActivityRecord{abc u0 "
                "com.android.permissioncontroller/.permission.ui.GrantPermissionsActivity t1}"
            )
        )
        with patch.object(executor, "_run_adb", return_value=result):
            self.assertEqual(
                executor._current_foreground_component(),
                "com.android.permissioncontroller/"
                "com.android.permissioncontroller.permission.ui.GrantPermissionsActivity",
            )

    def test_foreground_probe_accepts_vivo_colon_format(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        result = Mock(
            stdout=(
                "mResumedActivity: ActivityRecord{739e76f u0 "
                "com.example.app/com.example.shared.MainActivity d0 s501 t503}"
            )
        )

        with patch.object(executor, "_run_adb", return_value=result) as probe:
            component = executor._current_foreground_component()

        self.assertEqual(
            component,
            "com.example.app/com.example.shared.MainActivity",
        )
        probe.assert_called_once()

    def test_foreground_probe_falls_back_to_window_dumpsys(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        results = [
            Mock(stdout="Activity display has no resumed marker"),
            Mock(
                stdout=(
                    "mCurrentFocus=Window{2f613bd u0 "
                    "com.example.app/.MainActivity}"
                )
            ),
        ]

        with patch.object(executor, "_run_adb", side_effect=results) as probe:
            component = executor._current_foreground_component()

        self.assertEqual(
            component,
            "com.example.app/com.example.app.MainActivity",
        )
        self.assertEqual(probe.call_count, 2)

    def test_bounded_foreground_probe_does_not_run_unbounded_fallbacks(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        calls = []

        def blocked_probe(_arguments, timeout=0, retries=1):
            calls.append(float(timeout))
            raise subprocess.TimeoutExpired("adb", timeout)

        with patch.object(executor, "_run_adb", side_effect=blocked_probe):
            started = time.monotonic()
            component = executor._current_foreground_component(timeout=0.2)
            elapsed = time.monotonic() - started

        self.assertIsNone(component)
        self.assertLess(elapsed, 1.0)
        self.assertTrue(calls)
        self.assertLessEqual(max(calls), 0.2)

    def test_zero_timeout_foreground_wait_uses_one_lightweight_probe(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        stop_event = threading.Event()
        with patch.object(
            executor, "_current_foreground_component", return_value=None
        ) as probe:
            self.assertIsNone(
                executor._wait_for_target_foreground(stop_event, timeout=0.0)
            )

        probe.assert_called_once()
        self.assertLessEqual(
            float(probe.call_args.kwargs["timeout"]),
            executor.FOREGROUND_ZERO_WAIT_PROBE_SECONDS,
        )

    def test_foreground_guard_ignores_one_transient_launcher_probe(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = Mock()
        process.poll.return_value = None
        launcher = "com.android.launcher/com.android.launcher.Launcher"
        target = "com.example.app/com.example.app.MainActivity"
        with patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[launcher, target],
        ), patch.object(executor, "_restore_target_foreground") as restore:
            executor._start_foreground_guard(
                process,
                interval_seconds=0.2,
                startup_grace_seconds=0.0,
            )
            time.sleep(0.55)
            executor._stop_foreground_guard()

        restore.assert_not_called()

    def test_permission_dialog_is_allowed_but_settings_is_not(self):
        self.assertTrue(
            TestExecutor._is_permission_dialog_component(
                "com.android.permissioncontroller/.permission.ui.GrantPermissionsActivity"
            )
        )
        self.assertTrue(
            TestExecutor._is_permission_dialog_component(
                "com.lbe.security.miui/com.android.packageinstaller.permission.ui.GrantPermissionsActivity"
            )
        )
        self.assertFalse(
            TestExecutor._is_permission_dialog_component(
                "com.android.permissioncontroller/.permission.ui.ManagePermissionsActivity"
            )
        )
        self.assertFalse(
            TestExecutor._is_permission_dialog_component(
                "com.miui.home/.launcher.Launcher"
            )
        )

    def test_miui_privacy_dialog_is_not_runtime_permission_dialog(self):
        component = (
            "com.miui.securitycenter/"
            "com.miui.permcenter.permissions.SystemAppPermissionDialogActivity"
        )
        self.assertTrue(TestExecutor._is_privacy_dialog_component(component))
        self.assertFalse(TestExecutor._is_permission_dialog_component(component))

    def test_miui_privacy_button1_is_selected_and_button2_is_ignored(self):
        deny = ET.fromstring(
            '<node resource-id="android:id/button2" text="不同意" '
            'enabled="true" bounds="[80,2076][520,2216]" />'
        )
        title = ET.fromstring(
            '<node resource-id="com.miui.securitycenter:id/alertTitle" '
            'text="用户须知" enabled="true" bounds="[440,997][640,1064]" />'
        )
        allow = ET.fromstring(
            '<node resource-id="android:id/button1" text="同意" '
            'enabled="true" bounds="[559,2076][1000,2216]" />'
        )
        self.assertIs(
            TestExecutor._find_privacy_accept_node(
                [deny, title, allow],
                {"privacy_resource_id": ""},
            ),
            allow,
        )

    def test_permission_allow_all_wins_over_honor_selected_photo_action(self):
        selected = ET.fromstring(
            '<node resource-id="com.android.permissioncontroller:id/'
            'permission_allow_selected_button" text="选择部分照片和视频" '
            'enabled="true" bounds="[144,1848][936,1980]" />'
        )
        allow_all = ET.fromstring(
            '<node resource-id="com.android.permissioncontroller:id/'
            'permission_allow_all_button" text="允许访问全部照片和视频" '
            'enabled="true" bounds="[144,2004][936,2136]" />'
        )
        self.assertIs(
            TestExecutor._find_permission_allow_node([selected, allow_all]),
            allow_all,
        )

    def test_permission_allow_all_matches_miui_short_media_label(self):
        deny = ET.fromstring(
            '<node text="拒绝" enabled="true" bounds="[101,1771][979,1911]" />'
        )
        selected = ET.fromstring(
            '<node text="选择部分" enabled="true" bounds="[101,1944][979,2084]" />'
        )
        allow_all = ET.fromstring(
            '<node text="允许访问全部" enabled="true" '
            'bounds="[101,2117][979,2257]" />'
        )

        self.assertIs(
            TestExecutor._find_permission_allow_node([deny, selected, allow_all]),
            allow_all,
        )

    def test_permission_dialog_briefly_pauses_and_resumes_monkey(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        stop_event = Mock()
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", side_effect=[["123"], ["123"]]
        ) as remote_signal:
            executor._settle_permission_dialog(stop_event)
        self.assertEqual(
            remote_signal.call_args_list,
            [call(["123"], 19), call(["123"], 18)],
        )
        stop_event.wait.assert_called_once_with(2)

    def test_permission_dialog_positive_button_is_granted(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        allow = ET.fromstring(
            '<node resource-id="com.android.permissioncontroller:id/'
            'permission_allow_foreground_only_button" text="" '
            'enabled="true" bounds="[10,20][110,120]" />'
        )
        stop_event = Mock()
        stop_event.wait.return_value = False
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", side_effect=[["123"], ["123"]]
        ) as signal_pids, patch.object(
            executor, "_dump_ui_nodes", return_value=[allow]
        ), patch.object(
            executor, "_tap_ui_node", return_value=True
        ) as tap:
            self.assertTrue(executor._grant_runtime_permission_dialog(stop_event))

        self.assertEqual(signal_pids.call_args_list, [call(["123"], 19), call(["123"], 18)])
        tap.assert_called_once_with(allow)
        stop_event.wait.assert_called_once_with(0.5)

    def test_permission_allow_node_matches_vendor_label_and_skips_deny_action(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        deny = ET.fromstring(
            '<node text="\u7981\u6b62" enabled="true" '
            'bounds="[10,20][110,120]" />'
        )
        allow = ET.fromstring(
            '<node text="\u4ec5\u4f7f\u7528\u671f\u95f4\u5141\u8bb8" '
            'enabled="true" bounds="[120,20][300,120]" />'
        )

        self.assertIs(TestExecutor._find_permission_allow_node([deny, allow]), allow)

    def test_stop_fastbot_resumes_permission_handoff_pids(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._permission_recovery_paused_pids = ["123", "456"]
        process = Mock()
        process.poll.return_value = None
        process.returncode = 0
        process.wait.return_value = 0
        with patch.object(
            executor, "_resume_paused_remote_pids", return_value=({"123", "456"}, set())
        ) as resume, patch.object(
            executor, "_collect_detached_fastbot_log", return_value=True
        ), patch.object(
            executor, "_stop_foreground_guard", return_value=True
        ), patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=True,
        ):
            self.assertTrue(executor.stop_fastbot(process))

        resume.assert_called_once_with(["123", "456"])
        self.assertEqual(executor._permission_recovery_paused_pids, [])

    def test_runtime_permission_recovery_resumes_after_handoff_dialog_disappears(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._permission_recovery_component = (
            "com.lbe.security.miui/"
            "com.android.packageinstaller.permission.ui.GrantPermissionsActivity"
        )
        target = "com.example.app/com.example.app.MainActivity"
        clock = _PermissionRecoveryClock()
        with patch("core.executor.time.monotonic", side_effect=lambda: clock.now), patch.object(
            executor, "_foreground_component_probe", return_value=target
        ), patch.object(executor, "_dump_ui_nodes") as dump, patch.object(
            executor, "_tap_ui_node"
        ) as tap, patch.object(
            executor, "capture_permission_recovery_evidence", return_value={"artifacts": {}}
        ) as capture:
            result = executor.recover_runtime_permission_dialogs(
                stop_event=clock, timeout=3.0, output_dir="recovery"
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(result["dialogs_seen"], 0)
        self.assertEqual(result["last_component"], target)
        self.assertEqual(
            capture.call_args.kwargs["recovery"]["completion_reason"],
            "dialog_disappeared_without_confirmed_tap",
        )
        dump.assert_not_called()
        tap.assert_not_called()

    def test_runtime_permission_recovery_does_not_resume_without_foreground_confirmation(self):
        permission = (
            "com.android.permissioncontroller/"
            "com.android.permissioncontroller.permission.ui.GrantPermissionsActivity"
        )
        target = "com.example.app/com.example.app.MainActivity"
        external = "com.android.settings/com.android.settings.Settings"
        allow = ET.fromstring(
            '<node resource-id="com.android.permissioncontroller:id/'
            'permission_allow_foreground_only_button" enabled="true" '
            'bounds="[10,20][110,120]" />'
        )
        for initial_component, observations, click_permission in (
            (permission, (target, target, None), False),
            (permission, (target, target, external), False),
            (None, (target,), False),
            (external, (target,), False),
            (permission, (target, target, None), True),
            (permission, (target, target, external), True),
        ):
            with self.subTest(
                initial_component=initial_component,
                observations=observations,
                click_permission=click_permission,
            ):
                executor = self._executor("/sdcard/monkeyq.jar")
                executor._permission_recovery_component = initial_component
                clock = _PermissionRecoveryClock()
                components = itertools.chain(
                    (permission,) if click_permission else (),
                    itertools.cycle(observations),
                )
                with patch("core.executor.time.monotonic", side_effect=lambda: clock.now), patch.object(
                    executor, "_foreground_component_probe", side_effect=components
                ), patch.object(
                    executor, "_dump_ui_nodes", return_value=[allow] if click_permission else []
                ), patch.object(
                    executor, "_tap_ui_node", return_value=True
                ) as tap, patch.object(
                    executor, "capture_permission_recovery_evidence", return_value={"artifacts": {}}
                ):
                    result = executor.recover_runtime_permission_dialogs(
                        stop_event=clock, timeout=3.0
                    )

                self.assertFalse(result["success"], result)
                self.assertEqual(tap.call_count, int(click_permission))

    def test_runtime_permission_recovery_handles_two_dialogs(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.PERMISSION_RECOVERY_POLL_SECONDS = 0.2
        permission = (
            "com.android.permissioncontroller/"
            "com.android.permissioncontroller.permission.ui.GrantPermissionsActivity"
        )
        target = "com.example.app/com.example.app.MainActivity"
        allow_one = ET.fromstring(
            '<node resource-id="com.android.permissioncontroller:id/'
            'permission_allow_foreground_only_button" enabled="true" '
            'bounds="[10,20][110,120]" />'
        )
        allow_two = ET.fromstring(
            '<node resource-id="com.android.permissioncontroller:id/'
            'permission_allow_one_time_button" enabled="true" '
            'bounds="[10,20][110,120]" />'
        )

        clock = _PermissionRecoveryClock()
        components = iter([permission, permission, permission, target, target, target, target, target])
        with patch("core.executor.time.monotonic", side_effect=lambda: clock.now), patch.object(
            executor, "_foreground_component_probe", side_effect=lambda _timeout: next(components)
        ), patch.object(
            executor, "_dump_ui_nodes", side_effect=[[allow_one], [allow_two]]
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap, patch.object(
            executor, "capture_permission_recovery_evidence", return_value={"artifacts": {}}
        ):
            result = executor.recover_runtime_permission_dialogs(
                stop_event=clock, timeout=3.0, output_dir="recovery"
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["dialogs_seen"], 2)
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["completion_reason"], "target_returned_after_tap")
        self.assertEqual(tap.call_count, 2)

    def test_runtime_permission_recovery_failure_keeps_dump_diagnostics(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        permission = (
            "com.lbe.security.miui/"
            "com.android.packageinstaller.permission.ui.GrantPermissionsActivity"
        )

        clock = _PermissionRecoveryClock()

        def dump_without_button(_device=None):
            executor._last_audit_ui_dump_succeeded = False
            executor._last_ui_dump_error = "uiautomator dump failed"
            return []

        evidence = {"artifacts": {"ui_hierarchy": "recovery/ui_hierarchy.xml"}}
        with patch("core.executor.time.monotonic", side_effect=lambda: clock.now), patch.object(
            executor, "_foreground_component_probe", return_value=permission
        ), patch.object(executor, "_dump_ui_nodes", side_effect=dump_without_button), patch.object(
            executor, "capture_permission_recovery_evidence", return_value=evidence
        ) as capture:
            result = executor.recover_runtime_permission_dialogs(
                stop_event=clock, timeout=3.0
            )

        self.assertFalse(result["success"])
        self.assertIn("error", result)
        capture.assert_called_once()
        self.assertEqual(result["evidence"], evidence)

    def test_audit_activity_component_is_normalized(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "com.example.app/.SplashActivity",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            }
        }
        self.assertTrue(
            executor._is_audit_activity(
                "com.example.app/com.example.app.SplashActivity"
            )
        )

    def test_generic_audit_recovery_uses_hierarchy_without_activity(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            }
        }
        self.assertTrue(
            executor._is_audit_activity("com.example.app/com.example.app.AnyPage")
        )
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="" enabled="true" bounds="[10,20][110,120]" />'
        )
        with patch.object(executor, "_dump_ui_nodes", return_value=[audit]):
            self.assertTrue(executor._audit_control_visible())

    def test_audit_guard_respects_explicit_disabled_configuration(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {"enabled": False, "activity": ""}
        }
        self.assertIsNone(executor._audit_recovery_options())

    def test_audit_guard_defaults_to_enabled_for_legacy_configuration(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {}
        self.assertEqual(
            {
                "activity": "",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            },
            executor._audit_recovery_options(),
        )

    def test_foreground_guard_recovers_generic_gate_on_any_target_activity(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            }
        }
        process = Mock()
        process.poll.side_effect = [None, 0]
        with patch.object(
            executor, "_current_foreground_component", return_value="com.example.app/com.example.app.PrivacyPage"
        ), patch.object(executor, "_audit_control_visible", return_value=True), patch.object(
            executor, "_recover_audit_page", return_value=True
        ) as recover:
            executor._start_foreground_guard(process, recover_audit=True, interval_seconds=0.2)
            time.sleep(0.45)
            executor._stop_foreground_guard()
        recover.assert_called_once()

    def test_audit_page_recovery_pauses_clicks_and_resumes_monkey(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "com.example.app/.SplashActivity",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            }
        }
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="审核中" enabled="true" bounds="[10,20][110,120]" />'
        )
        confirm = ET.fromstring(
            '<node resource-id="" text="确定" enabled="true" '
            'bounds="[120,220][320,320]" />'
        )
        stop_event = Mock()
        stop_event.wait.return_value = False
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", side_effect=[["123"], ["123"]]
        ) as signal_pids, patch.object(
            executor,
            "_dump_ui_nodes",
            side_effect=[[audit], [confirm], []],
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap:
            self.assertTrue(executor._recover_audit_page(stop_event))

        self.assertEqual(executor.audit_recovery_count, 1)
        self.assertEqual(signal_pids.call_args_list, [call(["123"], 19), call(["123"], 18)])
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(stop_event.wait.call_args_list, [call(0.5), call(1.0)])

    def test_audit_gate_is_completed_before_fastbot_starts(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "",
                "confirm_text": "",
            }
        }
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="审核中" enabled="true" bounds="[10,20][110,120]" />'
        )
        confirm = ET.fromstring(
            '<node text="确定" enabled="true" bounds="[120,220][320,320]" />'
        )
        hierarchies = iter([[audit], [confirm]])

        def dump_nodes():
            executor._last_audit_ui_dump_succeeded = True
            return next(hierarchies, [])

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=dump_nodes
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap:
            self.assertTrue(executor.prepare_audit_gate(timeout=2))
        self.assertEqual(tap.call_count, 2)

    def test_audit_prepare_retries_failed_dump_and_handles_following_privacy(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="审核中" enabled="true" bounds="[10,20][110,120]" />'
        )
        confirm = ET.fromstring(
            '<node text="确定" enabled="true" bounds="[120,220][320,320]" />'
        )
        privacy = ET.fromstring(
            '<node resource-id="com.example.app:id/btn_submit" '
            'text="同意并继续" enabled="true" bounds="[40,400][360,500]" />'
        )
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        observations = iter(
            [
                ([audit], True),
                ([confirm], True),
                ([], False),
                ([privacy], True),
            ]
        )

        def dump_nodes():
            nodes, succeeded = next(observations, ([home], True))
            executor._last_audit_ui_dump_succeeded = succeeded
            return nodes

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=dump_nodes
        ), patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.example.app/.SplashActivity",
                "com.example.app/.PrivacyActivity",
            ],
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap:
            self.assertTrue(executor.prepare_audit_gate(timeout=3))

        self.assertEqual(
            [audit, confirm, privacy],
            [item.args[0] for item in tap.call_args_list],
        )

    def test_audit_prepare_leaves_in_app_agreement_page(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        title = ET.fromstring(
            '<node package="com.example.app" text="《用户协议》" '
            'enabled="true" bounds="[360,120][720,220]" />'
        )
        home = ET.fromstring(
            '<node package="com.example.app" text="首页" '
            'enabled="true" bounds="[0,0][400,600]" />'
        )
        observations = iter([[title], [home]])

        def dump_nodes():
            executor._last_audit_ui_dump_succeeded = True
            return next(observations, [home])

        with patch.object(executor, "_dump_ui_nodes", side_effect=dump_nodes), patch.object(
            executor,
            "_run_adb",
            return_value=Mock(returncode=0, stdout="", stderr=""),
        ) as adb, patch("core.executor.time.sleep"):
            self.assertTrue(executor.prepare_audit_gate(timeout=2))

        self.assertTrue(
            any(
                call_args.args[0][-2:] == ["keyevent", "4"]
                for call_args in adb.call_args_list
            )
        )

    def test_audit_prepare_never_treats_failed_dumps_as_a_clear_page(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        def failed_dump():
            executor._last_audit_ui_dump_succeeded = False
            return []

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=failed_dump
        ), patch(
            "core.executor.time.monotonic",
            side_effect=[0.0, 0.0, 0.2, 0.4, 0.6, 0.8, 1.1],
        ), patch("core.executor.time.sleep"):
            self.assertFalse(executor.prepare_audit_gate(timeout=1))

    def test_audit_prepare_retries_when_dump_helper_raises(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        calls = 0

        def dump_nodes():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("ADB hierarchy command timed out")
            executor._last_audit_ui_dump_succeeded = True
            return [home]

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=dump_nodes
        ), patch(
            "core.executor.time.monotonic",
            side_effect=[0.0, 0.0, 0.1, 0.1, 0.1, 1.2, 1.2],
        ), patch("core.executor.time.sleep"):
            self.assertTrue(executor.prepare_audit_gate(timeout=2))

        self.assertEqual(3, calls)

    def test_audit_prepare_uses_adb_hierarchy_after_stale_device_dump(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor.config["scenario"] = {
            "audit_recovery": {
                "enabled": True,
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "确定",
            }
        }
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="审核中" enabled="true" bounds="[10,20][110,120]" />'
        )
        confirm = ET.fromstring(
            '<node text="确定" enabled="true" bounds="[120,220][320,320]" />'
        )
        privacy = ET.fromstring(
            '<node resource-id="com.example.app:id/btn_submit" '
            'text="同意并继续" enabled="true" bounds="[40,400][360,500]" />'
        )
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        primary_calls = []

        def primary_dump(device=None):
            primary_calls.append(device)
            if len(primary_calls) == 1:
                executor._last_audit_ui_dump_succeeded = True
                return [audit]
            if len(primary_calls) == 2:
                executor._last_audit_ui_dump_succeeded = True
                return [confirm]
            executor._last_audit_ui_dump_succeeded = False
            raise RuntimeError("stale UiAutomation session")

        fallback_calls = []

        def adb_dump(**_kwargs):
            fallback_calls.append(True)
            executor._last_audit_ui_dump_succeeded = True
            return [privacy] if len(fallback_calls) == 1 else [home]

        device = object()
        with patch.object(
            executor, "_dump_ui_nodes", side_effect=primary_dump
        ), patch.object(
            executor, "_dump_ui_nodes_from_adb", side_effect=adb_dump
        ), patch.object(
            executor, "_tap_ui_node", return_value=True
        ) as tap:
            self.assertTrue(executor.prepare_audit_gate(timeout=3, device=device))

        self.assertGreaterEqual(len(fallback_calls), 1)
        self.assertEqual([audit, confirm, privacy], [item.args[0] for item in tap.call_args_list])

    def test_privacy_tap_failure_is_retried_and_verified(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        privacy = ET.fromstring(
            '<node resource-id="com.example.app:id/btn_submit" '
            'text="同意并继续" enabled="true" bounds="[40,400][360,500]" />'
        )
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        hierarchies = iter([[privacy], [home]])

        def dump_nodes():
            executor._last_audit_ui_dump_succeeded = True
            return next(hierarchies, [home])

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=dump_nodes
        ), patch.object(
            executor, "_tap_ui_node", side_effect=[False, True]
        ) as tap, patch("core.executor.time.sleep"):
            self.assertTrue(executor.prepare_audit_gate(timeout=3))

        self.assertEqual(2, tap.call_count)

    def test_audit_privacy_chain_is_completed_before_fastbot_starts(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="审核中" enabled="true" bounds="[10,20][110,120]" />'
        )
        confirm = ET.fromstring(
            '<node text="确定" enabled="true" bounds="[120,220][320,320]" />'
        )
        privacy = ET.fromstring(
            '<node resource-id="com.example.app:id/btn_submit" '
            'text="同意并继续" enabled="true" bounds="[40,400][360,500]" />'
        )
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        hierarchies = iter([[audit], [confirm], [privacy]])

        def dump_nodes():
            executor._last_audit_ui_dump_succeeded = True
            return next(hierarchies, [home])

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=dump_nodes
        ), patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.example.app/.SplashActivity",
                "com.example.app/.PrivacyActivity",
            ],
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap:
            self.assertTrue(executor.prepare_audit_gate(timeout=3))

        self.assertEqual([audit, confirm, privacy], [item.args[0] for item in tap.call_args_list])

    def test_checked_audit_and_confirm_on_same_page_do_not_toggle_radio(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        audit = ET.fromstring(
            '<node resource-id="com.example.app:id/auditRadioButton" '
            'text="审核中" checked="true" enabled="true" '
            'bounds="[10,20][110,120]" />'
        )
        confirm = ET.fromstring(
            '<node text="确定" enabled="true" bounds="[120,220][320,320]" />'
        )
        privacy = ET.fromstring(
            '<node resource-id="com.example.app:id/btn_submit" '
            'text="同意并继续" enabled="true" bounds="[40,400][360,500]" />'
        )
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        hierarchies = iter([[audit, confirm], [privacy]])

        def dump_nodes():
            executor._last_audit_ui_dump_succeeded = True
            return next(hierarchies, [home])

        with patch.object(
            executor, "_dump_ui_nodes", side_effect=dump_nodes
        ), patch.object(
            executor,
            "_current_foreground_component",
            return_value="com.example.app/.PrivacyActivity",
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap:
            self.assertTrue(executor.prepare_audit_gate(timeout=3))

        self.assertEqual([confirm, privacy], [item.args[0] for item in tap.call_args_list])

    def test_privacy_gate_reappearing_during_pressure_is_recovered(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        privacy = ET.fromstring(
            '<node resource-id="com.example.app:id/btn_submit" '
            'text="同意并继续" enabled="true" bounds="[40,400][360,500]" />'
        )
        home = ET.fromstring(
            '<node resource-id="com.example.app:id/home" '
            'text="首页" enabled="true" bounds="[0,0][400,600]" />'
        )
        stop_event = Mock()
        stop_event.wait.return_value = False
        executor._last_foreground_component = "com.example.app/.PrivacyActivity"
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", side_effect=[["123"], ["123"]]
        ) as signal_pids, patch.object(
            executor, "_dump_ui_nodes", side_effect=[[privacy], [home]]
        ), patch.object(executor, "_tap_ui_node", return_value=True) as tap:
            self.assertTrue(executor._recover_audit_page(stop_event))

        tap.assert_called_once_with(privacy)
        self.assertEqual(signal_pids.call_args_list, [call(["123"], 19), call(["123"], 18)])
        self.assertEqual(executor.audit_recovery_count, 1)

    def test_foreground_restore_uses_explicit_target_component(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        resolve = Mock(
            returncode=0,
            stdout=(
                "priority=0\n"
                "com.example.app/.MainActivity\n"
            ),
            stderr="",
        )
        start = Mock(returncode=0, stdout="Starting", stderr="")
        with patch.object(executor, "_run_adb", side_effect=[resolve, start]) as adb:
            self.assertTrue(executor._relaunch_target())
        start_arguments = adb.call_args_list[1].args[0]
        self.assertIn("-n", start_arguments)
        self.assertIn("com.example.app/.MainActivity", start_arguments)
        self.assertEqual(executor.foreground_restore_count, 1)

    def test_foreground_restore_pauses_engine_and_waits_for_target(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        stop_event = threading.Event()
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", side_effect=[["123"], ["123"]]
        ) as signal_pids, patch.object(
            executor, "_relaunch_target", return_value=True
        ) as relaunch, patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.miui.home/com.miui.home.launcher.Launcher",
                "com.example.app/com.example.app.MainActivity",
            ],
        ), patch.object(executor, "_audit_recovery_options", return_value=None):
            self.assertTrue(executor._restore_target_foreground(stop_event))

        relaunch.assert_called_once_with(timeout=12)
        self.assertEqual(
            signal_pids.call_args_list,
            [call(["123"], 19), call(["123"], 18)],
        )
        self.assertFalse(executor.foreground_restore_failed)
        self.assertIsNone(executor.foreground_restore_error)

    def test_relaunch_timeout_does_not_kill_engine_when_target_reaches_foreground(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        stop_event = threading.Event()
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", side_effect=[["123"], ["123"]]
        ) as signal_pids, patch.object(
            executor, "_relaunch_target", side_effect=TimeoutError("am start timed out")
        ) as relaunch, patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.miui.home/com.miui.home.launcher.Launcher",
                "com.example.app/com.example.app.MainActivity",
            ],
        ), patch.object(executor, "_audit_recovery_options", return_value=None):
            self.assertTrue(executor._restore_target_foreground(stop_event))

        relaunch.assert_called_once_with(timeout=12)
        self.assertEqual(
            signal_pids.call_args_list,
            [call(["123"], 19), call(["123"], 18)],
        )
        self.assertNotIn(call(["123"], 9), signal_pids.call_args_list)

    def test_emergency_foreground_handoff_marks_engine_stop_as_recoverable(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        stop_event = threading.Event()
        target = "com.example.app/com.example.app.MainActivity"
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor,
            "_signal_remote_pids",
            side_effect=[["123"], ["123"]],
        ) as signal_pids, patch.object(
            executor,
            "_relaunch_target",
            side_effect=[TimeoutError("am start timed out"), True],
        ), patch.object(
            executor,
            "_wait_for_target_foreground",
            side_effect=[None, None, target],
        ), patch.object(
            executor, "_resume_paused_remote_pids", return_value=(set(), set())
        ), patch.object(executor, "_audit_recovery_options", return_value=None):
            self.assertTrue(executor._restore_target_foreground(stop_event))

        self.assertIn(call(["123"], 9), signal_pids.call_args_list)
        self.assertTrue(executor.foreground_recovery_requested)
        self.assertFalse(executor.foreground_restore_failed)

    def test_intentional_foreground_handoff_stop_is_not_engine_failure(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._foreground_recovery_requested = True
        executor._foreground_recovery_stop_requested = True

        class StoppedProcess:
            returncode = 137

            def poll(self):
                return self.returncode

        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=True,
        ):
            self.assertTrue(executor.stop_fastbot(StoppedProcess()))

        self.assertEqual(executor.pressure_stop_reason, "foreground_recovery")
        self.assertIsNone(executor.pressure_stop_error)

    def test_detached_stop_intent_classifies_137_after_flag_race(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = _DetachedRemoteProcess(
            "4321",
            "/sdcard/fastbot.log",
            "/sdcard/fastbot.log.status",
            remote_intent_path="/sdcard/fastbot.log.intent",
        )
        process.returncode = 137
        process.stop_intent_reason = "foreground_recovery"
        process.stop_intent_at = "2026-09-02T12:40:38+00:00"

        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=True,
        ), patch.object(executor, "_collect_detached_fastbot_log", return_value=True), patch.object(
            executor, "_wait_for_detached_fastbot_exit", return_value=True
        ):
            self.assertTrue(executor.stop_fastbot(process))

        self.assertEqual(executor.pressure_stop_reason, "foreground_recovery")
        self.assertIsNone(executor.pressure_stop_error)

    def test_stall_stop_intent_only_classifies_its_own_detached_phase(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        previous = _DetachedRemoteProcess("4321", "/sdcard/previous.log")
        current = _DetachedRemoteProcess("5432", "/sdcard/current.log")
        previous.returncode = 137
        current.returncode = 137

        with patch.object(
            executor, "_run_adb", return_value=Mock(returncode=0, stdout="")
        ), patch.object(
            executor, "_read_remote_pressure_stop_intent", return_value=None
        ), patch.object(
            executor, "_wait_for_detached_fastbot_exit", return_value=True
        ), patch.object(
            executor, "_collect_detached_fastbot_log", return_value=True
        ), patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running", return_value=True
        ):
            executor._record_pressure_stop_intent("stall_recovery", previous)
            # Simulate losing the in-memory and remote intent during a stop.
            previous.stop_intent_reason = None
            executor._pressure_stop_intent_reason = None
            self.assertTrue(executor.stop_fastbot(previous))
            self.assertEqual(executor.pressure_stop_reason, "stall_recovery")
            self.assertIsNone(executor.pressure_stop_error)

            executor._pressure_stop_intent_reason = None
            self.assertFalse(executor.stop_fastbot(current))
            self.assertIn("return code 137", executor.pressure_stop_error)

    def test_detached_wrapper_must_exit_before_handoff(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = _DetachedRemoteProcess("4321", "/sdcard/current.log")
        process.returncode = 137
        process.stop_intent_reason = "stall_recovery"
        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running", return_value=True
        ), patch.object(
            executor, "_wait_for_detached_fastbot_exit", return_value=False
        ), patch.object(executor, "_collect_detached_fastbot_log", return_value=True):
            self.assertFalse(executor.stop_fastbot(process))

        self.assertFalse(executor.pressure_stop_verified)
        self.assertIn("wrapper did not settle", executor.pressure_stop_error)

    def test_detached_wrapper_probe_does_not_trust_cached_status(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        process = _DetachedRemoteProcess("4321", "/sdcard/current.log")
        process.returncode = 137
        with patch.object(
            executor,
            "_run_adb",
            side_effect=[
                Mock(returncode=0, stdout="RUNNING\n"),
                Mock(returncode=0, stdout="EXITED\n"),
            ],
        ) as adb, patch("core.executor.time.sleep"):
            self.assertTrue(executor._wait_for_detached_fastbot_exit(process))
        self.assertEqual(adb.call_count, 2)

    def test_unowned_137_remains_a_pressure_engine_failure(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        class StoppedProcess:
            returncode = 137

            def poll(self):
                return self.returncode

        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=True,
        ):
            self.assertFalse(executor.stop_fastbot(StoppedProcess()))

        self.assertIsNone(executor.pressure_stop_reason)
        self.assertIn("Fastbot exited with return code 137", executor.pressure_stop_error)

    def test_custom_fastbot_guard_requests_handoff_without_ui_dump(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._cached_audit_activity = (
            "com.example.app/com.example.app.SplashActivity"
        )
        executor._cached_audit_tap_center = (200, 300)
        process = Mock()
        process.poll.side_effect = [None, None, None, 0]
        component = "com.example.app/com.example.app.SplashActivity"
        with patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.example.app/com.example.app.MainActivity",
                component,
                component,
            ],
        ), patch.object(
            executor, "request_audit_recovery", return_value=True
        ) as request, patch.object(
            executor, "_dump_ui_nodes"
        ) as dump, patch.object(
            executor, "_recover_audit_page"
        ) as recover:
            executor._start_foreground_guard(
                process,
                recover_audit=True,
                allow_ui_dump=False,
                interval_seconds=0.2,
                audit_startup_grace_seconds=0.35,
            )
            time.sleep(0.85)
            executor._stop_foreground_guard()

        request.assert_called_once_with(component)
        dump.assert_not_called()
        recover.assert_not_called()

    def test_custom_fastbot_guard_ignores_transient_splash_during_startup(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        component = "com.example.app/com.example.app.SplashActivity"
        target = "com.example.app/com.example.app.MainActivity"
        executor._cached_audit_activity = component
        executor._cached_audit_tap_center = (200, 300)
        process = Mock()
        process.poll.return_value = None
        with patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[target, component, component, target, target],
        ), patch.object(
            executor, "request_audit_recovery", return_value=True
        ) as request:
            executor._start_foreground_guard(
                process,
                recover_audit=True,
                allow_ui_dump=False,
                interval_seconds=0.2,
                audit_startup_grace_seconds=0.75,
            )
            time.sleep(1.1)
            executor._stop_foreground_guard()

        request.assert_not_called()

    def test_custom_fastbot_guard_ignores_cached_activity_without_control_evidence(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        executor._cached_audit_activity = (
            "com.example.app/com.example.app.SplashActivity"
        )
        process = Mock()
        process.poll.side_effect = [None, None, None, 0]
        component = "com.example.app/com.example.app.SplashActivity"
        with patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.example.app/com.example.app.MainActivity",
                component,
                component,
            ],
        ), patch.object(
            executor, "request_audit_recovery", return_value=True
        ) as request:
            executor._start_foreground_guard(
                process,
                recover_audit=True,
                allow_ui_dump=False,
                interval_seconds=0.2,
            )
            time.sleep(0.85)
            executor._stop_foreground_guard()

        request.assert_not_called()

    def test_foreground_restore_transfers_frozen_engine_to_audit_handoff(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        component = "com.example.app/com.example.app.SplashActivity"
        executor._cached_audit_activity = component
        executor._cached_audit_tap_center = (200, 300)
        stop_event = threading.Event()
        with patch.object(
            executor, "_remote_system_monkey_pids", return_value=["123"]
        ), patch.object(
            executor, "_signal_remote_pids", return_value=["123"]
        ) as signal_pids, patch.object(
            executor, "_relaunch_target", return_value=True
        ), patch.object(
            executor,
            "_current_foreground_component",
            side_effect=[
                "com.android.launcher/com.android.launcher.Launcher",
                component,
            ],
        ):
            self.assertTrue(
                executor._restore_target_foreground(
                    stop_event, allow_ui_dump=False
                )
            )

        self.assertEqual(signal_pids.call_args_list, [call(["123"], 19)])
        self.assertTrue(executor.claim_audit_recovery_request())
        self.assertEqual(executor._audit_recovery_paused_pids, ["123"])

    def test_deadline_stop_is_not_reported_as_engine_failure(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        class StoppedProcess:
            returncode = None

            def poll(self):
                return None

            def send_signal(self, _signal):
                self.returncode = 1

            def wait(self, timeout=None):
                del timeout
                return self.returncode

        process = StoppedProcess()
        executor.mark_pressure_deadline_reached()
        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running", return_value=True
        ):
            self.assertTrue(executor.stop_fastbot(process))

    def test_resume_ignores_pids_that_exit_before_sigcont(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        with patch.object(executor, "_signal_remote_pids", return_value=[]), patch.object(
            executor, "_live_remote_pids", return_value=set()
        ):
            resumed, unresolved = executor._resume_paused_remote_pids(["123"])
        self.assertEqual(set(), resumed)
        self.assertEqual(set(), unresolved)

    def test_resume_keeps_confirmed_live_pids_as_failures(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        with patch.object(executor, "_signal_remote_pids", return_value=[]), patch.object(
            executor, "_live_remote_pids", return_value={"123"}
        ):
            resumed, unresolved = executor._resume_paused_remote_pids(["123"])
        self.assertEqual(set(), resumed)
        self.assertEqual({"123"}, unresolved)

    def test_start_refuses_when_previous_engine_cannot_be_verified_stopped(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        with patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=False,
        ), patch.object(executor, "_verify_fastbot_files") as verify:
            with self.assertRaisesRegex(RuntimeError, "previous Fastbot process"):
                executor.start_fastbot(1, 10)
        verify.assert_not_called()

    def test_stop_reports_failure_when_device_engine_remains(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        class StoppedProcess:
            returncode = 0

            def poll(self):
                return 0

        with patch(
            "core.executor.MutexController.has_stop_request", return_value=False
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=False,
        ):
            self.assertFalse(executor.stop_fastbot(StoppedProcess()))
        self.assertIn(
            "device-side Fastbot process is still running",
            executor.pressure_stop_error,
        )
        self.assertFalse(executor.pressure_stop_verified)

    def test_manual_stop_is_recorded_as_expected_and_consumed(self):
        executor = self._executor("/sdcard/monkeyq.jar")

        class ManuallyStoppedProcess:
            returncode = 137

            def poll(self):
                return 137

        with patch(
            "core.executor.MutexController.has_stop_request", return_value=True
        ), patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            return_value=True,
        ), patch(
            "core.executor.MutexController.clear_stop_request", return_value=True
        ):
            self.assertTrue(executor.stop_fastbot(ManuallyStoppedProcess()))
        self.assertEqual(executor.pressure_stop_reason, "manual")
        self.assertTrue(executor.pressure_stop_verified)

    def test_manual_stop_arriving_during_cleanup_wins_host_failure(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        marker = {"present": False}

        class LateManuallyStoppedProcess:
            returncode = None
            terminated = False

            def poll(self):
                return self.returncode

            def send_signal(self, _signal):
                pass

            def wait(self, timeout=None):
                if timeout == 8:
                    raise subprocess.TimeoutExpired("fastbot", timeout)
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 137

        process = LateManuallyStoppedProcess()

        def finish_device_cleanup():
            marker["present"] = True
            return True

        with patch(
            "core.executor.MutexController.has_stop_request",
            side_effect=lambda: marker["present"],
        ) as has_stop_request, patch(
            "core.executor.MutexController._kill_fastbot_if_running",
            side_effect=finish_device_cleanup,
        ), patch(
            "core.executor.MutexController.clear_stop_request", return_value=True
        ) as clear_stop_request:
            self.assertTrue(executor.stop_fastbot(process))

        self.assertTrue(process.terminated)
        self.assertEqual(has_stop_request.call_count, 2)
        clear_stop_request.assert_called_once_with()
        self.assertEqual(executor.pressure_stop_reason, "manual")

    def test_artifact_output_cannot_be_a_network_location(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        with self.assertRaises(ValueError):
            executor.set_report_dir("https://example.invalid/reports")

    def test_logcat_collection_uses_recent_tail_and_records_marker_boundary(self):
        executor = self._executor("/sdcard/monkeyq.jar")
        log_path = Path(executor.report_dir) / "logcat.log"
        process = Mock()
        process.poll.return_value = 0
        success = Mock(returncode=0, stdout="", stderr="")

        with patch.object(
            executor, "_run_adb", side_effect=[success, success]
        ) as adb, patch.object(
            executor, "_wait_for_logcat_boundary", return_value=321
        ) as boundary, patch(
            "core.executor.subprocess.Popen", return_value=process
        ) as popen:
            self.assertIs(executor.start_logcat_collection(str(log_path)), process)

        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-T") + 1], "1")
        marker_command = adb.call_args_list[1].args[0]
        self.assertEqual(marker_command[:5], ["shell", "log", "-p", "i", "-t"])
        self.assertIn(executor.LOGCAT_BOUNDARY_TAG, marker_command)
        boundary.assert_called_once()
        self.assertEqual(executor.get_logcat_capture_offset(process), 321)
        self.assertTrue(executor.stop_logcat_collection(process))

    def test_logcat_boundary_offset_is_after_old_buffer_and_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "logcat.log"
            old = b"old am_kill com.example.app\n"
            marker = "app_press_test_marker"
            path.write_bytes(old + marker.encode("utf-8") + b"\ncurrent line\n")

            offset = TestExecutor._find_logcat_boundary_offset(path, marker, 0)

        self.assertEqual(offset, len(old) + len(marker.encode("utf-8")) + 1)

    def test_blocking_window_is_dismissed_with_one_controlled_back(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor._resumed_activity_component = Mock(
            return_value="com.example.app/com.example.app.ui.MainActivity"
        )
        executor._foreground_component_probe = Mock(
            return_value="com.miui.phrase/com.miui.phrase.PhraseActivity"
        )
        executor._run_adb = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
        executor._wait_for_target_foreground = Mock(
            return_value="com.example.app/com.example.app.ui.MainActivity"
        )
        stop_event = threading.Event()

        self.assertTrue(executor._dismiss_blocking_window(stop_event))
        # A successful dismissal clears the counter so a later, unrelated
        # blocking window still gets its own attempts.
        self.assertEqual(0, executor._blocking_window_dismiss_count)
        self.assertFalse(executor.blocking_window_blocked())
        self.assertTrue(
            any(
                call.args[0][:4] == ["shell", "input", "keyevent", "BACK"]
                for call in executor._run_adb.call_args_list
            )
        )

    def test_blocking_window_ignores_a_different_foreground_app(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor._resumed_activity_component = Mock(
            return_value="com.other.app/com.other.app.MainActivity"
        )
        executor._run_adb = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))

        self.assertFalse(executor._dismiss_blocking_window(threading.Event()))
        executor._run_adb.assert_not_called()

    def test_blocking_window_ignores_focus_owned_by_the_target(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor._resumed_activity_component = Mock(
            return_value="com.example.app/com.example.app.ui.MainActivity"
        )
        executor._foreground_component_probe = Mock(
            return_value="com.example.app/com.example.app.ui.EditorActivity"
        )
        executor._run_adb = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))

        self.assertFalse(executor._dismiss_blocking_window(threading.Event()))
        executor._run_adb.assert_not_called()

    def test_blocking_window_stops_the_cycle_after_the_limit(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor._resumed_activity_component = Mock(
            return_value="com.example.app/com.example.app.ui.MainActivity"
        )
        executor._foreground_component_probe = Mock(
            return_value="com.miui.phrase/com.miui.phrase.PhraseActivity"
        )
        executor._run_adb = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
        executor._wait_for_target_foreground = Mock(return_value=None)
        stop_event = threading.Event()

        self.assertFalse(executor._dismiss_blocking_window(stop_event))
        self.assertFalse(executor._dismiss_blocking_window(stop_event))

        self.assertTrue(executor.blocking_window_blocked())
        self.assertEqual(
            TestExecutor.BLOCKING_WINDOW_DISMISS_LIMIT,
            executor._blocking_window_dismiss_count,
        )
        # A blocked window makes the guard request an intentional stop rather
        # than relaunching the app forever.
        self.assertFalse(executor._restore_target_foreground(stop_event))
        self.assertTrue(executor._foreground_recovery_stop_requested)

    def test_vendor_dialog_window_is_detected_from_the_window_list(self):
        executor = self._executor("/sdcard/monkey.apk")
        output = (
            "  Window #3 Window{abc u0 GrantPermissions}:\n"
            "    mOwnerUid=10150\n"
            "    package=com.lbe.security.miui\n"
        )
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout=output, stderr="")
        )

        self.assertEqual(
            "com.lbe.security.miui", executor._system_dialog_window_present()
        )

    def test_vendor_dialog_window_ignores_the_target_package(self):
        executor = self._executor("/sdcard/monkey.apk")
        output = "  Window #3 Window{abc u0 Main}:\n    package=com.example.app\n"
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout=output, stderr="")
        )

        self.assertIsNone(executor._system_dialog_window_present())

    def test_vendor_dialog_is_transient_only_inside_the_grace_window(self):
        executor = self._executor("/sdcard/monkey.apk")
        output = (
            "  Window #3 Window{abc u0 GrantPermissions}:\n"
            "    package=com.lbe.security.miui\n"
        )
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout=output, stderr="")
        )
        started = 1000.0

        with patch("core.executor.time.monotonic", return_value=started):
            self.assertTrue(executor._system_dialog_above_target())
        with patch("core.executor.time.monotonic", return_value=started + 5.0):
            self.assertTrue(executor._system_dialog_above_target())
        with patch(
            "core.executor.time.monotonic",
            return_value=started + executor.SYSTEM_DIALOG_GRACE_SECONDS + 1.0,
        ):
            self.assertFalse(executor._system_dialog_above_target())

    def test_restore_is_skipped_while_a_vendor_dialog_is_above_the_target(self):
        executor = self._executor("/sdcard/monkey.apk")
        output = (
            "  Window #3 Window{abc u0 GrantPermissions}:\n"
            "    package=com.lbe.security.miui\n"
        )
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout=output, stderr="")
        )

        self.assertFalse(executor._restore_target_foreground(threading.Event()))
        self.assertFalse(executor._foreground_recovery_requested)
        self.assertFalse(executor._foreground_restore_failed)

    def test_mute_device_audio_silences_ringer_and_media(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout="", stderr="")
        )

        self.assertTrue(executor.mute_device_audio())
        commands = [call.args[0] for call in executor._run_adb.call_args_list]

        self.assertTrue(
            any(
                command[:5] == ["shell", "cmd", "audio", "set-ringer-mode", "SILENT"]
                for command in commands
            )
        )
        self.assertTrue(
            any(
                command[:3] == ["shell", "cmd", "media_session"]
                and command[-1] == "0"
                for command in commands
            )
        )
        self.assertTrue(
            any(
                command[:5] == ["shell", "settings", "put", "global", "mode_ringer"]
                for command in commands
            )
        )

    def test_mute_device_audio_can_be_disabled_by_config(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor.config.setdefault("test", {})["keep_device_silent"] = False
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout="", stderr="")
        )

        self.assertFalse(executor.mute_device_audio())
        executor._run_adb.assert_not_called()

    def test_touch_overlay_is_disabled_by_default(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout="", stderr="")
        )

        self.assertTrue(executor.configure_touch_overlay())
        commands = [call.args[0] for call in executor._run_adb.call_args_list]

        self.assertEqual(2, len(commands))
        for command in commands:
            self.assertEqual(
                ["shell", "settings", "put", "system"], command[:4]
            )
            self.assertEqual("0", command[-1])

    def test_touch_overlay_can_be_enabled_by_config(self):
        executor = self._executor("/sdcard/monkey.apk")
        executor.config.setdefault("test", {})["touch_overlay"] = True
        executor._run_adb = Mock(
            return_value=Mock(returncode=0, stdout="", stderr="")
        )

        self.assertTrue(executor.configure_touch_overlay())
        commands = [call.args[0] for call in executor._run_adb.call_args_list]

        self.assertTrue(all(command[-1] == "1" for command in commands))
        self.assertEqual(
            {"pointer_location", "show_touches"},
            {command[-2] for command in commands},
        )


if __name__ == "__main__":
    unittest.main()
