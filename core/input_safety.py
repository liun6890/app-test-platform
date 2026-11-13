"""Passive IME inspection and bounded, non-touch keyboard dismissal."""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET


class InputRecoveryRequired(RuntimeError):
    """The page must be restored before another input can be delivered."""


# Vendor permission/consent prompts are usually *windows* rather than
# Activities.  While one is visible the target app still owns its task, so a
# foreground check must not read the popup (or the launcher the vendor briefly
# highlights behind it) as "the user left the app".
SYSTEM_DIALOG_PACKAGES = frozenset(
    {
        "com.lbe.security.miui",
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
        "com.android.packageinstaller",
        "com.miui.securitycenter",
        "com.miui.permcenter",
        "com.huawei.systemmanager",
        "com.coloros.securitypermission",
    }
)


def is_system_dialog_package(package: Any) -> bool:
    """Return whether a foreground package is a transient vendor prompt."""
    return str(package or "").lower() in SYSTEM_DIALOG_PACKAGES


def input_safety_options(config: Mapping[str, Any]) -> Dict[str, Any]:
    # load_config enables this for new runs. Minimal legacy adapters retain
    # their previous contract until they opt into the new guard.
    raw = config.get("input_safety", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def parse_input_method_state(output: str) -> Dict[str, Any]:
    """Check both the IME window and its input view for actual visibility.

    Hidden test IMEs can have mInputShown=true and mIsInputViewShown=false.
    A dismissed vendor IME can retain mIsInputViewShown=true even when only
    mInputShown=false remains available to confirm dismissal.
    Historical show/hide records must not override the current service state.
    """
    lines = []
    history_indent = None
    for line in output.splitlines():
        indent = len(line) - len(line.lstrip())
        if history_indent is not None and line.strip() and indent <= history_indent:
            history_indent = None
        if re.search(r"history\s*:", line, re.I):
            history_indent = indent
        if history_indent is None:
            lines.append(line)
    current = "\n".join(lines)
    package = re.search(r"\bmCur(?:Id|MethodId)\s*[=:]\s*([\w.]+)/[^\s]+", current)

    def flags(name: str):
        return re.findall(r"\b%s\s*[=:]\s*(true|false)\b" % name, current)

    view = flags("mIsInputViewShown")
    window = flags("mWindowVisible") + flags("mDecorViewVisible")
    logical = flags("mInputShown")
    # Hidden containers cannot display a retained input view. Without window
    # flags, a definite logical hide also overrides the retained view flag.
    # Conflicting current copies of a flag do not establish visibility.
    if window and "true" not in window:
        shown = False
    elif len(set(view)) > 1 or (not window and len(set(logical)) > 1):
        shown = None
    elif not window and logical and "true" not in logical:
        shown = False
    elif view:
        shown = "true" in view
    elif window:
        shown = "true" in window
    elif logical:
        shown = "true" in logical
    else:
        shown = None
    return {
        "known": shown is not None,
        "visible": shown,
        "package": package.group(1) if package else "",
    }


def touch_path_is_owned(
    nodes: Sequence[ET.Element], package: str, points: Sequence[Tuple[int, int]],
) -> bool:
    """Require app coverage and reject external bounds along the whole path."""
    if not nodes or not points or not package:
        return False
    x1, x2 = min(p[0] for p in points), max(p[0] for p in points)
    y1, y2 = min(p[1] for p in points), max(p[1] for p in points)
    covered = False
    for node in nodes:
        match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds", ""))
        if not match:
            return False
        left, top, right, bottom = map(int, match.groups())
        if right <= left or bottom <= top:
            continue
        if node.get("package") == package:
            covered = covered or (left <= x1 <= x2 < right and top <= y1 <= y2 < bottom)
        elif left <= x2 and right > x1 and top <= y2 and bottom > y1:
            return False
    return covered


def probe_input_method(
    run_adb: Callable[..., Any], timeout: float = 6.0, retries: int = 2,
) -> Dict[str, Any]:
    """Read the IME state, tolerating a slow device.

    A loaded device can take several seconds to answer ``dumpsys input_method``;
    the old 1.5s window made slow answers look like "unknown". Retries reduce
    timing noise; callers decide whether unknown visibility should block their
    operation or only produce a warning.
    """
    attempts = max(1, int(retries) + 1)
    for attempt in range(attempts):
        try:
            result = run_adb(
                ["shell", "dumpsys", "input_method"],
                timeout=timeout + attempt * 2.0,
            )
            if result is not None and result.returncode == 0:
                state = parse_input_method_state(result.stdout or "")
                if state.get("known"):
                    return state
        except Exception:
            continue
    return {"known": False, "visible": None, "package": ""}


def hide_input_method(
    run_adb: Callable[..., Any], timeout: float = 3.0,
    should_stop: Callable[[], bool] = lambda: False,
    *, can_dismiss: Optional[Callable[[], bool]] = None,
) -> bool:
    """Dismiss a verified visible IME once, then confirm it is actually hidden.

    Android's standard ``ime`` command has no hide operation. BACK is allowed
    only while the caller confirms target foreground ownership and input is
    exclusive. Unknown visibility never authorizes a key event.
    """
    deadline = time.monotonic() + max(0.1, min(15.0, timeout))
    hide_attempted = False
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        state = probe_input_method(run_adb, min(1.5, remaining))
        if should_stop():
            return False
        if state["known"] and state["visible"] is False:
            return True
        if not hide_attempted and state["known"] and state["visible"] is True:
            if can_dismiss is None:
                return False
            try:
                if not can_dismiss():
                    return False
            except Exception:
                return False
            if should_stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # The foreground query may race a keyboard animation. Recheck
            # before BACK so an already hidden IME cannot navigate the app.
            state = probe_input_method(run_adb, min(1.5, remaining))
            if should_stop():
                return False
            if state["known"] and state["visible"] is False:
                return True
            remaining = deadline - time.monotonic()
            if state["known"] and state["visible"] is True and remaining > 0:
                hide_attempted = True
                try:
                    run_adb(["shell", "input", "keyevent", "BACK"], timeout=min(1.5, remaining))
                except Exception:
                    pass
        time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))
    return False
