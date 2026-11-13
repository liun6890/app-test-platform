import unittest
from unittest.mock import Mock, patch

from core.device_manager import DeviceManager


class DeviceManagerTests(unittest.TestCase):
    def test_device_states_preserve_online_offline_and_unauthorized_rows(self):
        output = """List of devices attached
ready-1 device product:x model:Phone_A transport_id:1
offline-1 offline transport_id:2
locked-1 unauthorized usb:3-1
"""
        with patch.object(
            DeviceManager,
            "_run_adb",
            return_value=Mock(returncode=0, stdout=output, stderr=""),
        ):
            states = DeviceManager.get_device_states()

        self.assertEqual(states["ready-1"]["adb_state"], "device")
        self.assertEqual(states["ready-1"]["model"], "Phone_A")
        self.assertTrue(states["ready-1"]["authorized"])
        self.assertEqual(states["offline-1"]["adb_state"], "offline")
        self.assertFalse(states["offline-1"]["connected"])
        self.assertEqual(states["locked-1"]["adb_state"], "unauthorized")

    def test_connected_devices_return_only_authorized_online_rows(self):
        states = {
            "ready": {"adb_state": "device"},
            "offline": {"adb_state": "offline"},
            "locked": {"adb_state": "unauthorized"},
        }
        with patch.object(DeviceManager, "get_device_states", return_value=states):
            self.assertEqual(DeviceManager.get_connected_devices(), ["ready"])

    def test_device_is_not_ready_when_vendor_blocks_adb_input(self):
        with patch.object(
            DeviceManager,
            "_run_adb",
            side_effect=[
                Mock(stdout="device\n"),
                Mock(stdout="1\n"),
                Mock(stdout="0\n"),
            ],
        ) as run_adb:
            self.assertFalse(DeviceManager.is_device_ready("xiaomi-1"))

        self.assertEqual(3, run_adb.call_count)

    def test_device_is_ready_when_vendor_input_property_is_not_present(self):
        with patch.object(
            DeviceManager,
            "_run_adb",
            side_effect=[
                Mock(stdout="device\n"),
                Mock(stdout="1\n"),
                Mock(stdout="\n"),
                Mock(stdout="mShowingLockscreen=false\n"),
            ],
        ):
            self.assertTrue(DeviceManager.is_device_ready("android-1"))


if __name__ == "__main__":
    unittest.main()
