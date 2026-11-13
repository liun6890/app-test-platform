"""Vendor install prompts must be answered by label, never by coordinates."""

from __future__ import annotations

import threading
import time
import unittest

from core.vendor_install import (
    InstallPromptClicker,
    find_confirm_button,
    hierarchy_offers_reject,
    is_installer_package,
)


def _hierarchy(buttons):
    nodes = "".join(
        '<node text="%s" enabled="%s" bounds="[%d,%d][%d,%d]" />'
        % (text, "false" if disabled else "true", left, top, right, bottom)
        for text, disabled, left, top, right, bottom in buttons
    )
    return "<hierarchy>%s</hierarchy>" % nodes


class ConfirmButtonTests(unittest.TestCase):
    def test_continue_install_wins_over_cancel(self):
        page = _hierarchy(
            [
                ("取消", False, 0, 2000, 300, 2100),
                ("继续安装", False, 700, 2000, 1080, 2100),
            ]
        )
        self.assertTrue(hierarchy_offers_reject(page))
        self.assertEqual(("继续安装", 890, 2050), find_confirm_button(page))

    def test_reject_labels_are_never_selected(self):
        page = _hierarchy([("取消安装", False, 10, 10, 20, 20)])
        self.assertIsNone(find_confirm_button(page))

    def test_disabled_confirm_is_skipped(self):
        page = _hierarchy([("继续安装", True, 0, 0, 100, 100)])
        self.assertIsNone(find_confirm_button(page))

    def test_installer_package_detection(self):
        self.assertTrue(is_installer_package("com.miui.securitycenter"))
        self.assertTrue(is_installer_package("com.android.packageinstaller"))
        self.assertFalse(is_installer_package("com.example.shop"))


class _FakeDevice:
    def __init__(self, package, page):
        self.package = package
        self.page = page
        self.clicks = []

    def app_current(self):
        return {"package": self.package}

    def dump_hierarchy(self, compressed=False):
        return self.page

    def click(self, x, y):
        self.clicks.append((x, y))


class ClickerTests(unittest.TestCase):
    def test_clicker_confirms_installer_page(self):
        device = _FakeDevice(
            "com.android.packageinstaller",
            _hierarchy([("继续安装", False, 0, 0, 100, 100)]),
        )
        clicker = InstallPromptClicker(
            "serial-1", poll_seconds=0.05, timeout_seconds=2, device_factory=lambda _s: device
        ).start()
        deadline = time.monotonic() + 2
        while not device.clicks and time.monotonic() < deadline:
            time.sleep(0.01)
        clicker.stop()

        self.assertEqual([(50, 50)], device.clicks)
        self.assertEqual(["继续安装"], clicker.clicks)

    def test_clicker_ignores_ambiguous_page_outside_installer(self):
        device = _FakeDevice(
            "com.example.shop",
            _hierarchy([("安装", False, 0, 0, 100, 100)]),
        )
        clicker = InstallPromptClicker(
            "serial-1", poll_seconds=0.05, timeout_seconds=0.5, device_factory=lambda _s: device
        ).start()
        clicker.stop(timeout=2)

        self.assertEqual([], device.clicks)


class HuaweiGalleryPromptTest(unittest.TestCase):
    def test_appgallery_install_page_is_answered(self):
        page = _hierarchy(
            [
                ("继续安装", False, 700, 2100, 1080, 2200),
                ("华为应用市场安装", False, 0, 2100, 380, 2200),
            ]
        )
        self.assertEqual(("继续安装", 890, 2150), find_confirm_button(page))


class AmbiguousInstallButtonTests(unittest.TestCase):
    def test_gallery_page_without_continue_is_not_clicked(self):
        device = _FakeDevice(
            "com.huawei.appmarket",
            _hierarchy([("安装", False, 0, 0, 100, 100), ("安装", False, 0, 200, 100, 300)]),
        )
        clicker = InstallPromptClicker(
            "serial-1", poll_seconds=0.05, timeout_seconds=0.5, device_factory=lambda _s: device
        ).start()
        clicker.stop(timeout=2)
        self.assertEqual([], device.clicks)

    def test_gallery_continue_button_is_clicked(self):
        device = _FakeDevice(
            "com.huawei.appmarket",
            _hierarchy([("继续安装", False, 0, 0, 100, 100), ("取消", False, 0, 200, 100, 300)]),
        )
        clicker = InstallPromptClicker(
            "serial-1", poll_seconds=0.05, timeout_seconds=2, device_factory=lambda _s: device
        ).start()
        deadline = time.monotonic() + 2
        while not device.clicks and time.monotonic() < deadline:
            time.sleep(0.01)
        clicker.stop()
        self.assertEqual([(50, 50)], device.clicks)

    def test_repeated_install_labels_are_skipped(self):
        device = _FakeDevice(
            "com.android.packageinstaller",
            _hierarchy([("安装", False, 0, 0, 100, 100), ("安装", False, 0, 200, 100, 300)]),
        )
        clicker = InstallPromptClicker(
            "serial-1", poll_seconds=0.05, timeout_seconds=0.5, device_factory=lambda _s: device
        ).start()
        clicker.stop(timeout=2)
        self.assertEqual([], device.clicks)


if __name__ == "__main__":
    unittest.main()
