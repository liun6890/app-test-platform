from __future__ import annotations

import hashlib
import json
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import server


class ServerConfigTests(unittest.TestCase):

    def test_device_snapshot_marks_dead_fastbot_as_recovering(self) -> None:
        checkpoint = {
            "job_id": "job-1",
            "device_serial": "serial-1",
            "cycle": 1,
            "phase": 1,
            "phase_name": "fastbot",
            "elapsed_seconds": 20,
            "operation_count": 4,
            "resume_state": "running",
            "fastbot_alive": False,
            "fastbot_pid": "1234",
        }
        job = {
            "id": "job-1",
            "devices": ["serial-1"],
            "duration": 1,
            "cycles": 1,
            "packageName": "com.example.sample",
        }
        with mock.patch.object(
            server, "_job_device_artifacts", return_value=(None, checkpoint)
        ):
            snapshot = server._device_resume_snapshot("serial-1", job)

        self.assertEqual("recovering", snapshot["resume_state"])
        self.assertFalse(snapshot["fastbot_alive"])
        self.assertIn("Fastbot", snapshot["last_error"])

    def test_device_snapshot_marks_stalled_fastbot_as_recovering(self) -> None:
        checkpoint = {
            "job_id": "job-1",
            "device_serial": "serial-1",
            "cycle": 1,
            "phase": 1,
            "phase_name": "fastbot",
            "elapsed_seconds": 240,
            "operation_count": 4,
            "resume_state": "running",
            "fastbot_alive": True,
            "fastbot_started_at": "2020-01-01T00:00:00+00:00",
            "last_operation_at": "2020-01-01T00:00:00+00:00",
        }
        job = {
            "id": "job-1",
            "devices": ["serial-1"],
            "duration": 10,
            "cycles": 1,
            "packageName": "com.example.sample",
            "config": {
                "stall_watchdog": {
                    "enabled": True,
                    "unchanged_seconds": 150,
                    "startup_grace_seconds": 30,
                }
            },
        }
        with mock.patch.object(
            server, "_job_device_artifacts", return_value=(None, checkpoint)
        ):
            snapshot = server._device_resume_snapshot("serial-1", job)

        self.assertEqual("recovering", snapshot["resume_state"])
        self.assertTrue(snapshot["operation_stalled"])
        self.assertGreaterEqual(snapshot["stalled_for_seconds"], 150)

    def test_device_snapshot_without_operation_timestamp_is_not_stalled(self) -> None:
        # Fastbot builds without "Event id" markers still grow their log; a
        # missing operation timestamp must not be read as a dead engine.
        checkpoint = {
            "job_id": "job-1",
            "device_serial": "serial-1",
            "cycle": 1,
            "phase": 1,
            "phase_name": "fastbot",
            "elapsed_seconds": 240,
            "operation_count": 4,
            "resume_state": "running",
            "fastbot_alive": True,
            "fastbot_started_at": "2020-01-01T00:00:00+00:00",
        }
        job = {
            "id": "job-1",
            "devices": ["serial-1"],
            "duration": 10,
            "cycles": 1,
            "packageName": "com.example.sample",
            "config": {
                "stall_watchdog": {
                    "enabled": True,
                    "unchanged_seconds": 150,
                    "startup_grace_seconds": 30,
                }
            },
        }
        with mock.patch.object(
            server, "_job_device_artifacts", return_value=(None, checkpoint)
        ):
            snapshot = server._device_resume_snapshot("serial-1", job)

        self.assertEqual("running", snapshot["resume_state"])
        self.assertFalse(snapshot["operation_stalled"])

    def test_console_server_port_cannot_be_shared(self) -> None:
        first = server.ExclusiveThreadingHTTPServer(
            ("127.0.0.1", 0), server.ConsoleHandler
        )
        try:
            with self.assertRaises(OSError):
                server.ExclusiveThreadingHTTPServer(
                    first.server_address, server.ConsoleHandler
                )
        finally:
            first.server_close()



    def test_runtime_storage_uses_the_shared_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ,
                {"APP_PRESS_TEST_OUTPUT_ROOT": temporary},
            ):
                self.assertEqual(
                    (Path(temporary) / "frontend" / "runtime").resolve(),
                    server.runtime_storage_root(),
                )

    def test_screen_record_artifact_requires_a_complete_mp4(self) -> None:
        def box(name: bytes, payload: bytes) -> bytes:
            return (len(payload) + 8).to_bytes(4, "big") + name + payload

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            report_path.write_text("{}", encoding="utf-8")
            recording = root / "screen_record.mp4"
            recording.write_bytes(
                box(b"ftyp", b"isom0000")
                + box(b"mdat", b"video")
                + box(b"moov", b"index")
            )
            complete = server.report_artifacts(report_path, {})

            recording.write_bytes(
                box(b"ftyp", b"isom0000")
                + (1024).to_bytes(4, "big")
                + b"mdat"
                + b"partial"
            )
            interrupted = server.report_artifacts(report_path, {})

        self.assertIn("screen_record", complete)
        self.assertNotIn("screen_record", interrupted)

    def test_framework_templates_expose_the_audit_start_page(self) -> None:
        templates = server.available_configs()
        baibai = next(item for item in templates if item["name"] == "acceptance_baibai_fastbot.json")

        self.assertEqual(
            [{
                "id": "audit_gate",
                "label": "审核中",
                "description": "按框架步骤勾选“审核中”并确认，再继续后续引导。",
            }],
            baibai["startupPages"],
        )

    def test_default_ui_config_remains_usable_without_templates(self) -> None:
        with mock.patch.object(server, "available_configs", return_value=[]):
            value = server.default_ui_config()

        self.assertIn("packageName", value)
        self.assertTrue(value["auditGateEveryLaunch"])
        self.assertNotIn("tabRotation", value)

    def test_matched_workflow_keeps_the_audit_step_as_its_first_navigation_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "configFile": "acceptance_baibai_fastbot.json",
                "workflowMode": "matched",
                "startupPage": "audit_gate",
                "packageName": "com.ofuodapz.recordsofbaibaichat",
                "apkPath": str(apk),
                "devices": ["serial-1"],
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        self.assertEqual("complete_audit_gate", config["scenario"]["navigation_steps"][0]["action"])

    def test_package_check_uses_cmd_package_when_pm_alias_has_no_result(self) -> None:
        first = mock.Mock(stdout="", stderr="")
        second = mock.Mock(stdout="package:/data/app/~~token/base.apk", stderr="")
        with mock.patch.object(
            server.DeviceManager,
            "_run_adb",
            side_effect=[first, second],
        ) as run_adb:
            installed = server._package_installed("serial-1", "com.example.sample")

        self.assertTrue(installed)
        self.assertEqual(2, run_adb.call_count)

    def test_package_match_compares_selected_apk_with_installed_base_apk(self) -> None:
        expected = "2ab6a82203e9cb9dfaf3593a404d2919"
        with mock.patch.object(
            server.DeviceManager,
            "_run_adb",
            side_effect=[
                mock.Mock(
                    returncode=0,
                    stdout="package:/data/app/com.example.sample/base.apk\n",
                    stderr="",
                ),
                mock.Mock(
                    returncode=0,
                    stdout=f"{expected}  /data/app/com.example.sample/base.apk\n",
                    stderr="",
                ),
            ],
        ):
            self.assertTrue(
                server._package_matches_apk(
                    "serial-1", "com.example.sample", expected
                )
            )

    def test_package_match_rejects_same_package_with_different_apk(self) -> None:
        with mock.patch.object(
            server.DeviceManager,
            "_run_adb",
            side_effect=[
                mock.Mock(
                    returncode=0,
                    stdout="package:/data/app/com.example.sample/base.apk\n",
                    stderr="",
                ),
                mock.Mock(
                    returncode=0,
                    stdout=(
                        "d2580a376f7db39dce29eddd0ba1cd29  "
                        "/data/app/com.example.sample/base.apk\n"
                    ),
                    stderr="",
                ),
            ],
        ):
            self.assertFalse(
                server._package_matches_apk(
                    "serial-1",
                    "com.example.sample",
                    "2ab6a82203e9cb9dfaf3593a404d2919",
                )
            )

    def test_fastbot_preflight_checks_bundled_runtime_for_device_abi(self) -> None:
        native_path = "/data/local/tmp/monkey.apk!/lib/arm64-v8a/libfastbot_native.so"
        runtime_md5 = server.runtime_asset_md5()
        self.assertTrue(runtime_md5)
        with mock.patch.object(
            server.DeviceManager,
            "_run_adb",
            side_effect=[
                mock.Mock(returncode=0, stdout="/data/local/tmp/monkey.apk\n"),
                mock.Mock(
                    returncode=0,
                    stdout=f"{runtime_md5}  /data/local/tmp/monkey.apk\n",
                ),
                mock.Mock(returncode=0, stdout="arm64-v8a\n"),
            ],
        ):
            result = server._fastbot_device_check(
                "serial-1",
                {
                    "classpath": "/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar",
                    "expected_monkeyq_md5": "1963730138c4e61f908f14e436bbdaef",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("arm64-v8a", result["nativeAbi"])
        self.assertEqual(native_path, result["nativePath"])
        self.assertEqual("/data/local/tmp/monkey.apk", result["classpath"])
        self.assertIsNone(result["actualNativeMd5"])

    def test_fastbot_preflight_rejects_missing_runtime_bundle(self) -> None:
        with mock.patch.object(
            server.DeviceManager,
            "_run_adb",
            side_effect=[
                mock.Mock(returncode=1, stdout="", stderr="missing"),
                mock.Mock(returncode=0, stdout="arm64-v8a\n"),
            ],
        ):
            result = server._fastbot_device_check(
                "serial-1",
                {
                    "classpath": "/data/local/tmp/monkey.apk",
                    "expected_monkeyq_md5": "1963730138c4e61f908f14e436bbdaef",
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("monkey.apk", result["error"])

    def test_draft_persists_valid_choices_and_rejects_external_apk_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            draft_path = root / "draft.json"
            payload = {
                "selectedDevices": ["serial-1", "serial-2", "serial-1"],
                "config": {
                    "configFile": "acceptance_baibai_fastbot.json",
                    "packageName": "com.invalid.will.be.replaced",
                    "apkPath": str(apk),
                    "apkName": "sample.apk",
                    "apkPackageName": "com.example.sample",
                    "duration": 480,
                    "cycles": 2,
                    "throttle": 300,
                    "workers": 8,
                    "startupPage": "audit_gate",
                    "auditGateEveryLaunch": True,
                    "clearAppData": True,
                },
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(server, "DRAFT_PATH", draft_path),
            ):
                saved = server.save_draft(payload)
                restored = server.read_draft()
                invalid = server.sanitize_draft({
                    "selectedDevices": ["serial-1"],
                    "config": {"apkPath": str(root / "outside.apk"), "apkPackageName": "com.example.outside"},
                })

        self.assertEqual(["serial-1", "serial-2"], saved["selectedDevices"])
        self.assertEqual(480, restored["config"]["duration"])
        self.assertEqual(2, restored["config"]["workers"])
        self.assertTrue(restored["config"]["auditGateEveryLaunch"])
        self.assertEqual("com.example.sample", restored["config"]["packageName"])
        self.assertNotIn("apkPath", invalid["config"])

    def test_generated_run_config_always_uses_fastbot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "devices": ["serial-1"],
                "duration": 5,
                "cycles": 2,
                "throttle": 250,
                "workers": 1,
                "screenshot": True,
                "stallWatchdog": False,
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        self.assertEqual("fastbot", config["fastbot"]["engine"])
        self.assertEqual(
            "/data/local/tmp/monkey.apk",
            config["fastbot"]["classpath"],
        )
        self.assertEqual([{"serial": "serial-1", "alias": "serial-1"}], config["devices"])
        self.assertFalse(config["stall_watchdog"]["enabled"])

    def test_legacy_generic_run_defaults_to_one_time_stall_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "devices": ["serial-1"],
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        self.assertTrue(config["stall_watchdog"]["enabled"])
        self.assertEqual("recover_once", config["stall_watchdog"]["mode"])

    def test_explicitly_disabled_stall_watchdog_is_preserved_in_generic_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "configFile": "acceptance_baibai_fastbot.json",
                "workflowMode": "generic",
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "apkPackageName": "com.example.sample",
                "devices": ["serial-1"],
                "stallWatchdog": False,
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(
                    server,
                    "apk_package_name",
                    return_value=("com.example.sample", ""),
                ),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        self.assertFalse(config["stall_watchdog"]["enabled"])
        self.assertEqual("observe", config["stall_watchdog"]["mode"])

    def test_generic_workflow_attempts_the_same_audit_gate_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "configFile": "acceptance_baibai_fastbot.json",
                "workflowMode": "generic",
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "apkPackageName": "com.example.sample",
                "devices": ["serial-1"],
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(
                    server,
                    "apk_package_name",
                    return_value=("com.example.sample", ""),
                ),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        steps = config["scenario"]["navigation_steps"]
        self.assertEqual(
            ["launch_app", "complete_audit_gate", "verify_foreground_stable"],
            [step["action"] for step in steps],
        )
        self.assertEqual(
            "com.example.sample:id/auditRadioButton",
            steps[1]["audit_locator"]["resourceId"],
        )
        self.assertEqual(
            {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "com.example.sample:id/auditRadioButton",
                "confirm_text": "确定",
            },
            config["scenario"]["audit_recovery"],
        )
        self.assertNotIn("tab_rotation", config)

    def test_generic_home_selection_keeps_audit_gate_and_global_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "configFile": "acceptance_baibai_fastbot.json",
                "workflowMode": "generic",
                "startupPage": "app_home",
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "apkPackageName": "com.example.sample",
                "devices": ["serial-1"],
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(server, "apk_package_name", return_value=("com.example.sample", "")),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        self.assertEqual(
            ["launch_app", "complete_audit_gate", "verify_foreground_stable"],
            [step["action"] for step in config["scenario"]["navigation_steps"]],
        )
        self.assertTrue(config["scenario"]["audit_recovery"]["enabled"])

    def test_generic_workflow_can_explicitly_disable_every_launch_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "configFile": "acceptance_baibai_fastbot.json",
                "workflowMode": "generic",
                "auditGateEveryLaunch": False,
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "apkPackageName": "com.example.sample",
                "devices": ["serial-1"],
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(server, "apk_package_name", return_value=("com.example.sample", "")),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        self.assertEqual(
            ["launch_app", "verify_foreground_stable"],
            [step["action"] for step in config["scenario"]["navigation_steps"]],
        )
        self.assertFalse(config["scenario"]["audit_recovery"]["enabled"])

    def test_matched_workflow_injects_every_launch_audit_for_any_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = uploads / "sample.apk"
            apk.write_bytes(b"apk")
            payload = {
                "configFile": "run_netcoolemail_8h_250ms_20260828.json",
                "workflowMode": "matched",
                "startupPage": "app_home",
                "auditGateEveryLaunch": True,
                "packageName": "com.jzeposbs.netcoolemailmaster",
                "apkPath": str(apk),
                "devices": ["serial-1"],
            }
            with (
                mock.patch.object(server, "UPLOAD_ROOT", uploads),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
            ):
                config = server.build_run_config(payload, root / "job")

        steps = config["scenario"]["navigation_steps"]
        self.assertEqual(
            ["launch_app", "complete_audit_gate", "verify_foreground_stable"],
            [step["action"] for step in steps],
        )
        self.assertEqual(
            "com.jzeposbs.netcoolemailmaster:id/auditRadioButton",
            steps[1]["audit_locator"]["resourceId"],
        )
        self.assertTrue(config["scenario"]["audit_recovery"]["enabled"])

    def test_run_rejects_apk_outside_upload_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            apk = root / "outside.apk"
            apk.write_bytes(b"apk")
            payload = {
                "packageName": "com.example.sample",
                "apkPath": str(apk),
                "devices": ["serial-1"],
            }
            with mock.patch.object(server, "UPLOAD_ROOT", uploads):
                with self.assertRaisesRegex(server.ApiError, "必须先通过网页上传"):
                    server.build_run_config(payload, root / "job")

    def test_manual_install_confirmation_starts_cli_only_after_package_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {
                "id": "WEB-test",
                "status": "awaiting_install",
                "phase": "awaiting_install",
                "startedAt": server.utc_now(),
                "startedEpoch": 1.0,
                "finishedAt": None,
                "packageName": "com.example.sample",
                "apkName": "sample.apk",
                "devices": ["serial-1"],
                "duration": 1,
                "cycles": 1,
                "workers": 1,
                "pid": None,
                "exitCode": None,
                "jobDir": str(root),
                "reportRoot": str(root / "reports"),
                "logPath": str(root / "console.log"),
                "configPath": str(root / "config.json"),
                "process": None,
                "logStream": io.BytesIO(),
                "installProcesses": {},
                "installation": {
                    "required": True,
                    "status": "waiting",
                    "pendingDevices": ["serial-1"],
                    "confirmedAt": None,
                    "devices": {
                        "serial-1": {
                            "serial": "serial-1",
                            "required": True,
                            "installed": False,
                            "status": "waiting",
                        }
                    },
                },
            }
            process = mock.Mock(pid=1234)
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(server, "_package_installed", return_value=True),
                    mock.patch.object(server.subprocess, "Popen", return_value=process) as popen,
                    mock.patch.object(server, "persist_job"),
                ):
                    result = server.confirm_job_install(job["id"])
            finally:
                server.JOBS.pop(job["id"], None)

        self.assertEqual("preparing", result["status"])
        self.assertEqual("confirmed", result["installation"]["status"])
        self.assertEqual(1234, result["pid"])
        popen.assert_called_once()

    def test_manual_install_confirmation_rejects_missing_package(self) -> None:
        job = {
            "id": "WEB-missing",
            "status": "awaiting_install",
            "packageName": "com.example.sample",
            "devices": ["serial-1"],
            "installation": {
                "required": True,
                "status": "waiting",
                "pendingDevices": ["serial-1"],
                "devices": {},
            },
        }
        server.JOBS[job["id"]] = job
        try:
            with (
                mock.patch.object(server, "_package_installed", return_value=False),
                mock.patch.object(server, "persist_job"),
            ):
                with self.assertRaisesRegex(server.ApiError, "尚未检测到 APK 安装完成"):
                    server.confirm_job_install(job["id"])
        finally:
            server.JOBS.pop(job["id"], None)

    def test_terminal_refresh_syncs_evidence_phase_with_job_status(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 1
        job = {
            "id": "WEB-terminal-evidence",
            "status": "running",
            "phase": "running",
            "process": process,
            "finishedAt": None,
        }
        evidence = {"phase": "running", "pressureStarted": True, "errors": []}
        with (
            mock.patch.object(server, "_job_evidence", return_value=evidence),
            mock.patch.object(server, "persist_job"),
        ):
            server.refresh_job(job)

        self.assertEqual("failed", job["status"])
        self.assertEqual("failed", job["phase"])
        self.assertEqual("failed", job["evidence"]["phase"])
        self.assertTrue(job["evidence"]["finishedAt"])

    def test_manual_install_confirmation_persists_partial_device_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {
                "id": "WEB-partial",
                "status": "awaiting_install",
                "packageName": "com.example.sample",
                "devices": ["serial-1", "serial-2"],
                "jobDir": str(root),
                "installation": {
                    "required": True,
                    "status": "waiting",
                    "pendingDevices": ["serial-1", "serial-2"],
                    "devices": {
                        "serial-1": {"serial": "serial-1", "installed": False, "status": "waiting"},
                        "serial-2": {"serial": "serial-2", "installed": False, "status": "waiting"},
                    },
                },
            }
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(server, "_package_installed", side_effect=[True, False]),
                    mock.patch.object(server, "persist_job") as persist,
                ):
                    with self.assertRaisesRegex(server.ApiError, "serial-2"):
                        server.confirm_job_install(job["id"])
            finally:
                server.JOBS.pop(job["id"], None)

        self.assertEqual(["serial-2"], job["installation"]["pendingDevices"])
        self.assertTrue(job["installation"]["devices"]["serial-1"]["installed"])
        persist.assert_called_once()


class ResumeSchedulerTests(unittest.TestCase):
    @staticmethod
    def _write_install_job(
        root: Path,
        *,
        status: str = "awaiting_install",
        installation_status: str = "waiting",
        manual_stop: bool = False,
    ) -> tuple[Path, Path]:
        runs_root = root / "runs"
        job_dir = runs_root / "WEB-install"
        job_dir.mkdir(parents=True)
        (job_dir / "config.json").write_text(
            json.dumps(
                {
                    "app": {
                        "package_name": "com.example.sample",
                        "apk_path": str(root / "sample.apk"),
                    },
                    "resume": {"enabled": True, "mode": "auto"},
                }
            ),
            encoding="utf-8",
        )
        job = {
            "id": "WEB-install",
            "runId": "run-install",
            "status": status,
            "phase": status,
            "startedAt": "2026-09-04T00:00:00+00:00",
            "devices": ["serial-1"],
            "packageName": "com.example.sample",
            "apkName": "sample.apk",
            "duration": 1,
            "cycles": 1,
            "pid": None,
            "manualStopRequested": manual_stop,
            "installation": {
                "required": True,
                "status": installation_status,
                "pendingDevices": ["serial-1"],
                "devices": {
                    "serial-1": {
                        "serial": "serial-1",
                        "required": True,
                        "installed": False,
                        "status": installation_status,
                    }
                },
            },
        }
        (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        return runs_root, job_dir

    def test_pid_probe_treats_windows_system_error_as_stale(self) -> None:
        with (
            mock.patch.object(server.os, "name", "posix"),
            mock.patch.object(server.os, "kill", side_effect=SystemError("WinError 87")),
        ):
            self.assertFalse(server._pid_is_alive(123456789))

    def test_pid_probe_uses_windows_process_api_without_console_signal(self) -> None:
        open_process = mock.Mock(return_value=object())

        def fill_exit_code(_handle, code_pointer):
            # GetExitCodeProcess returns non-zero on success and writes the
            # process status into the pointer; STILL_ACTIVE (259) is alive.
            code_pointer._obj.value = 259
            return 1

        get_exit_code = mock.Mock(side_effect=fill_exit_code)
        close_handle = mock.Mock()
        kernel32 = mock.Mock(
            OpenProcess=open_process,
            GetExitCodeProcess=get_exit_code,
            CloseHandle=close_handle,
        )
        with (
            mock.patch.object(server.os, "name", "nt"),
            mock.patch.object(server.ctypes, "WinDLL", return_value=kernel32),
        ):
            self.assertTrue(server._pid_is_alive(123456789))

        open_process.assert_called_once_with(0x1000, 0, 123456789)
        get_exit_code.assert_called_once()
        close_handle.assert_called_once()

    def test_restart_preserves_awaiting_install_without_starting_any_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir = self._write_install_job(Path(temporary))
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(server, "_start_manual_install") as install,
                    mock.patch.object(server, "_launch_job_cli") as launch,
                ):
                    server.load_persisted_jobs()
                restored = server.JOBS["WEB-install"]
                self.assertEqual("awaiting_install", restored["status"])
                self.assertEqual("waiting", restored["installation"]["status"])
                install.assert_not_called()
                launch.assert_not_called()
            finally:
                stream = server.JOBS.get("WEB-install", {}).pop("logStream", None)
                if stream is not None:
                    stream.close()
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_restart_does_not_relaunch_stopped_install_stage_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir = self._write_install_job(
                Path(temporary), status="stopped", manual_stop=True
            )
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(server, "_start_manual_install") as install,
                    mock.patch.object(server, "_launch_job_cli") as launch,
                ):
                    server.load_persisted_jobs()
                self.assertEqual("stopped", server.JOBS["WEB-install"]["status"])
                install.assert_not_called()
                launch.assert_not_called()
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_restart_keeps_cancelled_install_stopped_and_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir = self._write_install_job(
                Path(temporary), installation_status="cancelled"
            )
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(server, "_start_manual_install") as install,
                    mock.patch.object(server, "_launch_job_cli") as launch,
                ):
                    server.load_persisted_jobs()
                restored = server.JOBS["WEB-install"]
                self.assertEqual("stopped", restored["status"])
                self.assertTrue(restored["manualStopRequested"])
                install.assert_not_called()
                launch.assert_not_called()
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_restart_requires_artifact_before_automatic_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, job_dir = self._write_install_job(
                Path(temporary), status="recovering", installation_status="confirmed"
            )
            job_path = job_dir / "job.json"
            value = json.loads(job_path.read_text(encoding="utf-8"))
            value["installation"]["required"] = False
            job_path.write_text(json.dumps(value), encoding="utf-8")
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(server, "_best_effort_remote_worker_cleanup_status") as cleanup,
                    mock.patch.object(server, "_launch_job_cli") as launch,
                ):
                    server.load_persisted_jobs()
                self.assertEqual(
                    "needs_manual_resume", server.JOBS["WEB-install"]["status"]
                )
                cleanup.assert_not_called()
                launch.assert_not_called()
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    @staticmethod
    def _write_persisted_job(root: Path, *, wait_deadline: str = "") -> tuple[Path, Path, Path]:
        runs_root = root / "runs"
        job_dir = runs_root / "WEB-resume"
        report_dir = job_dir / "reports" / "run-1_serial-1"
        report_dir.mkdir(parents=True)
        config = {
            "resume": {
                "enabled": True,
                "mode": "auto",
                "max_attempts": 3,
                "wait_for_device_seconds": 60,
            },
            "app": {"package_name": "com.example.sample"},
            "test": {"fastbot_duration_minutes": 1, "max_cycles": 2},
        }
        (job_dir / "config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        report = {
            "run_id": "run-1",
            "job_id": "WEB-resume",
            "device_serial": "serial-1",
            "package_name": "com.example.sample",
            "status": "recovering",
            "resume_state": "recovering",
            "resume_attempts": 1,
            "current_cycle": {
                "cycle": 1,
                "status": "recovering",
                "pressure_elapsed_seconds": 12,
                "operation_count": 7,
            },
            "cycles": [],
            "errors": [],
        }
        checkpoint = {
            "schema_version": 1,
            "job_id": "run-1",
            "device_serial": "serial-1",
            "cycle": 1,
            "phase": "pressure",
            "elapsed_seconds": 12,
            "remaining_seconds": 48,
            "operation_count": 7,
            "resume_attempts": 1,
            "resume_state": "recovering",
        }
        if wait_deadline:
            report["resume_state"] = "waiting_for_device"
            report["status"] = "waiting_for_device"
            report["wait_deadline_at"] = wait_deadline
            checkpoint["resume_state"] = "waiting_for_device"
            checkpoint["wait_deadline_at"] = wait_deadline
        (report_dir / "report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        (report_dir / "checkpoint.json").write_text(
            json.dumps(checkpoint), encoding="utf-8"
        )
        job = {
            "id": "WEB-resume",
            "runId": "run-1",
            "status": "recovering",
            "phase": "recovering",
            "startedAt": "2026-09-04T00:00:00+00:00",
            "devices": ["serial-1"],
            "packageName": "com.example.sample",
            "apkName": "sample.apk",
            "duration": 1,
            "cycles": 2,
            "pid": None,
        }
        (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        return runs_root, job_dir, report_dir

    def test_restart_keeps_offline_device_waiting_and_retries_after_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir, report_dir = self._write_persisted_job(Path(temporary))
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_DEVICE_UNAVAILABLE,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                    mock.patch.object(server, "persist_job"),
                ):
                    server.load_persisted_jobs()
                    job = server.JOBS["WEB-resume"]
                    self.assertEqual("waiting_for_device", job["status"])
                    self.assertEqual("waiting_for_device", job["phase"])
                    self.assertFalse(launch.called)

                report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
                checkpoint = json.loads(
                    (report_dir / "checkpoint.json").read_text(encoding="utf-8")
                )
                self.assertEqual("waiting_for_device", report["resume_state"])
                self.assertTrue(report.get("wait_deadline_at"))
                self.assertEqual("waiting_for_device", checkpoint["resume_state"])
                self.assertTrue(checkpoint.get("wait_deadline_at"))

                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                    mock.patch.object(server, "persist_job"),
                ):
                    server.refresh_job(job)
                launch.assert_called_once_with(job, devices=["serial-1"])
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_expired_persisted_wait_becomes_manual_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir, _report_dir = self._write_persisted_job(
                Path(temporary), wait_deadline="2020-01-01T00:00:00+00:00"
            )
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                    mock.patch.object(server, "persist_job"),
                ):
                    server.load_persisted_jobs()
                    job = server.JOBS["WEB-resume"]
                self.assertEqual("needs_manual_resume", job["status"])
                launch.assert_not_called()
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_explicit_request_dispatches_manual_mode_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, job_dir, report_dir = self._write_persisted_job(Path(temporary))
            config_path = job_dir / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["resume"]["mode"] = "manual"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            for name in ("report.json", "checkpoint.json"):
                path = report_dir / name
                value = json.loads(path.read_text(encoding="utf-8"))
                value["status"] = "needs_manual_resume"
                value["resume_state"] = "needs_manual_resume"
                value["manual_stop"] = True
                path.write_text(json.dumps(value), encoding="utf-8")
            server.request_manual_resume(report_dir, requested_by="web")

            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                    mock.patch.object(server, "persist_job"),
                ):
                    server.load_persisted_jobs()

                job = server.JOBS["WEB-resume"]
                launch.assert_called_once_with(job, devices=["serial-1"])
                self.assertTrue((report_dir / "resume.request.json").is_file())
                for name in ("report.json", "checkpoint.json"):
                    value = json.loads((report_dir / name).read_text(encoding="utf-8"))
                    self.assertEqual("resuming", value["resume_state"])
                    self.assertFalse(value.get("manual_stop", False))
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_explicit_request_dispatches_expired_manual_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir, report_dir = self._write_persisted_job(
                Path(temporary), wait_deadline="2020-01-01T00:00:00+00:00"
            )
            for name in ("report.json", "checkpoint.json"):
                path = report_dir / name
                value = json.loads(path.read_text(encoding="utf-8"))
                value["status"] = "needs_manual_resume"
                value["resume_state"] = "needs_manual_resume"
                path.write_text(json.dumps(value), encoding="utf-8")
            server.request_manual_resume(report_dir, requested_by="web")
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                    mock.patch.object(server, "persist_job"),
                ):
                    server.load_persisted_jobs()

                job = server.JOBS["WEB-resume"]
                launch.assert_called_once_with(job, devices=["serial-1"])
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_explicit_request_stays_waiting_when_device_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root, _job_dir, report_dir = self._write_persisted_job(Path(temporary))
            server.request_manual_resume(report_dir, requested_by="web")
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", runs_root),
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_DEVICE_UNAVAILABLE,
                    ) as cleanup,
                    mock.patch.object(server, "_launch_job_cli") as launch,
                    mock.patch.object(server, "persist_job"),
                ):
                    server.load_persisted_jobs()

                job = server.JOBS["WEB-resume"]
                self.assertEqual("waiting_for_device", job["status"])
                launch.assert_not_called()
                cleanup.assert_called_once_with("serial-1")
                self.assertTrue((report_dir / "resume.request.json").is_file())
                report = json.loads(
                    (report_dir / "report.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    "device transport is unavailable before requested resume",
                    report["last_error"],
                )
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_explicit_request_detaches_only_target_from_live_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, report_paths = ManualStopTests._job_with_reports(
                root, ["serial-1", "serial-2"]
            )
            server.request_manual_resume(report_paths["serial-1"].parent, requested_by="web")
            primary = mock.Mock(spec=server.subprocess.Popen)
            primary.pid = 123456794
            primary.poll.return_value = None
            job.update(
                {
                    "status": "running",
                    "process": primary,
                    "pid": primary.pid,
                    "_primaryProcessKey": "primary",
                    "_processes": {"primary": primary},
                    "_processDevices": {"primary": ["serial-2"]},
                }
            )
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                ):
                    dispatched = server._maybe_dispatch_persisted_recovery(job)

                self.assertTrue(dispatched)
                launch.assert_called_once_with(job, devices=["serial-1"])
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_auto_recovery_states_are_explicitly_resumable(self) -> None:
        self.assertTrue(server._can_resume_device_state("recovering", resume_mode="auto"))
        self.assertTrue(
            server._can_resume_device_state("waiting_for_device", resume_mode="auto")
        )
        self.assertTrue(server._can_resume_device_state("paused", resume_mode="auto"))
        self.assertFalse(server._can_resume_device_state("resuming", resume_mode="auto"))


