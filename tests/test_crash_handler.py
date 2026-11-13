import subprocess
import unittest
from unittest.mock import Mock, patch

from core.crash_handler import CrashHandler
from core.input_safety import InputRecoveryRequired


class CrashHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = CrashHandler("offline-device", "com.example.app")

    def test_strict_replay_sets_text_on_app_control_without_keyboard(self):
        self.handler.options["input_safety"] = {"enabled": True}
        device = Mock()
        device.window_size.return_value = (1080, 2400)
        device.app_current.return_value = {"package": "com.example.app"}
        device.return_value.info = {"packageName": "com.example.app"}
        with patch.object(self.handler, "_connect", return_value=device), patch(
            "core.crash_handler.hide_input_method", return_value=True
        ), patch.object(self.handler, "_adb_command") as adb:
            self.handler._execute_event_adb({
                "action": "input", "text": "\u4e2d\u6587", "clear": True, "delay_ms": 0,
            }, 1080, 2400)
        device.assert_called_once_with(focused=True, packageName="com.example.app")
        device.return_value.set_text.assert_called_once_with("\u4e2d\u6587")
        device.click.assert_not_called()
        device.send_keys.assert_not_called()
        adb.assert_not_called()

    def test_strict_replay_refuses_keyboard_and_external_overlay(self):
        self.handler.options["input_safety"] = {"enabled": True}
        device = Mock()
        device.window_size.return_value = (1080, 2400)
        device.app_current.return_value = {"package": "com.example.app"}
        device.dump_hierarchy.return_value = (
            '<hierarchy><node package="com.example.app" bounds="[0,50][1080,2300]"/>'
            '<node package="com.external" bounds="[200,200][500,500]"/></hierarchy>'
        )
        with patch("core.crash_handler.hide_input_method", return_value=False) as hide:
            with self.assertRaises(InputRecoveryRequired):
                self.handler._execute_event(device, {"action": "click", "x": 800, "y": 800})
            hide.return_value = True
            with self.assertRaises(InputRecoveryRequired):
                self.handler._execute_event(device, {"action": "click", "x": 300, "y": 300})
            with self.assertRaises(InputRecoveryRequired):
                self.handler._execute_event(device, {"action": "home"})
        device.click.assert_not_called()
        device.press.assert_not_called()

    def test_target_java_crash_is_detected_and_signed(self):
        content = """08-04 12:00:00.000 E/AndroidRuntime: FATAL EXCEPTION: main
08-04 12:00:00.001 E/AndroidRuntime: Process: com.example.app, PID: 12
08-04 12:00:00.002 E/AndroidRuntime: java.lang.NullPointerException: boom
08-04 12:00:00.003 E/AndroidRuntime:     at com.example.app.MainActivity.onCreate(MainActivity.java:42)
08-04 12:00:00.004 E/AndroidRuntime:     at android.app.Activity.performCreate(Activity.java:8000)
"""
        crash = self.handler.detect_crash(content)
        self.assertIsNotNone(crash)
        self.assertEqual(
            crash["exception_type"], "java.lang.NullPointerException"
        )
        self.assertEqual(crash["class"], "com.example.app.MainActivity")
        self.assertTrue(crash["signature"])

    def test_other_package_crash_is_ignored(self):
        content = """E/AndroidRuntime: FATAL EXCEPTION: main
E/AndroidRuntime: Process: com.other.app, PID: 13
E/AndroidRuntime: java.lang.IllegalStateException: unrelated
E/AndroidRuntime:     at com.other.app.Main.run(Main.java:1)
"""
        self.assertIsNone(self.handler.detect_crash(content))

    def test_system_tool_crash_is_not_claimed_by_later_target_log(self):
        content = """E/AndroidRuntime: FATAL EXCEPTION: main
E/AndroidRuntime: PID: 31953
E/AndroidRuntime: java.lang.IllegalStateException: UiAutomationService already registered
E/AndroidRuntime:     at com.android.commands.uiautomator.DumpCommand.run(DumpCommand.java:78)
I/ActivityTaskManager: topResumedActivity=com.example.app/.MainActivity
"""
        self.assertIsNone(self.handler.detect_crash(content))

    def test_native_target_crash_ignores_key_process_metadata(self):
        content = """08-10 17:38:01.171 F/DEBUG: Fatal signal 5 (SIGTRAP), code 1 (TRAP_BRKPT), fault addr 0x0
08-10 17:38:01.172 F/DEBUG: keyProcess: 0
08-10 17:38:01.173 F/DEBUG: pid: 28834, tid: 28899, name: DedicatedWorker  >>> com.example.app <<<
"""
        crash = self.handler.detect_crash(content)
        self.assertIsNotNone(crash)
        self.assertEqual(crash["crash_type"], "native")
        self.assertEqual(crash["exception_type"], "NativeSignal5")

    def test_native_other_package_with_key_process_is_ignored(self):
        content = """F/DEBUG: Fatal signal 5 (SIGTRAP), code 1 (TRAP_BRKPT), fault addr 0x0
F/DEBUG: keyProcess: 0
F/DEBUG: pid: 31, tid: 32, name: DedicatedWorker  >>> com.other.app <<<
"""
        self.assertIsNone(self.handler.detect_crash(content))

    def test_signature_is_stable(self):
        info = {
            "crash_type": "java",
            "exception_type": "IllegalStateException",
            "class": "com.example.app.Main",
            "method": "run",
        }
        self.assertEqual(
            self.handler.generate_signature(info), self.handler.generate_signature(info)
        )

    def test_vendor_digest_line_does_not_replace_java_exception_type(self):
        content = """08-12 10:26:51.147 E/AndroidRuntime( 8520): FATAL EXCEPTION: main
08-12 10:26:51.147 E/AndroidRuntime( 8520): Process: com.example.app, PID: 8520
08-12 10:26:51.147 E/AndroidRuntime( 8520): java.lang.NoSuchFieldError: No field API_SCHEMA
08-12 10:26:51.147 E/AndroidRuntime( 8520):     at com.example.app.Main.run(Unknown Source:5)
08-12 10:26:51.193 D/DigestGenerator( 2969): digest=============com.example.appjava.lang.NoSuchFieldError: No field API_SCHEMA
"""
        crash = self.handler.detect_crash(content)
        self.assertIsNotNone(crash)
        self.assertEqual(crash["exception_type"], "java.lang.NoSuchFieldError")

    def test_ahzy_crash_page_is_recognized_and_text_is_extracted(self):
        component = (
            "com.example.app/"
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
        )
        hierarchy = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="崩溃信息" content-desc="" />
  <node text="java.lang.NoSuchFieldError: No field API_SCHEMA&#10;    at com.example.app.Main.run(Main.java:4)" content-desc="" />
