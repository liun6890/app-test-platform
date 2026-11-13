import subprocess
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

from core.executor import TestExecutor as Executor
from core.input_safety import hide_input_method, parse_input_method_state, touch_path_is_owned
from core.input_safety import InputRecoveryRequired
from core.state_manager import StateManager


class InputSafetyTests(unittest.TestCase):
    def test_headless_ime_uses_actual_view_visibility(self):
        state = parse_input_method_state(
            "mCurId=com.panda/.PandaService\n"
            "mInputShown=true\n"
            "mIsInputViewShown=false\n"
        )
        self.assertTrue(state["known"])
        self.assertFalse(state["visible"])
        self.assertEqual("com.panda", state["package"])

    def test_unknown_ime_state_is_not_declared_hidden(self):
        state = parse_input_method_state("mCurId=com.panda/.PandaService\n")
        self.assertFalse(state["known"])
        self.assertIsNone(state["visible"])

    def test_history_does_not_hide_later_current_ime_service_state(self):
        state = parse_input_method_state(
            "  mInputShown=true\n"
            "  InputMethodShowHideHistory:\n"
            "    #0 mIsInputViewShown=true\n"
            "Current Input Method Service:\n"
            "  mWindowVisible=true\n"
            "  mIsInputViewShown=false\n"
        )
        self.assertTrue(state["known"])
        self.assertFalse(state["visible"])

    def test_conflicting_current_view_flags_are_unknown(self):
        state = parse_input_method_state("mIsInputViewShown=false\nmIsInputViewShown=true")
        self.assertFalse(state["known"])
        self.assertIsNone(state["visible"])

    def test_dismissed_ime_overrides_retained_view_without_window_flags(self):
        state = parse_input_method_state(
            "mCurMethodId=\n"
            "mInputShown=false\n"
            "mIsInputViewShown=true\n"
        )
        self.assertTrue(state["known"])
        self.assertFalse(state["visible"])

    def test_conflicting_current_logical_flags_are_unknown(self):
        state = parse_input_method_state(
            "mInputShown=false\nmInputShown=true\nmIsInputViewShown=true\n"
        )
        self.assertFalse(state["known"])
        self.assertIsNone(state["visible"])

    def test_hidden_window_overrides_retained_input_view_flag(self):
        state = parse_input_method_state(
            "mCurId=com.sohu.inputmethod.sogou.xiaomi/.SogouIME\n"
            "mInputShown=false\n"
            "mDecorViewVisible=false mWindowVisible=false\n"
            "mIsInputViewShown=true\n"
        )
        self.assertTrue(state["known"])
        self.assertFalse(state["visible"])

    def test_visible_or_conflicting_window_flags_still_block_input(self):
        for window_state in (
            "mDecorViewVisible=true mWindowVisible=true",
            "mDecorViewVisible=true mWindowVisible=false",
            "mDecorViewVisible=false mWindowVisible=true",
            "mWindowVisible=false\nmWindowVisible=true",
        ):
            with self.subTest(window_state=window_state):
                state = parse_input_method_state(
                    "mInputShown=false\nmIsInputViewShown=true\n" + window_state
                )
                self.assertTrue(state["visible"])

    def test_hidden_window_does_not_trigger_another_dismissal(self):
        run = Mock(return_value=subprocess.CompletedProcess(
            [], 0,
            "mDecorViewVisible=false mWindowVisible=false\n"
            "mIsInputViewShown=true\n", "",
        ))
        self.assertTrue(hide_input_method(run, timeout=0.1))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["shell", "dumpsys", "input_method"])

    def test_touch_path_rejects_external_overlay_between_swipe_endpoints(self):
        nodes = list(ET.fromstring(
            '<hierarchy><node package="com.target" bounds="[0,50][1000,1900]"/>'
            '<node package="com.overlay" bounds="[200,200][400,400]"/></hierarchy>'
        ).iter("node"))
        self.assertTrue(touch_path_is_owned(nodes, "com.target", [(100, 300)]))
        self.assertTrue(touch_path_is_owned(nodes, "com.target", [(500, 300)]))
        self.assertFalse(touch_path_is_owned(nodes, "com.target", [(100, 300), (500, 300)]))
        self.assertFalse(touch_path_is_owned(nodes, "com.target", [(500, 20)]))
        self.assertFalse(touch_path_is_owned([], "com.target", [(500, 300)]))

    def test_state_manager_refuses_external_touch_and_unknown_hierarchy(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.target"
        manager._input_safety = {"enabled": True}
        manager.d = Mock()
        manager.d.window_size.return_value = (1000, 2000)
        with patch.object(manager, "ensure_input_safe"), patch.object(
            manager, "_dump_hierarchy_text", return_value=
            '<hierarchy><node package="com.target" bounds="[0,50][1000,1900]"/>'
            '<node package="com.overlay" bounds="[200,200][400,400]"/></hierarchy>',
        ) as hierarchy:
            manager._assert_safe_coordinates(500, 300)
            with self.assertRaises(InputRecoveryRequired):
                manager._assert_safe_coordinates(300, 300)
            hierarchy.return_value = ""
            with self.assertRaises(InputRecoveryRequired):
                manager._assert_safe_coordinates(500, 300)
        manager.d.click.assert_not_called()

    def test_unknown_input_method_never_sends_key_or_touch(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 1, "Unknown command", ""))
        can_dismiss = Mock(return_value=True)
        self.assertFalse(hide_input_method(run, timeout=0.1, can_dismiss=can_dismiss))
        commands = [item.args[0] for item in run.call_args_list]
        self.assertTrue(all(command == ["shell", "dumpsys", "input_method"] for command in commands))
        can_dismiss.assert_not_called()

    def test_visible_keyboard_cannot_be_dismissed_over_an_external_app(self):
        run = Mock(return_value=subprocess.CompletedProcess(
            [], 0, "mWindowVisible=true\nmIsInputViewShown=true\n", "",
        ))
        self.assertFalse(hide_input_method(run, timeout=0.1, can_dismiss=lambda: False))
        self.assertTrue(all(call.args[0][1] == "dumpsys" for call in run.call_args_list))

    def test_keyboard_disappearing_during_foreground_check_does_not_send_back(self):
        run = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, "mWindowVisible=true", ""),
            subprocess.CompletedProcess([], 0, "mWindowVisible=false", ""),
        ])
        self.assertTrue(hide_input_method(run, timeout=1, can_dismiss=lambda: True))
        self.assertEqual(2, run.call_count)
        self.assertTrue(all(call.args[0][1] == "dumpsys" for call in run.call_args_list))

    def test_cancel_during_foreground_check_does_not_send_back(self):
        stopped = False

        def can_dismiss():
            nonlocal stopped
            stopped = True
            return True

        run = Mock(return_value=subprocess.CompletedProcess([], 0, "mWindowVisible=true", ""))
        self.assertFalse(hide_input_method(
            run, timeout=1, should_stop=lambda: stopped, can_dismiss=can_dismiss,
        ))
        self.assertTrue(all(call.args[0][1] == "dumpsys" for call in run.call_args_list))

    def test_visible_keyboard_gets_only_one_back_and_requires_hidden_confirmation(self):
        run = Mock(return_value=subprocess.CompletedProcess(
            [], 0, "mWindowVisible=true\nmIsInputViewShown=true\n", "",
        ))
        self.assertFalse(hide_input_method(run, timeout=0.1, can_dismiss=lambda: True))
        keys = [call.args[0] for call in run.call_args_list if call.args[0][1] == "input"]
        self.assertEqual([["shell", "input", "keyevent", "BACK"]], keys)

    def test_visible_keyboard_without_foreground_verification_never_sends_back(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "mWindowVisible=true", ""))
        self.assertFalse(hide_input_method(run, timeout=0.1))
        self.assertTrue(all(call.args[0][1] == "dumpsys" for call in run.call_args_list))

    def test_xpath_input_resolves_native_control_without_xpath_set_text(self):
        manager = StateManager.__new__(StateManager)
        manager.package_name = "com.example.app"
        manager._input_safety = {"enabled": True}
        info = {
            "packageName": manager.package_name,
            "className": "android.widget.EditText",
            "resourceName": manager.package_name + ":id/input",
            "bounds": {"left": 50, "top": 100, "right": 400, "bottom": 180},
        }
        xpath = Mock()
        xpath.get.return_value.info = info
        native = Mock(info=info)

        class Candidates:
            count = 1
            def __getitem__(self, index):
                return native

        manager.d = Mock(return_value=Candidates())
        with patch.object(manager, "ensure_input_safe"), patch.object(
            manager, "_wait_for_selector", return_value=(xpath, True)
        ):
            manager._set_text({"xpath": "//android.widget.EditText"}, "\u4e2d\u6587")
        native.set_text.assert_called_once_with("\u4e2d\u6587")
        xpath.set_text.assert_not_called()
        xpath.click.assert_not_called()
        manager.d.send_keys.assert_not_called()

    def test_hide_requires_observed_hidden_state(self):
        calls = []
        visible = True

        def run(arguments, timeout):
            nonlocal visible
            calls.append(arguments)
            if arguments[-1] == "input_method":
                return subprocess.CompletedProcess(
                    arguments, 0,
                    "mInputShown=%s\nmIsInputViewShown=%s\n" % (
                        str(visible).lower(), str(visible).lower()
                    ),
                    "",
                )
            self.assertEqual(["shell", "input", "keyevent", "BACK"], arguments)
            visible = False
            return subprocess.CompletedProcess(arguments, 0, "", "")

        self.assertTrue(hide_input_method(run, timeout=1.0, can_dismiss=lambda: True))
        self.assertEqual(1, calls.count(["shell", "input", "keyevent", "BACK"]))


if __name__ == "__main__":
    unittest.main()
