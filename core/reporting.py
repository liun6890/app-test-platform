"""Unified logging and JSON/HTML report generation."""

from __future__ import annotations

import html
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from core.file_io import atomic_write_text


def describe_permission_failure(cycle: Mapping[str, Any]) -> str:
    """Describe a failed permission handoff using only recorded evidence."""
    if cycle.get("status") != "failed":
        return ""
    prefix = "runtime permission recovery failed:"

    def detail(value: Any) -> str:
        text = str(value or "").strip()
        return text[len(prefix):].strip() if text.lower().startswith(prefix) else text

    raw = detail(cycle.get("permission_recovery_error"))
    errors = cycle.get("errors")
    if not raw and isinstance(errors, list):
        raw = next((detail(item) for item in errors if str(item).lower().startswith(prefix)), "")
    if not raw and not cycle.get("permission_recovery_failed"):
        return ""

    normalized = raw.lower()
    if re.search(r"\bdevice(?: ['\"][^'\"]+['\"])? (?:not found|(?:is )?offline)\b", normalized) or (
        "no devices/emulators found" in normalized
    ):
        return "权限处理期间设备断连：ADB 无法访问设备，压测未能继续。"

    history = cycle.get("permission_recoveries")
    last = history[-1] if isinstance(history, list) and history else None
    # Use observations from the handoff that failed, not an earlier successful
    # attempt (for example, when the repeated-prompt guard stopped the cycle).
    recovery = last if (
        isinstance(last, Mapping)
        and last.get("success") is False
        and detail(last.get("error")) == raw
    ) else {}
    package, _, activity = str(recovery.get("last_component") or "").partition("/")
    if normalized in {
        "permission recovery timed out",
        "permission dialog remained visible after the positive tap",
        "permission recovery did not reach a stable target page",
    } and package == "com.android.settings" and activity.removeprefix(package).lstrip(".") == (
        "Settings$AppManageExternalStorageActivity"
    ):
        return "权限处理超时：设备停在系统的“所有文件访问权限”设置页，未返回被测 App。"

    if normalized == "positive permission button was not found":
        attempts = recovery.get("attempts")
        attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
        if isinstance(attempt, Mapping) and attempt.get("ui_dump_succeeded") is False:
            return "权限弹窗处理失败：无法读取弹窗控件，不能定位“允许”按钮。"
        return "权限弹窗处理失败：未找到可点击的“允许”按钮。"

    return {
        "adb tap of the positive permission button failed": "权限弹窗处理失败：未能发送“允许”点击。",
        "permission dialog remained visible after the positive tap": "权限弹窗处理失败：发送“允许”点击后，未确认被测 App 回到前台。",
        "permission dialog reappeared repeatedly without pressure progress": "权限弹窗反复出现，连续恢复未能让压测继续。",
        "permission recovery timed out": "权限处理超时：未确认被测 App 回到前台。",
        "permission recovery did not reach a stable target page": "权限处理失败：未确认被测 App 稳定回到前台。",
        "permission recovery attempt limit reached": "权限处理失败：达到本次弹窗的处理次数上限。",
        "permission activity disappeared before its button rendered": "权限处理失败：弹窗消失前没有出现可操作的按钮。",
        "permission activity disappeared before its positive button could be clicked": "权限处理失败：弹窗在点击“允许”前消失。",
        "permission recovery was cancelled": "权限处理已取消，压测未能继续。",
        "permission dialog could not be handled": "权限处理失败：报告未记录具体卡点。",
    }.get(normalized, f"权限处理失败：{raw}" if raw else "权限处理失败：报告未记录具体卡点。")


def configure_logging(
    log_dir: str,
    level: str = "INFO",
    file_name: str = "app_press_test.log",
) -> Path:
    """Configure root logging to both console and a UTF-8 file."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / file_name
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(processName)s | %(name)s | %(message)s"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(numeric_level)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)
    root.addHandler(console)
    root.addHandler(file_handler)
    return log_file


def close_logging_handlers() -> None:
    """Flush, detach, and close handlers owned by this process."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.flush()
        except Exception:
            pass
        finally:
            handler.close()


