import tempfile
import unittest
from pathlib import Path

from core.reporting import ReportGenerator


class ReportingTests(unittest.TestCase):
    def test_html_error_identifies_the_settings_page_that_blocked_permission_recovery(self):
        report = {"cycles": [{
            "cycle": 1,
            "status": "failed",
            "permission_recovery_failed": True,
            "permission_recovery_error": "permission recovery timed out",
            "permission_recoveries": [{
                "success": False,
                "error": "permission recovery timed out",
                "last_component": "com.android.settings/.Settings$AppManageExternalStorageActivity",
            }],
        }]}
        with tempfile.TemporaryDirectory() as temporary:
            path = ReportGenerator.write_html(report, Path(temporary) / "report.html")
            html = path.read_text(encoding="utf-8")

        self.assertIn("设备停在系统的“所有文件访问权限”设置页", html)

    def test_json_and_html_reports_are_written(self):
        report = {
            "run_id": "offline",
            "device_serial": "device-1",
            "status": "completed",
            "device_info": {"model": "test"},
            "cycles": [
                {
                    "cycle": 1,
                    "status": "passed",
                    "operation_count": 4,
                    "stall_detection_count": 1,
                }
            ],
            "crashes": [],
            "statistics": {
                "total_cycles": 1,
                "total_operations": 4,
                "stall_detection_count": 1,
                "crash_count": 0,
                "coverage_rate": None,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            paths = ReportGenerator.write_all(report, Path(temporary))
            self.assertTrue(Path(paths["json"]).is_file())
            self.assertTrue(Path(paths["html"]).is_file())
            content = Path(paths["html"]).read_text()
            self.assertIn("Android Stability Report", content)
            self.assertIn("Static-page observations", content)
            self.assertIn("not automatically app defects", content)

    def test_html_reports_stall_recovery_request_attempt_skip_and_failure_counts(self):
        report = {
            "run_id": "recovery",
            "device_serial": "device-1",
            "status": "completed_with_errors",
            "cycles": [
                {
                    "cycle": 1,
                    "status": "failed",
                    "stall_recovery_requested_count": 3,
                    "stall_recovery_count": 2,
                    "stall_recovery_skipped_count": 1,
                    "stall_recovery_failure_count": 1,
                }
            ],
            "crashes": [],
            "statistics": {
                "stall_recovery_requested_count": 3,
                "stall_recovery_count": 2,
                "stall_recovery_skipped_count": 1,
                "stall_recovery_failure_count": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = ReportGenerator.write_html(
                report, Path(temporary) / "report.html"
            )
            content = path.read_text(encoding="utf-8")

        self.assertIn("Fallback requests</span><strong>3", content)
        self.assertIn("Fallback attempts</span><strong>2", content)
        self.assertIn("Recovery skipped</span><strong>1", content)
        self.assertIn("Recovery failures</span><strong>1", content)
        self.assertIn("<th>Stall recovery</th>", content)
        self.assertIn(
            "<td>3 requested / 2 attempted / 1 skipped / 1 failed</td>",
            content,
        )

    def test_html_derives_requested_recovery_count_for_legacy_reports(self):
        report = {
            "cycles": [
                {
                    "cycle": 1,
                    "stall_recovery_count": 2,
                    "stall_recovery_failure_count": 1,
                }
            ],
            "statistics": {
                "stall_recovery_count": 2,
                "stall_recovery_failure_count": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = ReportGenerator.write_html(
                report, Path(temporary) / "report.html"
            )
            content = path.read_text(encoding="utf-8")

        self.assertIn("Fallback requests</span><strong>2", content)
        self.assertIn(
            "<td>2 requested / 2 attempted / 0 skipped / 1 failed</td>",
            content,
        )

    def test_run_summary_writes_device_coverage_matrix(self):
        summary = {
            "run_id": "run-1",
            "package_name": "com.example.app",
            "status": "completed_with_gaps",
            "expected_device_count": 3,
            "coverage_counts": {
                "complete": 1,
                "missing": 1,
                "zero_events": 1,
            },
            "devices": [
                {
                    "device_serial": "online",
                    "model": "Vendor Phone",
                    "adb_state": "device",
                    "ready": True,
                    "execution_status": "completed",
                    "coverage_status": "complete",
                    "operation_count": 10,
                    "pressure_elapsed_seconds": 60,
                    "duration_ratio": 1,
                    "report_dir": "online/report.html",
                },
                {
                    "device_serial": "missing",
                    "adb_state": "missing",
                    "coverage_status": "missing",
                    "error": "not listed",
                },
                {
                    "device_serial": "zero",
                    "adb_state": "device",
                    "coverage_status": "zero_events",
                    "error": "no events",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            paths = ReportGenerator.write_run_summary(summary, Path(temporary))
            self.assertTrue(Path(paths["json"]).is_file())
            self.assertTrue(Path(paths["html"]).is_file())
            content = Path(paths["html"]).read_text(encoding="utf-8")

        self.assertIn("Device Coverage", content)
        self.assertIn("missing", content)
        self.assertIn("zero_events", content)


if __name__ == "__main__":
    unittest.main()
