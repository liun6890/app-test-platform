"""Tests for the pure-Python APK manifest reader.

``aapt`` lives in the Android SDK Build Tools, which most installs do not
have.  These tests build real binary XML chunks so the fallback parser is
exercised without any Android tooling.
"""
from __future__ import annotations

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import console_apk

_PACKAGE = "com.example.app"


def _string_pool(strings, utf8=True):
    """Encode a ``ResStringPool`` chunk the way aapt does."""
    data = bytearray()
    offsets = []
    base = 28 + 4 * len(strings)
    for value in strings:
        offsets.append(len(data))
        if utf8:
            encoded = value.encode("utf-8")
            data += struct.pack("<BB", len(value), len(encoded))
            data += encoded + b"\x00"
        else:
            encoded = value.encode("utf-16-le")
            data += struct.pack("<H", len(value))
            data += encoded + b"\x00\x00"
    size = base + len(data)
    header = struct.pack(
        "<HHIIIIII",
        0x0001,  # RES_STRING_POOL_TYPE
        28,
        size,
        len(strings),
        0,
        0x100 if utf8 else 0,
        base,
        0,
    )
    return header + struct.pack("<%dI" % len(offsets), *offsets) + bytes(data)


def _start_element(name_index, attributes=()):
    """Encode a ``RES_XML_START_ELEMENT`` chunk with string attributes.

    ``headerSize`` is the ``ResXMLTree_node`` size (16); the attribute
    extension follows the node header, exactly as aapt writes it.
    """
    size = 36 + 20 * len(attributes)
    out = bytearray(struct.pack("<HHI", 0x0102, 16, size))
    out += struct.pack("<II", 1, 0xFFFFFFFF)  # lineNumber, comment
    out += struct.pack("<II", 0xFFFFFFFF, name_index)
    out += struct.pack("<HHHHHH", 20, 20, len(attributes), 0, 0, 0)
    for name, value in attributes:
        out += struct.pack("<III", 0xFFFFFFFF, name, value)
        out += struct.pack("<HBBI", 8, 0, 0x03, value)
    return bytes(out)


def _axml(strings, chunks, utf8=True):
    """Wrap string pool and element chunks in the XML file header."""
    body = _string_pool(strings, utf8=utf8) + b"".join(chunks)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def _manifest_bytes(package=_PACKAGE, utf8=True, prefix_element=None):
    strings = ["manifest", "package", package, "activity"]
    if prefix_element:
        strings.append(prefix_element)
    package_index = strings.index("package")
    value_index = strings.index(package)
    manifest_index = strings.index("manifest")
    chunks = []
    if prefix_element:
        chunks.append(
            _start_element(strings.index(prefix_element), [(package_index, value_index)])
        )
    chunks.append(_start_element(manifest_index, [(package_index, value_index)]))
    return _axml(strings, chunks, utf8=utf8)


def _write_apk(path: Path, payload: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", payload)
    return path


class PackageNameFromApkTests(unittest.TestCase):
    def test_reads_package_from_utf8_string_pool(self):
        name, detail = console_apk.package_name_from_apk(
            _write_apk(Path(tempfile.mkdtemp()) / "utf8.apk", _manifest_bytes())
        )
        self.assertEqual(_PACKAGE, name, detail)

    def test_reads_package_from_utf16_string_pool(self):
        name, detail = console_apk.package_name_from_apk(
            _write_apk(
                Path(tempfile.mkdtemp()) / "utf16.apk", _manifest_bytes(utf8=False)
            )
        )
        self.assertEqual(_PACKAGE, name, detail)

    def test_skips_start_elements_before_manifest(self):
        payload = _manifest_bytes(prefix_element="activity")
        name, detail = console_apk.package_name_from_apk(
            _write_apk(Path(tempfile.mkdtemp()) / "prefix.apk", payload)
        )
        self.assertEqual(_PACKAGE, name, detail)

    def test_missing_manifest_element_is_reported(self):
        strings = ["activity", "package", _PACKAGE]
        payload = _axml(
            strings,
            [_start_element(0, [(strings.index("package"), strings.index(_PACKAGE))])],
        )
        name, detail = console_apk.package_name_from_apk(
            _write_apk(Path(tempfile.mkdtemp()) / "no-manifest.apk", payload)
        )
        self.assertIsNone(name)
        self.assertIn("no <manifest", detail)

    def test_invalid_package_value_is_rejected(self):
        payload = _manifest_bytes(package="not a package")
        name, detail = console_apk.package_name_from_apk(
            _write_apk(Path(tempfile.mkdtemp()) / "broken.apk", payload)
        )
        self.assertIsNone(name)
        self.assertIn("包名不合法", detail)

    def test_truncated_manifest_raises_a_readable_error(self):
        payload = _manifest_bytes()[:-12]
        name, detail = console_apk.package_name_from_apk(
            _write_apk(Path(tempfile.mkdtemp()) / "truncated.apk", payload)
        )
        self.assertIsNone(name)
        self.assertIn("truncated", detail)


class AaptFallbackTests(unittest.TestCase):
    def test_upload_works_without_aapt(self):
        payload = _manifest_bytes()
        apk = _write_apk(Path(tempfile.mkdtemp()) / "no-aapt.apk", payload)
        with mock.patch.object(console_apk, "_aapt_path", return_value=None):
            name, detail = console_apk.apk_package_name(apk)
        self.assertEqual(_PACKAGE, name, detail)


if __name__ == "__main__":
    unittest.main()
