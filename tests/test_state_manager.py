import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core.state_manager import StateManager


class _InstalledDevice:
    def __init__(self):
        self.app_list_called = False

    def app_list(self):
        self.app_list_called = True
        return ["com.example.app"]


class _NotInstalledDevice:
    def __init__(self):
        self.install_called = False

    def app_list(self):
        return []

    def app_install(self, _path):
        self.install_called = True


class _NavigationDevice:
    def __init__(self):
        self.started = []

    def app_start(self, package_name, stop=False, wait=False):
        self.started.append((package_name, stop, wait))
        return True


class _ForegroundDevice:
    def __init__(self, package="com.example.app"):
        self.package = package
        self.started = []

    def app_current(self):
        return {
            "package": self.package,
            "activity": ".MainActivity",
        }

    def app_start(self, package_name, stop=False, wait=False):
        self.started.append((package_name, stop, wait))
        self.package = package_name
        return True


class _MissingSelector:
    def wait(self, timeout=0):
        return False


class _ClickableSelector:
    def __init__(self, wait_fn, click_fn, info_fn=None):
        self._wait_fn = wait_fn
        self._click_fn = click_fn
        self._info_fn = info_fn

    def wait(self, timeout=0):
        return self._wait_fn()

    def click(self):
        self._click_fn()

    @property
    def info(self):
        return self._info_fn() if self._info_fn is not None else {}


class _LoginSelector:
    def __init__(self):
        self.clicked = False

    def click(self):
        self.clicked = True


class _DirectTextSelector:
    def __init__(self):
        self.values = []
        self.clicked = False
        self.info = {
            "packageName": "com.example.app",
            "bounds": {"left": 100, "top": 100, "right": 500, "bottom": 180},
        }

    def wait(self, timeout=0):
        return True

    def set_text(self, value):
        self.values.append(value)

    def click(self):
        self.clicked = True
        raise AssertionError("input control must use set_text without click")


class _OptionalNavigationDevice:
    def __call__(self, **_locator):
        return _MissingSelector()


class _UsbDialogNavigationDevice:
    def __init__(self):
        self.usb_dialog = True
        self.audit_clicked = False
        self.usb_cancel_clicked = False

    def dump_hierarchy(self, compressed=False):
        if self.usb_dialog:
            return (
                '<hierarchy><node package="com.android.settings" '
                'text="USB 连接方式" resource-id="android:id/alertTitle" />'
                '<node package="com.android.settings" text="取消" '
                'resource-id="android:id/button2" /></hierarchy>'
            )
        return (
            '<hierarchy><node package="com.example.app" '
            'resource-id="com.example.app:id/auditRadioButton" /></hierarchy>'
        )

    def __call__(self, **locator):
        if locator.get("resourceId") == "android:id/button2":
            return _ClickableSelector(
                lambda: self.usb_dialog,
                self._click_usb_cancel,
            )
        if locator.get("resourceId") == "com.example.app:id/auditRadioButton":
            return _ClickableSelector(
                lambda: not self.usb_dialog,
                self._click_audit,
            )
        return _MissingSelector()

    def _click_usb_cancel(self):
        self.usb_cancel_clicked = True
        self.usb_dialog = False

    def _click_audit(self):
        self.audit_clicked = True


class _AuditGateDevice:
    def __init__(
        self, phase="audit", audit_checked=False, confirmations_before_advance=1
    ):
        self.phase = phase
        self.audit_clicked = False
        self.confirm_clicked = False
        self.audit_checked = audit_checked
        self.audit_click_count = 0
        self.confirm_click_count = 0
        self.confirmations_before_advance = confirmations_before_advance

    def app_current(self):
        if self.phase == "main":
            return {
                "package": "com.example.app",
                "activity": ".MainActivity",
            }
        return {
            "package": "com.example.app",
            "activity": ".SplashActivity",
        }

    def dump_hierarchy(self, compressed=False):
        if self.phase == "audit":
            return (
                '<hierarchy><node package="com.example.app" '
                'resource-id="com.example.app:id/channelRadioGroup" />'
                '<node package="com.example.app" '
                'resource-id="com.example.app:id/appStateRadioGroup" />'
                '<node package="com.example.app" text="审核中" '
                'resource-id="com.example.app:id/auditRadioButton" />'
                '<node package="com.example.app" text="确定" '
                'class="android.widget.Button" /></hierarchy>'
            )
        return (
            '<hierarchy><node package="com.example.app" '
            'resource-id="com.example.app:id/vp_main" /></hierarchy>'
        )

    def __call__(self, **locator):
        if locator.get("resourceId") == "com.example.app:id/auditRadioButton":
            return _ClickableSelector(
                lambda: self.phase == "audit",
                self._click_audit,
                lambda: {"checked": self.audit_checked},
            )
        if locator.get("text") == "确定":
            return _ClickableSelector(
                lambda: self.phase == "audit" and self.audit_checked,
                self._click_confirm,
            )
        return _MissingSelector()

    def _click_audit(self):
        self.audit_clicked = True
        self.audit_click_count += 1
        self.audit_checked = not self.audit_checked

    def _click_confirm(self):
        self.confirm_clicked = True
        self.confirm_click_count += 1
        if (
            self.audit_checked
            and self.confirm_click_count >= self.confirmations_before_advance
        ):
            self.phase = "main"


class _AuditThenPrivacyDevice(_AuditGateDevice):
    def __init__(self, consent_advances):
        super().__init__()
        self.consent_advances = consent_advances
        self.consent_clicks = 0

    def _click_confirm(self):
        super()._click_confirm()
        if self.phase == "main":
            self.phase = "privacy"

    def dump_hierarchy(self, compressed=False):
        if self.phase == "privacy":
            return (
                '<hierarchy><node package="com.example.app" '
                'text="请阅读《用户协议》和《隐私政策》" />'
                '<node package="com.example.app" text="同意并继续" '
                'resource-id="com.example.app:id/btn_submit" />'
                '<node package="com.example.app" text="不同意" />'
                '</hierarchy>'
            )
        return super().dump_hierarchy(compressed=compressed)

    def __call__(self, **locator):
        if locator.get("text") == "同意并继续":
            return _ClickableSelector(
                lambda: self.phase == "privacy", self._click_consent
            )
        return super().__call__(**locator)

    def _click_consent(self):
        self.consent_clicks += 1
        if self.consent_advances:
            self.phase = "main"


class _TargetForegroundWithoutHierarchyDevice:
    def __init__(self):
        self.hierarchy_attempts = 0

    def app_current(self):
        return {
            "package": "com.example.app",
            "activity": ".SplashActivity",
        }

    def dump_hierarchy(self, compressed=False):
        self.hierarchy_attempts += 1
        raise RuntimeError("uiautomator hierarchy unavailable")

    def __call__(self, **_locator):
        return _MissingSelector()


