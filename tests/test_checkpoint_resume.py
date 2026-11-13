from __future__ import annotations

import copy
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
from core import checkpoint


class CheckpointTests(unittest.TestCase):
    def _report(self):
        return {
            "run_id": "job-1",
            "device_serial": "serial-1",
            "package_name": "com.example.app",
            "resume_state": "running",
            "resume_attempts": 2,
            "cycles": [],
        }

    def test_heartbeat_writes_required_fields_and_clamps_interval(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            report = self._report()
            heartbeat = checkpoint.CheckpointHeartbeat(
                report_dir,
                report,
                interval_seconds=1,
                cycle_seconds=60,
                total_seconds=120,
                apk_fingerprint="abc123",
            )
            self.assertEqual(5.0, heartbeat.interval_seconds)
            heartbeat.begin_cycle(
                1, elapsed_seconds=12.5, operation_count=7, phase=3
            )
            heartbeat.update(
                phase_name="fastbot",
                fastbot_pid="1234",
                remote_log_path="/sdcard/fastbot.log",
                remote_log_offset=88,
                foreground_component="com.example.app/.MainActivity",
                last_error="adb timeout",
            )
            with patch.object(
                checkpoint, "atomic_write_text", wraps=checkpoint.atomic_write_text
            ) as atomic_write:
                value = heartbeat.flush()

            required = {
                "cycle",
                "phase",
                "elapsed_seconds",
                "operation_count",
                "fastbot_pid",
                "remote_log_path",
                "remote_log_offset",
                "foreground_component",
                "last_error",
                "apk_fingerprint",
                "saved_at",
                "last_heartbeat",
                "remaining_seconds",
            }
            self.assertTrue(required.issubset(value))
            self.assertEqual(1, value["cycle"])
            self.assertEqual(7, value["operation_count"])
            self.assertEqual(2, report["resume_attempts"])
            self.assertGreaterEqual(atomic_write.call_count, 2)
            persisted = json.loads(
                (report_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(value["last_heartbeat"], persisted["last_heartbeat"])
            self.assertFalse(list(report_dir.glob("*.tmp")))

    def test_process_alive_treats_windows_system_error_as_stale_pid(self):
        with patch.object(checkpoint.os, "name", "posix"), patch.object(
            checkpoint.os, "kill", side_effect=SystemError("WinError 87")
        ):
            self.assertFalse(checkpoint._process_alive(123456789))

    def test_windows_process_probe_reads_status_without_sending_signals(self):
        for exit_code, expected in ((259, True), (0, False)):
            with self.subTest(exit_code=exit_code):
                def fill_exit_code(_handle, pointer):
                    pointer._obj.value = exit_code
                    return 1

                kernel32 = Mock()
                kernel32.OpenProcess.return_value = 123
                kernel32.GetExitCodeProcess.side_effect = fill_exit_code
                with patch.object(checkpoint.os, "name", "nt"), patch.object(
                    ctypes, "WinDLL", return_value=kernel32, create=True
                ), patch.object(checkpoint.os, "kill") as kill:
                    self.assertIs(expected, checkpoint._process_alive(123456789))
                kill.assert_not_called()
                kernel32.OpenProcess.assert_called_once_with(0x1000, 0, 123456789)
                kernel32.CloseHandle.assert_called_once_with(123)

    def test_windows_process_probe_preserves_inaccessible_owner_claim(self):
        for last_error, expected in ((5, True), (87, False)):
            with self.subTest(last_error=last_error):
                kernel32 = Mock()
                kernel32.OpenProcess.return_value = None
                with patch.object(checkpoint.os, "name", "nt"), patch.object(
                    ctypes, "WinDLL", return_value=kernel32, create=True
                ), patch.object(
                    ctypes, "get_last_error", return_value=last_error, create=True
                ), patch.object(checkpoint.os, "kill") as kill:
                    self.assertIs(expected, checkpoint._process_alive(123456789))
                kill.assert_not_called()
                kernel32.CloseHandle.assert_not_called()

    def test_windows_process_probe_closes_handle_when_query_fails(self):
        kernel32 = Mock()
        kernel32.OpenProcess.return_value = 123
        kernel32.GetExitCodeProcess.return_value = 0
        with patch.object(checkpoint.os, "name", "nt"), patch.object(
            ctypes, "WinDLL", return_value=kernel32, create=True
        ), patch.object(checkpoint.os, "kill") as kill:
            self.assertFalse(checkpoint._process_alive(123456789))
        kill.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(os.name == "nt", "Windows process probe regression")
    def test_process_probe_does_not_terminate_its_own_process(self):
        # A separate hidden console keeps the pre-fix signal/exit confined to
        # this child, so the regression cannot interrupt pytest or live jobs.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        result = subprocess.run(
            [
                getattr(sys, "_base_executable", sys.executable), "-u", "-c",
                "import os; from core.checkpoint import _process_alive; "
                "print(_process_alive(os.getpid()), flush=True); "
                "print('probe survived', flush=True)",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("True", result.stdout)
        self.assertIn("probe survived", result.stdout)

    def test_heartbeat_records_fastbot_liveness_and_last_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = checkpoint.CheckpointHeartbeat(
                Path(temporary),
                self._report(),
                interval_seconds=10,
                cycle_seconds=60,
                total_seconds=60,
                apk_fingerprint="abc123",
            )
            heartbeat.begin_cycle(1)
            operation_count = {"value": 0}
            heartbeat.attach_fastbot(
                pid="1234",
                remote_log_path="/sdcard/fastbot.log",
                provider=lambda: {"fastbot_alive": True, "operation_count": operation_count["value"]},
            )
            operation_count["value"] = 2
            value = heartbeat.flush()
            self.assertTrue(value["fastbot_alive"])
            self.assertEqual(2, value["operation_count"])
            self.assertIsNotNone(value["last_operation_at"])

            heartbeat.update(fastbot_alive=False, fastbot_exit_code=1)
            value = heartbeat.snapshot()
            self.assertFalse(value["fastbot_alive"])
            self.assertEqual(1, value["fastbot_exit_code"])

    def test_clean_phase_clears_prior_engine_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = checkpoint.CheckpointHeartbeat(
                Path(temporary),
                self._report(),
                interval_seconds=10,
                cycle_seconds=60,
                total_seconds=60,
                apk_fingerprint="abc123",
            )
            heartbeat.begin_cycle(1)
            heartbeat.finish_phase(
                elapsed_seconds=10,
                operation_count=2,
                last_error="Fastbot exited with return code 137",
            )
            self.assertIn("137", heartbeat.snapshot()["last_error"])
            heartbeat.begin_phase(2, elapsed_seconds=10, operation_count=2)
            heartbeat.finish_phase(elapsed_seconds=60, operation_count=30)
            self.assertIsNone(heartbeat.snapshot()["last_error"])

    def test_completed_cycle_is_immutable(self):
        report = {
            "cycles": [
                {
                    "cycle": 1,
                    "status": "passed",
                    "completed": True,
                    "operation_count": 9,
                }
            ]
        }
        before = copy.deepcopy(report)
        changed = checkpoint.upsert_completed_cycle(
            report,
            {"cycle": 1, "status": "passed", "operation_count": 999},
        )
        self.assertFalse(changed)
        self.assertEqual(before, report)

    def test_manual_stop_gate_survives_a_late_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            report = self._report()
            heartbeat = checkpoint.CheckpointHeartbeat(
                report_dir,
                report,
                interval_seconds=10,
                cycle_seconds=60,
                total_seconds=60,
                apk_fingerprint="abc123",
            )
            heartbeat.begin_cycle(1, elapsed_seconds=5, operation_count=2, phase=1)
            heartbeat.flush()

            # Simulate the web console pausing a device while the old worker
            # is still alive in a reconnect wait.
            for name in ("report.json", "checkpoint.json"):
                path = report_dir / name
                value = json.loads(path.read_text(encoding="utf-8"))
                value.update(
                    {
                        "status": "paused",
                        "resume_state": "paused",
                        "manual_stop": True,
                        "last_error": "pressure stopped by web console",
                    }
                )
                checkpoint.write_json(path, value)

            heartbeat.flush()
            persisted_report = json.loads(
                (report_dir / "report.json").read_text(encoding="utf-8")
            )
            persisted_checkpoint = json.loads(
                (report_dir / "checkpoint.json").read_text(encoding="utf-8")
            )

        self.assertEqual("paused", persisted_report["resume_state"])
        self.assertTrue(persisted_report["manual_stop"])
        self.assertEqual("paused", persisted_checkpoint["resume_state"])
        self.assertTrue(persisted_checkpoint["manual_stop"])

    def test_manual_end_terminal_survives_a_late_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            report = self._report()
            heartbeat = checkpoint.CheckpointHeartbeat(
                report_dir,
                report,
                interval_seconds=10,
                cycle_seconds=60,
                total_seconds=60,
                apk_fingerprint="abc123",
            )
            heartbeat.begin_cycle(1, elapsed_seconds=5, operation_count=2, phase=1)
            heartbeat.flush()
            finished_at = "2026-09-14T01:02:03+00:00"
            for name in ("report.json", "checkpoint.json"):
                path = report_dir / name
                value = json.loads(path.read_text(encoding="utf-8"))
                value.update(
                    {
                        "status": "stopped",
                        "resume_state": "manually_ended",
                        "manual_end": True,
                        "manual_stop": True,
                        "finished_at": finished_at,
                        "remaining_seconds": 0,
                        "last_error": "device test ended manually from web console",
                    }
                )
                checkpoint.write_json(path, value)

            # This heartbeat was created before the web action and still has
            # an in-memory running state. It must not reopen the old task.
            heartbeat.update(
                write=False,
                resume_state="running",
                elapsed_seconds=20,
                operation_count=99,
            )
            heartbeat.flush()
            persisted_report = checkpoint.read_json(report_dir / "report.json")
            persisted_checkpoint = checkpoint.read_json(
                report_dir / "checkpoint.json"
            )

        self.assertEqual("manually_ended", persisted_report["resume_state"])
        self.assertTrue(persisted_report["manual_end"])
        self.assertEqual(finished_at, persisted_report["finished_at"])
        self.assertEqual(0.0, persisted_report["remaining_seconds"])
        self.assertEqual("manually_ended", persisted_checkpoint["resume_state"])
        self.assertEqual(0.0, persisted_checkpoint["remaining_seconds"])

    def test_stale_report_writer_preserves_durable_manual_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            durable = {
                **self._report(),
                "status": "stopped",
                "resume_state": "manually_ended",
                "manual_end": True,
                "manual_stop": True,
                "finished_at": "2026-09-14T01:02:03+00:00",
                "remaining_seconds": 0,
                "last_error": "device test ended manually from web console",
            }
            checkpoint.write_json(report_dir / "report.json", durable)
            checkpoint.write_json(
                report_dir / "checkpoint.json",
                {
                    "job_id": "job-1",
                    "device_serial": "serial-1",
                    "resume_state": "manually_ended",
                    "manual_end": True,
                    "remaining_seconds": 0,
                },
            )
            stale = {
                **self._report(),
                "status": "running",
                "resume_state": "recovering",
                "remaining_seconds": 40,
                "coverage": {"status": "incomplete"},
                "statistics": {},
                "artifacts": {},
                "issues": [],
                "crashes": [],
            }

            def write_json(value, path):
                checkpoint.write_json(path, value)
                return path

            with (
                patch.object(main.ReportGenerator, "write_all", return_value={}),
                patch.object(
                    main.ReportGenerator,
                    "write_artifact_manifest",
                    return_value=report_dir / "artifact_manifest.json",
                ),
                patch.object(
                    main.ReportGenerator, "write_json", side_effect=write_json
                ),
            ):
                main._persist_report(stale, report_dir)

            persisted = checkpoint.read_json(report_dir / "report.json")

        self.assertEqual("manually_ended", stale["resume_state"])
        self.assertEqual("stopped", stale["status"])
        self.assertTrue(stale["manual_end"])
        self.assertEqual(0.0, stale["remaining_seconds"])
        self.assertEqual("manually_ended", persisted["resume_state"])
        self.assertTrue(persisted["manual_end"])

    def test_run_test_on_manually_ended_device_is_a_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            config = {
                "app": {"package_name": "com.example.app"},
                "test": {"fastbot_duration_minutes": 1, "max_cycles": 1},
                "paths": {"reports_dir": str(reports_root)},
                "resume": {"enabled": True, "mode": "auto"},
                "logging": {"log_dir": str(Path(temporary) / "logs")},
            }
            report_dir = main.report_directory(config, "job-1", "serial-1")
            checkpoint.write_json(
                report_dir / "report.json",
                {
                    **self._report(),
                    "status": "stopped",
                    "resume_state": "manually_ended",
                    "manual_end": True,
                    "manual_stop": True,
                    "finished_at": "2026-09-14T01:02:03+00:00",
                    "remaining_seconds": 0,
                    "coverage": {"status": "stopped"},
                },
            )
            checkpoint.write_json(
                report_dir / "checkpoint.json",
                {
                    "job_id": "job-1",
                    "device_serial": "serial-1",
                    "resume_state": "manually_ended",
                    "manual_end": True,
                    "remaining_seconds": 0,
                },
            )
            checkpoint.request_manual_resume(report_dir, requested_by="test")
            request_path = checkpoint.recovery_request_path(report_dir)
            with (
                patch.object(main, "configure_logging"),
                patch.object(main, "close_logging_handlers"),
                patch.object(main, "require_local_apk") as require_apk,
                patch.object(main, "StateManager") as state_manager,
                patch.object(main, "TestExecutor") as executor,
            ):
                result = main.run_test_on_device(
                    "serial-1", config, run_id="job-1"
                )

            self.assertEqual("manually_ended", result["resume_state"])
            self.assertTrue(result["skipped_manual_end"])
            self.assertFalse(result["recoverable"])
            self.assertEqual(0.0, result["remaining_seconds"])
            self.assertTrue(request_path.is_file())
            require_apk.assert_not_called()
            state_manager.assert_not_called()
            executor.assert_not_called()

    def test_recovery_classifier_covers_automatic_and_manual_categories(self):
        cases = (
            ("device offline while polling", "waiting_for_device", True),
            ("adb command timed out", "waiting_for_device", True),
            ("Launcher foreground recovery timed out", "recovering", True),
            ("am start did not return target", "recovering", True),
            ("Fastbot exited with return code 1", "recovering", True),
            ("uiautomator2 control unavailable", "recovering", True),
            ("permission recovery failed", "needs_manual_resume", False),
            ("APK fingerprint mismatch", "needs_manual_resume", False),
            ("wait_for_device_timeout", "needs_manual_resume", False),
        )
        for message, state, automatic in cases:
            with self.subTest(message=message):
                decision = checkpoint.classify_recovery(message)
                self.assertEqual(state, decision["state"])
                self.assertEqual(automatic, decision["automatic"])
        self.assertEqual(
            "paused", checkpoint.classify_recovery("anything", manual_stop=True)["state"]
        )

    def test_recovery_attempt_keeps_explicit_adb_error_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = checkpoint.create_recovery_attempt(
                Path(temporary),
                1,
                state="waiting_for_device",
                error="device disconnected",
                checkpoint={
                    "job_id": "job-1",
                    "device_serial": "serial-1",
                    "adb_error": "transport endpoint is not connected",
                },
            )
            manifest = json.loads(
                (directory / "recovery.json").read_text(encoding="utf-8")
            )
            self.assertIn("adb_error", manifest)
            self.assertIn("cleanup_before_pid_snapshot", manifest)
            self.assertIn("cleanup_after_pid_snapshot", manifest)

            state = Mock()
            state.current_component.return_value = "com.android.launcher/.Launcher"
            state.capture_screenshot_png.return_value = b"PNG"
            state.dump_hierarchy_text.return_value = "<hierarchy />"
            executor = Mock()
            executor.capture_process_diagnostics.return_value = {
                "adb_error": "adb: device offline"
            }
            with patch.object(main, "create_recovery_attempt", return_value=directory):
                result = main._save_recovery_attempt_evidence(
                    Path(temporary),
                    1,
                    {"state": "waiting_for_device", "category": "device_transport"},
                    "device disconnected",
                    {
                        "job_id": "job-1",
                        "device_serial": "serial-1",
                        "last_error": "device disconnected",
                    },
                    state,
                    executor,
                    Mock(),
                )
            self.assertEqual(directory, result)
            self.assertTrue((directory / "adb_error.txt").is_file())
            self.assertIn(
                "device disconnected",
                (directory / "adb_error.txt").read_text(encoding="utf-8"),
            )
            final_manifest = json.loads(
                (directory / "recovery.json").read_text(encoding="utf-8")
            )
            self.assertTrue(final_manifest.get("adb_error_path"))


class ResumeAccountingTests(unittest.TestCase):
    def test_cumulative_phase_values_are_not_added_twice(self):
        class State:
            device_serial = "serial-1"
            package_name = "com.example.app"

        trajectory = {
            "operation_count": 17,
            "operation_count_mode": "cumulative",
            "stop_reason": main._WAIT_COMPLETED,
            "phase": {
                "engine_elapsed_seconds": 60,
                "elapsed_seconds_mode": "cumulative",
            },
        }
        config = {
            "test": {"fastbot_duration_minutes": 1},
            "crash_recovery": {},
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "main._run_fastbot_phase_once",
            return_value=(trajectory, "", {}),
        ) as run_phase:
            result, _log, _artifacts = main._run_fastbot_phase(
                State(),
                Mock(),
                Mock(),
                config,
                Path(temporary),
                1,
                resume_context={"elapsed_seconds": 12, "operation_count": 17},
            )
        self.assertEqual(1, run_phase.call_count)
        self.assertEqual(17, result["operation_count"])
        self.assertEqual(60, result["pressure_elapsed_seconds"])
        self.assertEqual(0, result["pressure_phases"][0]["operation_count"])

    def test_scheduler_reads_persisted_resume_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "job-1_serial-1"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "run_id": "job-1",
                        "device_serial": "serial-1",
                        "status": "recovering",
                        "resume_state": "recovering",
                        "resume_attempts": 2,
                        "retry_history": [{"attempt": 1, "category": "foreground"}],
                    }
                ),
                encoding="utf-8",
            )
            checkpoint.write_json(
                report_dir / "checkpoint.json",
                {
                    "job_id": "job-1",
                    "device_serial": "serial-1",
                    "cycle": 1,
                    "elapsed_seconds": 10,
                    "operation_count": 4,
                    "resume_state": "recovering",
                    "resume_attempts": 3,
                },
            )
            config = {"paths": {"reports_dir": str(root)}}
            value = main._persisted_resume_metadata(config, "job-1", "serial-1")
            self.assertEqual(3, value["attempts"])
            self.assertEqual(1, len(value["history"]))

    def test_scheduler_restores_persisted_device_wait_deadline(self):
        started = "2026-09-04T00:00:00+00:00"
        deadline = "2026-09-04T00:30:00+00:00"
        wall_now = datetime.fromisoformat("2026-09-04T00:20:00+00:00").timestamp()

        window = main._restored_device_wait_window(
            {
                "state": "waiting_for_device",
                "report": {
                    "wait_started_at": started,
                    "wait_deadline_at": deadline,
                    "scheduler_updated_at": "2026-09-04T00:10:00+00:00",
                },
                "checkpoint": None,
            },
            1800,
            wall_now=wall_now,
            monotonic_now=1000,
        )

        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(started, window["wait_started_at"])
        self.assertEqual(deadline, window["wait_deadline_at"])
        self.assertEqual(-200, window["started"])
        self.assertEqual(1600, window["deadline"])

    def test_run_devices_skips_offline_device_after_manual_end(self):
        class Event:
            def __init__(self):
                self.value = False

            def set(self):
                self.value = True

            def is_set(self):
                return self.value

            def wait(self, _seconds):
                return self.value

        class Queue:
            @staticmethod
            def get_nowait():
                raise main.queue.Empty

            @staticmethod
            def cancel_join_thread():
                return None

            @staticmethod
            def close():
                return None

            @staticmethod
            def join_thread():
                return None

        class Context:
            @staticmethod
            def Event():
                return Event()

            @staticmethod
            def Queue():
                return Queue()

            @staticmethod
            def Process(*_args, **_kwargs):
                raise AssertionError("manual-ended device must not spawn a worker")

        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            report_dir = reports_root / "job-1_serial-1"
            report_dir.mkdir(parents=True)
            checkpoint.write_json(
                report_dir / "report.json",
                {
                    "run_id": "job-1",
                    "device_serial": "serial-1",
                    "status": "stopped",
                    "resume_state": "manually_ended",
                    "manual_end": True,
                    "remaining_seconds": 0,
                    "cycles": [],
                    "statistics": {},
                },
            )
            checkpoint.write_json(
                report_dir / "checkpoint.json",
                {
                    "job_id": "job-1",
                    "device_serial": "serial-1",
                    "resume_state": "manually_ended",
                    "manual_end": True,
                    "remaining_seconds": 0,
                },
            )
            config = {
                "app": {"package_name": "com.example.app"},
                "paths": {"reports_dir": str(reports_root)},
                "parallel": {"max_workers": 1},
                "resume": {"enabled": True, "mode": "auto"},
            }
            with (
                patch.object(main.mp, "get_context", return_value=Context()),
                patch.object(main.DeviceManager, "is_device_ready") as ready,
            ):
                results, interrupted = main.run_devices(
                    ["serial-1"],
                    config,
                    run_id="job-1",
                    device_states={},
                )

        self.assertFalse(interrupted)
        self.assertEqual("manually_ended", results["serial-1"]["status"])
        self.assertEqual("stopped", results["serial-1"]["coverage_status"])
        self.assertFalse(results["serial-1"]["recoverable"])
        ready.assert_not_called()

    def test_run_devices_does_not_reset_expired_wait_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "job-1_serial-1"
            report_dir.mkdir()
            wait_started_at = datetime.fromtimestamp(
                time.time() - 3600, timezone.utc
            ).isoformat()
            wait_deadline_at = datetime.fromtimestamp(
                time.time() - 60, timezone.utc
            ).isoformat()
            report = {
                "run_id": "job-1",
                "device_serial": "serial-1",
                "status": "waiting_for_device",
                "resume_state": "waiting_for_device",
                "resume_attempts": 1,
                "wait_started_at": wait_started_at,
                "wait_deadline_at": wait_deadline_at,
                "cycles": [],
                "statistics": {},
            }
            checkpoint.write_json(report_dir / "report.json", report)
            checkpoint.write_json(
                report_dir / "checkpoint.json",
                {
                    "job_id": "job-1",
                    "device_serial": "serial-1",
                    "cycle": 1,
                    "elapsed_seconds": 10,
                    "operation_count": 4,
                    "resume_state": "waiting_for_device",
                    "resume_attempts": 1,
                    "wait_started_at": wait_started_at,
                    "wait_deadline_at": wait_deadline_at,
                },
            )
            config = {
                "app": {"package_name": "com.example.app"},
                "paths": {"reports_dir": str(root)},
                "resume": {
                    "enabled": True,
                    "mode": "auto",
                    "wait_for_device_seconds": 1800,
                },
            }

            results, interrupted = main.run_devices(
                ["serial-1"],
                config,
                run_id="job-1",
                device_states={},
            )
            persisted = checkpoint.read_json(report_dir / "report.json")

        self.assertFalse(interrupted)
        self.assertEqual("needs_manual_resume", results["serial-1"]["status"])
        self.assertEqual(
            "device_wait_timeout", results["serial-1"]["recovery_category"]
        )
        self.assertEqual(wait_deadline_at, results["serial-1"]["wait_deadline_at"])
        self.assertEqual("needs_manual_resume", persisted["resume_state"])
        self.assertEqual(wait_deadline_at, persisted["wait_deadline_at"])

    def test_worker_wait_honors_persisted_deadline_before_probing_adb(self):
        class Cancel:
            def is_set(self):
                return False

            def wait(self, _seconds):
                return None

        with patch.object(main.time, "time", return_value=2000.0), patch.object(
            main.time, "monotonic", return_value=100.0
        ), patch.object(main.DeviceManager, "is_device_ready", return_value=False) as ready:
            result = main._wait_for_device_ready(
                "serial-1",
                1800,
                cancel_event=Cancel(),
                wait_started_at="1970-01-01T00:00:00+00:00",
                wait_deadline_at="1970-01-01T00:16:40+00:00",
            )

        self.assertFalse(result)
        ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