class ReportGenerator:
    """Persist a structured report and a standalone visual HTML summary."""

    @staticmethod
    def _stall_recovery_counts(values: Dict[str, Any]) -> Dict[str, Any]:
        """Return recovery counters, deriving requests for legacy reports."""
        attempted = values.get("stall_recovery_count", 0)
        skipped = values.get("stall_recovery_skipped_count", 0)
        requested = values.get("stall_recovery_requested_count")
        if requested is None:
            try:
                requested = attempted + skipped
            except TypeError:
                requested = attempted
        return {
            "requested": requested,
            "attempted": attempted,
            "skipped": skipped,
            "failed": values.get("stall_recovery_failure_count", 0),
        }

    @staticmethod
    def _atomic_text_write(path: Path, content: str) -> None:
        atomic_write_text(path, content)

    @classmethod
    def write_json(cls, report: Dict[str, Any], path: Path) -> Path:
        """Write an indented UTF-8 JSON report atomically."""
        cls._atomic_text_write(
            path,
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
        )
        return path

    @classmethod
    def write_artifact_manifest(cls, report_dir: Path) -> Path:
        """Hash stable run artifacts so stale or replaced evidence is visible."""
        directory = Path(report_dir)
        entries = []
        excluded = {
            "artifact_manifest.json",
            "report.json",
            "report.html",
        }
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name in excluded or path.suffix == ".tmp":
                continue
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": str(path.relative_to(directory)),
                    "size": stat.st_size,
                    "sha256": digest.hexdigest(),
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                }
            )
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "artifact_count": len(entries),
            "artifacts": entries,
        }
        path = directory / "artifact_manifest.json"
        cls.write_json(manifest, path)
        return path

    @staticmethod
    def _cycle_rows(cycles: Iterable[Dict[str, Any]]) -> str:
        rows = []
        for cycle in cycles:
            errors = cycle.get("error")
            if not errors and isinstance(cycle.get("errors"), list):
                errors = "; ".join(str(item) for item in cycle["errors"])
            permission_failure = describe_permission_failure(cycle)
            if permission_failure:
                errors = "；".join(filter(None, (permission_failure, str(errors or ""))))
            recovery = ReportGenerator._stall_recovery_counts(cycle)
            rows.append(
                "<tr><td>{cycle}</td><td>{status}</td><td>{operations}</td>"
                "<td>{stalls}</td>"
                "<td>{stall_recovery}</td>"
                "<td>{permission_dialogs}</td><td>{audit_recoveries}</td>"
                "<td>{restores}</td>"
                "<td>{crash}</td><td>{error}</td></tr>".format(
                    cycle=html.escape(str(cycle.get("cycle"))),
                    status=html.escape(str(cycle.get("status", "unknown"))),
                    operations=html.escape(str(cycle.get("operation_count", 0))),
                    stalls=html.escape(
                        str(cycle.get("stall_detection_count", 0))
                    ),
                    stall_recovery=html.escape(
                        "{requested} requested / {attempted} attempted / "
                        "{skipped} skipped / {failed} failed".format(
                            **recovery
                        )
                    ),
                    permission_dialogs=html.escape(
                        str(cycle.get("permission_dialog_count", 0))
                    ),
                    audit_recoveries=html.escape(
                        str(cycle.get("audit_recovery_count", 0))
                    ),
                    restores=html.escape(
                        str(cycle.get("foreground_restore_count", 0))
                    ),
                    crash="yes" if cycle.get("crash_signature") else "no",
                    error=html.escape(str(errors or "")),
                )
            )
        return "".join(rows) or '<tr><td colspan="10">No cycles</td></tr>'

    @staticmethod
    def _crash_sections(crashes: Iterable[Dict[str, Any]]) -> str:
        sections = []
        for crash in crashes:
            stack = crash.get("stacktrace") or crash.get("raw_log") or ""
            page_content = str(crash.get("page_content") or "")
            copied_content = str(crash.get("copied_crash_content") or "")
            page_section = (
                "<h4>App crash page</h4><pre>{page}</pre>".format(
                    page=html.escape(page_content)
                )
                if page_content
                else ""
            )
            copied_section = (
                "<h4>Copied crash details</h4><pre>{content}</pre>".format(
                    content=html.escape(copied_content)
                )
                if copied_content
                else ""
            )
            sections.append(
                "<details><summary>{signature}</summary>"
                "<p>Type: {crash_type}; reproducible: {reproducible}</p>"
                 "<pre>{stack}</pre>{page_section}{copied_section}</details>".format(
                    signature=html.escape(str(crash.get("signature", "unknown"))),
                    crash_type=html.escape(str(crash.get("crash_type", "unknown"))),
                    reproducible=html.escape(str(crash.get("reproducible", False))),
                    stack=html.escape(str(stack)),
                    page_section=page_section,
                    copied_section=copied_section,
                )
            )
        return "".join(sections) or "<p>No target-app crash detected.</p>"

    @staticmethod
    def _issue_sections(issues: Iterable[Dict[str, Any]]) -> str:
        sections = []
        for issue in issues:
            artifacts = issue.get("artifacts", {})
            artifact_rows = ""
            if isinstance(artifacts, dict):
                artifact_rows = "".join(
                    "<li><strong>{name}</strong>: <code>{path}</code></li>".format(
                        name=html.escape(str(name)),
                        path=html.escape(str(path)),
                    )
                    for name, path in sorted(artifacts.items())
                )
            key_log = str(issue.get("key_log_excerpt") or "")
            sections.append(
                "<article class=\"issue\"><h3>{issue_id} | {title}</h3>"
                "<p><strong>Severity:</strong> {severity}; "
                "<strong>Cycle/phase:</strong> {cycle}/{phase}</p>"
                "<p>{description}</p>"
                "<p><strong>Suggested investigation:</strong> {suggestion}</p>"
                "{log}<h4>Artifacts</h4><ul>{artifacts}</ul></article>".format(
                    issue_id=html.escape(str(issue.get("issue_id", "unknown"))),
                    title=html.escape(str(issue.get("title", "unknown issue"))),
                    severity=html.escape(str(issue.get("severity", "unknown"))),
                    cycle=html.escape(str(issue.get("cycle", ""))),
                    phase=html.escape(str(issue.get("phase", ""))),
                    description=html.escape(
                        str(issue.get("problem_description", ""))
                    ),
                    suggestion=html.escape(
                        str(issue.get("suggested_investigation", ""))
                    ),
                    log=(
                        "<h4>Key log</h4><pre>%s</pre>" % html.escape(key_log)
                        if key_log
                        else ""
                    ),
                    artifacts=artifact_rows or "<li>No artifact recorded</li>",
                )
            )
        return "".join(sections) or "<p>No issue detected.</p>"

    @staticmethod
    def _stall_record_sections(records: Iterable[Dict[str, Any]]) -> str:
        sections = []
        for record in records:
            recovery = record.get("recovery", {})
            if not isinstance(recovery, Mapping):
                recovery = {}
            sections.append(
                "<article class=\"record\"><h3>{title}</h3>"
                "<p><strong>Cycle:</strong> {cycle}; "
                "<strong>Recovery success:</strong> {success}</p>"
                "<p>{description}</p></article>".format(
                    title=html.escape(
                        str(record.get("title", "Monkey fallback record"))
                    ),
                    cycle=html.escape(str(record.get("cycle", ""))),
                    success=html.escape(str(recovery.get("success", False))),
                    description=html.escape(
                        str(record.get("problem_description", ""))
                    ),
                )
            )
        return "".join(sections) or "<p>No Monkey fallback recovery was needed.</p>"

    @classmethod
    def write_html(cls, report: Dict[str, Any], path: Path) -> Path:
        """Generate a dependency-free standalone HTML report."""
        statistics = report.get("statistics", {})
        device = report.get("device_info", {})
        recovery = cls._stall_recovery_counts(statistics)
        document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Android Stability Report</title>