class ResumeEndpointTests(unittest.TestCase):
    @staticmethod
    def _job_fixture(root: Path) -> tuple[dict, Path, Path]:
        job_dir = root / "job"
        report_root = job_dir / "reports"
        report_dir = report_root / "run-1_serial-1"
        report_dir.mkdir(parents=True)
        config_path = job_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "resume": {
                        "enabled": True,
                        "mode": "auto",
                        "max_attempts": 3,
                        "checkpoint_interval_seconds": 10,
                    },
                    "app": {"package_name": "com.example.sample"},
                    "test": {"fastbot_duration_minutes": 1, "max_cycles": 2},
                }
            ),
            encoding="utf-8",
        )
        (job_dir / "console.log").write_bytes(b"")
        report = {
            "run_id": "run-1",
            "job_id": "WEB-resume-endpoint",
            "device_serial": "serial-1",
            "package_name": "com.example.sample",
            "status": "paused",
            "resume_state": "paused",
            "manual_stop": True,
            "resume_attempts": 1,
            "last_error": "pressure stopped by web console",
            "current_cycle": {
                "cycle": 1,
                "status": "paused",
                "pressure_elapsed_seconds": 12,
                "operation_count": 7,
            },
            "cycles": [],
            "errors": ["pressure stopped by web console"],
        }
        checkpoint_value = {
            "schema_version": 1,
            "job_id": "run-1",
            "device_serial": "serial-1",
            "cycle": 1,
            "phase": 1,
            "elapsed_seconds": 12,
            "remaining_seconds": 48,
            "operation_count": 7,
            "resume_attempts": 1,
            "resume_state": "paused",
            "manual_stop": True,
            "last_error": "pressure stopped by web console",
        }
        (report_dir / "report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        (report_dir / "checkpoint.json").write_text(
            json.dumps(checkpoint_value), encoding="utf-8"
        )
        job = {
            "id": "WEB-resume-endpoint",
            "runId": "run-1",
            "status": "paused",
            "phase": "paused",
            "startedAt": server.utc_now(),
            "startedEpoch": 1.0,
            "finishedAt": None,
            "packageName": "com.example.sample",
            "apkName": "sample.apk",
            "devices": ["serial-1"],
            "duration": 1,
            "cycles": 2,
            "workers": 1,
            "pid": None,
            "jobDir": str(job_dir),
            "reportRoot": str(report_root),
            "logPath": str(job_dir / "console.log"),
            "configPath": str(config_path),
            "process": None,
            "_processes": {},
            "_processDevices": {},
            "manualStopRequested": False,
            "manualStopDevices": ["serial-1"],
        }
        return job, report_dir / "report.json", report_dir / "checkpoint.json"

    def test_resume_publishes_resuming_before_child_heartbeat(self) -> None:
        class FakeProcess:
            pid = 123456789

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            job, report_path, checkpoint_path = self._job_fixture(Path(temporary))
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server.subprocess, "Popen", return_value=FakeProcess()) as launch,
                ):
                    result = server.resume_job_device(job["id"], "serial-1")

                self.assertEqual("resuming", job["status"])
                self.assertEqual("resuming", result["status"])
                report = json.loads(report_path.read_text(encoding="utf-8"))
                checkpoint_value = json.loads(
                    checkpoint_path.read_text(encoding="utf-8")
                )
                self.assertEqual("resuming", report["resume_state"])
                self.assertFalse(report["manual_stop"])
                self.assertEqual("resuming", checkpoint_value["resume_state"])
                self.assertFalse(checkpoint_value["manual_stop"])
                launch.assert_called_once()
            finally:
                claim = server._read_resume_claim(job, "serial-1")
                if claim:
                    parent = server._resume_claim_parent(job, "serial-1")
                    if parent is not None:
                        server.release_resume_claim(
                            parent, str(claim.get("claim_id") or "")
                        )
                stream = job.pop("logStream", None)
                if stream is not None:
                    stream.close()
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_manual_resume_clears_stale_whole_job_stop_marker(self) -> None:
        class FakeProcess:
            pid = 123456791

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            job, _report_path, _checkpoint_path = self._job_fixture(Path(temporary))
            job["manualStopRequested"] = True
            job["stopRequestedAt"] = "2026-09-04T00:00:00+00:00"
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server.subprocess, "Popen", return_value=FakeProcess()),
                ):
                    server.resume_job_device(job["id"], "serial-1")

                self.assertFalse(job["manualStopRequested"])
                self.assertIsNone(job["stopRequestedAt"])
                self.assertIsNone(job.get("stopDeadlineEpoch"))
            finally:
                claim = server._read_resume_claim(job, "serial-1")
                if claim:
                    parent = server._resume_claim_parent(job, "serial-1")
                    if parent is not None:
                        server.release_resume_claim(
                            parent, str(claim.get("claim_id") or "")
                        )
                stream = job.pop("logStream", None)
                if stream is not None:
                    stream.close()
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_offline_manual_resume_renews_expired_reconnect_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, report_path, checkpoint_path = self._job_fixture(Path(temporary))
            expired = "2020-01-01T00:00:00+00:00"
            for path in (report_path, checkpoint_path):
                value = json.loads(path.read_text(encoding="utf-8"))
                value["status"] = "needs_manual_resume"
                value["resume_state"] = "needs_manual_resume"
                value["wait_started_at"] = expired
                value["wait_deadline_at"] = expired
                path.write_text(json.dumps(value), encoding="utf-8")
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_DEVICE_UNAVAILABLE,
                    ),
                    mock.patch.object(server, "persist_job"),
                ):
                    result = server.resume_job_device(job["id"], "serial-1")
                    server.refresh_job(job)

                self.assertEqual("waiting_for_device", result["status"])
                self.assertEqual("waiting_for_device", job["status"])
                for path in (report_path, checkpoint_path):
                    value = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual("waiting_for_device", value["resume_state"])
                    self.assertNotEqual(expired, value["wait_started_at"])
                    self.assertNotEqual(expired, value["wait_deadline_at"])
            finally:
                stream = job.pop("logStream", None)
                if stream is not None:
                    stream.close()
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_duplicate_resume_click_cannot_detach_inflight_worker(self) -> None:
        class FakeProcess:
            pid = 123456790

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            job, report_path, checkpoint_path = self._job_fixture(Path(temporary))
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server.subprocess, "Popen", return_value=FakeProcess()) as launch,
                ):
                    server.resume_job_device(job["id"], "serial-1")

                    # Simulate the delayed first worker heartbeat.  The
                    # durable in-flight claim must still win over this stale
                    # paused marker.
                    for path in (report_path, checkpoint_path):
                        value = json.loads(path.read_text(encoding="utf-8"))
                        value["status"] = "paused"
                        value["resume_state"] = "paused"
                        value["manual_stop"] = True
                        path.write_text(json.dumps(value), encoding="utf-8")

                    with self.assertRaises(server.ApiError) as raised:
                        server.resume_job_device(job["id"], "serial-1")

                self.assertEqual(409, raised.exception.status)
                launch.assert_called_once()
                self.assertEqual(
                    ["serial-1"], job.get("resumeInFlightDevices")
                )
            finally:
                claim = server._read_resume_claim(job, "serial-1")
                if claim:
                    parent = server._resume_claim_parent(job, "serial-1")
                    if parent is not None:
                        server.release_resume_claim(
                            parent, str(claim.get("claim_id") or "")
                        )
                stream = job.pop("logStream", None)
                if stream is not None:
                    stream.close()
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_manual_resume_releases_finished_device_claim_from_shared_worker(self) -> None:
        class FakeProcess:
            def __init__(self, pid: int):
                self.pid = pid

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            job, report_path, checkpoint_path = self._job_fixture(Path(temporary))
            for path in (report_path, checkpoint_path):
                value = json.loads(path.read_text(encoding="utf-8"))
                value["status"] = "needs_manual_resume"
                value["resume_state"] = "needs_manual_resume"
                path.write_text(json.dumps(value), encoding="utf-8")
            primary = FakeProcess(os.getpid())
            job.update({
                "status": "running",
                "devices": ["serial-1", "serial-2"],
                "process": primary,
                "_primaryProcessKey": "primary",
                "_processes": {"primary": primary},
                "_processDevices": {"primary": ["serial-1", "serial-2"]},
            })
            claim = server.acquire_resume_claim(
                report_path.parent,
                job_id=job["id"],
                device_serial="serial-1",
                requested_by="worker",
            )
            self.assertIsNotNone(claim)
            server.update_resume_claim(
                report_path.parent,
                str(claim["claim_id"]),
                worker_pid=os.getpid(),
            )
            server._remember_resume_claim(job, "serial-1", claim)
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(
                        server.subprocess,
                        "Popen",
                        return_value=FakeProcess(123456792),
                    ) as launch,
                ):
                    result = server.resume_job_device(job["id"], "serial-1")

                self.assertEqual("resuming", result["status"])
                self.assertEqual(["serial-2"], job["_processDevices"]["primary"])
                launch.assert_called_once()
            finally:
                current = server._read_resume_claim(job, "serial-1")
                if current:
                    server.release_resume_claim(
                        report_path.parent, str(current.get("claim_id") or "")
                    )
                server.RESUME_IN_FLIGHT.discard((job["id"], "serial-1"))
                stream = job.pop("logStream", None)
                if stream is not None:
                    stream.close()
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_refresh_dispatches_detached_waiting_device_while_siblings_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, report_path, checkpoint_path = self._job_fixture(Path(temporary))
            for path in (report_path, checkpoint_path):
                value = json.loads(path.read_text(encoding="utf-8"))
                value["status"] = "waiting_for_device"
                value["resume_state"] = "waiting_for_device"
                value["manual_stop"] = False
                path.write_text(json.dumps(value), encoding="utf-8")

            primary = mock.Mock(spec=server.subprocess.Popen)
            primary.pid = 123456793
            primary.poll.return_value = None
            job.update(
                {
                    "status": "running",
                    "phase": "running",
                    "devices": ["serial-1", "serial-2"],
                    "process": primary,
                    "pid": primary.pid,
                    "_primaryProcessKey": "primary",
                    "_processes": {"primary": primary},
                    "manualStopDevices": [],
                    # serial-1 was detached by its explicit resume request;
                    # the original CLI still owns only its live sibling.
                    "_processDevices": {"primary": ["serial-2"]},
                }
            )

            with (
                mock.patch.object(
                    server,
                    "_best_effort_remote_worker_cleanup_status",
                    return_value=server._REMOTE_CLEANUP_RELEASED,
                ) as cleanup,
                mock.patch.object(server, "_launch_job_cli") as launch,
                mock.patch.object(server, "persist_job"),
            ):
                server.refresh_job(job)

            cleanup.assert_called_once_with("serial-1")
            launch.assert_called_once_with(job, devices=["serial-1"])

    def test_refresh_does_not_dispatch_device_still_owned_by_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, report_path, checkpoint_path = self._job_fixture(Path(temporary))
            for path in (report_path, checkpoint_path):
                value = json.loads(path.read_text(encoding="utf-8"))
                value["status"] = "waiting_for_device"
                value["resume_state"] = "waiting_for_device"
                value["manual_stop"] = False
                path.write_text(json.dumps(value), encoding="utf-8")

            primary = mock.Mock(spec=server.subprocess.Popen)
            primary.pid = 123456794
            primary.poll.return_value = None
            job.update(
                {
                    "status": "running",
                    "phase": "running",
                    "process": primary,
                    "pid": primary.pid,
                    "_primaryProcessKey": "primary",
                    "_processes": {"primary": primary},
                    "_processDevices": {"primary": ["serial-1"]},
                }
            )

            with (
                mock.patch.object(
                    server, "_best_effort_remote_worker_cleanup_status"
                ) as cleanup,
                mock.patch.object(server, "_launch_job_cli") as launch,
                mock.patch.object(server, "persist_job"),
            ):
                server.refresh_job(job)

            cleanup.assert_not_called()
            launch.assert_not_called()


