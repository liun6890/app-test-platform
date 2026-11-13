"""APK metadata helpers for the console."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")


def _aapt_path() -> Optional[str]:
    candidates = [os.environ.get("AAPT_PATH", ""), shutil.which("aapt"), shutil.which("aapt2")]
    sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk_root:
        candidates.extend(str(item) for item in Path(sdk_root).glob("build-tools/*/aapt.exe"))
        candidates.extend(str(item) for item in Path(sdk_root).glob("build-tools/*/aapt"))
    if os.name == "nt":
        candidates.extend(
            str(item)
            for root in (Path("D:/Android/android-sdk"), Path("C:/Android/Sdk"))
            for item in root.glob("build-tools/*/aapt.exe")
        )
    return next((item for item in candidates if item and Path(item).is_file()), None)


def apk_package_name(path: Path) -> Tuple[Optional[str], str]:
    """Read the installed package name from an APK manifest using local aapt."""
    executable = _aapt_path()
    if not executable:
        return None, "本机未找到 aapt，请设置 AAPT_PATH 或安装 Android SDK Build Tools"
    try:
        result = subprocess.run(
            [executable, "dump", "badging", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "无法执行 aapt: %s" % exc
    match = re.search(r"^package:\s+name='([^']+)'", result.stdout or "", re.MULTILINE)
    if not match or not PACKAGE_PATTERN.fullmatch(match.group(1)):
        detail = (result.stderr or result.stdout or "未读到 package manifest").strip()
        return None, "APK manifest 包名解析失败: %s" % detail[-500:]
    return match.group(1), ""
