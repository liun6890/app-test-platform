import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from core.retention import (
    apply_cleanup,
    list_run_directories,
    plan_run_cleanup,
    plan_upload_cleanup,
)


class RetentionTests(unittest.TestCase):
    def _run(self, root: Path, name: str, *, age_days: float, crash: bool = False) -> Path:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(
            json.dumps({"id": name, "status": "completed"}), encoding="utf-8"
        )
        reports = directory / "reports" / "device"
        reports.mkdir(parents=True)
        (reports / "report.json").write_text("{}", encoding="utf-8")
        if crash:
            (reports / "crash_pages").mkdir()
            (reports / "crash_pages" / "page.json").write_text("{}", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(directory, (stamp, stamp))
        return directory

    def test_old_runs_are_removed_and_recent_ones_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._run(root, "OLD", age_days=30)
            fresh = self._run(root, "NEW", age_days=1)

            plan = plan_run_cleanup(root, max_age_days=14)

            self.assertEqual([old], plan)
            self.assertNotIn(fresh, plan)

    def test_protected_ids_are_never_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._run(root, "OLD", age_days=30)

            plan = plan_run_cleanup(
                root, protected_ids=["OLD"], max_age_days=14
            )

            self.assertEqual([], plan)
            self.assertTrue(old.is_dir())

    def test_crash_runs_are_kept_when_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crash_run = self._run(root, "CRASH", age_days=60, crash=True)
            plain = self._run(root, "PLAIN", age_days=60)

            plan = plan_run_cleanup(root, max_age_days=14, keep_crash_runs=True)

            self.assertIn(plain, plan)
            self.assertNotIn(crash_run, plan)

    def test_size_cap_removes_oldest_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oldest = self._run(root, "A", age_days=3)
            middle = self._run(root, "B", age_days=2)
            newest = self._run(root, "C", age_days=1)
            # 1 byte payloads; cap of 0 forces deletion of everything but keeps
            # ordering predictable for assertions below.
            for name in ("A", "B", "C"):
                (root / name / "blob.bin").write_bytes(b"x" * 1024)

            plan = plan_run_cleanup(
                root, max_age_days=365, max_total_gb=0.000001, now=time.time()
            )

            self.assertTrue(plan)
            self.assertEqual(oldest, plan[0])
            self.assertLessEqual(plan.index(middle), 1)
            self.assertNotIn(newest, plan[:1])

    def test_uploads_keep_referenced_and_recent_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(exist_ok=True)
            referenced = root / "referenced.apk"
            stale = root / "stale.apk"
            for path, age in ((referenced, 90), (stale, 90)):
                path.write_bytes(b"apk")
                stamp = time.time() - age * 86400
                os.utime(path, (stamp, stamp))

            plan = plan_upload_cleanup(
                root,
                keep_paths=[str(referenced.resolve())],
                max_age_days=30,
            )

            self.assertEqual([stale], plan)

    def test_apply_cleanup_deletes_directories_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._run(root, "OLD", age_days=30)
            file_path = root / "old.apk"
            file_path.write_bytes(b"x")

            removed = apply_cleanup([directory, file_path])

            self.assertEqual(2, removed)
            self.assertFalse(directory.exists())
            self.assertFalse(file_path.exists())

    def test_list_run_directories_is_sorted_by_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            newer = self._run(root, "NEW", age_days=1)
            older = self._run(root, "OLD", age_days=5)

            self.assertEqual([older, newer], list_run_directories(root))


if __name__ == "__main__":
    unittest.main()
