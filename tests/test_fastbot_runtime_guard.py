import unittest
import zipfile

from core.fastbot_assets import (
    normalize_runtime_options,
    runtime_asset_md5,
    runtime_asset_path,
    runtime_remote_path,
)


class FastbotRuntimeGuardTests(unittest.TestCase):
    def test_previous_runtime_checksum_migrates_to_guarded_bundle(self):
        for checksum in (
            "c5f5aa76d27ce6f114c8dac0f526aca6",
            "ea2d4d7461c3137590898765470ce7ab",
            "e67cc9eab9c5300cc9caf00d96c6c9d6",
            "1cd1722e4abade89fecdefea244fc588",
        ):
            with self.subTest(checksum=checksum):
                options = {
                    "classpath": runtime_remote_path(),
                    "expected_monkeyq_md5": checksum,
                }

                normalize_runtime_options(options)

                self.assertEqual(runtime_asset_md5(), options["expected_monkeyq_md5"])

    def test_motion_guard_keeps_vendor_bottom_gesture_fallback(self):
        source = runtime_asset_path().with_name("MonkeyMotionEvent.java").read_text(
            encoding="utf-8"
        )

        self.assertIn("NAVIGATION_GESTURE_FALLBACK_HEIGHT_PERCENT = 4", source)
        self.assertIn("height * NAVIGATION_GESTURE_FALLBACK_HEIGHT_PERCENT", source)

    def test_shared_motion_injector_owns_status_bar_service_guard(self):
        classes_jar = runtime_asset_path().with_name(
            "monkey-android15-safe-input-classes.jar"
        )
        with zipfile.ZipFile(classes_jar) as archive:
            motion_class = archive.read(
                "com/android/commands/monkey/events/base/MonkeyMotionEvent.class"
            )

        self.assertIn(b"android/os/ServiceManager", motion_class)
        self.assertIn(b"collapsePanels", motion_class)
        self.assertIn(
            b"disabled notification shade expansion; process exit restores it",
            motion_class,
        )

    def test_runtime_dex_contains_system_gesture_and_system_ui_guards(self):
        with zipfile.ZipFile(runtime_asset_path()) as archive:
            dex = archive.read("classes.dex")

        self.assertIn(b"StatusBarInputGuard dropped gesture", dex)
        self.assertIn(b"SystemGestureInputGuard safe bounds", dex)
        self.assertIn(b"navigation_bar_height", dex)
        self.assertIn(b"navigation_bar_width", dex)
        self.assertIn(b"SystemUiInputGuard cleared pending random input", dex)
        self.assertIn(b"com.android.systemui", dex)
        self.assertIn(b"getStableInsets", dex)
        self.assertIn(b"getWindows", dex)
        self.assertIn(
            b"disabled notification shade expansion; process exit restores it", dex
        )

    def test_runtime_dex_contains_app_window_and_control_text_guards(self):
        with zipfile.ZipFile(runtime_asset_path()) as archive:
            dex = archive.read("classes.dex")
        source = runtime_asset_path().with_name("AppInputGuard.java").read_text(
            encoding="utf-8"
        )
        self.assertIn(b"AppInputGuard blocked external window", dex)
        self.assertIn(b"AppTextEvent set configured text through control", dex)
        self.assertIn("TYPE_INPUT_METHOD", source)

    def test_strict_runtime_suppresses_native_back_before_it_exits_target(self):
        source = runtime_asset_path().with_name("MonkeyKeyEvent.java").read_text(
            encoding="utf-8"
        )
        with zipfile.ZipFile(runtime_asset_path()) as archive:
            dex = archive.read("classes.dex")

        self.assertIn("getKeyCode() == KeyEvent.KEYCODE_BACK && !systemUiRecovery", source)
        self.assertIn(b"Strict input guard suppressed BACK", dex)


if __name__ == "__main__":
    unittest.main()