class ManualStopTests(unittest.TestCase):
    @staticmethod
    def _job_with_reports(root: Path, serials: list[str]) -> tuple[dict, dict[str, Path]]:
        job_dir = root / "runs" / "WEB-manual-stop"
        report_root = job_dir / "reports"
        report_root.mkdir(parents=True)
        config_path = job_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "app": {"package_name": "com.example.sample"},
                    "test": {"fastbot_duration_minutes": 1, "max_cycles": 2},
                    "resume": {"enabled": True, "mode": "auto", "max_attempts": 3},
                }
            ),
            encoding="utf-8",
        )
        report_paths: dict[str, Path] = {}
        for serial in serials:
            report_dir = report_root / ("run-1_%s" % serial)
            report_dir.mkdir()
            report_path = report_dir / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "device_serial": serial,
                        "package_name": "com.example.sample",
                        "status": "waiting_for_device" if serial == serials[0] else "recovering",
                        "resume_state": "waiting_for_device" if serial == serials[0] else "recovering",
                        "resume_attempts": 1,
                        "remaining_seconds": 48,
                        "current_cycle": {
                            "cycle": 1,
                            "phase": 1,
                            "status": "waiting_for_device" if serial == serials[0] else "recovering",
                            "pressure_elapsed_seconds": 12,
                            "operation_count": 7,
                        },
                        "cycles": [],
                        "errors": [],
                        "last_error": "device transport is temporarily unavailable",
                    }
                ),
                encoding="utf-8",
            )
            (report_dir / "checkpoint.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": "run-1",
                        "device_serial": serial,
                        "cycle": 1,
                        "phase": 1,
                        "elapsed_seconds": 12,
                        "remaining_seconds": 48,
                        "operation_count": 7,
                        "resume_attempts": 1,
                        "resume_state": "waiting_for_device" if serial == serials[0] else "recovering",
                        "last_heartbeat": server.utc_now(),
                    }
                ),
                encoding="utf-8",
            )
            report_paths[serial] = report_path
        job = {
            "id": "WEB-manual-stop",
            "runId": "run-1",
            "status": "waiting_for_device",
            "phase": "waiting_for_device",
            "startedAt": server.utc_now(),
            "startedEpoch": 1.0,
            "finishedAt": None,
            "packageName": "com.example.sample",
            "apkName": "sample.apk",
            "devices": serials,
            "duration": 1,
            "cycles": 2,
            "workers": 2,
            "pid": None,
            "exitCode": None,
            "jobDir": str(job_dir),
            "reportRoot": str(report_root),
            "logPath": str(job_dir / "console.log"),
            "configPath": str(config_path),
            "process": None,
            "_processes": {},
            "_processDevices": {},
            "manualStopRequested": False,
            "manualStopDevices": [],
        }
        return job, report_paths

    def test_offline_stop_is_persisted_as_manual_pause_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, report_paths = self._job_with_reports(root, ["serial-1"])
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "offline"}},
                ):
                    result = server.stop_devices(["serial-1"])

                self.assertFalse(result[0]["stopped"])
                self.assertEqual(["serial-1"], job["manualStopDevices"])
                self.assertEqual("paused", job["status"])
                report = json.loads(report_paths["serial-1"].read_text(encoding="utf-8"))
                checkpoint = json.loads(
                    (report_paths["serial-1"].parent / "checkpoint.json").read_text(
                        encoding="utf-8"
                    )
                )
                for value in (report, checkpoint):
                    self.assertEqual("paused", value["resume_state"])
                    self.assertEqual("manual_stop", value["recovery_category"])
                    self.assertEqual("pressure stopped by web console", value["last_error"])

                # Rehydrate the same job after a service restart.  A device
                # that was explicitly stopped must not be auto-dispatched
                # merely because it becomes reachable again.
                server.JOBS.clear()
                with mock.patch.object(server, "RUN_ROOT", root / "runs"):
                    server.load_persisted_jobs()
                restored = server.JOBS[job["id"]]
                self.assertEqual("paused", restored["status"])
                with mock.patch.object(server, "_launch_job_cli") as launch:
                    server.refresh_job(restored)
                launch.assert_not_called()
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)

    def test_stopping_offline_sibling_does_not_block_recovery_of_other_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, report_paths = self._job_with_reports(
                Path(temporary), ["serial-1", "serial-2"]
            )
            previous_jobs = dict(server.JOBS)
            server.JOBS.clear()
            server.JOBS[job["id"]] = job
            try:
                with mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "offline"}},
                ):
                    server.stop_devices(["serial-1"])
                self.assertEqual("paused", server._persisted_job_device_states(job)["serial-1"]["state"])

                with (
                    mock.patch.object(
                        server,
                        "_best_effort_remote_worker_cleanup_status",
                        return_value=server._REMOTE_CLEANUP_RELEASED,
                    ),
                    mock.patch.object(server, "_launch_job_cli") as launch,
                ):
                    dispatched = server._maybe_dispatch_persisted_recovery(job)

                self.assertTrue(dispatched)
                launch.assert_called_once_with(job, devices=["serial-2"])
                sibling = json.loads(report_paths["serial-2"].read_text(encoding="utf-8"))
                self.assertEqual("recovering", sibling["resume_state"])
            finally:
                server.JOBS.clear()
                server.JOBS.update(previous_jobs)


class ManualEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_jobs = dict(server.JOBS)
        server.JOBS.clear()

    def tearDown(self) -> None:
        server.JOBS.clear()
        server.JOBS.update(self.previous_jobs)

    def test_offline_end_releases_only_target_and_makes_it_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, report_paths = ManualStopTests._job_with_reports(
                Path(temporary), ["serial-1", "serial-2"]
            )
            server.JOBS[job["id"]] = job
            request_path = server.recovery_request_path(
                report_paths["serial-1"].parent
            )
            request_path.write_text("{}", encoding="utf-8")

            with mock.patch.object(
                server.DeviceManager,
                "get_device_states",
                return_value={"serial-1": {"adb_state": "offline"}},
            ):
                result = server.end_job_device(job["id"], "serial-1")

            self.assertTrue(result["manualEnd"])
            self.assertFalse(result["remoteStopped"])
            self.assertFalse(request_path.exists())
            ended_report = json.loads(
                report_paths["serial-1"].read_text(encoding="utf-8")
            )
            ended_checkpoint = json.loads(
                (report_paths["serial-1"].parent / "checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            for value in (ended_report, ended_checkpoint):
                self.assertEqual("manually_ended", value["resume_state"])
                self.assertTrue(value["manual_end"])
                self.assertEqual(0.0, value["remaining_seconds"])
            sibling = json.loads(
                report_paths["serial-2"].read_text(encoding="utf-8")
            )
            self.assertEqual("recovering", sibling["resume_state"])
            self.assertEqual(["serial-1"], job["manualEndDevices"])

            online_states = {
                "serial-1": {"adb_state": "device"},
                "serial-2": {"adb_state": "device"},
            }
            with (
                mock.patch.object(server, "_active_jobs", return_value=[job]),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value=online_states,
                ),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_info",
                    side_effect=lambda serial: {
                        "manufacturer": "Example",
                        "model": serial,
                        "android_version": "14",
                        "sdk": "34",
                        "screen_size": "1080x2400",
                    },
                ),
                mock.patch.object(
                    server.DeviceManager, "is_device_ready", return_value=True
                ),
                mock.patch.object(server, "_battery_level", return_value=80),
            ):
                devices = {
                    item["serial"]: item for item in server.list_devices()
                }

            self.assertEqual("ready", devices["serial-1"]["status"])
            self.assertIsNone(devices["serial-1"]["jobId"])
            self.assertFalse(devices["serial-1"]["canResume"])
            self.assertFalse(devices["serial-1"]["canEnd"])
            self.assertTrue(devices["serial-1"]["manualEnd"])
            self.assertEqual(job["id"], devices["serial-2"]["jobId"])
            self.assertTrue(devices["serial-2"]["canEnd"])

    def test_repeated_end_is_idempotent_and_cannot_be_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, report_paths = ManualStopTests._job_with_reports(
                Path(temporary), ["serial-1"]
            )
            server.JOBS[job["id"]] = job
            controller = mock.Mock()
            controller.request_stop_remote_fastbot.return_value = True
            with (
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(
                    server, "MutexController", return_value=controller
                ),
            ):
                first = server.end_job_device(job["id"], "serial-1")
                request_path = server.recovery_request_path(
                    report_paths["serial-1"].parent
                )
                request_path.write_text("{}", encoding="utf-8")
                second = server.end_job_device(job["id"], "serial-1")

            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            controller.request_stop_remote_fastbot.assert_called_once_with()
            self.assertFalse(request_path.exists())
            self.assertEqual("stopped", job["status"])
            self.assertEqual(["serial-1"], job["manualEndDevices"])
            with self.assertRaises(server.ApiError) as raised:
                server.resume_job_device(job["id"], "serial-1")
            self.assertEqual(409, raised.exception.status)

    def test_shared_process_keeps_running_for_sibling(self) -> None:
        class FakeProcess:
            pid = 4321

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            job, _report_paths = ManualStopTests._job_with_reports(
                Path(temporary), ["serial-1", "serial-2"]
            )
            process = FakeProcess()
            job.update(
                {
                    "process": process,
                    "pid": process.pid,
                    "_primaryProcessKey": "primary",
                    "_processes": {"primary": process},
                    "_processDevices": {
                        "primary": ["serial-1", "serial-2"]
                    },
                }
            )
            server.JOBS[job["id"]] = job
            with (
                mock.patch.object(server.subprocess, "Popen", new=mock.Mock()),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "offline"}},
                ),
                mock.patch.object(server, "_signal_local_stop") as signal_stop,
            ):
                server.end_job_device(job["id"], "serial-1")

            signal_stop.assert_not_called()
            self.assertIs(process, job["process"])
            self.assertEqual(["serial-2"], job["_processDevices"]["primary"])
            self.assertNotEqual("stopped", job["status"])

    def test_solo_process_is_signalled_and_job_is_immediately_terminal(self) -> None:
        class FakeProcess:
            pid = 4322

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            job, _report_paths = ManualStopTests._job_with_reports(
                Path(temporary), ["serial-1"]
            )
            process = FakeProcess()
            job.update(
                {
                    "process": process,
                    "pid": process.pid,
                    "_primaryProcessKey": "primary",
                    "_processes": {"primary": process},
                    "_processDevices": {"primary": ["serial-1"]},
                }
            )
            server.JOBS[job["id"]] = job
            with (
                mock.patch.object(server.subprocess, "Popen", new=mock.Mock()),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "offline"}},
                ),
                mock.patch.object(
                    server, "_signal_local_stop", return_value=True
                ) as signal_stop,
            ):
                server.end_job_device(job["id"], "serial-1")

            signal_stop.assert_called_once_with(process)
            self.assertEqual("stopped", job["status"])
            self.assertEqual("stopped", job["phase"])
            self.assertIsNotNone(job["finishedAt"])

    def test_restart_does_not_reopen_a_durable_manual_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, _report_paths = ManualStopTests._job_with_reports(
                root, ["serial-1"]
            )
            persisted = server._persist_manual_end_artifacts(
                job,
                "serial-1",
                "device test ended manually from web console",
            )
            self.assertTrue(persisted["written"])
            # Simulate a service exit after the terminal device files were
            # committed but before the active job.json was updated.
            server.persist_job(job)
            server.JOBS.clear()

            with (
                mock.patch.object(server, "RUN_ROOT", root / "runs"),
                mock.patch.object(server, "_launch_job_cli") as launch,
                mock.patch.object(
                    server, "_best_effort_remote_worker_cleanup_status"
                ) as cleanup,
            ):
                server.load_persisted_jobs()

            restored = server.JOBS[job["id"]]
            self.assertEqual("stopped", restored["status"])
            self.assertEqual("stopped", restored["phase"])
            launch.assert_not_called()
            cleanup.assert_not_called()


