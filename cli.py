"""Command-line interface for local Android APK pressure testing."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

try:
    from .core.configuration import (
        ConfigurationError,
        load_config,
        require_local_apk,
        resolve_project_path,
    )
    from .core.device_manager import DeviceManager
    from .core.fastbot_assets import (
        first_supported_abi,
        native_asset_path,
        native_remote_path,
        runtime_bundle_has_native,
        runtime_asset_path,
        runtime_remote_path,
        LEGACY_RUNTIME_REMOTE_PATHS,
    )
    from .core.mutex_controller import MutexController
    from .core.reporting import configure_logging
    from .main import (
        prepare_device,
        run_fastbot_only,
        run_from_config,
        select_devices,
    )
except ImportError:  # pragma: no cover - exercised by ``python cli.py``
    from core.configuration import (  # type: ignore
        ConfigurationError,
        load_config,
        require_local_apk,
        resolve_project_path,
    )
    from core.device_manager import DeviceManager  # type: ignore
    from core.fastbot_assets import (  # type: ignore
        first_supported_abi,
        native_asset_path,
        native_remote_path,
        runtime_bundle_has_native,
        runtime_asset_path,
        runtime_remote_path,
        LEGACY_RUNTIME_REMOTE_PATHS,
    )
    from core.mutex_controller import MutexController  # type: ignore
    from core.reporting import configure_logging  # type: ignore
    from main import (  # type: ignore
        prepare_device,
        run_fastbot_only,
        run_from_config,
        select_devices,
    )


def _device_argument(value: Optional[str]) -> Optional[Iterable[str]]:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app_press_test",
        description=(
            "Run stability pressure tests for a local APK on Android devices "
            "already attached to the local ADB server."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("devices", help="list devices visible to local ADB")

    run_parser = subparsers.add_parser("run", help="run all configured test cycles")
    run_parser.add_argument("--config", default=None, help="settings JSON path")
    run_parser.add_argument(
        "--devices", default=None, help="comma-separated ADB serials (overrides config)"
    )
    run_parser.add_argument(
        "--run-id", default=None, help="resume an existing job/run id in place"
    )

    prepare_parser = subparsers.add_parser(
        "prepare", help="install the local APK and run the preparation scenario"
    )
    prepare_parser.add_argument("--config", default=None, help="settings JSON path")
    prepare_parser.add_argument("--device", required=True, help="ADB serial")

    fastbot_parser = subparsers.add_parser(
        "fastbot", help="run Fastbot against an already-prepared application"
    )
    fastbot_parser.add_argument("--config", default=None, help="settings JSON path")
    fastbot_parser.add_argument("--device", required=True, help="ADB serial")
    fastbot_parser.add_argument(
        "--duration", type=int, default=None, help="override duration in minutes"
    )

    stop_parser = subparsers.add_parser(
        "stop", help="stop and verify any Fastbot or system Monkey on selected devices"
    )
    stop_parser.add_argument(
        "--devices", required=True, help="comma-separated connected ADB serials"
    )

    sync_parser = subparsers.add_parser(
        "sync-fastbot", help="push and verify the single bundled Fastbot runtime APK"
    )
    sync_parser.add_argument(
        "--devices", required=True, help="comma-separated connected ADB serials"
    )

    report_parser = subparsers.add_parser("report", help="print a local test report summary")
    report_parser.add_argument("--config", default=None, help="settings JSON path")
    report_parser.add_argument("--latest", action="store_true", help="show the newest report")
    report_parser.add_argument("--device", default=None, help="filter by ADB serial")
    report_parser.add_argument("--json", action="store_true", help="print the full JSON report")

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="apply the run/upload retention policy to the local disk"
    )
    cleanup_parser.add_argument("--config", default=None, help="settings JSON path")
    cleanup_parser.add_argument(
        "--root", default=None, help="runtime root that holds runs/ and uploads/"
    )
    cleanup_parser.add_argument(
        "--max-age-days", type=int, default=None, help="override retention.max_age_days"
    )
    cleanup_parser.add_argument(
        "--max-total-gb", type=float, default=None, help="override retention.max_total_gb"
    )
    cleanup_parser.add_argument(
        "--uploads-max-age-days",
        type=int,
        default=None,
        help="override retention.uploads_max_age_days",
    )
    cleanup_parser.add_argument(
        "--keep-newest",
        type=int,
        default=5,
        help="always keep this many newest runs (default 5)",
    )
    cleanup_parser.add_argument(
        "--dry-run", action="store_true", help="print the plan without deleting"
    )
    return parser


def _cmd_devices() -> int:
    devices = DeviceManager.get_connected_devices()
    if not devices:
        print("No authorized Android devices are visible to local ADB.")
        return 1
    rows = []
    for serial in devices:
        try:
            info = DeviceManager.get_device_info(serial)
        except Exception as exc:
            info = {"serial": serial, "error": str(exc)}
        info["ready"] = DeviceManager.is_device_ready(serial)
        rows.append(info)
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def _load_for_command(path: Optional[str]) -> Dict[str, Any]:
    config = load_config(path)
    log_config = config.get("logging", {})
    configure_logging(
        str(resolve_project_path(str(log_config.get("log_dir", "./logs")))),
        str(log_config.get("level", "INFO")),
        "cli.log",
    )
    return config


def _cmd_run(args: argparse.Namespace) -> int:
    requested = _device_argument(args.devices)
    code, results = run_from_config(args.config, requested, run_id=args.run_id)
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return code


def _cmd_prepare(args: argparse.Namespace) -> int:
    config = _load_for_command(args.config)
    require_local_apk(config)
    selected = select_devices(config, [args.device])
    result = prepare_device(selected[0], config)
    snapshot = result.get("snapshot", {})
    summary = {
        "device_serial": result.get("device_serial"),
        "scenario": result.get("scenario"),
        "snapshot": {
            "name": snapshot.get("name"),
            "app_current": snapshot.get("app_current"),
            "created_at": snapshot.get("created_at"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_fastbot(args: argparse.Namespace) -> int:
    config = _load_for_command(args.config)
    require_local_apk(config)
    selected = select_devices(config, [args.device])
    result = run_fastbot_only(selected[0], config, args.duration)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    requested = list(_device_argument(args.devices) or [])
    connected = set(DeviceManager.get_connected_devices())
    results = []
    for serial in requested:
        if serial not in connected:
            results.append(
                {
                    "device_serial": serial,
                    "stopped": False,
                    "error": "device is not connected and authorized",
                }
            )
            continue
        stopped = MutexController(serial).request_stop_remote_fastbot()
        results.append({"device_serial": serial, "stopped": stopped})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(item["stopped"] for item in results) else 1


def _safe_fastbot_files() -> Sequence[Tuple[Path, str]]:
    """Return the single bundled Fastbot runtime APK and its device destination."""
    files = ((runtime_asset_path(), runtime_remote_path()),)
    missing = [str(source) for source, _remote_path in files if not source.is_file()]
    if missing:
        raise RuntimeError("Fastbot runtime bundle is missing: %s" % ", ".join(missing))
    return files


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _device_abi(adb_path: str, serial: str) -> Optional[str]:
    """Read the ABI Fastbot uses to resolve its native library directory."""
    for property_name in ("ro.product.cpu.abilist", "ro.product.cpu.abi"):
        try:
            result = subprocess.run(
                [adb_path, "-s", serial, "shell", "getprop", property_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            abi = first_supported_abi([result.stdout or ""])
            if abi:
                return abi
    return None


def _cleanup_legacy_fastbot_files(
    adb_path: str, serial: str, abi: Optional[str] = None
) -> Dict[str, Any]:
    """Remove obsolete split runtime files after the bundled runtime is verified."""
    removed = []
    errors = []
    remote_paths = list(LEGACY_RUNTIME_REMOTE_PATHS)
    if abi:
        remote_paths.append(native_remote_path(abi))
    for remote_path in remote_paths:
        try:
            result = subprocess.run(
                [adb_path, "-s", serial, "shell", "rm", "-f", remote_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append("%s: %s" % (remote_path, exc))
            continue
        if result.returncode == 0:
            removed.append(remote_path)
        else:
            errors.append(
                "%s: %s"
                % (
                    remote_path,
                    (result.stderr or result.stdout or "rm failed").strip(),
                )
            )
    return {"removed": removed, "errors": errors}


def _cmd_sync_fastbot(args: argparse.Namespace) -> int:
    requested = list(_device_argument(args.devices) or [])
    connected = set(DeviceManager.get_connected_devices())
    runtime_dependencies = [
        (source, remote_path, _md5(source))
        for source, remote_path in _safe_fastbot_files()
    ]
    adb_path = os.environ.get("ADB_PATH", "adb")
    results = []
    for serial in requested:
        if serial not in connected:
            results.append(
                {
                    "device_serial": serial,
                    "synced": False,
                    "error": "device is not connected and authorized",
                }
            )
            continue
        controller = MutexController(serial)
        processes = controller._remote_processes()
        if processes is None:
            results.append(
                {
                    "device_serial": serial,
                    "synced": False,
                    "error": "unable to verify device-side Fastbot state",
                }
            )
            continue
        if processes:
            results.append(
                {
                    "device_serial": serial,
                    "synced": False,
                    "error": "Fastbot is running; stop it before replacing runtime files",
                }
            )
            continue
        abi = _device_abi(adb_path, serial)
        if not abi:
            results.append(
                {
                    "device_serial": serial,
                    "synced": False,
                    "error": "unable to determine device ABI for Fastbot native library",
                }
            )
            continue
        if not runtime_bundle_has_native(abi):
            results.append(
                {
                    "device_serial": serial,
                    "synced": False,
                    "error": "Fastbot runtime bundle has no native library for ABI %s"
                    % abi,
                }
            )
            continue
        dependencies = list(runtime_dependencies)
        file_results = []
        for source, remote_path, expected_md5 in dependencies:
            file_result: Dict[str, Any] = {
                "name": Path(remote_path).name,
                "remote_path": remote_path,
                "synced": False,
                "expected_md5": expected_md5,
            }
            try:
                if remote_path.startswith("/data/local/tmp/"):
                    parent = str(Path(remote_path).parent).replace("\\", "/")
                    prepared = subprocess.run(
                        [adb_path, "-s", serial, "shell", "mkdir", "-p", parent],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=15,
                        check=False,
                    )
                    if prepared.returncode != 0:
                        file_result["error"] = (
                            prepared.stderr
                            or prepared.stdout
                            or "unable to create Fastbot runtime directory"
                        ).strip()
                        file_results.append(file_result)
                        continue
                pushed = subprocess.run(
                    [adb_path, "-s", serial, "push", str(source), remote_path],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
                if pushed.returncode != 0:
                    file_result["error"] = (
                        pushed.stderr or pushed.stdout or "ADB push failed"
                    ).strip()
                    file_results.append(file_result)
                    continue
                verified = subprocess.run(
                    [adb_path, "-s", serial, "shell", "md5sum", remote_path],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                file_result["error"] = str(exc)
                file_results.append(file_result)
                continue
            actual = (verified.stdout or "").strip().split()
            actual_md5 = actual[0].lower() if actual else ""
            file_result["actual_md5"] = actual_md5
            if verified.returncode == 0 and actual_md5 == expected_md5:
                file_result["synced"] = True
            else:
                file_result["error"] = (
                    verified.stderr
                    or verified.stdout
                    or "checksum verification failed (got %s)" % actual_md5
                ).strip()
            file_results.append(file_result)

        synced = bool(file_results) and all(item["synced"] for item in file_results)
        result: Dict[str, Any] = {
            "device_serial": serial,
            "synced": synced,
            "files": file_results,
        }
        if synced:
            result["md5"] = dependencies[0][2]
            result["native_abi"] = abi
            cleanup = _cleanup_legacy_fastbot_files(adb_path, serial, abi)
            result["legacy_removed"] = cleanup["removed"]
            if cleanup["errors"]:
                # The new runtime is already usable. Keep cleanup failures
                # visible without turning a successful sync into a launch
                # blocker.
                result["legacy_cleanup_errors"] = cleanup["errors"]
        else:
            failed_files = [item["name"] for item in file_results if not item["synced"]]
            result["error"] = "failed to sync: %s" % ", ".join(failed_files)
        results.append(result)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(item["synced"] for item in results) else 1


def _report_root(config_path: Optional[str]) -> Path:
    if config_path:
        config = load_config(config_path)
        paths = config.get("paths", {})
        return resolve_project_path(str(paths.get("reports_dir", "./reports")))
    return resolve_project_path("./reports")


def _find_report(root: Path, serial: Optional[str]) -> Optional[Path]:
    candidates = []
    for path in root.glob("*/report.json"):
        if serial:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if report.get("device_serial") != serial:
                continue
        candidates.append(path)
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _cmd_report(args: argparse.Namespace) -> int:
    root = _report_root(args.config)
    report_path = _find_report(root, args.device)
    if report_path is None:
        print("No local report was found under %s." % root)
        return 1
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("Unable to read report: %s" % exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        summary = {
            "report": str(report_path),
            "device_serial": report.get("device_serial"),
            "package_name": report.get("package_name"),
            "status": report.get("status"),
            "statistics": report.get("statistics", {}),
            "crashes": [crash.get("signature") for crash in report.get("crashes", [])],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _resolve_runtime_root(explicit: Optional[str]) -> Path:
    """Locate the runtime tree that holds ``runs/`` and ``uploads/``."""
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    if os.environ.get("APP_PRESS_TEST_OUTPUT_ROOT"):
        return resolve_project_path("./frontend/runtime")
    local = resolve_project_path("./frontend/runtime-local")
    if (local / "runs").is_dir():
        return local
    return resolve_project_path("./frontend/runtime")


def _retention_rules(config_path: Optional[str]) -> Dict[str, Any]:
    rules: Dict[str, Any] = {
        "enabled": True,
        "max_age_days": 14,
        "max_total_gb": 60.0,
        "uploads_max_age_days": 30,
        "keep_crash_runs": True,
    }
    if not config_path:
        return rules
    loaded = load_config(config_path)
    section = loaded.get("retention")
    if isinstance(section, dict):
        for key in list(rules):
            if key in section:
                rules[key] = section[key]
    return rules


def _referenced_apks(run_dirs: Sequence[Path]) -> set:
    """Collect APK paths still referenced by the runs we are keeping."""
    referenced = set()
    for directory in run_dirs:
        config_path = directory / "config.json"
        if not config_path.is_file():
            continue
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        candidates = []
        app = value.get("app")
        if isinstance(app, dict):
            candidates.append(app.get("apk_path"))
        candidates.append(value.get("apkPath"))
        candidates.append(value.get("apk_path"))
        for candidate in candidates:
            if candidate:
                referenced.add(str(Path(str(candidate)).resolve(strict=False)))
    return referenced


def _cmd_cleanup(args: argparse.Namespace) -> int:
    try:
        from .core.retention import (
            apply_cleanup,
            list_run_directories,
            plan_run_cleanup,
            plan_upload_cleanup,
        )
    except ImportError:  # pragma: no cover - exercised by ``python cli.py``
        from core.retention import (  # type: ignore
            apply_cleanup,
            list_run_directories,
            plan_run_cleanup,
            plan_upload_cleanup,
        )

    rules = _retention_rules(args.config)
    if args.max_age_days is not None:
        rules["max_age_days"] = args.max_age_days
    if args.max_total_gb is not None:
        rules["max_total_gb"] = args.max_total_gb
    if args.uploads_max_age_days is not None:
        rules["uploads_max_age_days"] = args.uploads_max_age_days

    root = _resolve_runtime_root(args.root)
    runs_root = root / "runs"
    uploads_root = root / "uploads"
    if not rules.get("enabled", True):
        print("Retention is disabled in the configuration; nothing to do.")
        return 0
    run_dirs = list_run_directories(runs_root)
    if not run_dirs and not uploads_root.is_dir():
        print("Nothing to clean under %s." % root)
        return 0

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    protected = [
        path.name
        for path in sorted(run_dirs, key=_mtime, reverse=True)[
            : max(0, int(args.keep_newest))
        ]
    ]
    run_plan = plan_run_cleanup(
        runs_root,
        protected_ids=protected,
        keep_crash_runs=bool(rules.get("keep_crash_runs", True)),
        max_age_days=int(rules.get("max_age_days", 14)),
        max_total_gb=float(rules.get("max_total_gb", 60.0)),
    )
    remaining = [path for path in run_dirs if path not in set(run_plan)]
    upload_plan = plan_upload_cleanup(
        uploads_root,
        keep_paths=_referenced_apks(remaining),
        max_age_days=int(rules.get("uploads_max_age_days", 30)),
    )

    print("Runtime root : %s" % root)
    print("Rules        : max_age_days=%s max_total_gb=%s uploads_max_age_days=%s keep_crash_runs=%s"
          % (
              rules.get("max_age_days"),
              rules.get("max_total_gb"),
              rules.get("uploads_max_age_days"),
              rules.get("keep_crash_runs"),
          ))
    print("Keep newest  : %d (%s)" % (len(protected), ", ".join(protected) or "-"))
    print("Runs to delete    : %d" % len(run_plan))
    for path in run_plan:
        print("  - %s" % path)
    print("Uploads to delete : %d" % len(upload_plan))
    for path in upload_plan:
        print("  - %s" % path)

    if args.dry_run:
        print("Dry run: nothing was deleted.")
        return 0
    removed = apply_cleanup(list(run_plan) + list(upload_plan))
    print("Deleted %d item(s)." % removed)
    expected = len(run_plan) + len(upload_plan)
    if expected and removed == 0:
        # On this host the project virtualenv is denied write access to the
        # output drive; the console and workers already run under the system
        # interpreter for the same reason.
        print(
            "Nothing could be removed. If the output drive is protected by "
            "endpoint security, rerun this command with the system Python "
            "(for example: %s)." % sys.executable.replace(".venv\\Scripts\\", "")
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and return a shell-friendly exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "devices":
            return _cmd_devices()
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "prepare":
            return _cmd_prepare(args)
        if args.command == "fastbot":
            return _cmd_fastbot(args)
        if args.command == "stop":
            return _cmd_stop(args)
        if args.command == "sync-fastbot":
            return _cmd_sync_fastbot(args)
        if args.command == "report":
            return _cmd_report(args)
        if args.command == "cleanup":
            return _cmd_cleanup(args)
    except (ConfigurationError, RuntimeError, ValueError, OSError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised manually
    raise SystemExit(main())