</hierarchy>"""
        page_text = self.handler.extract_app_crash_page_text(hierarchy)
        crash = self.handler.detect_app_crash_page(component, page_text)

        self.assertIn("崩溃信息", page_text)
        self.assertIsNotNone(crash)
        self.assertEqual(crash["exception_type"], "java.lang.NoSuchFieldError")
        self.assertEqual(crash["detection_source"], "app_crash_page")
        self.assertEqual(crash["class"], "com.example.app.Main")

    def test_same_crash_activity_from_other_package_is_ignored(self):
        component = (
            "com.other.app/"
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity"
        )
        self.assertIsNone(
            self.handler.detect_app_crash_page(component, "java.lang.Error: boom")
        )

    def test_configured_crash_activity_is_found_in_pressure_log(self):
        handler = CrashHandler(
            "offline",
            "com.example.app",
            {"crash_page_activities": ["com.example.app.CustomCrashActivity"]},
        )
        component = handler.find_app_crash_page_component(
            "top=ComponentInfo{com.example.app/com.example.app.CustomCrashActivity}"
        )
        self.assertEqual(
            component,
            "com.example.app/com.example.app.CustomCrashActivity",
        )

    def test_activity_inventory_does_not_count_as_crash_page(self):
        content = (
            "Explored app activities:\n"
            "  345 com.ahzy.common.module.mine.crash.AhzyCrashActivity\n"
        )
        self.assertIsNone(self.handler.find_app_crash_page_component(content))

    def test_explicit_foreground_shared_crash_activity_is_detected(self):
        content = (
            "// the top activity is "
            "com.ahzy.common.module.mine.crash.AhzyCrashActivity\n"
        )
        self.assertEqual(
            self.handler.find_app_crash_page_component(content),
            "com.example.app/com.ahzy.common.module.mine.crash.AhzyCrashActivity",
        )

    def test_force_stop_log_is_not_classified_as_unexpected_process_exit(self):
        content = (
            "08-17 10:00:00.000 I/ActivityManager: Force stopping "
            "com.example.app appid=10123 user=0\n"
        )
        self.assertIsNone(self.handler.detect_process_exit(content))

    def test_vendor_force_stop_kill_and_matching_proc_died_are_ignored(self):
        content = """08-17 18:12:03.054 I/am_kill ( 1721): [0,21838,com.example.app,0,stop com.example.appfrom pid 22457by app]
