"""The checkpoint heartbeat must read only new engine-log bytes."""

from __future__ import annotations

import unittest
from typing import Any

import main


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _RemoteLogDevice:
    """Minimal adb stand-in holding one device-side Fastbot log."""

    def __init__(self, path: str = "/sdcard/fastbot.log") -> None:
        self.path = path
        self.text = ""
        self.tail_requests = []

    def append_touches(self, count: int, padding: int = 40) -> None:
        self.text += ("x" * padding + "\n") * count
        self.text += "".join(
            "[Fastbot] :Sending Touch (ACTION_DOWN): 0:(1.0,2.0)\n"
            for _ in range(count)
        )

    def _run_adb(self, arguments, timeout=5, retries=1):
        if arguments[1:3] == ["wc", "-c"]:
            return _Result("%d\n" % len(self.text.encode("utf-8")))
        if arguments[1:3] == ["tail", "-c"]:
            size = int(arguments[3])
            self.tail_requests.append(size)
            payload = self.text.encode("utf-8")[-size:]
            return _Result(payload.decode("utf-8", errors="replace"))
        raise AssertionError("unexpected adb call: %r" % (arguments,))


class CheckpointProgressProbeTests(unittest.TestCase):
    def _probe(self, device: _RemoteLogDevice, base: int = 0):
        return main._checkpoint_progress_provider(
            device, object(), "serial-1", device.path, base
        )

    def test_first_probe_counts_the_existing_log(self):
        device = _RemoteLogDevice()
        device.append_touches(2)
        values = self._probe(device)()

        self.assertEqual(2, values["operation_count"])
        self.assertIn("last_operation_at", values)
        self.assertEqual([len(device.text)], device.tail_requests)

    def test_later_probes_read_only_the_appended_bytes(self):
        device = _RemoteLogDevice()
        device.append_touches(2)
        probe = self._probe(device, base=100)
        probe()

        before = len(device.text.encode("utf-8"))
        device.append_touches(3)
        appended = len(device.text.encode("utf-8")) - before
        values = probe()

        self.assertEqual(105, values["operation_count"])
        self.assertEqual(2, len(device.tail_requests))
        # Second call asks for the appended slice only, never the whole file.
        self.assertEqual(appended, device.tail_requests[-1])

    def test_quiet_engine_reuses_the_previous_count_without_reading_again(self):
        device = _RemoteLogDevice()
        device.append_touches(1)
        probe = self._probe(device)
        first = probe()
        calls = len(device.tail_requests)

        second = probe()

        self.assertEqual(first["operation_count"], second["operation_count"])
        self.assertEqual(calls, len(device.tail_requests))
        self.assertNotIn("last_operation_at", second)


if __name__ == "__main__":
    unittest.main()
