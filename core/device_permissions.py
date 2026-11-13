"""Grant runtime and special permissions without touching the UI.

A stability run starts from a fully-permissioned app: an unanswered system
prompt blocks random input, and a single accidental "deny" can hide dozens of
feature branches for the rest of the cycle.  Installation already passes ``-g``
to ``pm install``; this module covers what ``-g`` cannot:

* an explicit runtime-permission list (``pm grant``), for permissions that were
  added later, revoked by hand, or never granted because the APK targetSdk
  predates the automatic grant;
* appops-only switches such as ``SYSTEM_ALERT_WINDOW`` or
  ``MANAGE_EXTERNAL_STORAGE``;
* the OEM package-verifier settings that block unattended installs.

Every step is recorded so a silently failed grant cannot pass for coverage.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

APPOP_MODES = frozenset({"allow", "deny", "ignore", "default"})


def _run(run_adb: Any, arguments: Sequence[str], timeout: int = 10) -> Any:
    try:
        return run_adb(list(arguments), timeout=timeout)
    except Exception:  # pragma: no cover - adapter specific failures
        return None


def _output(result: Any) -> str:
    if result is None:
        return ""
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return ("%s\n%s" % (stdout, stderr)).strip()


def _succeeded(result: Any, output: str) -> bool:
    if result is None:
        return False
    if int(getattr(result, "returncode", 1) or 0) != 0:
        return False
    lowered = output.lower()
    return not any(
        marker in lowered
        for marker in ("error:", "exception occurred", "not a changeable permission")
    )


def grant_runtime_permissions(
    run_adb: Any, package_name: str, permissions: Iterable[str]
) -> Dict[str, Any]:
    """Grant each permission with ``pm grant`` and report per-item outcome."""
    requested: List[str] = []
    granted: List[str] = []
    failed: List[Dict[str, str]] = []
    for raw in permissions or ():
        permission = str(raw).strip()
        if not permission or permission in requested:
            continue
        requested.append(permission)
        result = _run(
            run_adb, ["shell", "pm", "grant", package_name, permission]
        )
        output = _output(result)
        if _succeeded(result, output):
            granted.append(permission)
        else:
            failed.append(
                {"permission": permission, "error": output[:300] or "no adb result"}
            )
    return {"requested": requested, "granted": granted, "failed": failed}


def apply_appops(
    run_adb: Any, package_name: str, appops: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Apply appops switches (special permissions) with validation."""
    applied: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []
    for raw_op, raw_mode in (appops or {}).items():
        op = str(raw_op).strip()
        mode = str(raw_mode).strip().lower()
        if not op:
            continue
        if mode not in APPOP_MODES:
            failed.append(
                {
                    "op": op,
                    "mode": mode,
                    "error": "invalid mode, expected one of %s"
                    % ", ".join(sorted(APPOP_MODES)),
                }
            )
            continue
        result = _run(
            run_adb, ["shell", "appops", "set", package_name, op, mode]
        )
        output = _output(result)
        if _succeeded(result, output):
            applied.append({"op": op, "mode": mode})
        else:
            failed.append(
                {"op": op, "mode": mode, "error": output[:300] or "no adb result"}
            )
    return {"applied": applied, "failed": failed}


def disable_package_verifier(run_adb: Any) -> Dict[str, Any]:
    """Turn off the install verifier where the ROM allows it."""
    settings = (
        ("global", "verifier_verify_adb_installs", "0"),
        ("global", "package_verifier_enable", "0"),
        ("secure", "install_non_market_apps", "1"),
    )
    applied: List[str] = []
    failed: List[Dict[str, str]] = []
    for namespace, key, value in settings:
        result = _run(run_adb, ["shell", "settings", "put", namespace, key, value])
        output = _output(result)
        if _succeeded(result, output):
            applied.append("%s/%s=%s" % (namespace, key, value))
        else:
            failed.append(
                {"setting": "%s/%s" % (namespace, key), "error": output[:300]}
            )
    return {"applied": applied, "failed": failed}


def apply_permission_setup(
    run_adb: Any,
    package_name: str,
    *,
    grant_permissions: Optional[Iterable[str]] = None,
    appops: Optional[Mapping[str, Any]] = None,
    disable_verifier: bool = False,
) -> Dict[str, Any]:
    """Apply the whole device permission profile and return an audit record."""
    report: Dict[str, Any] = {
        "package": package_name,
        "grant": {"requested": [], "granted": [], "failed": []},
        "appops": {"applied": [], "failed": []},
        "verifier": None,
    }
    if grant_permissions:
        report["grant"] = grant_runtime_permissions(
            run_adb, package_name, grant_permissions
        )
    if appops:
        report["appops"] = apply_appops(run_adb, package_name, appops)
    if disable_verifier:
        report["verifier"] = disable_package_verifier(run_adb)
    report["success"] = not report["grant"]["failed"] and not report["appops"][
        "failed"
    ]
    return report