<style>
body {{ font: 14px/1.5 Arial, sans-serif; margin: 0; color: #202124; background: white; }}
header {{ background: #25312d; color: white; padding: 24px max(24px, calc((100% - 1100px)/2)); }}
main {{ max-width: 1100px; margin: 0 auto; padding: 8px 24px 40px; }}
section {{ border-bottom: 1px solid #dfe3e8; padding: 22px 0; overflow-x: auto; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
.metric {{ border-left: 4px solid #147d64; padding: 8px 12px; background: #f4f8f7; }}
.metric strong {{ display: block; font-size: 22px; }}
.issue {{ border: 1px solid #dfe3e8; border-left: 4px solid #b3261e; padding: 12px 16px; margin: 12px 0; }}
.issue h3 {{ margin-top: 0; }}
.record {{ border: 1px solid #dfe3e8; border-left: 4px solid #327a68; padding: 12px 16px; margin: 12px 0; background: #f4f8f7; }}
.record h3 {{ margin-top: 0; }}
table {{ width: 100%; min-width: 760px; border-collapse: collapse; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 9px; text-align: left; vertical-align: top; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #111827; color: #eef2ff; padding: 12px; overflow: auto; }}
details {{ border-top: 1px solid #e5e7eb; padding: 10px 0; }}
</style>
</head>
<body>
<header><h1>Android Stability Report</h1><div>{run_id} | {serial}</div></header>
<main>
<section><h2>Summary</h2><div class="stats">
<div class="metric"><span>Status</span><strong>{status}</strong></div>
<div class="metric"><span>Cycles</span><strong>{cycles}</strong></div>
<div class="metric"><span>Operations</span><strong>{operations}</strong></div>
<div class="metric"><span>Static-page observations</span><strong>{stalls}</strong></div>
<div class="metric"><span>Fallback requests</span><strong>{stall_recovery_requests}</strong></div>
<div class="metric"><span>Fallback attempts</span><strong>{stall_recoveries}</strong></div>
<div class="metric"><span>Recovery skipped</span><strong>{stall_recovery_skipped}</strong></div>
<div class="metric"><span>Recovery failures</span><strong>{stall_recovery_failures}</strong></div>
<div class="metric"><span>Crashes</span><strong>{crashes}</strong></div>
<div class="metric"><span>Issues</span><strong>{issues}</strong></div>
<div class="metric"><span>Coverage</span><strong>{coverage}</strong></div>
</div></section>
<section><h2>Device</h2><p>{manufacturer} {model}; Android {android}; SDK {sdk}; {screen}</p></section>
<section><h2>Cycles</h2><table><thead><tr><th>Cycle</th><th>Status</th><th>Operations</th><th>Observed stalls</th><th>Stall recovery</th><th>Permission dialogs</th><th>Audit recoveries</th><th>App restores</th><th>Crash</th><th>Error</th></tr></thead>
<tbody>{cycle_rows}</tbody></table></section>
<section><h2>Monkey fallback records (not automatically app defects)</h2>{stall_record_sections}</section>
<section><h2>Developer-ready issues</h2>{issue_sections}</section>
<section><h2>Crashes</h2>{crash_sections}</section>
</main></body></html>""".format(
            run_id=html.escape(str(report.get("run_id", "unknown"))),
            serial=html.escape(str(report.get("device_serial", "unknown"))),
            status=html.escape(str(report.get("status", "unknown"))),
            cycles=html.escape(str(statistics.get("total_cycles", 0))),
            operations=html.escape(str(statistics.get("total_operations", 0))),
            stalls=html.escape(str(statistics.get("stall_detection_count", 0))),
            stall_recovery_requests=html.escape(str(recovery["requested"])),
            stall_recoveries=html.escape(str(recovery["attempted"])),
            stall_recovery_skipped=html.escape(str(recovery["skipped"])),
            stall_recovery_failures=html.escape(str(recovery["failed"])),
            crashes=html.escape(str(statistics.get("crash_count", 0))),
            issues=html.escape(str(statistics.get("issue_count", 0))),
            coverage=html.escape(
                "N/A"
                if statistics.get("coverage_rate") is None
                else str(statistics.get("coverage_rate"))
            ),
            manufacturer=html.escape(str(device.get("manufacturer", ""))),
            model=html.escape(str(device.get("model", "unknown"))),
            android=html.escape(str(device.get("android_version", "unknown"))),
            sdk=html.escape(str(device.get("sdk", "unknown"))),
            screen=html.escape(str(device.get("screen_size", "unknown"))),
            cycle_rows=cls._cycle_rows(report.get("cycles", [])),
            stall_record_sections=cls._stall_record_sections(
                report.get("stall_records", [])
            ),
            issue_sections=cls._issue_sections(report.get("issues", [])),
            crash_sections=cls._crash_sections(report.get("crashes", [])),
        )
        cls._atomic_text_write(path, document)
        return path

    @classmethod
    def write_all(cls, report: Dict[str, Any], report_dir: Path) -> Dict[str, str]:
        """Write both mandatory report formats and return their paths."""
        json_path = cls.write_json(report, report_dir / "report.json")
        html_path = cls.write_html(report, report_dir / "report.html")
        return {"json": str(json_path), "html": str(html_path)}

    @staticmethod
    def _device_matrix_rows(devices: Iterable[Mapping[str, Any]]) -> str:
        rows = []
        for device in devices:
            error = str(device.get("error") or "")
            report_dir = str(device.get("report_dir") or "")
            rows.append(
                "<tr><td>{serial}</td><td>{model}</td><td>{adb}</td>"
                "<td>{ready}</td><td>{execution}</td><td>{coverage}</td>"
                "<td>{operations}</td><td>{elapsed}</td><td>{ratio}</td>"
                "<td><code>{report}</code></td><td>{error}</td></tr>".format(
                    serial=html.escape(str(device.get("device_serial", ""))),
                    model=html.escape(str(device.get("model", ""))),
                    adb=html.escape(str(device.get("adb_state", "missing"))),
                    ready=html.escape(str(device.get("ready", False))),
                    execution=html.escape(
                        str(device.get("execution_status", "not_run"))
                    ),
                    coverage=html.escape(
                        str(device.get("coverage_status", "unknown"))
                    ),
                    operations=html.escape(str(device.get("operation_count", 0))),
                    elapsed=html.escape(
                        str(device.get("pressure_elapsed_seconds", 0))
                    ),
                    ratio=html.escape(str(device.get("duration_ratio", 0))),
                    report=html.escape(report_dir),
                    error=html.escape(error),
                )
            )
        return "".join(rows) or '<tr><td colspan="11">No target devices</td></tr>'

    @classmethod
    def write_run_summary_html(
        cls, summary: Dict[str, Any], path: Path
    ) -> Path:
        """Write a standalone run-level device coverage matrix."""
        counts = summary.get("coverage_counts", {})
        if not isinstance(counts, Mapping):
            counts = {}
        count_text = ", ".join(
            "%s=%s" % (key, value) for key, value in sorted(counts.items())
        ) or "none"
        document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pressure Test Device Coverage</title>
<style>
body {{ font: 14px/1.5 Arial, sans-serif; margin: 0; color: #202124; background: white; }}
header {{ background: #25312d; color: white; padding: 24px; }}
main {{ padding: 20px 24px 40px; overflow-x: auto; }}
.summary {{ border-left: 4px solid #147d64; padding: 8px 12px; background: #f4f8f7; }}
table {{ width: 100%; min-width: 1180px; border-collapse: collapse; margin-top: 20px; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 9px; text-align: left; vertical-align: top; }}
code {{ overflow-wrap: anywhere; }}
</style></head><body>
<header><h1>Pressure Test Device Coverage</h1><div>{run_id} | {package}</div></header>
<main><div class="summary"><strong>Status:</strong> {status}<br>
<strong>Expected devices:</strong> {expected}; <strong>Coverage:</strong> {counts}</div>
<table><thead><tr><th>Serial</th><th>Model</th><th>ADB</th><th>Ready</th>
<th>Execution</th><th>Coverage</th><th>Operations</th><th>Pressure seconds</th>
<th>Duration ratio</th><th>Report</th><th>Error</th></tr></thead>
<tbody>{rows}</tbody></table></main></body></html>""".format(
            run_id=html.escape(str(summary.get("run_id", "unknown"))),
            package=html.escape(str(summary.get("package_name", "unknown"))),
            status=html.escape(str(summary.get("status", "unknown"))),
            expected=html.escape(str(summary.get("expected_device_count", 0))),
            counts=html.escape(count_text),
            rows=cls._device_matrix_rows(summary.get("devices", [])),
        )
        cls._atomic_text_write(path, document)
        return path

    @classmethod
    def write_run_summary(
        cls, summary: Dict[str, Any], report_dir: Path
    ) -> Dict[str, str]:
        """Write JSON and HTML run-level coverage reports."""
        directory = Path(report_dir)
        json_path = cls.write_json(summary, directory / "run_summary.json")
        html_path = cls.write_run_summary_html(
            summary, directory / "run_summary.html"
        )
        return {"json": str(json_path), "html": str(html_path)}
