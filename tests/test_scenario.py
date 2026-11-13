import unittest
from unittest.mock import Mock, patch

from scripts.test_scenario import (
    get_navigation_steps,
    prepare_test_scenario,
    resolve_input_steps,
)


class _ScenarioManager:
    device_serial = "device-1"

    def __init__(self):
        self.calls = []

    def navigate_to(self, target_page, steps):
        self.calls.append(("navigate", target_page, steps))
        return True

    def login(self, username, password, locators):
        self.calls.append(("login", username, password, locators))
        return True


class ScenarioTests(unittest.TestCase):
    def test_device_steps_are_run_before_shared_steps(self):
        config = {
            "scenario": {
                "navigation_steps": [{"action": "click", "text": "shared"}],
                "navigation_steps_by_device": {
                    "device-1": [{"action": "click", "text": "specific"}]
                },
            }
        }

        self.assertEqual(
            get_navigation_steps(config, "device-1"),
            [
                {"action": "click", "text": "specific"},
                {"action": "click", "text": "shared"},
            ],
        )

    def test_pre_login_steps_run_before_test_account_login(self):
        manager = _ScenarioManager()
        config = {
            "app": {"package_name": "com.example.app"},
            "scenario": {
                "login_enabled": True,
                "target_page": "MainPage",
                "pre_login_steps": [{"action": "optional_click", "text": "other"}],
                "login_locators": {
                    "username": {"resourceId": "com.example.app:id/phone"},
                    "password": {"resourceId": "com.example.app:id/code"},
                    "login_button": {"text": "Login"},
                    "allow_empty_password": True,
                },
                "navigation_steps": [{"action": "wait", "text": "Home"}],
            },
        }

        with patch.dict(
            "os.environ",
            {"APP_TEST_USERNAME": "12000000000", "APP_TEST_PASSWORD": ""},
            clear=False,
        ):
            result = prepare_test_scenario(manager, config)

        self.assertTrue(result["pre_login_success"])
        self.assertTrue(result["login_success"])
        self.assertEqual(manager.calls[0][0:2], ("navigate", "LoginPage"))
        self.assertEqual(manager.calls[1][0:3], ("login", "12000000000", ""))
        self.assertEqual(manager.calls[2][0:2], ("navigate", "MainPage"))

    def test_navigation_failure_detail_is_returned_for_reporting(self):
        manager = _ScenarioManager()
        manager.last_navigation_error = (
            "step 1 (click): RemoteDisconnected: connection closed"
        )
        manager.navigate_to = lambda _target, _steps: False
        config = {
            "app": {"package_name": "com.example.app"},
            "scenario": {"login_enabled": False, "navigation_steps": []},
        }

        result = prepare_test_scenario(manager, config)

        self.assertFalse(result["navigation_success"])
        self.assertIn("RemoteDisconnected", result["navigation_error"])

    def test_named_input_case_is_selected_sequentially_by_cycle(self):
        config = {
            "scenario": {
                "input_cases": {
                    "keyword": {"values": ["北京南站", "上海虹桥"], "mode": "sequential"}
                }
            }
        }
        steps = [{"action": "input", "data": "keyword"}]
        self.assertEqual("北京南站", resolve_input_steps(config, steps, 1)[0]["text"])
        self.assertEqual("上海虹桥", resolve_input_steps(config, steps, 2)[0]["text"])
        self.assertEqual("北京南站", resolve_input_steps(config, steps, 3)[0]["text"])
        self.assertNotIn("text", steps[0])

    def test_input_recovery_preserves_cycle_value_and_app_data(self):
        manager = _ScenarioManager()
        manager.input_recovery_required = False
        manager.reset_app = Mock(return_value=True)
        observations = []

        def navigate(_page, steps):
            observations.append(steps[0]["text"])
            manager.input_recovery_required = len(observations) == 1
            return not manager.input_recovery_required

        manager.navigate_to = navigate
        config = {
            "app": {"package_name": "com.example.app"},
            "input_safety": {"enabled": True, "recovery_attempts": 3},
            "scenario": {
                "login_enabled": False,
                "input_cases": {"search": ["first", "second"]},
                "navigation_steps": [{"action": "input", "data": "search"}],
            },
        }
        result = prepare_test_scenario(manager, config, cycle_number=2)
        self.assertEqual(["second", "second"], observations)
        manager.reset_app.assert_called_once_with(clear_data=False, clear_task=True)
        self.assertTrue(result["navigation_success"])
        self.assertTrue(result["continued_after_input_recovery"])
        self.assertNotIn("input_recovery_exhausted", result)

    def test_permanently_unknown_input_is_retried_only_to_the_limit(self):
        manager = _ScenarioManager()
        manager.reset_app = Mock(return_value=True)

        def navigate(_page, _steps):
            manager.input_recovery_required = True
            return False

        manager.navigate_to = navigate
        config = {
            "app": {"package_name": "com.example.app"},
            "input_safety": {"enabled": True, "recovery_attempts": 2},
            "scenario": {"login_enabled": False},
        }
        result = prepare_test_scenario(manager, config)
        self.assertEqual(2, manager.reset_app.call_count)
        self.assertTrue(result["input_recovery_exhausted"])
        self.assertFalse(result["navigation_success"])


if __name__ == "__main__":
    unittest.main()