class _GateWithMissingHierarchyDevice:
    def __init__(self, after_confirmation):
        self.after_confirmation = after_confirmation
        self.audit_checked = False
        self.audit_probe_count = 0

    @staticmethod
    def app_current():
        return {
            "package": "com.example.app",
            "activity": ".ConsentActivity",
        }

    @staticmethod
    def dump_hierarchy(compressed=False):
        return (
            '<hierarchy><node package="com.example.app" '
            'class="android.widget.FrameLayout" /></hierarchy>'
        )

    def __call__(self, **locator):
        if locator.get("resourceId") == "com.example.app:id/auditToggle":
            return _ClickableSelector(
                self._wait_for_audit,
                self._select_audit,
                lambda: {"checked": self.audit_checked},
            )
        if locator.get("text") == "confirm":
            return _ClickableSelector(lambda: True, lambda: None)
        return _MissingSelector()

    def _wait_for_audit(self):
        self.audit_probe_count += 1
        if self.audit_probe_count <= 3:
            return True
        if self.after_confirmation == "absent":
            return False
        raise RuntimeError("uiautomator selector unavailable")

    def _select_audit(self):
        self.audit_checked = True


class _DelayedAuditGateDevice(_AuditGateDevice):
    def __init__(self, visible_on_probe=3):
        super().__init__(
            "audit", audit_checked=False, confirmations_before_advance=1000
        )
        self.visible_on_probe = visible_on_probe
        self.audit_probe_count = 0

    def dump_hierarchy(self, compressed=False):
        if self.audit_probe_count < self.visible_on_probe:
            return ""
        return super().dump_hierarchy(compressed=compressed)

    def __call__(self, **locator):
        if locator.get("resourceId") == "com.example.app:id/auditRadioButton":
            return _ClickableSelector(
                self._wait_for_delayed_audit,
                self._click_audit,
                lambda: {"checked": self.audit_checked},
            )
        return super().__call__(**locator)

    def _wait_for_delayed_audit(self):
        self.audit_probe_count += 1
        if self.audit_probe_count < self.visible_on_probe:
            raise RuntimeError("uiautomator hierarchy unavailable")
        return self.phase == "audit"


class _ResetDevice:
    def __init__(self, output="Success", exit_code=0):
        self.output = output
        self.exit_code = exit_code
        self.started = False

    def app_stop(self, _package_name):
        return None

    def shell(self, _command, timeout=0):
        self.shell_timeout = timeout
        return type(
            "ShellResult",
            (),
            {"output": self.output, "exit_code": self.exit_code},
        )()

    def app_start(self, _package_name, stop=False, wait=False):
        self.started = stop and wait
        return True


class _VendorInstallDevice:
    def __init__(self):
        self.package = "com.example.app"
        self.phase = "guide"
        self.back_pressed = False
        self.started = False

    def app_stop(self, _package_name):
        return None

    def shell(self, command, timeout=0):
        if command[:2] == ["pm", "clear"]:
            return type(
                "ShellResult",
                (),
                {
                    "output": "SecurityException: missing CLEAR_APP_USER_DATA",
                    "exit_code": 255,
                },
            )()
        if command[:3] == ["cmd", "package", "resolve-activity"]:
            return type(
                "ShellResult",
                (),
                {
                    "output": f"priority=0 preferredOrder=0\n{self.package}/.MainActivity\n",
                    "exit_code": 0,
                },
            )()
        if command[:3] == ["input", "keyevent", "4"]:
            self.back_pressed = True
            self.phase = "app"
            return type(
                "ShellResult",
                (),
                {"output": "", "exit_code": 0},
            )()
        return type("ShellResult", (), {"output": "", "exit_code": 0})()

    def app_current(self):
        if self.back_pressed or self.started or self.phase == "app":
            return {
                "package": self.package,
                "activity": ".MainActivity",
            }
        if self.phase == "finish":
            return {
                "package": "com.oplus.appdetail",
                "activity": "com.oplus.appdetail.model.finish.InstallFinishActivity",
            }
        return {
            "package": "com.oplus.appdetail",
            "activity": "com.oplus.appdetail.model.guide.ui.InstallGuideActivity",
        }

    def press(self, key):
        if key == "back":
            self.back_pressed = True
            self.phase = "app"

    def app_start(self, package_name, stop=False, wait=False):
        self.started = True
        self.package = package_name
        self.phase = "app"
        return True


class _VendorInstallProcess:
    def __init__(self, device):
        self.device = device
        self.args = ["adb", "install", "-r", "-t", "local.apk"]
        self.returncode = None
        self.killed = False

    def poll(self):
        if self.device.back_pressed:
            self.returncode = 0
        return self.returncode

    def communicate(self):
        if self.returncode is None:
            self.returncode = 0 if self.device.back_pressed else -9
        return ("Success\n", "")

    def kill(self):
        self.killed = True
        self.returncode = -9


class _ExplicitLaunchDevice:
    def __init__(self):
        self.started = None
        self.shell_commands = []

    def app_stop(self, _package_name):
        return None

    def shell(self, command, timeout=0):
        self.shell_commands.append((list(command), timeout))
        if command[:2] == ["am", "start"]:
            return type(
                "ShellResult",
                (),
                {"output": "Starting: Intent", "exit_code": 0},
            )()
        return type(
            "ShellResult",
            (),
            {
                "output": (
                    "priority=0 preferredOrder=0\n"
                    "com.example.app/.ui.SplashActivity\n"
                ),
                "exit_code": 0,
            },
        )()

    def app_start(
        self, package_name, activity=None, stop=False, wait=False
    ):
        self.started = (package_name, activity, stop, wait)
        return True


class _SettingsForegroundDevice(_ExplicitLaunchDevice):
    def __init__(self):
        super().__init__()
        self.relaunches = 0

    def app_current(self):
        if self.relaunches:
            return {
                "package": "com.example.app",
                "activity": ".ui.SplashActivity",
            }
        return {
            "package": "com.android.settings",
            "activity": ".SubSettings",
        }

    def shell(self, command, timeout=0):
        result = super().shell(command, timeout=timeout)
        if command[:2] == ["am", "start"]:
            self.relaunches += 1
        return result

    def app_start(
        self, package_name, activity=None, stop=False, wait=False
    ):
        self.relaunches += 1
        return super().app_start(
            package_name,
            activity=activity,
            stop=stop,
            wait=wait,
        )


class _TransientLauncherDevice:
    info = {"manufacturer": "oppo"}

    def app_stop(self, _package_name):
        return None

    def shell(self, _command, timeout=0):
        raise RuntimeError("uiautomator2 transport is reconnecting")


class _OplusModelIdentityDevice:
    info = {"productName": "PGBM10"}


class _OplusNoFallbackDevice(_OplusModelIdentityDevice):
    def __init__(self):
        self.app_start_called = False

    def app_start(self, *_args, **_kwargs):
        self.app_start_called = True
        raise AssertionError("uiautomator2.app_start must not run on Oplus fallback")


class _OplusSnapshotDevice(_OplusModelIdentityDevice):
    def app_current(self):
        raise AssertionError("snapshot must not poll uiautomator2.app_current")

    def dump_hierarchy(self, compressed=False):
        raise AssertionError("snapshot must not dump the Oplus UI hierarchy")


class _OplusAuditUiDevice(_OplusModelIdentityDevice):
    def app_current(self):
        raise AssertionError("audit UI must not probe foreground without permission UI")


