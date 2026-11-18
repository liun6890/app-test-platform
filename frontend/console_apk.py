"""APK metadata helpers for the console.

The console has to know the package name of an uploaded APK before it can
start a run.  ``aapt`` answers that instantly, but it ships with the Android
SDK Build Tools, so a machine that only installed Platform Tools (or nothing
from the SDK at all) cannot upload a package.  ``package_name_from_apk``
parses ``AndroidManifest.xml`` directly, which keeps the upload path working
after nothing more than ``pip install -r requirements.txt``.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")

# Binary XML chunk types (frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h).
_XML_TYPE = 0x0003
_STRING_POOL_TYPE = 0x0001
_START_ELEMENT_TYPE = 0x0102
# String pool header flag: strings are stored as UTF-8 rather than UTF-16.
_UTF8_FLAG = 1 << 8
# ``ResStringPool_ref`` sentinel used by attributes without a raw value.
_NO_ENTRY = 0xFFFFFFFF
# Res_value::TYPE_STRING
_TYPE_STRING = 0x03
_STRING_POOL_HEADER_SIZE = 28
_START_ELEMENT_HEADER_SIZE = 16
_ATTRIBUTE_SIZE = 20


class AxmlError(ValueError):
    """Raised when an APK manifest is not the binary XML we can read."""


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


def _read_utf8_length(data: bytes, offset: int) -> Tuple[int, int]:
    """Return ``(length, next_offset)`` for a UTF-8 pool entry length prefix."""
    if offset >= len(data):
        raise AxmlError("manifest string pool is truncated")
    first = data[offset]
    if first & 0x80:
        if offset + 1 >= len(data):
            raise AxmlError("manifest string pool is truncated")
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _read_utf16_length(data: bytes, offset: int) -> Tuple[int, int]:
    """Return ``(length, next_offset)`` for a UTF-16 pool entry length prefix."""
    if offset + 1 >= len(data):
        raise AxmlError("manifest string pool is truncated")
    first = struct.unpack_from("<H", data, offset)[0]
    if first & 0x8000:
        if offset + 3 >= len(data):
            raise AxmlError("manifest string pool is truncated")
        second = struct.unpack_from("<H", data, offset + 2)[0]
        return ((first & 0x7FFF) << 16) | second, offset + 4
    return first, offset + 2


def _parse_string_pool(chunk: bytes, header_size: int) -> List[str]:
    """Decode one ``ResStringPool`` chunk into its ordered strings."""
    if header_size < _STRING_POOL_HEADER_SIZE or len(chunk) < header_size:
        raise AxmlError("manifest string pool header is truncated")
    string_count, _style_count, flags, strings_start, _styles_start = struct.unpack_from(
        "<IIIII", chunk, 8
    )
    if header_size + string_count * 4 > len(chunk):
        raise AxmlError("manifest string pool offset table is truncated")
    offsets = struct.unpack_from("<%dI" % string_count, chunk, header_size)
    utf8 = bool(flags & _UTF8_FLAG)
    strings: List[str] = []
    for item in offsets:
        cursor = strings_start + item
        if cursor >= len(chunk):
            raise AxmlError("manifest string pool entry is out of range")
        if utf8:
            # UTF-8 entries carry the character count and then the byte count.
            _characters, cursor = _read_utf8_length(chunk, cursor)
            byte_length, cursor = _read_utf8_length(chunk, cursor)
            end = cursor + byte_length
            if end > len(chunk):
                raise AxmlError("manifest string pool entry is truncated")
            strings.append(chunk[cursor:end].decode("utf-8", errors="replace"))
        else:
            characters, cursor = _read_utf16_length(chunk, cursor)
            end = cursor + characters * 2
            if end > len(chunk):
                raise AxmlError("manifest string pool entry is truncated")
            strings.append(chunk[cursor:end].decode("utf-16-le", errors="replace"))
    return strings


def _start_element_package(
    names: List[str], chunk: bytes, header_size: int
) -> Optional[str]:
    """Return the ``package`` attribute when this element is ``<manifest>``.

    ``header_size`` is the size of ``ResXMLTree_node`` (16 bytes); the
    ``ResXMLTree_attrExt`` structure that carries the element name and its
    attributes starts immediately after it.
    """
    ext = header_size
    if header_size < _START_ELEMENT_HEADER_SIZE or ext + 20 > len(chunk):
        raise AxmlError("manifest start element is truncated")
    _ns_index, name_index = struct.unpack_from("<II", chunk, ext)
    if name_index >= len(names) or names[name_index] != "manifest":
        return None
    attribute_start, attribute_size, attribute_count = struct.unpack_from(
        "<HHH", chunk, ext + 8
    )
    if attribute_size < _ATTRIBUTE_SIZE:
        raise AxmlError("manifest attribute size is invalid")
    for index in range(attribute_count):
        offset = ext + attribute_start + index * attribute_size
        if offset + _ATTRIBUTE_SIZE > len(chunk):
            raise AxmlError("manifest attribute is truncated")
        _ns, attribute_name, raw_value = struct.unpack_from("<III", chunk, offset)
        _size, _res0, value_type, value_data = struct.unpack_from(
            "<HBBI", chunk, offset + 12
        )
        if attribute_name >= len(names) or names[attribute_name] != "package":
            continue
        if raw_value != _NO_ENTRY and raw_value < len(names):
            return names[raw_value]
        if value_type == _TYPE_STRING and value_data < len(names):
            return names[value_data]
        return None
    return None


def _manifest_package(data: bytes) -> str:
    """Walk the binary XML chunks and return ``<manifest package="...">``."""
    if len(data) < 8:
        raise AxmlError("manifest is too small to be binary XML")
    chunk_type, header_size, total_size = struct.unpack_from("<HHI", data, 0)
    if chunk_type != _XML_TYPE:
        raise AxmlError("manifest is not binary XML (chunk type 0x%04x)" % chunk_type)
    end = min(len(data), total_size) if total_size else len(data)
    offset = header_size if 8 <= header_size <= end else 8
    names: List[str] = []
    while offset + 8 <= end:
        entry_type, entry_header, entry_size = struct.unpack_from("<HHI", data, offset)
        if entry_size < 8 or offset + entry_size > end:
            raise AxmlError("manifest chunk at offset %d is truncated" % offset)
        chunk = data[offset : offset + entry_size]
        if entry_type == _STRING_POOL_TYPE:
            names = _parse_string_pool(chunk, entry_header)
        elif entry_type == _START_ELEMENT_TYPE and names:
            package = _start_element_package(names, chunk, entry_header)
            if package:
                return package
        offset += entry_size
    raise AxmlError('manifest has no <manifest package="..."> element')


def package_name_from_apk(path: Path) -> Tuple[Optional[str], str]:
    """Read the package name straight out of ``AndroidManifest.xml``."""
    try:
        with zipfile.ZipFile(path) as archive:
            data = archive.read("AndroidManifest.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        return None, "无法读取 APK 内的 AndroidManifest.xml: %s" % exc
    try:
        name = _manifest_package(data)
    except AxmlError as exc:
        return None, "APK manifest 解析失败: %s" % exc
    if not PACKAGE_PATTERN.fullmatch(name):
        return None, "APK manifest 中的包名不合法: %s" % name
    return name, ""


def _package_name_from_aapt(path: Path) -> Tuple[Optional[str], str]:
    """Read the package name with a local ``aapt`` when one is installed."""
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


def apk_package_name(path: Path) -> Tuple[Optional[str], str]:
    """Read the package name, preferring aapt and falling back to the parser."""
    name, aapt_detail = _package_name_from_aapt(path)
    if name:
        return name, ""
    parsed, parse_detail = package_name_from_apk(path)
    if parsed:
        return parsed, ""
    detail = parse_detail or aapt_detail
    if aapt_detail and parse_detail and parse_detail != aapt_detail:
        detail = "%s；%s" % (aapt_detail, parse_detail)
    return None, detail
