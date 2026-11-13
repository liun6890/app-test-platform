import argparse
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import cli


class SyncFastbotTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        root = Path(self.temp_directory.name)
        source = root / "fastbot-runtime.apk"
        source.write_bytes(b"bundled-runtime")
        self.dependencies = [(source, "/data/local/tmp/monkey.apk")]
        self.native_source = root / "libfastbot_native.so"
        self.native_source.write_bytes(b"native")

    def tearDown(self):
        self.temp_directory.cleanup()

    def _run_sync(self, subprocess_results):
        output = StringIO()
        controller = Mock()
        controller._remote_processes.return_value = {}
        with (
            patch.object(cli.DeviceManager, "get_connected_devices", return_value=["device-1"]),
            patch.object(cli, "MutexController", return_value=controller),
            patch.object(cli, "_safe_fastbot_files", return_value=self.dependencies),
            patch.object(cli, "_device_abi", return_value="arm64-v8a"),
            patch.object(cli, "runtime_bundle_has_native", return_value=True),
            patch.object(cli, "native_asset_path", return_value=self.native_source),
            patch.object(
                cli,
                "native_remote_path",
                return_value="/data/local/tmp/arm64-v8a/libfastbot_native.so",
            ),
            patch.object(cli.subprocess, "run", side_effect=subprocess_results) as run,
            redirect_stdout(output),
        ):
            return_code = cli._cmd_sync_fastbot(argparse.Namespace(devices="device-1"))
        return return_code, json.loads(output.getvalue()), run

    def test_sync_pushes_and_verifies_every_fastbot_dependency(self):
        digest = hashlib.md5(self.dependencies[0][0].read_bytes()).hexdigest()
        subprocess_results = [
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout="pushed", stderr=""),
            Mock(returncode=0, stdout=f"{digest}  /data/local/tmp/monkey.apk\n", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
        ]

        return_code, result, run = self._run_sync(subprocess_results)

        self.assertEqual(return_code, 0)
        self.assertTrue(result[0]["synced"])
        self.assertEqual(
            [item["remote_path"] for item in result[0]["files"]],
            [remote_path for _source, remote_path in self.dependencies],
        )
        self.assertTrue(all(item["synced"] for item in result[0]["files"]))
        self.assertEqual(run.call_count, 7)
        file_calls = [
            invocation
            for invocation in run.call_args_list
            if invocation.args[0][3] == "push"
            or (
                invocation.args[0][3] == "shell"
                and len(invocation.args[0]) > 4
                and invocation.args[0][4] == "md5sum"
            )
        ]
        self.assertEqual(
            [invocation.args[0][-1] for invocation in file_calls],
            [
                "/data/local/tmp/monkey.apk",
                "/data/local/tmp/monkey.apk",
            ],
        )
        self.assertEqual(result[0]["native_abi"], "arm64-v8a")
        self.assertEqual(
            result[0]["legacy_removed"],
            [
                "/sdcard/monkeyq.jar",
                "/sdcard/framework.jar",
                "/sdcard/fastbot-thirdpart.jar",
                "/data/local/tmp/arm64-v8a/libfastbot_native.so",
            ],
        )

    def test_sync_fails_device_when_runtime_cannot_be_pushed(self):
        return_code, result, run = self._run_sync(
            [
                Mock(returncode=0, stdout="", stderr=""),
                Mock(returncode=1, stdout="", stderr="permission denied"),
            ]
        )

        self.assertEqual(return_code, 1)
        self.assertFalse(result[0]["synced"])
        self.assertEqual(
            result[0]["error"], "failed to sync: monkey.apk"
        )
        self.assertEqual(result[0]["files"][0]["error"], "permission denied")
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