class DeviceAvailabilityTests(unittest.TestCase):
    def test_new_task_owns_reused_devices_in_either_job_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def make_job(job_id, device_states, status):
                reports = root / job_id / "reports"
                reports.mkdir(parents=True)
                job = {
                    "id": job_id,
                    "runId": job_id + "-run",
                    "status": status,
                    "packageName": "com.example.sample",
                    "devices": list(device_states),
                    "duration": 10,
                    "cycles": 1,
                    "reportRoot": str(reports),
                }
                for serial, device_state in device_states.items():
                    if device_state is None:
                        continue
                    report_dir = reports / (job["runId"] + "_" + serial)
                    report_dir.mkdir()
                    running = device_state == "running"
                    (report_dir / "report.json").write_text(
                        json.dumps({
                            "run_id": job["runId"],
                            "device_serial": serial,
                            "package_name": job["packageName"],
                            "status": device_state,
                            "resume_state": device_state,
                            "statistics": {
                                "pressure_elapsed_seconds": 180 if running else 60,
                                "total_operations": 34098 if running else 118,
                            },
                        }),
                        encoding="utf-8",
                    )
                return job

            old = make_job("WEB-original", {
                "reused-running": "completed_with_errors",
                "reused-preparing": "completed_with_errors",
                "completed-sibling": "completed",
                "recovering-sibling": "needs_manual_resume",
            }, "needs_manual_resume")
            running = make_job("WEB-second", {"reused-running": "running"}, "running")
            preparing = make_job("WEB-third", {"reused-preparing": None}, "preparing")
            states = {serial: {"adb_state": "device"} for serial in old["devices"]}
            for jobs in ([old, running, preparing], [preparing, running, old]):
                with (
                    self.subTest(job_order=[job["id"] for job in jobs]),
                    mock.patch.object(server, "_active_jobs", return_value=jobs),
                    mock.patch.object(server, "_live_process_owners", return_value=[]),
                    mock.patch.object(server.DeviceManager, "get_device_states", return_value=states),
                    mock.patch.object(server.DeviceManager, "get_device_info", return_value={}),
                    mock.patch.object(server.DeviceManager, "is_device_ready", return_value=True),
                    mock.patch.object(server, "_battery_level", return_value=80),
                ):
                    rows = {row["serial"]: row for row in server.list_devices()}
                    self.assertEqual(running["id"], rows["reused-running"]["jobId"])
                    self.assertEqual("running", rows["reused-running"]["status"])
                    self.assertEqual(34098, rows["reused-running"]["operations"])
                    self.assertEqual(30, rows["reused-running"]["progress"])
                    self.assertTrue(rows["reused-running"]["canEnd"])
                    self.assertEqual(preparing["id"], rows["reused-preparing"]["jobId"])
                    self.assertEqual("preparing", rows["reused-preparing"]["status"])
                    self.assertEqual(0, rows["reused-preparing"]["operations"])
                    self.assertIsNone(rows["completed-sibling"]["jobId"])
                    self.assertEqual("ready", rows["completed-sibling"]["status"])
                    self.assertEqual(old["id"], rows["recovering-sibling"]["jobId"])
                    self.assertEqual("needs_manual_resume", rows["recovering-sibling"]["status"])

    def test_completed_siblings_are_selectable_while_one_device_needs_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "WEB-device-availability"
            report_root = job_dir / "reports"
            report_root.mkdir(parents=True)
            config_path = job_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "resume": {"enabled": True, "mode": "auto"},
                        "app": {"package_name": "com.example.sample"},
                        "test": {"fastbot_duration_minutes": 1, "max_cycles": 1},
                    }
                ),
                encoding="utf-8",
            )

            def write_report(serial: str, state: str) -> None:
                report_dir = report_root / ("run-1_%s" % serial)
                report_dir.mkdir()
                completed = state == "completed"
                (report_dir / "report.json").write_text(
                    json.dumps(
                        {
                            "run_id": "run-1",
                            "device_serial": serial,
                            "package_name": "com.example.sample",
                            "status": state,
                            "resume_state": state,
                            "resume_attempts": 0 if completed else 3,
                            "remaining_seconds": 0 if completed else 50,
                            "device_info": {
                                "manufacturer": "Example",
                                "model": serial,
                                "android_version": "14",
                                "sdk": "34",
                                "screen_size": "1080x2400",
                            },
                            "cycles": [
                                {
                                    "cycle": 1,
                                    "status": "passed",
                                    "pressure_elapsed_seconds": 60,
                                    "operation_count": 100,
                                }
                            ]
                            if completed
                            else [],
                            "current_cycle": None
                            if completed
                            else {
                                "cycle": 1,
                                "status": state,
                                "pressure_elapsed_seconds": 10,
                                "operation_count": 10,
                            },
                            "statistics": {
                                "pressure_elapsed_seconds": 60 if completed else 10,
                                "total_operations": 100 if completed else 10,
                            },
                            "last_error": None
                            if completed
                            else "device transport connection failed",
                        }
                    ),
                    encoding="utf-8",
                )

            completed_online = "serial-completed"
            completed_offline = "serial-completed-offline"
            interrupted = "serial-interrupted"
            for serial, state in (
                (completed_online, "completed"),
                (completed_offline, "completed"),
                (interrupted, "needs_manual_resume"),
            ):
                write_report(serial, state)

            job = {
                "id": "WEB-device-availability",
                "runId": "run-1",
                "status": "needs_manual_resume",
                "phase": "needs_manual_resume",
                "packageName": "com.example.sample",
                "devices": [completed_online, completed_offline, interrupted],
                "duration": 1,
                "cycles": 1,
                "reportRoot": str(report_root),
                "configPath": str(config_path),
            }
            device_states = {
                completed_online: {"adb_state": "device"},
                completed_offline: {"adb_state": "offline"},
                interrupted: {"adb_state": "device"},
            }
            with (
                mock.patch.object(server, "_active_jobs", return_value=[job]),
                # A multi-device CLI can remain alive for the interrupted
                # sibling after another device has written its terminal report.
                mock.patch.object(
                    server, "_live_process_owners", return_value=["primary"]
                ),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value=device_states,
                ),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_info",
                    side_effect=lambda serial: {
                        "manufacturer": "Example",
                        "model": serial,
                        "android_version": "14",
                        "sdk": "34",
                        "screen_size": "1080x2400",
                    },
                ),
                mock.patch.object(
                    server.DeviceManager,
                    "is_device_ready",
                    side_effect=lambda serial: serial != completed_offline,
                ),
                mock.patch.object(server, "_battery_level", return_value=80),
            ):
                rows = {
                    item["serial"]: item for item in server.list_devices()
                }

        self.assertEqual("ready", rows[completed_online]["status"])
        self.assertIsNone(rows[completed_online]["jobId"])
        self.assertFalse(rows[completed_online]["canResume"])
        self.assertEqual("offline", rows[completed_offline]["status"])
        self.assertIsNone(rows[completed_offline]["jobId"])
        self.assertFalse(rows[completed_offline]["canResume"])
        self.assertEqual("needs_manual_resume", rows[interrupted]["status"])
        self.assertEqual(job["id"], rows[interrupted]["jobId"])
        self.assertTrue(rows[interrupted]["canResume"])


