from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.apk = self.root / "sample.apk"
        self.apk.write_bytes(b"test apk")
        self.payload = {
            "packageName": "com.example.sample",
            "apkPackageName": "com.example.sample",
            "apkPath": str(self.apk),
            "devices": ["serial-1"],
        }
        for patcher in (
            mock.patch.object(server, "UPLOAD_ROOT", self.root),
            mock.patch.object(server, "apk_package_name", return_value=("com.example.sample", "")),
            mock.patch.object(server.DeviceManager, "get_device_states", return_value={"serial-1": {"adb_state": "device"}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def payloads(self):
        yield self.payload
        yield {**self.payload, "configFile": "acceptance_baibai_fastbot.json", "workflowMode": "generic"}

    def test_run_rejects_non_integer_numbers(self):
        for payload in self.payloads():
            for field in ("duration", "cycles", "throttle", "workers"):
                for value in (1.5, True, float("inf"), float("nan"), None):
                    with self.subTest(template=payload.get("configFile"), field=field, value=value):
                        with self.assertRaises(server.ApiError):
                            server.build_run_config({**payload, field: value}, self.root / "job")

    def test_run_preserves_integer_bounds(self):
        for payload in self.payloads():
            for duration, cycles, throttle in ((1, 1, 0), (1440, 100, 60000)):
                with self.subTest(template=payload.get("configFile"), duration=duration):
                    config = server.build_run_config(
                        {**payload, "duration": duration, "cycles": cycles, "throttle": throttle}, self.root / "job"
                    )
                    self.assertEqual(duration, config["test"]["fastbot_duration_minutes"])
                    self.assertEqual(cycles, config["test"]["max_cycles"])
                    self.assertEqual(throttle, config["test"]["fastbot_throttle_ms"])

    def test_run_rejects_values_outside_bounds(self):
        for payload in self.payloads():
            for field, values in (("duration", (0, 1441)), ("cycles", (0, 101)), ("throttle", (-1, 60001)), ("workers", (0, 33))):
                for value in values:
                    with self.subTest(template=payload.get("configFile"), field=field, value=value):
                        with self.assertRaises(server.ApiError):
                            server.build_run_config({**payload, field: value}, self.root / "job")

    def test_run_rejects_device_list_containing_only_whitespace(self):
        for payload in self.payloads():
            with self.subTest(template=payload.get("configFile")):
                with self.assertRaises(server.ApiError):
                    server.build_run_config({**payload, "devices": ["", "  "]}, self.root / "job")

    def test_draft_does_not_truncate_invalid_numeric_choices(self):
        for value in (1.5, True, float("inf"), float("nan")):
            with self.subTest(value=value):
                draft = server.sanitize_draft({"config": {"duration": value}})
                self.assertNotIn("duration", draft["config"])

    def test_unknown_api_get_returns_json_not_the_app_html(self):
        handler = object.__new__(server.ConsoleHandler)
        handler.path = "/api/does-not-exist"
        handler.command = "GET"
        handler.send_json = mock.Mock()
        handler.send_static = mock.Mock()
        handler.do_GET()
        handler.send_static.assert_not_called()
        handler.send_json.assert_called_once_with({"error": "\u63a5\u53e3\u4e0d\u5b58\u5728"}, 404)

    def test_invalid_json_is_a_client_error(self):
        for body in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(body=body):
                handler = object.__new__(server.ConsoleHandler)
                handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                with self.assertRaises(server.ApiError):
                    handler.read_json()


if __name__ == "__main__":
    unittest.main()
