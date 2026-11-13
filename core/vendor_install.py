"""Click vendor install-confirmation prompts while ``adb install`` is blocked.

Every OEM renders a different page for the same step: MIUI/HyperOS shows a
bottom sheet whose reject button auto-fires after a countdown, vivo/HONOR show
a full-page installer, and some builds put the prompt in the security centre.
A headless ``adb install`` waits forever until somebody taps "继续安装".

The clicker is purely label based (uiautomator2 hierarchy), never coordinates:
the same text appears at different places across screens and resolutions.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Any package name containing one of these hosts an install confirmation UI.
INSTALLER_PACKAGE_HINTS = (
    "packageinstaller",
    "permissioncontroller",
    "securitycenter",
    "appstore",
    "appdetail",
    "package_manager",
    "packagemanager",
    "installer",
    # Huawei routes sideloaded installs through the AppGallery risk-check page
    # (com.huawei.appmarket/...InstallDistActivity) instead of a package installer.
    "appmarket",
    "appgallery",
    "systeminstall",
    "installdist",
)

# Ordered by preference.  "继续安装" is the explicit continue action; the plain
# "安装" is only a fallback for pages that label the primary button that way.
CONFIRM_TEXTS = (
    "继续安装",
    "立即安装",
    "仍要安装",
    "仍然安装",
    "继续安装应用",
    "安装应用",
    "允许安装",
    "安装",
    "允许",
    "继续",
    "确定",
)

# Never click a button whose label carries one of these meanings.  On MIUI the
# reject button is what actually cancels the install.
REJECT_MARKERS = ("拒绝", "取消", "放弃", "以后", "不允许", "禁止", "卸载")

_BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(value: str) -> Optional[Tuple[int, int, int, int]]:
    match = _BOUNDS_PATTERN.search(str(value or ""))
    if match is None:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _node_text(node: ET.Element) -> str:
    return str(node.get("text") or "").strip()


def _is_visible_node(node: ET.Element) -> bool:
    if str(node.get("enabled") or "true").lower() == "false":
        return False
    if str(node.get("visible-to-user") or "true").lower() == "false":
        return False
    return True


def find_confirm_button(hierarchy: str) -> Optional[Tuple[str, int, int]]:
    """Return ``(text, center_x, center_y)`` for the best confirm button."""
    if not hierarchy:
        return None
    try:
        root = ET.fromstring(hierarchy)
    except ET.ParseError:
        return None

    candidates: Dict[str, Tuple[str, int, int]] = {}
    for node in root.iter("node"):
        text = _node_text(node)
        if text not in CONFIRM_TEXTS or not _is_visible_node(node):
            continue
        if any(marker in text for marker in REJECT_MARKERS):
            continue
        bounds = _parse_bounds(str(node.get("bounds") or ""))
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        candidates.setdefault(text, (text, (left + right) // 2, (top + bottom) // 2))

    for text in CONFIRM_TEXTS:
        if text in candidates:
            return candidates[text]
    return None


def hierarchy_offers_reject(hierarchy: str) -> bool:
    """True when the page shows a reject/cancel side, i.e. it is a real prompt."""
    if not hierarchy:
        return False
    return any(marker in hierarchy for marker in REJECT_MARKERS)


def is_installer_package(package: str) -> bool:
    value = str(package or "").lower()
    return any(hint in value for hint in INSTALLER_PACKAGE_HINTS)


def count_nodes_with_text(hierarchy: str, wanted: str) -> int:
    if not hierarchy:
        return 0
    try:
        root = ET.fromstring(hierarchy)
    except ET.ParseError:
        return 0
    return sum(
        1
        for node in root.iter("node")
        if _node_text(node) == wanted and _is_visible_node(node)
    )


def is_gallery_package(package: str) -> bool:
    value = str(package or "").lower()
    return "appmarket" in value or "appgallery" in value


class InstallPromptClicker:
    """Poll one device's hierarchy and answer vendor install prompts."""

    def __init__(
        self,
        serial: str,
        *,
        poll_seconds: float = 0.4,
        timeout_seconds: float = 900.0,
        device_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.serial = serial
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._device_factory = device_factory
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.clicks: List[str] = []
        self.last_error: Optional[str] = None
        # Some vendors show two pages in a row ("继续安装" then "安装").  Remember
        # the last click so a page that has not repainted yet is not tapped twice.
        self._last_click: Optional[Tuple[str, int, int, float]] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "InstallPromptClicker":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._run, name="install-prompt-%s" % self.serial, daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- internals ---------------------------------------------------------
    def _connect(self) -> Any:
        if self._device_factory is not None:
            return self._device_factory(self.serial)
        import uiautomator2 as u2

        return u2.connect(self.serial)

    def _handle(self, device: Any) -> Optional[str]:
        try:
            current = device.app_current() or {}
            package = str(dict(current).get("package") or "")
        except Exception:
            package = ""
        hierarchy = ""
        try:
            hierarchy = str(device.dump_hierarchy(compressed=False) or "")
        except Exception:
            return None
        known_installer = is_installer_package(package)
        if not known_installer:
            # Outside a known installer page, answer only prompts that visibly
            # offer both a confirm and a reject side; a lone "安装" could be app
            # content on the page underneath.
            if not hierarchy_offers_reject(hierarchy):
                return None
        button = find_confirm_button(hierarchy)
        if button is None:
            return None
        text, x, y = button
        # Huawei AppGallery pages list recommended apps, each with its own
        # "安装" button.  Only the explicit "继续安装" belongs to our session.
        if is_gallery_package(package) and text != "继续安装":
            return None
        # Several identical "安装" labels mean a list of items, not the prompt.
        if text == "安装" and count_nodes_with_text(hierarchy, "安装") > 1:
            return None
        now = time.monotonic()
        if self._last_click is not None:
            last_text, last_x, last_y, last_at = self._last_click
            if (last_text, last_x, last_y) == (text, x, y) and now - last_at < 1.2:
                return None
        try:
            device.click(x, y)
        except Exception as exc:
            self.last_error = str(exc)
            return None
        self._last_click = (text, x, y, now)
        self.clicks.append(text)
        logger.info(
            "Confirmed vendor install prompt on %s (text=%s, package=%s)",
            self.serial,
            text,
            package or "unknown",
        )
        return text

    def _run(self) -> None:
        try:
            device = self._connect()
        except Exception as exc:
            self.last_error = "connect: %s" % exc
            logger.warning("Install prompt clicker could not connect to %s: %s", self.serial, exc)
            return
        deadline = time.monotonic() + self.timeout_seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                self._handle(device)
            except Exception:
                logger.debug(
                    "Install prompt probe failed on %s", self.serial, exc_info=True
                )
            # Give the next prompt page a moment to render before probing again.
            self._stop.wait(max(self.poll_seconds, 0.8 if self.clicks else self.poll_seconds))


def start_clicker(serial: str, **kwargs: Any) -> InstallPromptClicker:
    return InstallPromptClicker(serial, **kwargs).start()
