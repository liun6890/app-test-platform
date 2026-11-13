import tempfile
import unittest
from pathlib import Path

import server


class LogRotationTests(unittest.TestCase):
    def test_small_log_is_appended_without_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "console.log"
            log.write_bytes(b"old\n")
            stream = server.open_job_log(log)
            stream.write(b"new\n")
            stream.close()

            self.assertEqual(b"old\nnew\n", log.read_bytes())
            self.assertFalse((Path(temporary) / "console.log.1").exists())

    def test_large_log_is_rotated_before_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "console.log"
            log.write_bytes(b"x" * (server.LOG_ROTATE_BYTES + 1))
            stream = server.open_job_log(log)
            stream.write(b"fresh\n")
            stream.close()

            backup = Path(temporary) / "console.log.1"
            self.assertTrue(backup.is_file())
            self.assertEqual(
                server.LOG_ROTATE_BYTES + 1, backup.stat().st_size
            )
            self.assertEqual(b"fresh\n", log.read_bytes())

    def test_existing_backup_is_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "console.log"
            log.write_bytes(b"x" * (server.LOG_ROTATE_BYTES + 1))
            backup = Path(temporary) / "console.log.1"
            backup.write_bytes(b"previous")

            stream = server.open_job_log(log)
            stream.close()

            self.assertEqual(
                server.LOG_ROTATE_BYTES + 1, backup.stat().st_size
            )


if __name__ == "__main__":
    unittest.main()
