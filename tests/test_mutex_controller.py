import unittest
from unittest.mock import Mock, call, patch

from core.mutex_controller import MutexController


class _Watcher:
    def __init__(self, should_fail=False, return_false=False):
        self.should_fail = should_fail
        self.return_false = return_false
        self.stopped = False

    def stop(self):
        if self.should_fail:
            raise RuntimeError("watcher failure")
        self.stopped = True
        return False if self.return_false else None


class _Device:
    def __init__(
        self,
        should_fail=False,
        return_false=False,
        stop_uiautomator_failure=False,
    ):
        self.watcher = _Watcher(should_fail, return_false)
        self.stop_uiautomator_failure = stop_uiautomator_failure
        self.uiautomator_stopped = False
        self.uiautomator_started = False

    def stop_uiautomator(self, wait=True):
        if self.stop_uiautomator_failure:
            raise RuntimeError("uiautomator stop failure")
        self.uiautomator_stopped = wait

    def start_uiautomator(self):
        self.uiautomator_started = True


class MutexControllerTests(unittest.TestCase):
    def setUp(self):
        self.remote_stop = patch.object(
            MutexController, "_kill_fastbot_if_running", return_value=True
        )
        self.remote_stop.start()
        self.addCleanup(self.remote_stop.stop)

    def test_fastbot_requires_watcher_stop(self):
        controller = MutexController("offline-device")
        device = _Device()
        self.assertTrue(controller.acquire("fastbot", device))
        self.assertTrue(device.watcher.stopped)
        self.assertTrue(device.uiautomator_stopped)
        self.assertTrue(controller.release("fastbot", device))
        self.assertTrue(device.uiautomator_started)

    def test_failed_watcher_stop_does_not_grant_control(self):
        controller = MutexController("offline-device")
        self.assertFalse(controller.acquire("fastbot", _Device(True)))
        self.assertIsNone(controller.get_current_holder())

    def test_fastbot_without_device_is_rejected(self):
        controller = MutexController("offline-device")
        self.assertFalse(controller.acquire("fastbot"))

    def test_explicit_false_watcher_result_is_rejected(self):
        controller = MutexController("offline-device")
        self.assertFalse(controller.acquire("fastbot", _Device(return_false=True)))

    def test_failed_uiautomator_stop_does_not_grant_control(self):
        controller = MutexController("offline-device")
        device = _Device(stop_uiautomator_failure=True)
        self.assertFalse(controller.acquire("fastbot", device))
        self.assertIsNone(controller.get_current_holder())

    def test_failed_handoff_restores_uiautomator_before_returning(self):
        controller = MutexController("offline-device")
        device = _Device()
        device.shell = Mock()
        with patch.object(
            controller, "_stop_remote_uiautomator_processes", return_value=False
        ):
            self.assertFalse(controller.acquire("fastbot", device))

        self.assertTrue(device.uiautomator_started)
        self.assertFalse(controller._uiautomator_stopped)
        self.assertIsNone(controller.get_current_holder())

    def test_unreleased_uiautomation_restores_uiautomator_before_returning(self):
        controller = MutexController("offline-device")
        device = _Device()
        device.shell = Mock()
        with patch.object(
            controller, "_stop_remote_uiautomator_processes", return_value=True
        ), patch.object(
            controller, "_wait_for_uiautomation_release", return_value=False
        ):
            self.assertFalse(controller.acquire("fastbot", device))

        self.assertTrue(device.uiautomator_started)
        self.assertFalse(controller._uiautomator_stopped)

    def test_uiautomator_restore_retries_after_coloros_registration_error(self):
        controller = MutexController("offline-device")
        device = _Device()
        device.stop_uiautomator = Mock()
        device.start_uiautomator = Mock(
            side_effect=[
                RuntimeError("UiAutomationService already registered"),
                RuntimeError("UiAutomation not connected"),
                None,
            ]
        )

        with patch("core.mutex_controller.time.sleep") as sleep:
            self.assertTrue(controller.acquire("fastbot", device))
            self.assertTrue(controller.release("fastbot", device))

        self.assertEqual(device.start_uiautomator.call_count, 3)
        self.assertEqual(device.stop_uiautomator.call_count, 3)
        self.assertEqual(sleep.call_args_list[-2:], [call(1.0), call(2.0)])
        self.assertFalse(controller._uiautomator_stopped)

    def test_uiautomator_restore_fails_closed_after_bounded_retries(self):
        controller = MutexController("offline-device")
        device = _Device()
        device.stop_uiautomator = Mock()
        device.start_uiautomator = Mock(
            side_effect=RuntimeError("UiAutomationService already registered")
        )

        with patch("core.mutex_controller.time.sleep") as sleep:
            self.assertTrue(controller.acquire("fastbot", device))
            self.assertFalse(controller.release("fastbot", device))

        self.assertEqual(
            device.start_uiautomator.call_count,
            MutexController.UIAUTOMATOR_RESTORE_ATTEMPTS,
        )
        self.assertEqual(
            device.stop_uiautomator.call_count,
            MutexController.UIAUTOMATOR_RESTORE_ATTEMPTS + 1,
        )
        self.assertEqual(
            sleep.call_args_list[-3:], [call(1.0), call(2.0), call(3.0)]
        )
        self.assertTrue(controller._uiautomator_stopped)

    def test_uiautomator_restore_waits_for_real_hierarchy_health(self):
        controller = MutexController("offline-device")
        device = _Device()
        device.dump_hierarchy = Mock(
            side_effect=[
                RuntimeError("UiAutomation not connected"),
                '<hierarchy rotation="0" />',
            ]
        )

        with patch("core.mutex_controller.time.sleep") as sleep:
            self.assertTrue(controller.acquire("fastbot", device))
            self.assertTrue(controller.release("fastbot", device))

        device.start_uiautomator.assert_not_called() if isinstance(
            device.start_uiautomator, Mock
        ) else None
        self.assertTrue(device.uiautomator_started)
        self.assertEqual(device.dump_hierarchy.call_count, 2)
        self.assertIn(
            call(MutexController.UIAUTOMATOR_HEALTH_RETRY_SECONDS),
            sleep.call_args_list,
        )

    def test_uiautomator_acquire_restarts_an_unhealthy_existing_service(self):
        controller = MutexController("offline-device")
        device = _Device()
        device.stop_uiautomator = Mock()
        device.start_uiautomator = Mock()
        device.dump_hierarchy = Mock(
            side_effect=[
                RuntimeError("UiAutomation not connected"),
                RuntimeError("UiAutomation not connected"),
                RuntimeError("UiAutomation not connected"),
                '<hierarchy rotation="0" />',
            ]
        )

        with patch("core.mutex_controller.time.sleep"):
            self.assertTrue(controller.acquire("uiautomator2", device))

        device.stop_uiautomator.assert_called_once_with(wait=True)
        device.start_uiautomator.assert_called_once_with()
        self.assertEqual(controller.get_current_holder(), "uiautomator2")