class StateManagerTests(unittest.TestCase):
    def test_reset_app_requires_explicit_clear_success(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.example.app"
        manager._audit_gate_known_complete = True
        manager.d = _ResetDevice(output="SecurityException", exit_code=255)

        self.assertFalse(manager.reset_app())
        self.assertFalse(manager.d.started)
        self.assertFalse(manager._audit_gate_known_complete)

    def test_reset_app_starts_only_after_clear_success(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.example.app"
        manager.d = _ResetDevice()

        self.assertTrue(manager.reset_app())
        self.assertTrue(manager.d.started)

    def test_reset_app_does_not_reinstall_when_vendor_denies_clear(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        device = _VendorInstallDevice()
        manager.d = device

        with TemporaryDirectory() as temporary:
            manager._apk_path = Path(temporary) / "local.apk"
            manager._apk_path.write_bytes(b"offline fixture")
            install_process = _VendorInstallProcess(device)

            def click_side_effect(locator, timeout=0.5):
                if locator.get("resourceId") == "com.oplus.appdetail:id/btn_left":
                    device.phase = "finish"
                    return True
                if locator.get("text") == "继续安装":
                    device.phase = "finish"
                    return True
                return False

            uninstall_result = type(
                "Result",
                (),
                {"returncode": 0, "stdout": "Success\n", "stderr": ""},
            )()
            clear_failure = type(
                "Result",
                (),
                {
                    "returncode": 255,
                    "stdout": "",
                    "stderr": "SecurityException: missing CLEAR_APP_USER_DATA",
                },
            )()
            pm_path_result = type(
                "Result",
                (),
                {"returncode": 0, "stdout": "package:/data/app/base.apk\n", "stderr": ""},
            )()

            with (
                patch.object(
                    manager,
                    "_run_direct_adb",
                    side_effect=[
                        clear_failure,
                        pm_path_result,
                        uninstall_result,
                        pm_path_result,
                    ],
                ),
                patch.object(
                    manager, "_clear_app_data_via_settings", return_value=False
                ),
                patch.object(manager, "_click_if_exists", side_effect=click_side_effect),
                patch(
                    "core.state_manager.subprocess.Popen",
                    return_value=install_process,
                ) as installer,
            ):
                self.assertFalse(manager.reset_app())
                installer.assert_not_called()

        self.assertFalse(device.started)

    def test_reset_app_does_not_install_when_vendor_clear_leaves_package_absent(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        device = _VendorInstallDevice()
        manager.d = device

        with TemporaryDirectory() as temporary:
            manager._apk_path = Path(temporary) / "local.apk"
            manager._apk_path.write_bytes(b"offline fixture")
            install_process = _VendorInstallProcess(device)

            def click_side_effect(locator, timeout=0.5):
                if locator.get("resourceId") == "com.oplus.appdetail:id/btn_left":
                    device.phase = "finish"
                    return True
                return False

            absent_result = type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": ""},
            )()
            clear_failure = type(
                "Result",
                (),
                {
                    "returncode": 255,
                    "stdout": "",
                    "stderr": "SecurityException: missing CLEAR_APP_USER_DATA",
                },
            )()
            pm_path_result = type(
                "Result",
                (),
                {"returncode": 0, "stdout": "package:/data/app/base.apk\n", "stderr": ""},
            )()

            with (
                patch.object(
                    manager,
                    "_run_direct_adb",
                    side_effect=[clear_failure, absent_result, pm_path_result],
                ),
                patch.object(
                    manager, "_clear_app_data_via_settings", return_value=False
                ),
                patch.object(manager, "_click_if_exists", side_effect=click_side_effect),
                patch(
                    "core.state_manager.subprocess.Popen",
                    return_value=install_process,
                ) as installer,
            ):
                self.assertFalse(manager.reset_app())
                installer.assert_not_called()

        self.assertFalse(device.started)

    def test_reset_app_uses_explicit_launcher_activity_when_available(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.example.app"
        manager.d = _ExplicitLaunchDevice()

        self.assertTrue(manager.reset_app(clear_data=False))
        self.assertEqual(
            manager.d.started,
            (
                "com.example.app",
                ".ui.SplashActivity",
                True,
                True,
            ),
        )

    def test_reset_app_uses_explicit_clear_task_launch_for_recovery(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.example.app"
        manager.d = _ExplicitLaunchDevice()

        self.assertTrue(manager.reset_app(clear_data=False, clear_task=True))

        self.assertIsNone(manager.d.started)
        self.assertIn(
            (
                [
                    "am",
                    "start",
                    "-W",
                    "-f",
                    "0x10008000",
                    "-n",
                    "com.example.app/.ui.SplashActivity",
                ],
                30,
            ),
            manager.d.shell_commands,
        )

    def test_reset_app_uses_clean_task_start_when_settings_remains_foreground(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _SettingsForegroundDevice()

        with patch.object(manager, "_clear_app_data", return_value=True):
            self.assertTrue(manager.reset_app())

        self.assertIsNone(manager.d.started)
        self.assertIn(
            (
                [
                    "am",
                    "start",
                    "-W",
                    "-f",
                    "0x10008000",
                    "-n",
                    "com.example.app/.ui.SplashActivity",
                ],
                30,
            ),
            manager.d.shell_commands,
        )

    def test_foreground_wait_uses_clean_task_after_vendor_settings_page(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _SettingsForegroundDevice()

        with patch.object(manager, "_foreground_app_from_adb", return_value={}):
            self.assertTrue(manager._wait_for_target_foreground(timeout=1))
        self.assertEqual(manager.d.relaunches, 1)
        self.assertIsNone(manager.d.started)
        self.assertIn(
            (
                [
                    "am",
                    "start",
                    "-W",
                    "-f",
                    "0x10008000",
                    "-n",
                    "com.example.app/.ui.SplashActivity",
                ],
                30,
            ),
            manager.d.shell_commands,
        )

    def test_foreground_wait_retries_explicit_component_after_miui_launcher(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "xiaomi-device"
        manager.package_name = "com.example.app"
        manager._launcher_activity = ".ui.SplashActivity"
        manager.d = Mock()
        manager.d.app_current.side_effect = [
            {"package": "com.miui.home", "activity": ".launcher.Launcher"},
            {"package": "com.example.app", "activity": ".ui.SplashActivity"},
        ]

        with patch.object(manager, "_foreground_app_from_adb", return_value={}), patch.object(
            manager, "_is_oplus_device", return_value=False
        ), patch.object(
            manager, "_start_target_app_via_direct_adb", return_value=True
        ) as relaunch, patch("core.state_manager.time.sleep"):
            self.assertTrue(manager._wait_for_target_foreground(timeout=1))

        relaunch.assert_called_once_with(
            stop=False,
            clear_task=True,
            activity=".ui.SplashActivity",
            retry_attempts=1,
        )

    def test_foreground_wait_fails_after_bounded_miui_launcher_retries(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "xiaomi-device"
        manager.package_name = "com.example.app"
        manager._launcher_activity = ".ui.SplashActivity"
        manager.FOREGROUND_LAUNCHER_RETRY_SECONDS = 0.0
        manager.d = Mock()
        manager.d.app_current.return_value = {
            "package": "com.miui.home",
            "activity": ".launcher.Launcher",
        }

        with patch.object(manager, "_foreground_app_from_adb", return_value={}), patch.object(
            manager, "_is_oplus_device", return_value=False
        ), patch.object(
            manager, "_start_target_app_via_direct_adb", return_value=True
        ) as relaunch, patch("core.state_manager.time.sleep"):
            self.assertFalse(manager._wait_for_target_foreground(timeout=0.1))

        self.assertEqual(
            relaunch.call_count,
            StateManager.FOREGROUND_LAUNCHER_RETRY_ATTEMPTS,
        )

    def test_foreground_parser_prefers_android_16_top_resumed_activity(self):
        output = """
          topResumedActivity=ActivityRecord{1a2b3c u0 com.example.app/.MainActivity t42}
          mFocusedApp=ActivityRecord{1a2b3c u0 com.example.app/.MainActivity t42}
          ACTIVITY com.stale.app/.OldActivity 1234 pid=8174
        """

        self.assertEqual(
            StateManager._foreground_app_from_output(output),
            {
                "package": "com.example.app",
                "activity": "com.example.app.MainActivity",
            },
        )

    def test_foreground_wait_uses_adb_result_when_adbutils_is_stale(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "android-16-device"
        manager.package_name = "com.example.app"
        manager.d = Mock()
        manager.d.app_current.return_value = {
            "package": "com.stale.app",
            "activity": ".OldActivity",
            "pid": 8174,
        }

        with patch.object(
            manager,
            "_foreground_app_from_adb",
            return_value={
                "package": "com.example.app",
                "activity": "com.example.app.MainActivity",
            },
        ), patch.object(manager, "_is_oplus_device", return_value=False):
            self.assertTrue(manager._wait_for_target_foreground(timeout=0.1))

        manager.d.app_current.assert_not_called()

    def test_reset_app_uses_direct_adb_when_uiautomator_launcher_fails(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _TransientLauncherDevice()
        resolved = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "com.example.app/.ui.SplashActivity\n",
                "stderr": "",
            },
        )()
        succeeded = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(
            manager,
            "_run_direct_adb",
            side_effect=[succeeded, resolved, succeeded],
        ) as direct:
            self.assertTrue(manager.reset_app(clear_data=False))

        self.assertEqual(
            direct.call_args_list[0].args[0],
            ["shell", "am", "force-stop", "com.example.app"],
        )
        self.assertEqual(
            direct.call_args_list[1].args[0],
            [
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "com.example.app",
            ],
        )
        self.assertEqual(
            direct.call_args_list[2].args[0],
            [
                "shell",
                "am",
                "start",
                "-n",
                "com.example.app/.ui.SplashActivity",
            ],
        )

    def test_oplus_detection_uses_all_identity_fields(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _OplusModelIdentityDevice()

        self.assertTrue(manager._is_oplus_device())

    def test_oplus_direct_start_does_not_fall_back_to_uiautomator_monkey(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _OplusNoFallbackDevice()
        failed = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "offline"})()

        with patch.object(manager, "_run_direct_adb", side_effect=[failed, failed]):
            self.assertFalse(manager._start_target_app(stop=False, wait=True))

        self.assertFalse(manager.d.app_start_called)

    def test_oplus_snapshot_uses_known_state_without_extra_uiautomator_calls(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _OplusSnapshotDevice()
        manager._last_known_app = {"package": "com.example.app", "activity": ".Main"}

        snapshot = manager.save_snapshot("initial")

        self.assertEqual(snapshot["app_current"]["package"], "com.example.app")
        self.assertEqual(snapshot["hierarchy"], "")

    def test_oplus_known_foreground_verification_does_not_poll_or_restart(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _OplusSnapshotDevice()
        manager._last_known_app = {"package": "com.example.app"}

        self.assertTrue(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "verify_foreground_stable",
                        "restore": True,
                        "stable_seconds": 0,
                        "timeout": 1,
                        "after": 0,
                    }
                ],
            )
        )

    def test_oplus_audit_ui_does_not_probe_permissions_without_permission_controls(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _OplusAuditUiDevice()

        self.assertFalse(
            manager._allow_runtime_permission_dialog(
                '<hierarchy><node resource-id="com.example.app:id/auditRadioButton" /></hierarchy>'
            )
        )

    def test_permission_allow_locators_cover_miui_chinese_buttons(self):
        text_locators = {
            locator.get("text") for locator in StateManager.PERMISSION_ALLOW_LOCATORS
        }

        self.assertIn("仅在使用中允许", text_locators)
        self.assertIn("本次运行允许", text_locators)
        self.assertIn("允许访问全部", text_locators)

    def test_permission_allow_locators_prioritize_all_photos_action(self):
        locators = StateManager.PERMISSION_ALLOW_LOCATORS
        self.assertEqual(
            r".*:id/permission_allow_all_button",
            locators[0].get("resourceIdMatches"),
        )
        self.assertEqual(
            r".*:id/permission_allow_foreground_only_button",
            locators[1].get("resourceIdMatches"),
        )

    def test_url_is_rejected_even_when_package_is_already_installed(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.example.app"
        manager.d = _InstalledDevice()
        self.assertFalse(manager.install_app("https://example.invalid/app.apk"))
        self.assertFalse(manager.d.app_list_called)

    def test_install_app_prefers_headless_adb_install(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _NotInstalledDevice()
        with TemporaryDirectory() as temporary:
            apk = Path(temporary) / "local.apk"
            apk.write_bytes(b"offline fixture")
            results = [
                type("Result", (), {"returncode": 0, "stdout": "Success\n", "stderr": ""})(),
                type("Result", (), {"returncode": 0, "stdout": "package:/data/app/base.apk\n", "stderr": ""})(),
            ]
            with patch("core.state_manager.subprocess.run", side_effect=results) as run:
                self.assertTrue(manager.install_app(str(apk)))
            self.assertFalse(manager.d.install_called)
            self.assertEqual(run.call_args_list[0].args[0][3:6], ["install", "-r", "-t"])

    def test_navigation_launches_only_the_target_package(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _NavigationDevice()

        self.assertTrue(
            manager.navigate_to(
                "MainPage",
                [{"action": "launch_app", "stop": True, "after": 0}],
            )
        )
        self.assertEqual(
            manager.d.started,
            [("com.example.app", True, True)],
        )

        self.assertFalse(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "launch_app",
                        "package": "com.other.app",
                        "after": 0,
                    }
                ],
            )
        )

    def test_navigation_verifies_target_foreground(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _ForegroundDevice()

        with patch.object(manager, "_foreground_app_from_adb", return_value={}):
            self.assertTrue(
                manager.navigate_to(
                    "MainPage",
                    [{"action": "verify_foreground", "timeout": 1, "after": 0}],
                )
            )

    def test_navigation_can_restore_target_foreground(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _ForegroundDevice("com.example.other")

        with patch.object(manager, "_foreground_app_from_adb", return_value={}):
            self.assertTrue(
                manager.navigate_to(
                    "MainPage",
                    [
                        {
                            "action": "verify_foreground",
                            "restore": True,
                            "timeout": 1,
                            "after": 0,
                        }
                    ],
                )
            )
        self.assertEqual(
            manager.d.started,
            [("com.example.app", False, True)],
        )

    def test_safe_input_uses_control_set_text_without_clicking_keyboard(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager._input_safety = {"enabled": True, "hide_timeout_seconds": 0.2}
        manager.input_recovery_required = False
        selector = _DirectTextSelector()

        class Device:
            def __call__(self, **_locator):
                return selector

            def window_size(self):
                return (1080, 2400)

        manager.d = Device()
        manager._run_direct_adb = lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "mInputShown=false\nmIsInputViewShown=false\n", "stderr": ""}
        )()
        manager._foreground_app = lambda: {"package": "com.example.app"}

        manager._set_text({"resourceId": "com.example.app:id/search"}, "上海虹桥")

        self.assertEqual(["上海虹桥"], selector.values)
        self.assertFalse(selector.clicked)

    def test_navigation_uses_adb_foreground_when_adbutils_is_stale(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "android-16-device"
        manager.package_name = "com.example.app"
        manager.d = _ForegroundDevice("com.stale.app")

        with patch.object(
            manager,
            "_foreground_app_from_adb",
            return_value={
                "package": "com.example.app",
                "activity": "com.example.app.MainActivity",
            },
        ):
            self.assertTrue(
                manager.navigate_to(
                    "MainPage",
                    [
                        {
                            "action": "verify_foreground_stable",
                            "stable_seconds": 0,
                            "timeout": 0.1,
                            "after": 0,
                        }
                    ],
                )
            )

    def test_optional_navigation_element_may_be_absent(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _OptionalNavigationDevice()

        self.assertTrue(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "click_if_exists",
                        "resourceId": "com.example.app:id/first_run_only",
                        "timeout": 0.1,
                        "after": 0,
                    }
                ],
            )
        )

    def test_required_navigation_element_must_be_present(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _OptionalNavigationDevice()

        self.assertFalse(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "click",
                        "resourceId": "com.example.app:id/required_control",
                        "timeout": 0.1,
                        "after": 0,
                    }
                ],
            )
        )

    def test_navigation_requeries_selector_after_stale_object_click(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "honor-device"
        manager.package_name = "com.example.app"
        manager.d = Mock()
        stale_selector = Mock()
        stale_selector.click.side_effect = RuntimeError(
            "androidx.test.uiautomator.StaleObjectException"
        )
        fresh_selector = Mock()
        locator = {"resourceId": "com.example.app:id/auditRadioButton"}

        with patch.object(
            manager, "_dismiss_transient_system_dialogs"
        ), patch.object(
            manager,
            "_wait_for_selector",
            side_effect=[(stale_selector, False), (fresh_selector, False)],
        ) as wait, patch("core.state_manager.time.sleep"):
            self.assertTrue(
                manager.navigate_to(
                    "MainPage",
                    [
                        {
                            "action": "click",
                            "locator": locator,
                            "timeout": 1,
                            "after": 0,
                        }
                    ],
                )
            )

        self.assertEqual(wait.call_count, 2)
        stale_selector.click.assert_called_once_with()
        fresh_selector.click.assert_called_once_with()

    def test_navigation_reconnects_after_transient_http_disconnect(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = Mock()
        disconnected_selector = Mock()
        disconnected_selector.click.side_effect = RuntimeError(
            "RemoteDisconnected: Remote end closed connection without response"
        )
        recovered_selector = Mock()
        recovered_device = Mock()
        locator = {"resourceId": "com.example.app:id/auditRadioButton"}

        with patch.object(
            manager, "_dismiss_transient_system_dialogs"
        ), patch.object(
            manager,
            "_wait_for_selector",
            side_effect=[
                (disconnected_selector, False),
                (recovered_selector, False),
            ],
        ), patch.object(
            manager, "_connect_with_retry", return_value=recovered_device
        ) as reconnect, patch("core.state_manager.time.sleep"):
            self.assertTrue(
                manager.navigate_to(
                    "MainPage",
                    [
                        {
                            "action": "click",
                            "locator": locator,
                            "timeout": 3,
                            "after": 0,
                        }
                    ],
                )
            )

        reconnect.assert_called_once_with()
        self.assertIs(manager.d, recovered_device)
        recovered_selector.click.assert_called_once_with()

    def test_optional_click_reconnects_after_transient_http_disconnect(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = Mock()
        disconnected_selector = Mock()
        disconnected_selector.wait.return_value = True
        disconnected_selector.click.side_effect = RuntimeError(
            "RemoteDisconnected: Remote end closed connection without response"
        )
        recovered_selector = Mock()
        recovered_selector.wait.return_value = True
        recovered_device = Mock()
        locator = {"resourceId": "com.example.app:id/optional"}

        with patch.object(
            manager,
            "_selector",
            side_effect=[(disconnected_selector, False), (recovered_selector, False)],
        ), patch.object(
            manager, "_connect_with_retry", return_value=recovered_device
        ) as reconnect, patch("core.state_manager.time.sleep"):
            self.assertTrue(manager._click_if_exists(locator, timeout=3))

        reconnect.assert_called_once_with()
        self.assertIs(manager.d, recovered_device)
        recovered_selector.click.assert_called_once_with()

    def test_direct_adb_retries_transport_failures(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        failed = type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": "error: device not found"},
        )()
        succeeded = type("Result", (), {"returncode": 0, "stdout": "Success\n", "stderr": ""})()

        with patch(
            "core.state_manager.subprocess.run", side_effect=[failed, failed, succeeded]
        ) as run, patch("core.state_manager.time.sleep"):
            result = manager._run_direct_adb(
                ["shell", "pm", "clear", "com.example.app"],
                timeout=30,
                transport_retries=2,
            )

        self.assertIs(result, succeeded)
        self.assertEqual(run.call_count, 3)

    def test_screenshot_capture_retries_empty_adb_output(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        empty = type(
            "Result", (), {"returncode": 0, "stdout": b"", "stderr": b""}
        )()
        succeeded = type(
            "Result", (), {"returncode": 0, "stdout": b"png", "stderr": b""}
        )()

        with patch(
            "core.state_manager.subprocess.run", side_effect=[empty, succeeded]
        ) as run, patch("core.state_manager.time.sleep"):
            content = manager.capture_screenshot_png()

        self.assertEqual(content, b"png")
        self.assertEqual(run.call_count, 2)

    def test_oplus_launcher_retries_start_without_repeating_force_stop(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "oppo-device"
        manager.package_name = "com.example.app"
        manager.d = _OplusModelIdentityDevice()
        resolved = type(
            "Result",
            (),
            {"returncode": 0, "stdout": "com.example.app/.MainActivity\n", "stderr": ""},
        )()
        failed = type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": "error: device not found"},
        )()
        succeeded = type("Result", (), {"returncode": 0, "stdout": "Starting: Intent\n", "stderr": ""})()

        with patch.object(
            manager, "_run_direct_adb", side_effect=[resolved, succeeded, failed, succeeded]
        ) as direct, patch("core.state_manager.time.sleep"):
            self.assertTrue(manager._start_target_app(stop=True, wait=True))

        commands = [call.args[0] for call in direct.call_args_list]
        self.assertEqual(commands[0][0:4], ["shell", "cmd", "package", "resolve-activity"])
        self.assertEqual(commands[1][0:3], ["shell", "am", "force-stop"])
        self.assertEqual(sum(command[0:3] == ["shell", "am", "force-stop"] for command in commands), 1)

    def test_login_may_leave_optional_verification_code_empty(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        agreement = _LoginSelector()
        login_button = _LoginSelector()
        username_locator = {"className": "android.widget.EditText", "instance": 0}
        verification_locator = {
            "className": "android.widget.EditText",
            "instance": 1,
        }
        locators = {
            "username": username_locator,
            "password": verification_locator,
            "agreement": {"resourceId": "com.example.app:id/agreement"},
            "login_button": {"text": "Login"},
            "allow_empty_password": True,
            "wait_after_login": 0,
        }

        with patch.object(manager, "_set_text") as set_text, patch.object(
            manager,
            "_wait_for_selector",
            side_effect=[(agreement, False), (login_button, False)],
        ):
            self.assertTrue(manager.login("12000000000", "", locators))

        set_text.assert_called_once_with(username_locator, "12000000000")
        self.assertTrue(agreement.clicked)
        self.assertTrue(login_button.clicked)

    def test_navigation_dismisses_usb_dialog_before_required_click(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _UsbDialogNavigationDevice()

        self.assertTrue(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "click",
                        "resourceId": "com.example.app:id/auditRadioButton",
                        "timeout": 1,
                        "after": 0,
                    }
                ],
            )
        )
        self.assertTrue(manager.d.usb_cancel_clicked)
        self.assertTrue(manager.d.audit_clicked)

    def test_navigation_completes_audit_gate_when_present(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("audit")

        self.assertTrue(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "complete_audit_gate",
                        "audit_locator": {
                            "resourceId": "com.example.app:id/auditRadioButton"
                        },
                        "confirm_locator": {"text": "确定"},
                        "audit_activity": "com.example.app/.SplashActivity",
                        "timeout": 1,
                        "after": 0,
                    }
                ],
            )
        )
        self.assertTrue(manager.d.audit_clicked)
        self.assertTrue(manager.d.confirm_clicked)
        self.assertEqual(1, manager.d.audit_click_count)
        self.assertTrue(manager._audit_gate_known_complete)

    def test_navigation_does_not_toggle_an_already_checked_audit_state(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("audit", audit_checked=True)

        self.assertTrue(
            manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=2,
                audit_activity="com.example.app/.SplashActivity",
            )
        )
        self.assertEqual(0, manager.d.audit_click_count)
        self.assertTrue(manager._audit_gate_known_complete)
        self.assertEqual(1, manager.d.confirm_click_count)

    def test_navigation_retries_confirmation_without_toggling_audit_off(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice(
            "audit", audit_checked=False, confirmations_before_advance=2
        )

        self.assertTrue(
            manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=3,
                audit_activity="com.example.app/.SplashActivity",
            )
        )
        self.assertEqual(1, manager.d.audit_click_count)
        self.assertEqual(2, manager.d.confirm_click_count)

    def test_recovery_waits_for_privacy_gate_after_audit_confirmation(self):
        for consent_advances in (True, False):
            with self.subTest(consent_advances=consent_advances):
                manager = StateManager.__new__(StateManager)
                manager.device_serial = "device-1"
                manager.package_name = "com.example.app"
                manager.d = _AuditThenPrivacyDevice(consent_advances)
                elapsed = 0.0

                def advance(seconds):
                    nonlocal elapsed
                    elapsed += seconds

                with patch("core.state_manager.time.monotonic", lambda: elapsed), patch(
                    "core.state_manager.time.sleep", side_effect=advance
                ):
                    completed = manager.recover_audit_gate(
                        {"resourceId": "com.example.app:id/auditRadioButton"},
                        {"text": "确定"},
                        timeout=3,
                    )

                self.assertEqual(consent_advances, completed)
                self.assertEqual(consent_advances, manager._audit_gate_known_complete)
                self.assertEqual(1, manager.d.audit_click_count)
                self.assertEqual(1, manager.d.confirm_click_count)
                self.assertGreaterEqual(manager.d.consent_clicks, 1)
                self.assertEqual("main" if consent_advances else "privacy", manager.d.phase)

    def test_navigation_treats_privacy_page_on_splash_activity_as_audit_complete(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("privacy")

        self.assertTrue(
            manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=1,
                audit_activity="com.example.app/.SplashActivity",
            )
        )
        self.assertEqual(0, manager.d.audit_click_count)

    def test_audit_gate_allows_stable_target_when_gate_was_never_observed(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _TargetForegroundWithoutHierarchyDevice()
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0

        with self.assertLogs("core.state_manager", level="WARNING") as captured, patch(
            "core.state_manager.time.sleep"
        ):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=1,
            )

        self.assertTrue(completed)
        self.assertGreaterEqual(
            manager.d.hierarchy_attempts,
            manager.AUDIT_GATE_ABSENT_STABLE_PROBES,
        )
        self.assertTrue(
            any("was known complete before relaunch" in line for line in captured.output)
        )

    def test_recovery_waits_for_stable_window_on_splash_hierarchy(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("privacy")
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0

        with patch.object(
            manager, "_dump_hierarchy_text", wraps=manager._dump_hierarchy_text
        ) as dump_hierarchy, patch("core.state_manager.time.sleep"):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "confirm"},
                timeout=1,
            )

        self.assertTrue(completed)
        self.assertGreaterEqual(
            dump_hierarchy.call_count,
            manager.AUDIT_GATE_ABSENT_STABLE_PROBES,
        )

    def test_audit_hierarchy_detection_uses_the_configured_locator(self):
        hierarchy = (
            '<hierarchy><node package="com.example.app" '
            'resource-id="com.example.app:id/customGateToggle" '
            'text="Custom gate" content-desc="primary consent" '
            'class="android.widget.Button" />'
            '<node package="com.example.app" text="Second" '
            'class="android.widget.Button" /></hierarchy>'
        )

        self.assertTrue(
            StateManager._hierarchy_matches_locator(
                hierarchy,
                {"resourceId": "com.example.app:id/customGateToggle"},
            )
        )
        self.assertTrue(
            StateManager._hierarchy_matches_locator(
                hierarchy,
                {"resourceIdMatches": r"com\.example\.app:id/customGate.*"},
            )
        )
        self.assertFalse(
            StateManager._hierarchy_matches_locator(
                hierarchy,
                {"resourceId": "com.example.app:id/auditRadioButton"},
            )
        )
        self.assertTrue(
            StateManager._hierarchy_matches_locator(
                hierarchy,
                {
                    "textContains": "Custom",
                    "descriptionContains": "consent",
                },
            )
        )
        self.assertTrue(
            StateManager._hierarchy_matches_locator(
                hierarchy,
                {"className": "android.widget.Button", "instance": 1},
            )
        )
        self.assertIsNone(
            StateManager._hierarchy_matches_locator(
                hierarchy,
                {
                    "xpath": "//*[@resource-id='com.example.app:id/customGateToggle']",
                    "resourceId": "ignored-by-xpath",
                },
            )
        )
        self.assertIsNone(
            StateManager._hierarchy_matches_locator(
                hierarchy, {"xpath": "//android.widget.Button[@text='Custom gate']"}
            )
        )
        self.assertIsNone(
            StateManager._hierarchy_matches_locator(
                hierarchy, {"xpath": "Custom gate"}
            )
        )
        self.assertIsNone(
            StateManager._hierarchy_matches_locator(
                hierarchy, {"resourceIdMatches": "["}
            )
        )
        self.assertIsNone(
            StateManager._hierarchy_matches_locator(hierarchy, {"text": ""})
        )
        self.assertIsNone(
            StateManager._hierarchy_matches_locator(
                '<hierarchy><node package="com.example.app">',
                {"resourceId": "com.example.app:id/customGateToggle"},
            )
        )

    def test_initial_audit_gate_uses_live_xpath_probe_for_absence(self):
        class Device:
            @staticmethod
            def app_current():
                return {
                    "package": "com.example.app",
                    "activity": ".ConsentActivity",
                }

            @staticmethod
            def dump_hierarchy(compressed=False):
                return (
                    '<hierarchy><node package="com.example.app" '
                    'class="android.widget.FrameLayout" /></hierarchy>'
                )

            @staticmethod
            def xpath(_locator):
                return _MissingSelector()

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.02],
        ), patch("core.state_manager.time.sleep"):
            completed = manager._complete_audit_gate(
                {"xpath": "//android.widget.RadioButton[@text='Gate']"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertTrue(completed)

    def test_initial_audit_gate_does_not_trust_xml_when_xpath_probe_fails(self):
        class ProbeFailure:
            @staticmethod
            def wait(timeout=0):
                raise RuntimeError("uiautomator xpath unavailable")

        class Device:
            @staticmethod
            def app_current():
                return {
                    "package": "com.example.app",
                    "activity": ".ConsentActivity",
                }

            @staticmethod
            def dump_hierarchy(compressed=False):
                return (
                    '<hierarchy><node package="com.example.app" '
                    'resource-id="com.example.app:id/customGateToggle" /></hierarchy>'
                )

            @staticmethod
            def xpath(_locator):
                return ProbeFailure()

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.02, 0.2],
        ), patch("core.state_manager.time.sleep"):
            completed = manager._complete_audit_gate(
                {"xpath": "//*[@resource-id='com.example.app:id/customGateToggle']"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)

    def test_recovery_fallback_requires_a_successful_xpath_absence_probe(self):
        class ProbeFailure:
            @staticmethod
            def wait(timeout=0):
                raise RuntimeError("uiautomator xpath unavailable")

        class Device:
            @staticmethod
            def app_current():
                return {
                    "package": "com.example.app",
                    "activity": ".SplashActivity",
                }

            @staticmethod
            def dump_hierarchy(compressed=False):
                return (
                    '<hierarchy><node package="com.example.app" '
                    'class="android.widget.FrameLayout" /></hierarchy>'
                )

            @staticmethod
            def xpath(_locator):
                return ProbeFailure()

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 1

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.02, 0.2],
        ), patch("core.state_manager.time.sleep"):
            completed = manager.recover_audit_gate(
                {"xpath": "//android.widget.RadioButton[@text='Gate']"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)

    def test_audit_recovery_accepts_stable_home_with_slow_device_probes(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "slow-device"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("main")
        clock = [0.0]

        def foreground():
            clock[0] += 4.0
            return {"package": manager.package_name, "activity": ".MainActivity"}

        def advance(seconds):
            clock[0] += seconds

        with patch("core.state_manager.time.monotonic", side_effect=lambda: clock[0]), patch(
            "core.state_manager.time.sleep", side_effect=advance
        ), patch.object(manager.d, "app_current", side_effect=foreground), patch.object(
            manager, "_dismiss_transient_system_dialogs"
        ):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "confirm"}, timeout=30,
            )
        self.assertTrue(completed)
        self.assertLess(clock[0], 30)
        self.assertFalse(manager.d.audit_clicked)
        self.assertFalse(manager.d.confirm_clicked)

    def test_recovery_fallback_does_not_reuse_an_old_absence_probe(self):
        class Device:
            def __init__(self):
                self.probes = 0

            @staticmethod
            def app_current():
                return {
                    "package": "com.example.app",
                    "activity": ".SplashActivity",
                }

            @staticmethod
            def dump_hierarchy(compressed=False):
                return (
                    '<hierarchy><node package="com.example.app" '
                    'class="android.widget.FrameLayout" /></hierarchy>'
                )

            def xpath(self, _locator):
                return self

            def wait(self, timeout=0):
                self.probes += 1
                if self.probes == 1:
                    return False
                raise RuntimeError("uiautomator xpath unavailable")

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 2

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.2],
        ), patch("core.state_manager.time.sleep"):
            completed = manager.recover_audit_gate(
                {"xpath": "//android.widget.RadioButton[@text='Gate']"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)

    def test_recovery_fallback_ignores_absence_observed_on_launcher(self):
        class Device:
            def __init__(self):
                self.apps = iter(
                    [
                        {"package": "com.android.launcher", "activity": ".Launcher"},
                        {"package": "com.android.launcher", "activity": ".Launcher"},
                        {
                            "package": "com.example.app",
                            "activity": ".SplashActivity",
                        },
                        {
                            "package": "com.example.app",
                            "activity": ".SplashActivity",
                        },
                    ]
                )
                self.probes = 0

            def app_current(self):
                return next(self.apps)

            @staticmethod
            def dump_hierarchy(compressed=False):
                return (
                    '<hierarchy><node package="com.example.app" '
                    'class="android.widget.FrameLayout" /></hierarchy>'
                )

            def xpath(self, _locator):
                return self

            def wait(self, timeout=0):
                self.probes += 1
                if self.probes == 1:
                    return False
                raise RuntimeError("uiautomator xpath unavailable")

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 1

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.03, 0.04, 0.05, 0.2],
        ), patch("core.state_manager.time.sleep"):
            completed = manager.recover_audit_gate(
                {"xpath": "//android.widget.RadioButton[@text='Gate']"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)

    def test_audit_activity_mismatch_does_not_override_a_visible_gate(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice(
            "audit", audit_checked=False, confirmations_before_advance=1000
        )

        with patch("core.state_manager.time.sleep"):
            completed = manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "confirm"},
                timeout=0.1,
                audit_activity="com.example.app/.ConfiguredAuditActivity",
            )

        self.assertFalse(completed)
        self.assertFalse(manager._audit_gate_known_complete)

    def test_observed_gate_may_complete_after_live_probe_confirms_absence(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _GateWithMissingHierarchyDevice("absent")

        with patch("core.state_manager.time.sleep"):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditToggle"},
                {"text": "confirm"},
                timeout=0.2,
            )

        self.assertTrue(completed)
        self.assertTrue(manager._audit_gate_known_complete)

    def test_xml_absence_does_not_override_failed_probe_after_gate_observation(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _GateWithMissingHierarchyDevice("error")

        with self.assertLogs("core.state_manager", level="WARNING") as captured, patch(
            "core.state_manager.time.sleep"
        ):
            completed = manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/auditToggle"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)
        self.assertFalse(manager._audit_gate_known_complete)
        self.assertTrue(any("did not clear" in line for line in captured.output))

    def test_initial_audit_gate_does_not_accept_indeterminate_hierarchy(self):
        class Device:
            @staticmethod
            def app_current():
                return {
                    "package": "com.example.app",
                    "activity": ".ConsentActivity",
                }

            @staticmethod
            def dump_hierarchy(compressed=False):
                return '<hierarchy><node package="com.example.app">'

            @staticmethod
            def __call__(**_locator):
                return _MissingSelector()

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.02, 0.2],
        ), patch("core.state_manager.time.sleep"):
            completed = manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/customGateToggle"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)

    def test_configured_custom_gate_blocks_recovery_fallback(self):
        class ProbeFailure:
            @staticmethod
            def wait(timeout=0):
                raise RuntimeError("uiautomator hierarchy unavailable")

        class Device:
            @staticmethod
            def app_current():
                return {
                    "package": "com.example.app",
                    "activity": ".ConsentActivity",
                }

            @staticmethod
            def dump_hierarchy(compressed=False):
                return (
                    '<hierarchy><node package="com.example.app" '
                    'resource-id="com.example.app:id/customGateToggle" />'
                    "</hierarchy>"
                )

            @staticmethod
            def __call__(**_locator):
                return ProbeFailure()

        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = Device()
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 1

        with patch(
            "core.state_manager.time.monotonic",
            side_effect=[0.0, 0.0, 0.01, 0.02, 0.2],
        ), patch("core.state_manager.time.sleep"):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/customGateToggle"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)
        self.assertFalse(manager._audit_gate_known_complete)

    def test_initial_audit_gate_does_not_use_recovery_foreground_fallback(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _TargetForegroundWithoutHierarchyDevice()
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 1

        with self.assertLogs("core.state_manager", level="WARNING") as captured:
            completed = manager._complete_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)
        self.assertTrue(
            any("remained inconclusive" in line for line in captured.output)
        )

    def test_recovery_foreground_fallback_requires_known_completed_gate(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _TargetForegroundWithoutHierarchyDevice()
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 1

        with self.assertLogs("core.state_manager", level="WARNING") as captured:
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)
        self.assertTrue(
            any("remained inconclusive" in line for line in captured.output)
        )

    def test_audit_gate_does_not_use_foreground_fallback_after_gate_was_seen(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice(
            "audit", audit_checked=False, confirmations_before_advance=1000
        )
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 1

        with self.assertLogs("core.state_manager", level="WARNING") as captured:
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=0.1,
            )

        self.assertFalse(completed)
        self.assertFalse(manager._audit_gate_known_complete)
        self.assertEqual(1, manager.d.audit_click_count)
        self.assertTrue(
            any("Audit gate did not clear" in line for line in captured.output)
        )

    def test_audit_gate_delayed_visibility_prevents_foreground_fallback(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _DelayedAuditGateDevice(visible_on_probe=4)
        manager._audit_gate_known_complete = True
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 10

        with self.assertLogs("core.state_manager", level="WARNING") as captured, patch(
            "core.state_manager.time.sleep"
        ):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "confirm"},
                timeout=0.1,
            )

        self.assertFalse(completed)
        self.assertGreaterEqual(manager.d.audit_probe_count, 4)
        self.assertEqual(1, manager.d.audit_click_count)
        self.assertFalse(manager._audit_gate_known_complete)
        self.assertTrue(
            any("Audit gate did not clear" in line for line in captured.output)
        )

    def test_recovery_skips_audit_gate_after_startup_gates_passed(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("main")
        manager._audit_gate_known_complete = False
        manager._startup_gates_passed = True

        with patch.object(manager, "_dump_hierarchy_text") as dump:
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=1,
            )

        self.assertTrue(completed)
        dump.assert_not_called()
        self.assertTrue(manager._audit_gate_known_complete)

    def test_audit_gate_uses_stable_foreground_when_locator_is_absent(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("main")
        manager._audit_gate_known_complete = False
        manager.AUDIT_GATE_ABSENT_STABLE_SECONDS = 0.0
        manager.AUDIT_GATE_ABSENT_STABLE_PROBES = 6

        with self.assertLogs("core.state_manager", level="WARNING") as captured, patch(
            "core.state_manager.time.sleep"
        ):
            completed = manager.recover_audit_gate(
                {"resourceId": "com.example.app:id/auditRadioButton"},
                {"text": "确定"},
                timeout=1,
            )

        self.assertTrue(completed)
        self.assertTrue(
            any("was not observed" in line for line in captured.output)
        )

    def test_navigation_skips_audit_gate_after_manual_completion(self):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "device-1"
        manager.package_name = "com.example.app"
        manager.d = _AuditGateDevice("main")

        self.assertTrue(
            manager.navigate_to(
                "MainPage",
                [
                    {
                        "action": "complete_audit_gate",
                        "audit_locator": {
                            "resourceId": "com.example.app:id/auditRadioButton"
                        },
                        "confirm_locator": {"text": "确定"},
                        "audit_activity": "com.example.app/.SplashActivity",
                        "timeout": 1,
                        "after": 0,
                    }
                ],
            )
        )
        self.assertFalse(manager.d.audit_clicked)
        self.assertFalse(manager.d.confirm_clicked)

    def _input_safe_manager(self, packages):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "offline-device"
        manager.package_name = "com.example.app"
        manager._input_safety = {"enabled": True}
        manager.input_recovery_required = False
        manager.last_navigation_error = None
        iterator = iter(packages)
        manager._foreground_app = lambda: {
            "package": next(iterator, packages[-1])
        }
        return manager

    def test_vendor_permission_popup_is_not_a_foreground_loss(self):
        manager = self._input_safe_manager(
            ["com.lbe.security.miui", "com.example.app"]
        )

        with patch("core.state_manager.time.sleep", return_value=None):
            manager.ensure_input_safe()

        self.assertFalse(manager.input_recovery_required)

    def test_vendor_permission_popup_still_requires_recovery_when_it_stays(self):
        manager = self._input_safe_manager(["com.lbe.security.miui"])
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 0.3
            return clock[0]

        with patch("core.state_manager.time.sleep", return_value=None), patch(
            "core.state_manager.time.monotonic", side_effect=fake_monotonic
        ):
            from core.input_safety import InputRecoveryRequired

            with self.assertRaises(InputRecoveryRequired):
                manager.ensure_input_safe()

        self.assertTrue(manager.input_recovery_required)


class VendorInstallPromptTests(unittest.TestCase):
    """The install prompt must be confirmed, never rejected."""

    def _manager(self, package, hierarchy=""):
        manager = StateManager.__new__(StateManager)
        manager.device_serial = "vendor-device"
        manager.package_name = "com.example.app"
        manager._input_safety = {}
        device = Mock()
        device.app_current.return_value = {"package": package, "activity": "installer"}
        manager.d = device
        manager._dump_hierarchy_text = Mock(return_value=hierarchy)
        return manager

    def test_miui_sheet_clicks_continue_before_the_reject_countdown(self):
        manager = self._manager("com.miui.packageinstaller")
        clicked = []
        manager._click_if_exists = lambda locator, timeout=0.5: (
            clicked.append(locator.get("text")) or locator.get("text") == "继续安装"
        )
        self.assertTrue(manager._dismiss_vendor_install_flow())
        self.assertEqual(clicked[0], "继续安装")

    def test_never_clicks_reject_when_no_confirm_button_exists(self):
        manager = self._manager("com.miui.packageinstaller")
        clicked = []
        manager._click_if_exists = lambda locator, timeout=0.5: (
            clicked.append(locator.get("text")) or False
        )
        self.assertFalse(manager._dismiss_vendor_install_flow())
        self.assertNotIn("拒绝", clicked)

    def test_vendor_page_outside_installer_package_needs_both_sides(self):
        manager = self._manager("com.android.launcher", hierarchy='text="继续安装" text="拒绝"')
        manager._click_if_exists = Mock(return_value=True)
        self.assertTrue(manager._dismiss_vendor_install_flow())

        manager = self._manager("com.android.launcher", hierarchy='text="安装"')
        manager._click_if_exists = Mock(return_value=True)
        self.assertFalse(manager._dismiss_vendor_install_flow())

    def test_strict_input_safety_never_taps_vendor_ui(self):
        manager = self._manager("com.miui.packageinstaller")
        manager._input_safety = {"enabled": True}
        manager._click_if_exists = Mock(return_value=True)
        self.assertFalse(manager._dismiss_vendor_install_flow())
        manager._click_if_exists.assert_not_called()


if __name__ == "__main__":
    unittest.main()
