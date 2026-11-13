import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.configuration import (
    ConfigurationError,
    MAX_SAFE_WORKERS,
    load_config,
    require_local_apk,
    resolve_project_path,
)


class ConfigurationTests(unittest.TestCase):

    @unittest.skipUnless(os.name == "nt", "Windows output-root default")
    def test_relative_output_path_defaults_to_e_drive_on_windows(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_PRESS_TEST_OUTPUT_ROOT", None)
            self.assertEqual(
                resolve_project_path("./reports"),
                Path(r"E:\app_press_test\reports").resolve(),
            )

    def test_relative_output_path_uses_configured_output_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {"APP_PRESS_TEST_OUTPUT_ROOT": temporary},
            ):
                self.assertEqual(
                    resolve_project_path("./reports"),
                    (Path(temporary) / "reports").resolve(),
                )

    def test_absolute_output_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = (Path(temporary) / "reports").resolve()
            self.assertEqual(resolve_project_path(str(output)), output)

    def _base_config(self):
        return {
            "app": {
                "package_name": "com.example.app",
                "apk_path": "target.apk",
            }
        }

    def _load_mapping(self, config):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "target.apk").write_bytes(b"offline fixture")
            path = directory / "settings.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return load_config(str(path))

    def test_crash_only_logcat_mode_is_accepted(self):
        config = self._base_config()
        config["diagnostics"] = {"logcat_mode": "crash_only"}
        loaded = self._load_mapping(config)
        self.assertEqual("crash_only", loaded["diagnostics"]["logcat_mode"])

    def test_permission_profile_is_accepted(self):
        config = self._base_config()
        config["test"] = {
            "grant_permissions": ["android.permission.CAMERA"],
            "appops_permissions": {"SYSTEM_ALERT_WINDOW": "allow"},
            "disable_package_verifier": True,
        }
        loaded = self._load_mapping(config)
        self.assertEqual(
            ["android.permission.CAMERA"], loaded["test"]["grant_permissions"]
        )
        self.assertTrue(loaded["test"]["disable_package_verifier"])

    def test_invalid_appops_mode_is_rejected(self):
        config = self._base_config()
        config["test"] = {"appops_permissions": {"SYSTEM_ALERT_WINDOW": "maybe"}}
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)

    def test_grant_permissions_must_be_a_list_of_strings(self):
        config = self._base_config()
        config["test"] = {"grant_permissions": ["android.permission.CAMERA", 7]}
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)

    def test_touch_overlay_flag_is_validated(self):
        config = self._base_config()
        config["test"] = {"touch_overlay": "yes"}
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)

        config["test"] = {"touch_overlay": True}
        self.assertTrue(self._load_mapping(config)["test"]["touch_overlay"])

    def test_retention_defaults_are_applied(self):
        loaded = self._load_mapping(self._base_config())
        retention = loaded["retention"]
        self.assertTrue(retention["enabled"])
        self.assertEqual(14, retention["max_age_days"])
        self.assertEqual(60.0, retention["max_total_gb"])
        self.assertTrue(retention["keep_crash_runs"])

    def test_retention_rejects_invalid_values(self):
        config = self._base_config()
        config["retention"] = {"max_age_days": 0}
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)

        config["retention"] = {"max_total_gb": 0}
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)

    def test_nonfinite_timeouts_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                config = self._base_config()
                config["crash_recovery"] = {"restart_backoff_seconds": value}
                with self.assertRaises(ConfigurationError):
                    self._load_mapping(config)


    def test_parallel_workers_are_bounded_for_host_stability(self):
        config = self._base_config()
        config["parallel"] = {"max_workers": 9}

        loaded = self._load_mapping(config)

        self.assertEqual(MAX_SAFE_WORKERS, loaded["parallel"]["max_workers"])






    def test_unknown_logcat_mode_is_rejected(self):
        config = self._base_config()
        config["diagnostics"] = {"logcat_mode": "verbose"}
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)
    def _write_config(self, directory: Path, apk_path: str) -> Path:
        config_path = directory / "settings.json"
        config_path.write_text(
            json.dumps(
                {
                    "app": {
                        "package_name": "com.example.app",
                        "apk_path": apk_path,
                    }
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def test_local_apk_is_resolved_relative_to_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"not-an-installable-apk-for-offline-test")
            config = load_config(str(self._write_config(directory, "target.apk")))
            self.assertEqual(require_local_apk(config), apk.resolve())
            self.assertTrue(Path(config["app"]["apk_path"]).is_absolute())

    def test_remote_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_config(Path(temporary), "https://example.invalid/app.apk")
            with self.assertRaises(ConfigurationError):
                load_config(str(path))

    def test_missing_local_apk_is_reported_only_when_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = load_config(
                str(self._write_config(Path(temporary), "missing.apk"))
            )
            with self.assertRaises(ConfigurationError):
                require_local_apk(config)

    def test_unknown_pressure_engine_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["fastbot"] = {"engine": "remote-service"}
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_permission_dialog_settle_seconds_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["test"] = {"permission_dialog_settle_seconds": 11}
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_device_data_clear_override_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["test"] = {
                "clear_app_data_each_cycle_by_device": {"oppo": "false"}
            }
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_pre_login_steps_must_be_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["scenario"] = {"pre_login_steps": ["other login"]}
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_input_safety_and_input_cases_are_normalized(self):
        config = self._base_config()
        config["input_safety"] = {
            "recovery_attempts": 2,
        }
        config["scenario"] = {
            "input_cases": {"keyword": ["one", "two"]},
            "navigation_steps": [
                {"action": "input", "data": "keyword", "locator": {"text": "Search"}}
            ],
        }
        loaded = self._load_mapping(config)
        self.assertTrue(loaded["input_safety"]["enabled"])
        self.assertEqual("sequential", loaded["scenario"]["input_cases"]["keyword"]["mode"])

    def test_input_step_must_reference_known_case(self):
        config = self._base_config()
        config["scenario"] = {
            "navigation_steps": [{"action": "input", "data": "missing"}]
        }
        with self.assertRaises(ConfigurationError):
            self._load_mapping(config)

    def test_strict_input_safety_rejects_unguarded_event_sources(self):
        for arguments in (["--agent", "random"], ["-f", "/sdcard/script"], ["--port", "1234"]):
            with self.subTest(arguments=arguments):
                config = self._base_config()
                config["fastbot"] = {"extra_args": arguments}
                with self.assertRaises(ConfigurationError):
                    self._load_mapping(config)

    def test_input_case_and_recovery_limits_are_validated(self):
        for values in ([], [1], ["bad\x00value"]):
            with self.subTest(values=values):
                config = self._base_config()
                config["scenario"] = {"input_cases": {"keyword": values}}
                with self.assertRaises(ConfigurationError):
                    self._load_mapping(config)
        for options in ({"recovery_attempts": 0}, {"recovery_attempts": 11}, {"hide_timeout_seconds": 16}):
            with self.subTest(options=options):
                config = self._base_config()
                config["input_safety"] = options
                with self.assertRaises(ConfigurationError):
                    self._load_mapping(config)

    def test_generic_audit_recovery_allows_activity_to_be_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["scenario"] = {
                "audit_recovery": {
                    "enabled": True,
                    "activity": "",
                    "audit_resource_id": "com.example.app:id/auditRadioButton",
                    "confirm_text": "确定",
                }
            }
            config_path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_config(str(config_path))
        self.assertEqual("", loaded["scenario"]["audit_recovery"]["activity"])

    def test_audit_recovery_defaults_to_every_launch_package_gate(self):
        loaded = self._load_mapping(self._base_config())

        self.assertEqual(
            {
                "enabled": True,
                "activity": "",
                "audit_resource_id": "com.example.app:id/auditRadioButton",
                "confirm_text": "\u786e\u5b9a",
            },
            loaded["scenario"]["audit_recovery"],
        )

    def test_system_monkey_event_percentages_must_total_100(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["fastbot"] = {
                "engine": "system-monkey",
                "event_percentages": {"touch": 70, "motion": 20, "pinchzoom": 5},
            }
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_invalid_expected_fastbot_jar_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["fastbot"] = {"expected_monkeyq_md5": "not-an-md5"}
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_stall_watchdog_defaults_to_one_time_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config = load_config(str(self._write_config(directory, "target.apk")))
            self.assertEqual(
                config["stall_watchdog"],
                {
                    "enabled": True,
                    "mode": "recover_once",
                    "sample_interval_seconds": 10,
                    "unchanged_seconds": 150,
                    "startup_grace_seconds": 30,
                    "max_hash_distance": 6,
                    "max_probe_failures": 3,
                    "max_recoveries_per_cycle": 1,
                },
            )

    def test_screen_recording_defaults_to_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")

            config = load_config(str(self._write_config(directory, "target.apk")))

        self.assertEqual({"enabled": False}, config["screen_recording"])

    def test_screen_recording_enabled_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["screen_recording"] = {"enabled": "yes"}
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_screen_recording_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["screen_recording"] = {"enabled": True}
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "disabled"):
                load_config(str(config_path))

    def test_stall_watchdog_requires_three_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["stall_watchdog"] = {
                "sample_interval_seconds": 20,
                "unchanged_seconds": 30,
            }
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(str(config_path))

    def test_stall_watchdog_allows_one_time_recovery_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["stall_watchdog"] = {
                "enabled": True,
                "mode": "recover_once",
                "sample_interval_seconds": 10,
                "unchanged_seconds": 30,
            }
            config_path.write_text(json.dumps(data), encoding="utf-8")

            config = load_config(str(config_path))

        self.assertEqual(config["stall_watchdog"]["mode"], "recover_once")

    def test_stall_watchdog_allows_repeated_recovery_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            apk = directory / "target.apk"
            apk.write_bytes(b"offline fixture")
            config_path = self._write_config(directory, "target.apk")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["stall_watchdog"] = {"enabled": True, "mode": "recover"}
            config_path.write_text(json.dumps(data), encoding="utf-8")

            config = load_config(str(config_path))

        self.assertEqual(config["stall_watchdog"]["mode"], "recover")
        self.assertEqual(config["stall_watchdog"]["max_recoveries_per_cycle"], 3)


if __name__ == "__main__":
    unittest.main()