class MutexRemoteStopTests(unittest.TestCase):
    def test_remote_stop_interrupts_then_verifies_absence(self):
        running = Mock(returncode=0, stdout="321\n", stderr="")
        signal = Mock(returncode=0, stdout="", stderr="")
        no_pid = Mock(returncode=1, stdout="", stderr="")
        empty_ps = Mock(returncode=0, stdout="PID ARGS\n", stderr="")
        with patch(
            "core.mutex_controller.subprocess.run",
            side_effect=[running, signal, no_pid, empty_ps],
        ) as run, patch("core.mutex_controller.time.sleep"):
            self.assertTrue(MutexController("offline-device")._kill_fastbot_if_running())

        self.assertIn(
            call(
                [
                    "adb",
                    "-s",
                    "offline-device",
                    "shell",
                    "kill",
                    "-2",
                    "321",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            ),
            run.call_args_list,
        )

    def test_remote_probe_failure_is_not_reported_as_stopped(self):
        unavailable = Mock(returncode=1, stdout="", stderr="device offline")
        with patch(
            "core.mutex_controller.subprocess.run", return_value=unavailable
        ), patch("core.mutex_controller.time.sleep"):
            self.assertFalse(MutexController("offline-device")._kill_fastbot_if_running())

    def test_remote_probe_recovers_after_transient_disconnect(self):
        unavailable = Mock(returncode=1, stdout="", stderr="device not found")
        no_pid = Mock(returncode=1, stdout="", stderr="")
        empty_ps = Mock(returncode=0, stdout="PID ARGS\n", stderr="")
        with patch(
            "core.mutex_controller.subprocess.run",
            side_effect=[unavailable, no_pid, empty_ps],
        ) as run, patch("core.mutex_controller.time.sleep") as sleep:
            self.assertEqual(MutexController("offline-device")._remote_processes(), {})

        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(MutexController.REMOTE_PROBE_RETRY_SECONDS)

    def test_remote_probe_recovers_when_adb_quotes_the_serial(self):
        unavailable = Mock(
            returncode=1,
            stdout="",
            stderr="adb.exe: device 'offline-device' not found",
        )
        no_pid = Mock(returncode=1, stdout="", stderr="")
        empty_ps = Mock(returncode=0, stdout="PID ARGS\n", stderr="")
        with patch(
            "core.mutex_controller.subprocess.run",
            side_effect=[unavailable, no_pid, empty_ps],
        ) as run, patch("core.mutex_controller.time.sleep") as sleep:
            self.assertEqual(MutexController("offline-device")._remote_processes(), {})

        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(MutexController.REMOTE_PROBE_RETRY_SECONDS)

    def test_remote_probe_finds_fastbot_running_as_app_process(self):
        no_pid = Mock(returncode=1, stdout="", stderr="")
        process_list = Mock(
            returncode=0,
            stdout=(
                "PID ARGS\n"
                "4321 app_process /system/bin "
                "com.android.commands.monkey.Monkey -p com.example.app\n"
            ),
            stderr="",
        )
        with patch(
            "core.mutex_controller.subprocess.run",
            side_effect=[no_pid, process_list],
        ):
            processes = MutexController("offline-device")._remote_processes()

        self.assertEqual(list(processes), ["4321"])


if __name__ == "__main__":
    unittest.main()
