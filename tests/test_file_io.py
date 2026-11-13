from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import core.file_io as file_io
from core.file_io import atomic_write_text


class AtomicFileWriteTests(unittest.TestCase):
    def test_temporary_path_fails_instead_of_reusing_a_fixed_short_name(self) -> None:
        destination = Path("d" * file_io._MAX_LEGACY_PATH_LENGTH) / "failure.json"

        with self.assertRaises(OSError) as captured:
            file_io._temporary_path(destination)

        self.assertEqual(errno.ENAMETOOLONG, captured.exception.errno)

    def test_long_destination_does_not_make_temporary_path_exceed_max_path(self) -> None:
        real_replace = file_io.os.replace
        replace_sources = []

        def replace(source: Path, destination: Path) -> None:
            replace_sources.append(Path(source))
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_name = "failure.json"
            target_length = 220
            padding_length = target_length - len(str(root)) - len(file_name) - 2
            self.assertGreater(padding_length, 0)
            path = root / ("d" * padding_length) / file_name
            old_temporary = path.with_name(
                ".%s.%s.%s.%s.tmp"
                % (path.name, os.getpid(), threading.get_ident(), "a" * 32)
            )

            self.assertEqual(target_length, len(os.path.abspath(os.fspath(path))))
            self.assertGreaterEqual(
                len(os.path.abspath(os.fspath(old_temporary))),
                260,
            )

            with mock.patch.object(file_io.os, "replace", side_effect=replace):
                atomic_write_text(path, '{"ok": true}')

            self.assertEqual({"ok": True}, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(1, len(replace_sources))
            self.assertLess(
                len(os.path.abspath(os.fspath(replace_sources[0]))),
                260,
            )
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_concurrent_writers_do_not_share_a_temporary_file(self) -> None:
        writer_count = 24
        barrier = threading.Barrier(writer_count)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "job.json"

            def write(index: int) -> None:
                barrier.wait()
                atomic_write_text(
                    path,
                    json.dumps({"writer": index, "payload": "x" * 16000}),
                )

            with ThreadPoolExecutor(max_workers=writer_count) as pool:
                list(pool.map(write, range(writer_count)))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(saved["writer"], range(writer_count))
            self.assertEqual([path], list(root.iterdir()))

    def test_replace_retries_a_transient_permission_error(self) -> None:
        real_replace = file_io.os.replace
        calls = 0

        def replace(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError(13, "destination is temporarily busy")
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            with (
                mock.patch.object(file_io.os, "replace", side_effect=replace),
                mock.patch.object(file_io.time, "sleep") as sleep,
            ):
                atomic_write_text(path, '{"ok": true}')

            self.assertEqual({"ok": True}, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(2, calls)
            sleep.assert_called_once_with(file_io._REPLACE_RETRY_DELAYS[0])

    def test_recreates_parent_after_it_disappears_before_replace(self) -> None:
        real_replace = file_io.os.replace
        calls = 0

        def replace(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                Path(source).unlink()
                Path(source).parent.rmdir()
                raise FileNotFoundError(2, "parent disappeared", str(source))
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transient" / "job.json"
            with (
                mock.patch.object(file_io.os, "replace", side_effect=replace),
                mock.patch.object(file_io.time, "sleep") as sleep,
            ):
                atomic_write_text(path, '{"ok": true}')

            self.assertEqual({"ok": True}, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(2, calls)
            sleep.assert_called_once_with(file_io._REPLACE_RETRY_DELAYS[0])
            self.assertEqual([path], list(path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
