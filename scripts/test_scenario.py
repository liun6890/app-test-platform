"""User-editable preparation scenario for login and page navigation."""

from __future__ import annotations

import logging
import os
import copy
from typing import Any, Dict, List, Optional

try:
    from ..core.state_manager import StateManager
except ImportError:  # pragma: no cover - direct ``python cli.py`` execution
    from core.state_manager import StateManager  # type: ignore


logger = logging.getLogger(__name__)


def get_login_locators(
    package_name: str, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Return configured locators or the documented example locators."""
    scenario = config.get("scenario", {})
    configured = scenario.get("login_locators") if isinstance(scenario, dict) else None
    if isinstance(configured, dict):
        return configured
    return {
        "username": {"resourceId": "%s:id/username_field" % package_name},
        "password": {"resourceId": "%s:id/password_field" % package_name},
        "login_button": {"resourceId": "%s:id/login_button" % package_name},
    }


def get_navigation_steps(
    config: Dict[str, Any], device_serial: str = ""
) -> List[Dict[str, Any]]:
    """Return device-specific preparation followed by shared navigation."""
    return _get_steps(config, "navigation_steps", device_serial)


def get_pre_login_steps(
    config: Dict[str, Any], device_serial: str = ""
) -> List[Dict[str, Any]]:
    """Return startup interactions that must finish before account entry."""
    return _get_steps(config, "pre_login_steps", device_serial)


def _get_steps(
    config: Dict[str, Any], step_key: str, device_serial: str = ""
) -> List[Dict[str, Any]]:
    """Return device-specific steps followed by the shared steps for a phase."""
    scenario = config.get("scenario", {})
    steps = scenario.get(step_key, []) if isinstance(scenario, dict) else []
    if not isinstance(steps, list):
        raise ValueError("scenario.%s must be a JSON array" % step_key)
    per_device_key = "%s_by_device" % step_key
    per_device = (
        scenario.get(per_device_key, {})
        if isinstance(scenario, dict)
        else {}
    )
    device_steps = per_device.get(device_serial, []) if isinstance(per_device, dict) else []
    if not isinstance(device_steps, list):
        raise ValueError(
            "scenario.%s entries must be JSON arrays" % per_device_key
        )
    return [
        step
        for step in [*device_steps, *steps]
        if isinstance(step, dict)
    ]


def resolve_input_steps(
    config: Dict[str, Any], steps: List[Dict[str, Any]], cycle_number: int = 1,
) -> List[Dict[str, Any]]:
    """Resolve named input data without changing shared config or retry values."""
    resolved = copy.deepcopy(steps)
    cases = config.get("scenario", {}).get("input_cases", {})
    for step in resolved:
        if str(step.get("action", "")).lower() not in {"input", "set_text"}:
            continue
        if "data" not in step:
            continue
        case = cases[step["data"]]
        values = case if isinstance(case, list) else case["values"]
        step["text"] = values[(max(1, cycle_number) - 1) % len(values)]
    return resolved


def _prepare_once(
    state_manager: StateManager, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Run the login and navigation phases required before Fastbot starts.

    Override this function for application-specific data creation, test-account
    preparation, deep links, or conditional flows. Credentials are read from
    environment variables so they are not copied into reports.
    """
    package_name = config["app"]["package_name"]
    scenario = config.get("scenario", {})
    login_enabled = (
        bool(scenario.get("login_enabled", True))
        if isinstance(scenario, dict)
        else True
    )
    username = os.environ.get("APP_TEST_USERNAME", "testuser")
    password = os.environ.get("APP_TEST_PASSWORD", "password")
    pre_login_result = True
    login_result = True
    if login_enabled:
        pre_login_result = state_manager.navigate_to(
            "LoginPage", resolve_input_steps(
                config, get_pre_login_steps(config, state_manager.device_serial),
                getattr(state_manager, "_input_cycle", 1),
            )
        )
        login_result = pre_login_result and state_manager.login(
            username, password, get_login_locators(package_name, config)
        )

    target_page = (
        str(scenario.get("target_page", "HomePage"))
        if isinstance(scenario, dict)
        else "HomePage"
    )
    navigation_result = login_result and state_manager.navigate_to(
        target_page, resolve_input_steps(
            config, get_navigation_steps(config, state_manager.device_serial),
            getattr(state_manager, "_input_cycle", 1),
        )
    )
    result = {
        "login_enabled": login_enabled,
        "pre_login_success": pre_login_result,
        "login_success": login_result,
        "target_page": target_page,
        "navigation_success": navigation_result,
    }
    navigation_error = str(
        getattr(state_manager, "last_navigation_error", "") or ""
    ).strip()
    if not navigation_result and navigation_error:
        result["navigation_error"] = navigation_error
    logger.info("Preparation scenario result: %s", result)
    return result


def prepare_test_scenario(
    state_manager: StateManager, config: Dict[str, Any], cycle_number: Optional[int] = None,
) -> Dict[str, Any]:
    """Restore an unsafe input page and retry the same data without clearing it."""
    if cycle_number is not None:
        state_manager._input_cycle = max(1, int(cycle_number))
    configure = getattr(state_manager, "configure_input_safety", None)
    if callable(configure):
        configure(config)
    options = config.get("input_safety", {})
    max_recoveries = int(options.get("recovery_attempts", 3)) if options.get("enabled") else 0
    if config.get("_input_recovery_managed"):
        max_recoveries = 0
    recoveries = []
    result = _prepare_once(state_manager, config)
    while getattr(state_manager, "input_recovery_required", False) and len(recoveries) < max_recoveries:
        recovery = {"attempt": len(recoveries) + 1, "success": False}
        recoveries.append(recovery)
        try:
            if not state_manager.reset_app(clear_data=False, clear_task=True):
                raise RuntimeError("input recovery relaunch failed")
            state_manager.input_recovery_required = False
            # A caller may have removed duplicate audit navigation steps; a
            # clean relaunch still needs the idempotent startup gate check.
            audit = config.get("scenario", {}).get("audit_recovery", {})
            recover_gate = getattr(state_manager, "recover_audit_gate", None)
            if audit.get("enabled") and callable(recover_gate) and not recover_gate(
                {"resourceId": audit["audit_resource_id"]},
                {"text": audit["confirm_text"]},
                timeout=20.0, audit_activity=audit.get("activity", ""),
            ):
                raise RuntimeError("input recovery startup gate failed")
            result = _prepare_once(state_manager, config)
            recovery["success"] = bool(result["login_success"] and result["navigation_success"])
            if recovery["success"]:
                break
            # Restoration failure is still part of this bounded transaction.
            state_manager.input_recovery_required = True
        except InterruptedError:
            raise
        except Exception as exc:
            state_manager.input_recovery_required = True
            recovery["error"] = str(exc)
            result["navigation_success"] = False
            result["navigation_error"] = str(exc)
        logger.warning(
            "Input page recovery attempt %s/%s on %s", len(recoveries),
            max_recoveries, state_manager.device_serial,
        )
    if recoveries:
        result["input_recoveries"] = recoveries
        result["input_recovery_count"] = len(recoveries)
        result["continued_after_input_recovery"] = bool(recoveries[-1]["success"])
    if getattr(state_manager, "input_recovery_required", False):
        result["input_recovery_exhausted"] = True
        result["navigation_success"] = False
    return result