class ReportTests(unittest.TestCase):
    def test_device_report_explains_permission_failure_from_the_final_observation(self) -> None:
        settings = "com.android.settings/com.android.settings.Settings$AppManageExternalStorageActivity"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            for error, recovery, expected in (
                ("permission recovery timed out", {"last_component": settings}, "所有文件访问权限"),
                ("permission recovery timed out", {"last_component": "com.android.settings/.Settings"}, "未确认被测 App 回到前台"),
                ("positive permission button was not found", {"attempts": [{"ui_dump_succeeded": True}]}, "未找到可点击的“允许”按钮"),
                ("positive permission button was not found", {"attempts": [{"ui_dump_succeeded": False}]}, "无法读取弹窗控件"),
                ("ADB tap of the positive permission button failed", {}, "未能发送“允许”点击"),
                ("permission dialog reappeared repeatedly without pressure progress", {"success": True, "last_component": settings}, "权限弹窗反复出现"),
                ("device 'serial-1' not found", {"last_component": settings}, "权限处理期间设备断连"),
            ):
                with self.subTest(error=error, recovery=recovery):
                    raw_error = "runtime permission recovery failed: " + error
                    path.write_text(json.dumps({
                        "run_id": "run-1",
                        "device_serial": "serial-1",
                        "package_name": "com.example.sample",
                        "status": "completed_with_errors",
                        "cycles": [{
                            "cycle": 1,
                            "status": "failed",
                            "stop_reason": "permission_recovery",
                            "errors": [raw_error],
                            "permission_recovery_failed": True,
                            "permission_recovery_error": error,
                            "permission_recoveries": [{"success": False, "error": error, **recovery}],
                        }],
                    }), encoding="utf-8")

                    device = server.device_report_detail(path)

                    self.assertEqual("failed", device["status"])
                    self.assertIn(expected, device.get("permissionFailureReason") or "")
                    if expected != "所有文件访问权限":
                        self.assertNotIn("所有文件访问权限", device["permissionFailureReason"])
                    self.assertIn(raw_error, device["cycleResults"][0]["errors"])

    def test_permission_recovery_that_did_not_end_the_cycle_has_no_permission_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps({
                "run_id": "run-1",
                "device_serial": "serial-1",
                "status": "completed_with_errors",
                "cycles": [{
                    "cycle": 1,
                    "status": "failed",
                    "stop_reason": "device_disconnected",
                    "errors": ["device offline"],
                    "permission_recoveries": [{"success": True}],
                }],
            }), encoding="utf-8")

            self.assertFalse(server.device_report_detail(path).get("permissionFailureReason"))

    @staticmethod
    def _write_report(
        path: Path,
        serial: str,
        status: str,
        elapsed: int,
        crashes=None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "run_id": "engine-run-1",
                "device_serial": serial,
                "package_name": "com.example.sample",
                "status": status,
                "config": {"test": {"fastbot_duration_minutes": 1, "max_cycles": 2}},
                "cycles": [
                    {
                        "cycle": 1,
                        "status": "passed" if status == "completed" else "failed",
                        "pressure_elapsed_seconds": elapsed,
                        "operation_count": 100,
                        "errors": [],
                    }
                ],
                "crashes": crashes or [],
                "issues": [],
                "statistics": {
                    "total_cycles": 1,
                    "total_operations": 100,
                    "pressure_elapsed_seconds": elapsed,
                    "crash_count": len(crashes or []),
                },
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_test_reports_group_every_device_under_one_web_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-TEST-001"
            reports = job_dir / "reports"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps({
                    "id": "WEB-TEST-001",
                    "status": "completed",
                    "packageName": "com.example.sample",
                    "apkName": "sample.apk",
                    "devices": ["serial-1", "serial-2"],
                    "duration": 1,
                    "cycles": 2,
                    "startedAt": "2026-08-24T10:00:00+00:00",
                    "finishedAt": "2026-08-24T10:10:00+00:00",
                }),
                encoding="utf-8",
            )
            self._write_report(reports / "one" / "report.json", "serial-1", "completed", 120)
            self._write_report(reports / "two" / "report.json", "serial-2", "completed_with_errors", 30, [{"signature": "boom"}])
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                values = server.list_test_reports()

        self.assertEqual(1, len(values))
        value = values[0]
        self.assertEqual("WEB-TEST-001", value["testId"])
        self.assertEqual(2, value["deviceCount"])
        self.assertEqual(2, value["reportedDevices"])
        self.assertEqual("failed", value["status"])
        self.assertEqual(150, value["pressureElapsedSeconds"])
        self.assertEqual(240, value["pressureTargetSeconds"])
        self.assertEqual(62.5, value["pressurePercent"])
        self.assertEqual(1, value["crashes"])

    def test_batch_pressure_does_not_credit_one_device_overrun_to_another(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-TEST-CAPPED"
            reports = job_dir / "reports"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps({
                    "id": "WEB-TEST-CAPPED",
                    "status": "failed",
                    "packageName": "com.example.sample",
                    "devices": ["serial-1", "serial-2"],
                    "duration": 1,
                    "cycles": 2,
                }),
                encoding="utf-8",
            )
            self._write_report(
                reports / "one" / "report.json", "serial-1", "completed", 135
            )
            self._write_report(
                reports / "two" / "report.json",
                "serial-2",
                "completed_with_errors",
                90,
            )
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                values = server.list_test_reports()

        self.assertEqual(1, len(values))
        value = values[0]
        self.assertEqual(210, value["pressureElapsedSeconds"])
        self.assertEqual(240, value["pressureTargetSeconds"])
        self.assertEqual(87.5, value["pressurePercent"])

    def test_running_batch_counts_pending_reports_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-TEST-RUNNING"
            reports = job_dir / "reports"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps({
                    "id": "WEB-TEST-RUNNING",
                    "status": "running",
                    "packageName": "com.example.sample",
                    "devices": ["serial-1", "serial-2"],
                    "duration": 1,
                    "cycles": 2,
                }),
                encoding="utf-8",
            )
            self._write_report(
                reports / "one" / "report.json", "serial-1", "running", 0
            )
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                value = server.list_test_reports()[0]

        self.assertEqual("running", value["status"])
        self.assertEqual(2, value["runningDevices"])
        self.assertEqual(0, value["stoppedDevices"])
        self.assertEqual(1, value["reportedDevices"])

    def test_empty_screenshot_is_not_reported_as_fastbot_asset_failure(self) -> None:
        issues = server.normalized_issues({
            "issues": [{
                "issue_id": "screenshot-1",
                "type": "pressure_error",
                "title": "压测框架或准备失败",
                "problem_description": (
                    "stall recovery failed: ADB screenshot capture returned no data"
                ),
                "suggested_investigation": "同步 Fastbot JAR",
            }]
        })

        self.assertEqual("screenshot_unavailable", issues[0]["type"])
        self.assertEqual("截图验证不可用", issues[0]["title"])
        self.assertIn("FLAG_SECURE", issues[0]["suggestion"])
        self.assertNotIn("同步 Fastbot", issues[0]["suggestion"])

    def test_failure_manifest_write_error_is_reported_as_storage_failure(self) -> None:
        issues = server.normalized_issues({
            "issues": [{
                "issue_id": "storage-1",
                "type": "pressure_error",
                "title": "压测框架或准备失败",
                "problem_description": (
                    "[Errno 2] No such file or directory: "
                    "C:/runs/report/runtime_failures/cycle_001/phase_001/"
                    ".failure.json.16968.tmp"
                ),
                "suggested_investigation": "同步 Fastbot JAR",
            }]
        })

        self.assertEqual("artifact_persistence_error", issues[0]["type"])
        self.assertEqual("运行产物落盘失败", issues[0]["title"])
        self.assertIn("路径长度", issues[0]["suggestion"])
        self.assertNotIn("同步 Fastbot", issues[0]["suggestion"])

    def test_zero_pressure_time_does_not_fall_back_to_wall_clock_duration(self) -> None:
        report = {
            "started_at": "2026-08-25T10:00:00+00:00",
            "finished_at": "2026-08-25T10:20:00+00:00",
            "statistics": {"pressure_elapsed_seconds": 0.0},
        }

        self.assertEqual(0, server.duration_seconds(report))

    def test_delete_test_report_removes_only_the_selected_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs"
            job_dir = run_root / "WEB-DELETE-001"
            other_job_dir = run_root / "WEB-KEEP-002"
            apk_path = root / "uploads" / "sample.apk"
            (job_dir / "reports" / "serial-1").mkdir(parents=True)
            (job_dir / "logs").mkdir()
            other_job_dir.mkdir(parents=True)
            apk_path.parent.mkdir()
            apk_path.write_bytes(b"apk")
            (job_dir / "job.json").write_text(
                json.dumps({
                    "id": "WEB-DELETE-001",
                    "status": "completed",
                    "devices": ["serial-1"],
                }),
                encoding="utf-8",
            )
            (job_dir / "logs" / "runner.log").write_text("done", encoding="utf-8")
            (other_job_dir / "job.json").write_text(
                json.dumps({"id": "WEB-KEEP-002", "status": "completed"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                report_id = next(
                    item["id"] for item in server.list_test_reports()
                    if item["testId"] == "WEB-DELETE-001"
                )
                result = server.delete_test_report(report_id)

            self.assertTrue(result["deleted"])
            self.assertEqual("WEB-DELETE-001", result["testId"])
            self.assertFalse(job_dir.exists())
            self.assertTrue(other_job_dir.is_dir())
            self.assertTrue(apk_path.is_file())

    def test_delete_test_report_rejects_an_active_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-ACTIVE-001"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps({"id": "WEB-ACTIVE-001", "status": "completed"}),
                encoding="utf-8",
            )
            server.JOBS["WEB-ACTIVE-001"] = {
                "id": "WEB-ACTIVE-001",
                "status": "running",
            }
            try:
                with (
                    mock.patch.object(server, "RUN_ROOT", run_root),
                    mock.patch.object(server, "report_roots", return_value=[run_root]),
                ):
                    report_id = server.list_test_reports()[0]["id"]
                    with self.assertRaisesRegex(server.ApiError, "仍在运行") as raised:
                        server.delete_test_report(report_id)
            finally:
                server.JOBS.pop("WEB-ACTIVE-001", None)

            self.assertEqual(409, raised.exception.status)
            self.assertTrue(job_dir.is_dir())

    def test_delete_legacy_test_report_without_job_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-LEGACY-DELETE"
            report_path = job_dir / "reports" / "serial-1" / "report.json"
            self._write_report(report_path, "serial-1", "stopped", 30)
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                reports = server.list_test_reports()
                self.assertEqual(1, len(reports))
                self.assertEqual("WEB-LEGACY-DELETE", reports[0]["testId"])
                result = server.delete_test_report(reports[0]["id"])

            self.assertTrue(result["deleted"])
            self.assertFalse(job_dir.exists())

    def test_delete_test_report_accepts_a_stale_id_after_job_metadata_appears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-STALE-ID"
            report_path = job_dir / "reports" / "serial-1" / "report.json"
            self._write_report(report_path, "serial-1", "stopped", 30)
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                # The pre-metadata implementation keyed this row by the
                # report's run_id.  Keep that already-rendered ID usable after
                # the batch metadata appears.
                stale_id = hashlib.sha256(
                    b"run:engine-run-1"
                ).hexdigest()[:20]
                (job_dir / "job.json").write_text(
                    json.dumps(
                        {
                            "id": "WEB-STALE-ID",
                            "status": "stopped",
                            "devices": ["serial-1"],
                        }
                    ),
                    encoding="utf-8",
                )
                result = server.delete_test_report(stale_id)

            self.assertTrue(result["deleted"])
            self.assertEqual("WEB-STALE-ID", result["testId"])
            self.assertFalse(job_dir.exists())

    def test_delete_legacy_test_report_rejects_an_active_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            job_dir = run_root / "WEB-LEGACY-ACTIVE"
            report_path = job_dir / "reports" / "serial-1" / "report.json"
            self._write_report(report_path, "serial-1", "running", 30)
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "report_roots", return_value=[run_root]),
            ):
                report_id = server.list_test_reports()[0]["id"]
                with self.assertRaisesRegex(server.ApiError, "仍在运行") as raised:
                    server.delete_test_report(report_id)

            self.assertEqual(409, raised.exception.status)
            self.assertTrue(job_dir.is_dir())

    def test_delete_test_report_rejects_a_directory_outside_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs"
            outside = root / "outside-job"
            run_root.mkdir()
            outside.mkdir()
            group = {
                "jobDir": outside,
                "job": {"id": outside.name, "status": "completed"},
            }
            with (
                mock.patch.object(server, "RUN_ROOT", run_root),
                mock.patch.object(server, "test_report_index", return_value={"report-id": group}),
            ):
                with self.assertRaisesRegex(server.ApiError, "之外") as raised:
                    server.delete_test_report("report-id")

            self.assertEqual(403, raised.exception.status)
            self.assertTrue(outside.is_dir())

    def test_device_detail_exposes_crash_page_copy_screenshot_and_replayability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary) / "report"
            screenshot = report_dir / "crash_pages" / "cycle_001" / "crash_page.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"png")
            trajectory = report_dir / "trajectory_cycle_001.json"
            trajectory.write_text(
                json.dumps({"replayable": True, "events": [{"action": "click", "x": 10, "y": 20}]}),
                encoding="utf-8",
            )
            crash = {
                "cycle": 1,
                "exception_type": "java.lang.IllegalStateException",
                "message": "sample crash",
                "signature": "sample#1",
                "stacktrace": "stack line 1\nstack line 2",
                "copied_crash_content": "copied page crash text",
                "trajectory_path": str(trajectory),
                "app_crash_page": {
                    "component": "com.example.sample/.CrashActivity",
                    "artifacts": {"crash_page_screenshot": str(screenshot)},
                },
            }
            report_path = report_dir / "report.json"
            self._write_report(report_path, "serial-1", "completed_with_errors", 30, [crash])

            detail = server.device_report_detail(report_path)

        evidence = detail["crashDetail"][0]
        self.assertEqual("copied page crash text", evidence["copiedInfo"])
        self.assertEqual("stack line 1\nstack line 2", evidence["crashLog"])
        self.assertEqual("crash-0-crash_page_screenshot", evidence["screenshotArtifact"])
        self.assertTrue(evidence["replay"]["available"])
        self.assertEqual(1, evidence["replay"]["eventCount"])

    def test_device_detail_exposes_runtime_crash_page_evidence_when_copy_text_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary) / "report"
            evidence_dir = report_dir / "runtime_failures" / "cycle_002" / "phase_001"
            evidence_dir.mkdir(parents=True)
            screenshot = evidence_dir / "screen.png"
            screenshot.write_bytes(b"png")
            logcat = evidence_dir / "logcat_tail.log"
            logcat.write_text("FATAL EXCEPTION: main\njava.lang.NullPointerException", encoding="utf-8")
            hierarchy = evidence_dir / "ui.xml"
            hierarchy.write_text(
                """<hierarchy><node text=\"崩溃信息\" content-desc=\"崩溃信息\" />
                <node text=\"复制\" class=\"android.widget.Button\" />
                <node text=\"java.lang.NullPointerException: boom\" resource-id=\"com.example.sample:id/crashLog\" /></hierarchy>""",
                encoding="utf-8",
            )
            runtime_failure = {
                "phase": 1,
                "evidence": {
                    "foreground_component": "com.example.sample/com.ahzy.common.module.mine.crash.AhzyCrashActivity",
                    "artifacts": {
                        "screenshot": str(screenshot),
                        "logcat_tail": str(logcat),
                        "ui_hierarchy": str(hierarchy),
                    },
                    "detection": {"stacktrace": "java.lang.NullPointerException"},
                },
            }
            crash = {
                "cycle": 2,
                "phase": 1,
                "exception_type": "java.lang.NullPointerException",
                "message": "boom",
                "signature": "sample#runtime",
                "stacktrace": "java.lang.NullPointerException",
                "runtime_failure": runtime_failure,
            }
            report_path = report_dir / "report.json"
            self._write_report(report_path, "serial-1", "completed_with_errors", 30, [crash])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["package_name"] = "com.example.sample"
            report["cycles"][0]["cycle"] = 2
            report["cycles"][0]["runtime_failures"] = [runtime_failure]
            report_path.write_text(json.dumps(report), encoding="utf-8")

            detail = server.device_report_detail(report_path)

        evidence = detail["crashDetail"][0]
        self.assertEqual("app_crash_page", evidence["pageType"])
        self.assertEqual(
            "com.example.sample/com.ahzy.common.module.mine.crash.AhzyCrashActivity",
            evidence["pageComponent"],
        )
        self.assertEqual("crash-0-screenshot", evidence["screenshotArtifact"])
        self.assertIn("java.lang.NullPointerException: boom", evidence["copiedInfo"])
        self.assertEqual("page_text_fallback", evidence["copyStatus"])
        self.assertIn("NullPointerException", evidence["crashLog"])

    def test_device_detail_exposes_environment_cycle_causes_and_issue_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["device_info"] = {
                "manufacturer": "Google",
                "model": "Pixel 8",
                "android_version": "15",
                "sdk": "35",
                "screen_size": "1080x2400",
            }
            report["cycles"][0].update({
                "collection_warnings": ["screenshot collector stopped"],
                "pressure_phases": [{"stop_reason": "process_exit"}],
            })
            report["issues"] = [{
                "issue_id": "ISS-1",
                "type": "pressure_error",
                "severity": "high",
                "title": "App process exited",
                "problem_description": "Target process disappeared during pressure input",
                "cycle": 1,
                "foreground_component": "com.example.sample/.MainActivity",
                "key_log_excerpt": "Process died",
                "suggested_investigation": "Inspect native tombstone and lifecycle handling",
            }]
            report["statistics"]["issue_count"] = 1
            report_path.write_text(json.dumps(report), encoding="utf-8")

            detail = server.device_report_detail(report_path)

        self.assertEqual("15", detail["androidVersion"])
        self.assertEqual("35", detail["sdk"])
        self.assertEqual(["process_exit"], detail["cycleResults"][0]["stopReasons"])
        self.assertEqual(
            ["screenshot collector stopped"],
            detail["collectionWarnings"],
        )
        self.assertEqual("ISS-1", detail["issueDetail"][0]["id"])
        self.assertIn("native tombstone", detail["issueDetail"][0]["suggestion"])

    def test_successful_legacy_stall_recovery_is_not_an_actionable_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            self._write_report(report_path, "serial-1", "completed", 120)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["statistics"].update({
                "stall_detection_count": 1,
                "stall_recovery_count": 1,
                "stall_recovery_failure_count": 0,
                "issue_count": 1,
            })
            report["issues"] = [{
                "issue_id": "ISS-STALL-001-001",
                "type": "stall",
                "severity": "medium",
                "title": "页面疑似卡死：com.example/.TerminalPage",
                "problem_description": "页面持续静止 150 秒，恢复结果：成功。",
                "cycle": 1,
                "foreground_component": "com.example/.TerminalPage",
                "recovery": {"attempted": True, "success": True},
            }]
            report_path.write_text(json.dumps(report), encoding="utf-8")

            summary = server.report_summary(report_path)
            detail = server.device_report_detail(report_path)

        assert summary is not None
        self.assertEqual(0, summary["issues"])
        self.assertEqual(1, summary["fallbackRecoveries"])
        self.assertEqual(0, summary["stallRecoveryFailures"])
        self.assertEqual([], detail["issueDetail"])
        self.assertEqual("fallback_recovery", detail["recoveryDetail"][0]["type"])
        self.assertIn("不等同于应用卡死", detail["recoveryDetail"][0]["description"])

    def test_failed_stall_recovery_remains_an_actionable_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["statistics"].update({
                "stall_detection_count": 1,
                "stall_recovery_count": 1,
                "stall_recovery_failure_count": 1,
                "issue_count": 1,
            })
            report["issues"] = [{
                "issue_id": "ISS-STALL-001-001",
                "type": "stall_recovery_failed",
                "severity": "medium",
                "title": "Monkey 保底恢复失败",
                "problem_description": "目标页面没有恢复。",
                "cycle": 1,
                "recovery": {
                    "attempted": True,
                    "success": False,
                    "error": "target did not return",
                },
            }]
            report_path.write_text(json.dumps(report), encoding="utf-8")

            summary = server.report_summary(report_path)
            detail = server.device_report_detail(report_path)

        assert summary is not None
        self.assertEqual(1, summary["issues"])
        self.assertEqual(0, summary["fallbackRecoveries"])
        self.assertEqual(1, summary["stallRecoveryFailures"])
        self.assertEqual("stall_recovery_failed", detail["issueDetail"][0]["type"])
        self.assertEqual([], detail["recoveryDetail"])

    def test_manual_replay_rejects_binary_fastbot_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(json.dumps({"replayable": False, "events": []}), encoding="utf-8")
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            with mock.patch.object(server, "report_index", return_value={"report-1": report_path}):
                with self.assertRaisesRegex(server.ApiError, "没有可回放的结构化轨迹"):
                    server.replay_report_crash("report-1", {"crashIndex": 0})

    def test_legacy_crash_replay_is_materialized_from_the_matching_phase_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            fastbot_log = report_dir / "fastbot.log"
            fastbot_log.write_text(
                """[Fastbot][2026-08-26 10:00:00.000] // Event id: 7
[Fastbot][2026-08-26 10:00:00.001] action type: LONG_CLICK
[Fastbot][2026-08-26 10:00:00.002] :Sending Touch (ACTION_DOWN): 0:(12.0,34.0)
[Fastbot][2026-08-26 10:00:01.002] Wait Event for 1000 milliseconds
[Fastbot][2026-08-26 10:00:01.003] :Sending Touch (ACTION_UP): 0:(12.0,34.0)
""",
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            report = {
                "device_serial": "serial-1",
                "package_name": "com.example.sample",
                "status": "completed_with_errors",
                "cycles": [{
                    "cycle": 2,
                    "pressure_phases": [{
                        "phase": 4,
                        "log_provenance": {
                            "path": str(fastbot_log),
                            "start_offset": 0,
                            "end_offset": fastbot_log.stat().st_size,
                        },
                    }],
                }],
                "crashes": [{
                    "signature": "boom",
                    "cycle": 2,
                    "phase": 4,
                }],
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")

            values = server.normalized_crashes(report_path, report)
            replay = values[0]["replay"]
            generated = list((report_dir / "replay_trajectories").glob("*.json"))

        self.assertTrue(replay["available"])
        self.assertEqual(1, replay["eventCount"])
        self.assertEqual(1, len(generated))
        self.assertIn("cycle_002_phase_004", generated[0].name)

    def test_manual_replay_executes_structured_events_and_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(
                json.dumps({"replayable": True, "events": [{"action": "click", "x": 1, "y": 2}]}),
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            handler = mock.Mock()
            handler.replay_trajectory.return_value = True
            handler.verify_crash_reproducible.return_value = True
            handler.last_replay_result = {
                "executed": True,
                "event_count": 1,
                "signature": "boom",
                "foreground_recovery_count": 2,
            }
            with (
                mock.patch.object(server, "report_index", return_value={"report-1": report_path}),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(server, "_package_installed", return_value=True),
                mock.patch.object(server.DeviceManager, "_run_adb") as run_adb,
                mock.patch.object(server, "CrashHandler", return_value=handler),
                mock.patch.object(server.time, "sleep"),
            ):
                result = server.replay_report_crash("report-1", {"crashIndex": 0})

            history = json.loads((report_dir / "manual_replays.json").read_text(encoding="utf-8"))

        self.assertEqual("reproduced", result["status"])
        self.assertTrue(result["sameCrash"])
        self.assertEqual(1, result["eventCount"])
        self.assertEqual(2, result["foregroundRecoveryCount"])
        self.assertEqual(2, run_adb.call_count)
        self.assertEqual(result["id"], history[0]["id"])

    def test_manual_replay_auto_uses_saved_page_without_force_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(
                json.dumps({
                    "replayable": True,
                    "events": [{"action": "click", "x": 1, "y": 2}],
                    "replay_plan": {
                        "page_anchor": {
                            "component": "com.example.sample/com.example.sample.EditorActivity"
                        }
                    },
                }),
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            handler = mock.Mock()
            handler.prepare_replay_start.return_value = {
                "success": True,
                "strategy": "existing_task_anchor",
                "activity_matched": True,
            }
            handler.prepare_replay_state.return_value = {
                "success": True,
                "reason": "no startup gate visible",
            }
            handler.replay_trajectory.return_value = True
            handler.verify_crash_reproducible.return_value = False
            handler.last_replay_result = {"executed": True, "event_count": 1}
            with (
                mock.patch.object(server, "report_index", return_value={"report-1": report_path}),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(server, "_package_installed", return_value=True),
                mock.patch.object(server.DeviceManager, "_run_adb") as run_adb,
                mock.patch.object(server, "CrashHandler", return_value=handler),
            ):
                result = server.replay_report_crash("report-1", {"crashIndex": 0, "startMode": "auto"})

        self.assertEqual("auto", result["startMode"])
        self.assertEqual("existing_task_anchor", result["replayStart"]["strategy"])
        self.assertFalse(
            any(
                call.args[0][:3] == ["shell", "am", "force-stop"]
                for call in run_adb.call_args_list
            )
        )
        handler.prepare_replay_start.assert_called_once()
        handler.prepare_replay_state.assert_called_once()

    def test_manual_replay_current_page_does_not_touch_startup_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(
                json.dumps({"replayable": True, "events": [{"action": "click", "x": 1, "y": 2}]}),
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            handler = mock.Mock()
            handler.prepare_replay_start.return_value = {
                "success": True,
                "strategy": "operator_positioned_current_page",
            }
            handler.replay_trajectory.return_value = True
            handler.verify_crash_reproducible.return_value = False
            handler.last_replay_result = {"executed": True, "event_count": 1}
            with (
                mock.patch.object(server, "report_index", return_value={"report-1": report_path}),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(server, "_package_installed", return_value=True),
                mock.patch.object(server, "CrashHandler", return_value=handler),
            ):
                result = server.replay_report_crash("report-1", {"crashIndex": 0, "startMode": "current"})

        self.assertEqual("current", result["startMode"])
        handler.prepare_replay_start.assert_called_once()
        handler.prepare_replay_state.assert_not_called()

    def test_manual_replay_start_preparation_failure_is_actionable_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(
                json.dumps({
                    "replayable": True,
                    "events": [{"action": "click", "x": 1, "y": 2}],
                }),
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            handler = mock.Mock()
            handler.prepare_replay_start.return_value = {
                "success": False,
                "strategy": "existing_task_anchor",
                "error": "saved task is no longer available",
            }
            with (
                mock.patch.object(server, "report_index", return_value={"report-1": report_path}),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(server, "_package_installed", return_value=True),
                mock.patch.object(server, "CrashHandler", return_value=handler),
            ):
                with self.assertRaises(server.ApiError) as raised:
                    server.replay_report_crash("report-1", {"crashIndex": 0})

            progress = json.loads(
                (report_dir / "manual_replay_progress_serial-1_0.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(409, raised.exception.status)
        self.assertIn("saved task is no longer available", raised.exception.message)
        self.assertIn("从当前页复现", raised.exception.message)
        self.assertEqual("failed", progress["status"])
        self.assertEqual(
            "existing_task_anchor", progress["replayStart"]["strategy"]
        )
        self.assertIn("从当前页复现", progress["message"])
        handler.replay_trajectory.assert_not_called()
        handler.prepare_replay_state.assert_not_called()

    def test_manual_replay_start_preparation_exception_is_not_server_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(
                json.dumps({
                    "replayable": True,
                    "events": [{"action": "click", "x": 1, "y": 2}],
                }),
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            handler = mock.Mock()
            handler.prepare_replay_start.side_effect = RuntimeError("adb timeout")
            with (
                mock.patch.object(server, "report_index", return_value={"report-1": report_path}),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(server, "_package_installed", return_value=True),
                mock.patch.object(server, "CrashHandler", return_value=handler),
            ):
                with self.assertRaises(server.ApiError) as raised:
                    server.replay_report_crash("report-1", {"crashIndex": 0})

            progress = json.loads(
                (report_dir / "manual_replay_progress_serial-1_0.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(409, raised.exception.status)
        self.assertIn("adb timeout", raised.exception.message)
        self.assertEqual("failed", progress["status"])
        self.assertEqual("adb timeout", progress["replayStart"]["error"])
        handler.replay_trajectory.assert_not_called()

    def test_manual_replay_startup_gate_failure_is_actionable_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            trajectory = report_dir / "trajectory.json"
            trajectory.write_text(
                json.dumps({
                    "replayable": True,
                    "events": [{"action": "click", "x": 1, "y": 2}],
                }),
                encoding="utf-8",
            )
            report_path = report_dir / "report.json"
            self._write_report(
                report_path,
                "serial-1",
                "completed_with_errors",
                30,
                [{"signature": "boom", "trajectory_path": str(trajectory)}],
            )
            handler = mock.Mock()
            handler.prepare_replay_start.return_value = {
                "success": True,
                "strategy": "existing_task_anchor",
                "activity_matched": True,
            }
            handler.prepare_replay_state.return_value = {
                "success": False,
                "reason": "审核页仍在等待确认",
                "error": "startup gate did not clear",
            }
            with (
                mock.patch.object(server, "report_index", return_value={"report-1": report_path}),
                mock.patch.object(
                    server.DeviceManager,
                    "get_device_states",
                    return_value={"serial-1": {"adb_state": "device"}},
                ),
                mock.patch.object(server, "_package_installed", return_value=True),
                mock.patch.object(server, "CrashHandler", return_value=handler),
            ):
                with self.assertRaises(server.ApiError) as raised:
                    server.replay_report_crash("report-1", {"crashIndex": 0})

            progress = json.loads(
                (report_dir / "manual_replay_progress_serial-1_0.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(409, raised.exception.status)
        self.assertIn("startup gate did not clear", raised.exception.message)
        self.assertIn("从当前页复现", raised.exception.message)
        self.assertEqual("failed", progress["status"])
        self.assertEqual(
            "existing_task_anchor", progress["replayStart"]["strategy"]
        )
        self.assertEqual(
            "startup gate did not clear", progress["preparation"]["error"]
        )
        handler.replay_trajectory.assert_not_called()

    def test_device_progress_uses_pressure_time_for_terminal_statuses(self) -> None:
        cases = [
            ("failed", [300, 120], 35),
            ("stopped", [420], 35),
            ("interrupted", [600], 50),
            ("completed", [600, 600], 100),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            report_root = Path(temporary) / "reports"
            report_dir = report_root / "serial-1"
            report_dir.mkdir(parents=True)
            report_path = report_dir / "report.json"
            job = {
                "reportRoot": str(report_root),
                "duration": 10,
                "cycles": 2,
            }
            for status, elapsed_values, expected in cases:
                with self.subTest(status=status):
                    report_path.write_text(
                        json.dumps({
                            "device_serial": "serial-1",
                            "status": status,
                            "cycles": [
                                {"pressure_elapsed_seconds": value}
                                for value in elapsed_values
                            ],
                        }),
                        encoding="utf-8",
                    )
                    progress, _cycle, report_status = server._device_progress(
                        "serial-1", job
                    )
                    self.assertEqual(expected, progress)
                    self.assertEqual(status, report_status)

    def test_failed_device_never_reports_one_hundred_percent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_root = Path(temporary) / "reports"
            report_dir = report_root / "serial-1"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(
                json.dumps({
                    "device_serial": "serial-1",
                    "status": "failed",
                    "cycles": [{"pressure_elapsed_seconds": 1200}],
                }),
                encoding="utf-8",
            )
            progress, _cycle, _status = server._device_progress(
                "serial-1",
                {"reportRoot": str(report_root), "duration": 10, "cycles": 2},
            )

        self.assertEqual(99, progress)

    def test_completed_report_is_mapped_to_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "device_serial": "serial-1",
                        "package_name": "com.example.sample",
                        "status": "completed",
                        "device_info": {"manufacturer": "Google", "model": "Pixel"},
                        "cycles": [{"status": "passed"}],
                        "crashes": [],
                        "issues": [],
                        "statistics": {
                            "total_cycles": 1,
                            "total_operations": 123,
                            "pressure_elapsed_seconds": 61,
                            "crash_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = server.report_summary(path)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual("passed", summary["status"])
        self.assertEqual(123, summary["operations"])
        self.assertEqual("Google Pixel", summary["device"])


class JobDurationTests(unittest.TestCase):
    """A run whose planned window elapsed must not wait for an operator forever."""

    def _job(self, started_minutes_ago, duration_minutes=240):
        started = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
        return {"id": "WEB-test", "startedAt": started.isoformat(), "duration": duration_minutes}

    def test_run_inside_its_window_is_not_elapsed(self):
        self.assertFalse(server._job_run_duration_elapsed(self._job(60)))

    def test_run_past_duration_and_grace_is_elapsed(self):
        self.assertTrue(server._job_run_duration_elapsed(self._job(251)))

    def test_missing_timestamps_are_never_treated_as_elapsed(self):
        self.assertFalse(server._job_run_duration_elapsed({"duration": 240}))
        self.assertFalse(server._job_run_duration_elapsed({"startedAt": "bad", "duration": 240}))
        self.assertFalse(server._job_run_duration_elapsed({"startedAt": self._job(500)["startedAt"]}))


class AutoInstallConfirmTests(unittest.TestCase):
    def test_watcher_confirms_when_every_device_is_ready(self):
        job = {"id": "job-auto", "status": "awaiting_install", "devices": ["s1", "s2"], "packageName": "com.example.app"}
        with mock.patch.dict(server.JOBS, {"job-auto": job}, clear=False), mock.patch.object(
            server, "_package_matches_apk", return_value=True
        ), mock.patch.object(server, "confirm_job_install") as confirm, mock.patch.object(
            server, "_job_log"
        ):
            server._auto_confirm_install_when_ready(
                "job-auto", ["s1", "s2"], "com.example.app", "md5", timeout_seconds=5, poll_seconds=0.01
            )
        confirm.assert_called_once_with("job-auto")

    def test_watcher_times_out_without_blocking_manual_confirm(self):
        job = {"id": "job-auto-2", "status": "awaiting_install", "devices": ["s1"], "packageName": "com.example.app"}
        logs = []
        with mock.patch.dict(server.JOBS, {"job-auto-2": job}, clear=False), mock.patch.object(
            server, "_package_matches_apk", return_value=False
        ), mock.patch.object(server, "confirm_job_install") as confirm, mock.patch.object(
            server, "_job_log", side_effect=lambda _job, message: logs.append(message)
        ):
            server._auto_confirm_install_when_ready(
                "job-auto-2", ["s1"], "com.example.app", "md5", timeout_seconds=30, poll_seconds=0.01
            )
        confirm.assert_not_called()
        self.assertTrue(any("超时" in message for message in logs))


class ReportPathCacheTests(unittest.TestCase):
    def test_cache_invalidates_when_report_json_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device_dir = root / "job_serial"
            device_dir.mkdir()
            job = {"id": "job", "reportRoot": str(root), "devices": ["s1"], "duration": 10}

            self.assertEqual([], server._cached_job_report_paths(job))

            (device_dir / "report.json").write_text("{}", encoding="utf-8")
            paths = server._cached_job_report_paths(job)

            self.assertEqual(1, len(paths))
            self.assertEqual("report.json", paths[0].name)

    def test_public_job_keeps_runtime_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = {
                "id": "job-paths",
                "devices": [],
                "duration": 1,
                "reportRoot": tmp,
                "jobDir": tmp,
                "configPath": str(Path(tmp) / "config.json"),
            }
            value = server.public_job(job)

            self.assertEqual(tmp, value["reportRoot"])
            self.assertEqual(tmp, value["jobDir"])


if __name__ == "__main__":
    unittest.main()
