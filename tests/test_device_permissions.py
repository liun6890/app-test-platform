import unittest
from unittest.mock import Mock

from core.device_permissions import (
    apply_appops,
    apply_permission_setup,
    disable_package_verifier,
    grant_runtime_permissions,
)


def _result(returncode=0, stdout="", stderr=""):
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class GrantRuntimePermissionsTest(unittest.TestCase):
    def test_grants_every_requested_permission_once(self):
        calls = []

        def run_adb(arguments, timeout=10):
            calls.append(arguments)
            return _result()

        result = grant_runtime_permissions(
            run_adb,
            "com.example.app",
            [
                "android.permission.CAMERA",
                "android.permission.CAMERA",
                "android.permission.ACCESS_FINE_LOCATION",
            ],
        )

        self.assertEqual(
            ["android.permission.CAMERA", "android.permission.ACCESS_FINE_LOCATION"],
            result["requested"],
        )
        self.assertEqual(2, len(result["granted"]))
        self.assertEqual([], result["failed"])
        self.assertEqual(
            ["shell", "pm", "grant", "com.example.app", "android.permission.CAMERA"],
            calls[0],
        )

    def test_records_failed_grant_with_reason(self):
        def run_adb(arguments, timeout=10):
            return _result(
                returncode=255,
                stderr="Exception occurred: Permission MANAGE_USERS is not a "
                "changeable permission type",
            )

        result = grant_runtime_permissions(
            run_adb, "com.example.app", ["android.permission.MANAGE_USERS"]
        )

        self.assertEqual([], result["granted"])
        self.assertEqual(1, len(result["failed"]))
        self.assertIn("not a changeable permission type", result["failed"][0]["error"])


class ApplyAppopsTest(unittest.TestCase):
    def test_applies_valid_modes_and_rejects_unknown_ones(self):
        calls = []

        def run_adb(arguments, timeout=10):
            calls.append(arguments)
            return _result()

        result = apply_appops(
            run_adb,
            "com.example.app",
            {"SYSTEM_ALERT_WINDOW": "allow", "POST_NOTIFICATION": "sometimes"},
        )

        self.assertEqual(
            [{"op": "SYSTEM_ALERT_WINDOW", "mode": "allow"}], result["applied"]
        )
        self.assertEqual(1, len(result["failed"]))
        self.assertEqual("POST_NOTIFICATION", result["failed"][0]["op"])
        self.assertEqual(1, len(calls))

    def test_appops_failure_is_recorded(self):
        def run_adb(arguments, timeout=10):
            return _result(returncode=1, stderr="Error: unknown op")

        result = apply_appops(
            run_adb, "com.example.app", {"MANAGE_EXTERNAL_STORAGE": "allow"}
        )

        self.assertEqual([], result["applied"])
        self.assertIn("unknown op", result["failed"][0]["error"])


class PackageVerifierTest(unittest.TestCase):
    def test_switches_are_applied(self):
        calls = []

        def run_adb(arguments, timeout=10):
            calls.append(arguments)
            return _result()

        result = disable_package_verifier(run_adb)

        self.assertEqual(3, len(result["applied"]))
        self.assertIn(
            ["shell", "settings", "put", "global", "verifier_verify_adb_installs", "0"],
            calls,
        )


class ApplyPermissionSetupTest(unittest.TestCase):
    def test_aggregates_results(self):
        def run_adb(arguments, timeout=10):
            return _result()

        result = apply_permission_setup(
            run_adb,
            "com.example.app",
            grant_permissions=["android.permission.CAMERA"],
            appops={"SYSTEM_ALERT_WINDOW": "allow"},
            disable_verifier=True,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, len(result["grant"]["granted"]))
        self.assertEqual(1, len(result["appops"]["applied"]))
        self.assertEqual(3, len(result["verifier"]["applied"]))

    def test_success_is_false_when_anything_failed(self):
        def run_adb(arguments, timeout=10):
            if arguments[:3] == ["shell", "pm", "grant"]:
                return _result(returncode=255, stderr="Error: not changeable")
            return _result()

        result = apply_permission_setup(
            run_adb,
            "com.example.app",
            grant_permissions=["android.permission.MANAGE_USERS"],
        )

        self.assertFalse(result["success"])
        self.assertEqual(1, len(result["grant"]["failed"]))

    def test_empty_profile_still_returns_a_record(self):
        result = apply_permission_setup(
            Mock(), "com.example.app", grant_permissions=[]
        )

        self.assertTrue(result["success"])
        self.assertEqual([], result["grant"]["requested"])
        self.assertIsNone(result["verifier"])


if __name__ == "__main__":
    unittest.main()