08-17 18:12:03.090 I/am_proc_died( 1721): [0,21838,com.example.app,0,2]
"""
        self.assertIsNone(self.handler.detect_process_exit(content))

    def test_real_current_kill_is_still_reported(self):
        content = (
            "08-17 18:15:00.000 I/am_kill ( 1721): "
            "[0,31001,com.example.app,900,excessive cpu during pressure]\n"
        )
        crash = self.handler.detect_process_exit(content)
        self.assertIsNotNone(crash)
        self.assertEqual(crash["process_event"], "am_kill")
        self.assertEqual(crash["pid"], 31001)

    def test_long_click_replay_event_uses_duration(self):
        class Device:
            def window_size(self):
                return (1080, 2400)

            def long_click(self, x, y, duration=None):
                self.call = (x, y, duration)

        device = Device()
        self.handler._execute_event(
            device,
            {"action": "long_click", "x": 12, "y": 34, "duration": 1.25, "delay_ms": 0},
        )
        self.assertEqual((12, 34, 1.25), device.call)

    def test_adb_replay_event_uses_bounded_device_input(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler._adb_command = Mock(
            return_value=subprocess.CompletedProcess([], 0, "", "")
        )

        handler._execute_event_adb(
            {"action": "click", "x": 12, "y": 34, "delay_ms": 0},
            1080,
            2400,
        )

        handler._adb_command.assert_called_once_with(
            ["shell", "input", "tap", "12", "34"]
        )

    def test_replay_trajectory_does_not_require_uiautomator2(self):
        handler = CrashHandler("device-1", "com.example.app")

        def adb(arguments, timeout=None):
            if arguments == ["get-state"]:
                return subprocess.CompletedProcess(arguments, 0, "device\n", "")
            if arguments[:3] == ["shell", "dumpsys", "activity"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    "mResumedActivity: ActivityRecord{u0 com.example.app/.MainActivity}\n",
                    "",
                )
            if arguments == ["shell", "wm", "size"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "Physical size: 1080x2400\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        handler._adb_command = Mock(side_effect=adb)
        handler._logcat = Mock(side_effect=["", ""])
        handler.detect_crash = Mock(return_value=None)

        result = handler.replay_trajectory(
            {"events": [{"action": "click", "x": 12, "y": 34, "delay_ms": 0}]},
            "com.example.app",
        )

        self.assertTrue(result)
        self.assertEqual(1, handler.last_replay_result["event_count"])
        self.assertEqual("adb", handler.last_replay_result["transport"])

    def test_replay_skips_unsupported_text_and_continues_events(self):
        handler = CrashHandler("device-1", "com.example.app")

        def adb(arguments, timeout=None):
            if arguments == ["get-state"]:
                return subprocess.CompletedProcess(arguments, 0, "device\n", "")
            if arguments[:3] == ["shell", "dumpsys", "activity"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    "mResumedActivity: ActivityRecord{u0 com.example.app/.MainActivity}\n",
                    "",
                )
            if arguments == ["shell", "wm", "size"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "Physical size: 1080x2400\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        handler._adb_command = Mock(side_effect=adb)
        handler._logcat = Mock(side_effect=["", ""])
        handler.detect_crash = Mock(return_value=None)
        result = handler.replay_trajectory(
            {
                "events": [
                    {"action": "input", "event_id": "text-1", "text": "中文", "delay_ms": 0},
                    {"action": "click", "event_id": "click-2", "x": 12, "y": 34, "delay_ms": 0},
                ]
            },
            "com.example.app",
        )

        self.assertTrue(result)
        self.assertEqual(1, handler.last_replay_result["event_count"])
        self.assertEqual(1, handler.last_replay_result["skipped_event_count"])
        self.assertEqual("text-1", handler.last_replay_result["skipped_events"][0]["event_id"])

    def test_replay_recovers_when_foreground_input_is_rejected(self):
        handler = CrashHandler("device-1", "com.example.app")
        input_attempts = []

        def adb(arguments, timeout=None):
            if arguments == ["get-state"]:
                return subprocess.CompletedProcess(arguments, 0, "device\n", "")
            if arguments == ["shell", "wm", "size"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "Physical size: 1080x2400\n", ""
                )
            if arguments[:3] == ["shell", "input", "tap"]:
                input_attempts.append(arguments)
                if len(input_attempts) == 1:
                    return subprocess.CompletedProcess(
                        arguments,
                        1,
                        "",
                        "java.lang.SecurityException: Injecting to another application requires INJECT_EVENTS permission",
                    )
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        handler._adb_command = Mock(side_effect=adb)
        handler._ensure_target_foreground = Mock()
        handler._logcat = Mock(side_effect=["", ""])
        handler.detect_crash = Mock(return_value=None)
        progress = []

        result = handler.replay_trajectory(
            {"events": [{"action": "click", "x": 12, "y": 34, "delay_ms": 0}]},
            "com.example.app",
            progress_callback=progress.append,
        )

        self.assertTrue(result)
        self.assertEqual(1, handler.last_replay_result["event_count"])
        self.assertEqual(2, len(input_attempts))
        self.assertEqual(2, handler._ensure_target_foreground.call_count)
        handler._ensure_target_foreground.assert_any_call("com.example.app")
        self.assertIn("recovering", [item["status"] for item in progress])

    def test_replay_recovers_when_external_page_accepts_input(self):
        handler = CrashHandler("device-1", "com.example.app")

        def adb(arguments, timeout=None):
            if arguments == ["get-state"]:
                return subprocess.CompletedProcess(arguments, 0, "device\n", "")
            if arguments == ["shell", "wm", "size"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "Physical size: 1080x2400\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        handler._adb_command = Mock(side_effect=adb)
        handler._ensure_target_foreground = Mock()
        handler._foreground_component_from_adb = Mock(
            return_value="com.android.settings/com.android.settings.Settings"
        )
        handler._logcat = Mock(side_effect=["", ""])
        handler.detect_crash = Mock(return_value=None)
        progress = []

        result = handler.replay_trajectory(
            {"events": [{"action": "click", "x": 12, "y": 34, "delay_ms": 0}]},
            "com.example.app",
            progress_callback=progress.append,
        )

        self.assertTrue(result)
        self.assertEqual(1, handler.last_replay_result["event_count"])
        self.assertEqual(
            1, handler.last_replay_result["foreground_recovery_count"]
        )
        self.assertEqual(2, handler._ensure_target_foreground.call_count)
        recovering = [item for item in progress if item["status"] == "recovering"]
        self.assertEqual(1, len(recovering))
        self.assertEqual(
            "com.android.settings/com.android.settings.Settings",
            recovering[0]["foregroundComponent"],
        )

    def test_replay_allows_runtime_permission_dialog(self):
        handler = CrashHandler("device-1", "com.example.app")

        def adb(arguments, timeout=None):
            if arguments == ["get-state"]:
                return subprocess.CompletedProcess(arguments, 0, "device\n", "")
            if arguments == ["shell", "wm", "size"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "Physical size: 1080x2400\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        handler._adb_command = Mock(side_effect=adb)
        handler._ensure_target_foreground = Mock()
        handler._foreground_component_from_adb = Mock(
            return_value=(
                "com.android.permissioncontroller/"
                "com.android.permissioncontroller.permission.ui.GrantPermissionsActivity"
            )
        )
        handler._logcat = Mock(side_effect=["", ""])
        handler.detect_crash = Mock(return_value=None)

        result = handler.replay_trajectory(
            {"events": [{"action": "click", "x": 12, "y": 34, "delay_ms": 0}]},
            "com.example.app",
        )

        self.assertTrue(result)
        self.assertEqual(1, handler._ensure_target_foreground.call_count)
        self.assertEqual(
            0, handler.last_replay_result["foreground_recovery_count"]
        )

    def test_foreground_restore_uses_soft_launch_before_force_stop(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler._foreground_component_from_adb = Mock(
            side_effect=[
                "com.android.settings/com.android.settings.Settings",
                "com.example.app/com.example.app.DetailActivity",
            ]
        )
        handler._adb_command = Mock(
            return_value=subprocess.CompletedProcess([], 0, "Events injected: 1\n", "")
        )

        handler._ensure_target_foreground("com.example.app")

        commands = [call.args[0] for call in handler._adb_command.call_args_list]
        self.assertEqual("monkey", commands[0][1])
        self.assertFalse(
            any(command[:3] == ["shell", "am", "force-stop"] for command in commands)
        )

    def test_permission_settings_page_is_not_treated_as_runtime_prompt(self):
        component = (
            "com.android.permissioncontroller/"
            "com.android.permissioncontroller.permission.ui.ManagePermissionsActivity"
        )
        self.assertFalse(CrashHandler._is_permission_dialog_component(component))

    def test_miui_privacy_page_is_not_treated_as_runtime_prompt(self):
        component = (
            "com.miui.securitycenter/"
            "com.miui.permcenter.permissions.SystemAppPermissionDialogActivity"
        )
        self.assertTrue(CrashHandler._is_privacy_dialog_component(component))
        self.assertFalse(CrashHandler._is_permission_dialog_component(component))

    def test_auto_replay_start_reuses_existing_anchor_without_force_stop(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler._foreground_component_from_adb = Mock(
            return_value="com.example.app/com.example.app.EditorActivity"
        )
        handler._adb_command = Mock()
        result = handler.prepare_replay_start(
            {
                "replay_plan": {
                    "page_anchor": {
                        "component": "com.example.app/com.example.app.EditorActivity"
                    },
                    "require_page_anchor": True,
                }
            },
            "com.example.app",
            "auto",
        )
        self.assertTrue(result["success"])
        self.assertEqual("existing_task_anchor", result["strategy"])
        self.assertFalse(
            any(
                call.args[0][:3] == ["shell", "am", "force-stop"]
                for call in handler._adb_command.call_args_list
            )
        )

    def test_current_replay_start_requires_matching_activity(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler._foreground_component_from_adb = Mock(
            return_value="com.example.app/com.example.app.HomeActivity"
        )
        result = handler.prepare_replay_start(
            {
                "replay_plan": {
                    "page_anchor": {
                        "component": "com.example.app/com.example.app.EditorActivity"
                    }
                }
            },
            "com.example.app",
            "current",
        )
        self.assertFalse(result["success"])
        self.assertIn("does not match", result["error"])

    def test_auto_replay_start_without_anchor_explains_manual_fallback(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler._foreground_component_from_adb = Mock(
            return_value="com.example.app/com.example.app.HomeActivity"
        )
        result = handler.prepare_replay_start(
            {"replay_plan": {"require_page_anchor": True}},
            "com.example.app",
            "auto",
        )
        self.assertFalse(result["success"])
        self.assertIn("current-page replay", result["error"])


    def test_replay_accepts_owned_edge_touch_rejected_by_old_margin(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler.options["input_safety"] = {"enabled": True}
        device = Mock()
        device.window_size.return_value = (1080, 2340)
        device.app_current.return_value = {"package": "com.example.app"}
        device.dump_hierarchy.return_value = (
            "<hierarchy><node package='com.example.app' "
            "bounds='[0,0][1080,2340]' class='android.widget.FrameLayout'/></hierarchy>"
        )
        with patch("core.crash_handler.hide_input_method", return_value=True):
            # y=113 is 4.8% of 2340 and used to fail the fixed 5% margin even
            # though a target-owned node covers the point.
            handler._verify_replay_input(device, [(924, 113)])

    def test_replay_rejects_touch_covered_by_foreign_window(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler.options["input_safety"] = {"enabled": True}
        device = Mock()
        device.window_size.return_value = (1080, 2340)
        device.app_current.return_value = {"package": "com.example.app"}
        device.dump_hierarchy.return_value = (
            "<hierarchy>"
            "<node package='com.example.app' bounds='[0,0][1080,2340]' class='android.widget.FrameLayout'/>"
            "<node package='com.android.systemui' bounds='[0,0][1080,120]' class='android.widget.FrameLayout'/>"
            "</hierarchy>"
        )
        with patch("core.crash_handler.hide_input_method", return_value=True):
            with self.assertRaises(InputRecoveryRequired):
                handler._verify_replay_input(device, [(924, 113)])


    def test_auto_replay_start_relaunches_existing_task_when_anchor_is_gone(self):
        handler = CrashHandler("device-1", "com.example.app")
        handler._foreground_component_from_adb = Mock(
            return_value="com.example.app/com.example.app.HomeActivity"
        )
        handler._adb_command = Mock()
        handler._reorder_replay_anchor = Mock(return_value=False)
        handler._launch_target_task = Mock(return_value=True)
        handler._wait_for_exact_component = Mock(return_value=False)
        result = handler.prepare_replay_start(
            {
                "replay_plan": {
                    "page_anchor": {
                        "component": "com.example.app/com.qq.e.ads.ADActivity"
                    },
                    "require_page_anchor": True,
                }
            },
            "com.example.app",
            "auto",
        )
        self.assertTrue(result["success"])
        self.assertEqual("relaunch_existing_task", result["strategy"])
        self.assertFalse(result["activity_matched"])
        handler._launch_target_task.assert_called_once_with("com.example.app")
        self.assertFalse(
            any(
                call.args[0][:3] == ["shell", "am", "force-stop"]
                for call in handler._adb_command.call_args_list
            )
        )


    def test_reproduction_match_grades_exact_likely_and_none(self):
        handler = CrashHandler("device-1", "com.example.app")
        expected = (
            "java.lang.IndexOutOfBoundsException@"
            "com.example.app.ui.Foo.bar#aaaaaaaaaaaa"
        )
        likely = (
            "java.lang.IndexOutOfBoundsException@"
            "com.example.app.ui.Foo.bar#bbbbbbbbbbbb"
        )
        different = (
            "java.lang.NullPointerException@"
            "com.example.app.ui.Baz.qux#cccccccccccc"
        )
        self.assertEqual("exact", handler.classify_reproduction(expected, {"signature": expected}))
        self.assertEqual("likely", handler.classify_reproduction(expected, {"signature": likely}))
        self.assertEqual("none", handler.classify_reproduction(expected, {"signature": different}))
        self.assertEqual("none", handler.classify_reproduction("", {"signature": expected}))
        self.assertTrue(handler.verify_crash_reproducible(expected, {"signature": likely}))


    def test_replay_fidelity_key_normalizes_both_component_forms(self):
        handler = CrashHandler("device-1", "com.example.app")
        recorded = handler._component_activity_key(
            "com.example.app.module.question.QuestionActivity", "com.example.app"
        )
        dumpsys = handler._component_activity_key(
            "com.example.app/.module.question.QuestionActivity", "com.example.app"
        )
        self.assertEqual(
            "com.example.app/com.example.app.module.question.QuestionActivity",
            recorded,
        )
        self.assertEqual(recorded, dumpsys)
        self.assertEqual("", handler._component_activity_key("", "com.example.app"))

    def test_event_timestamp_reads_fastbot_clock(self):
        handler = CrashHandler("device-1", "com.example.app")
        first = handler._event_timestamp("2026-11-02 15:44:01.375")
        second = handler._event_timestamp("2026-11-02 15:44:07.514")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertAlmostEqual(6.139, second - first, places=3)
        self.assertIsNone(handler._event_timestamp(""))
        self.assertIsNone(handler._event_timestamp("not-a-time"))

    def test_parse_stacktrace_prefers_app_crash_over_sdk_background_error(self):
        handler = CrashHandler("device-1", "com.example.app")
        page_text = (
            "java.lang.IndexOutOfBoundsException: Index 0 out of bounds for length 0\n"
            "\tat jdk.internal.util.Preconditions.outOfBounds(Preconditions.java:64)\n"
            "\tat java.util.ArrayList.get(ArrayList.java:434)\n"
            "\tat com.example.app.module.question.QuestionVM.g(QuestionVM.kt:170)\n"
            "\tat com.example.app.module.question.QuestionFragment$e.invoke(QuestionFragment.kt:40)\n"
            "java.lang.UnsatisfiedLinkError: No implementation found for java.lang.String\n"
            "\tat cn.shuzilm.core.DUHelper.zZVTFJRA(Native Method)\n"
            "\tat cn.shuzilm.core.e.run(DUHelper.java:96)\n"
        )
        parsed = handler.parse_stacktrace(page_text)
        self.assertEqual(
            "java.lang.IndexOutOfBoundsException", parsed["exception_type"]
        )
        self.assertEqual("com.example.app.module.question.QuestionVM", parsed["class"])
        self.assertEqual("g", parsed["method"])
        self.assertEqual(1, len(parsed["supplementary_exceptions"]))
        self.assertEqual(
            "java.lang.UnsatisfiedLinkError",
            parsed["supplementary_exceptions"][0]["exception_type"],
        )

    def test_parse_stacktrace_keeps_single_exception_behavior(self):
        handler = CrashHandler("device-1", "com.example.app")
        parsed = handler.parse_stacktrace(
            "java.lang.NullPointerException: boom\n"
            "\tat com.example.app.ui.MainActivity.onCreate(MainActivity.kt:12)\n"
        )
        self.assertEqual("java.lang.NullPointerException", parsed["exception_type"])
        self.assertEqual("boom", parsed["message"])
        self.assertEqual([], parsed["supplementary_exceptions"])


if __name__ == "__main__":
    unittest.main()
