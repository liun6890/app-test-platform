"""Scenario/audit-gate helpers shared by the console."""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping


def scenario_start_pages(scenario: Any) -> List[Dict[str, str]]:
    """Expose only the real initial page gates defined by a project config."""
    if not isinstance(scenario, Mapping):
        return []
    navigation = scenario.get("navigation_steps", [])
    if not isinstance(navigation, list):
        return []
    for step in navigation:
        if not isinstance(step, Mapping):
            continue
        action = str(step.get("action") or "").strip().lower()
        locator = step.get("locator") if isinstance(step.get("locator"), Mapping) else {}
        audit_locator = step.get("audit_locator") if isinstance(step.get("audit_locator"), Mapping) else {}
        text = " ".join(
            str(locator.get(key) or audit_locator.get(key) or "")
            for key in ("text", "textContains", "description", "resourceId")
        )
        if action == "complete_audit_gate" or "审核" in text:
            return [{
                "id": "audit_gate",
                "label": "审核中",
                "description": "按框架步骤勾选“审核中”并确认，再继续后续引导。",
            }]
        if action in {"launch_app", "launch"}:
            return [{
                "id": "app_home",
                "label": "应用首页",
                "description": "按框架步骤启动应用并验证前台页面。",
            }]
    return []


_AUDIT_GATE_ACTIONS = {"complete_audit_gate", "handle_audit_gate", "audit_gate"}

# Resource-id suffixes on the shared audit page.  ``audit`` (审核中) is the
# default; the other two states exist for packages that need them.
AUDIT_STATUS_LOCATORS = {
    "audit": "auditRadioButton",
    "online": "onlineRadioButton",
    "advertising": "advertisingRadioButton",
}
AUDIT_STATUS_LABELS = {
    "audit": "审核中",
    "online": "自然用户",
    "advertising": "投放用户",
}
AUDIT_CHANNEL_LOCATORS = {
    "oppo": "oppoRadioButton",
    "vivo": "vivoRadioButton",
    "xiaomi": "xiaomiRadioButton",
    "huawei": "huaweiRadioButton",
    "honor": "honorRadioButton",
    "qq": "qqRadioButton",
    "baidu": "baiduRadioButton",
    "toutiao": "toutiaoRadioButton",
    "kuaishou": "kuaishouRadioButton",
    "guangdiantong": "guangdiantongRadioButton",
}


def _audit_gate_step(
    package_name: str, channel: str = "", status: str = "audit"
) -> Dict[str, Any]:
    status_key = status if status in AUDIT_STATUS_LOCATORS else "audit"
    step: Dict[str, Any] = {
        "action": "complete_audit_gate",
        "audit_locator": {
            "resourceId": "%s:id/%s"
            % (package_name, AUDIT_STATUS_LOCATORS[status_key])
        },
        "confirm_locator": {"text": "确定"},
        "timeout": 30,
        "after": 3,
    }
    channel_key = str(channel or "").strip().lower()
    if channel_key in AUDIT_CHANNEL_LOCATORS:
        step["channel_locator"] = {
            "resourceId": "%s:id/%s"
            % (package_name, AUDIT_CHANNEL_LOCATORS[channel_key])
        }
    return step


def _step_locator_text(step: Mapping[str, Any]) -> str:
    values: List[str] = []
    for key in ("locator", "audit_locator", "confirm_locator"):
        locator = step.get(key)
        if not isinstance(locator, Mapping):
            continue
        values.extend(str(locator.get(name) or "") for name in (
            "text", "textContains", "description", "resourceId"
        ))
    return " ".join(values).lower()


def _is_audit_gate_step(step: Mapping[str, Any]) -> bool:
    action = str(step.get("action") or "").strip().lower()
    marker = _step_locator_text(step)
    return (
        action in _AUDIT_GATE_ACTIONS
        or "审核" in marker
        or "auditradiobutton" in marker
    )


def _is_gate_confirmation_step(step: Mapping[str, Any]) -> bool:
    action = str(step.get("action") or "").strip().lower()
    marker = _step_locator_text(step).strip()
    return action in {"click", "tap"} and marker in {"确定", "确认", "确"}


def _without_audit_gate_steps(steps: Any) -> List[Dict[str, Any]]:
    if not isinstance(steps, list):
        return []
    filtered: List[Dict[str, Any]] = []
    skip_confirmation = False
    for value in steps:
        if not isinstance(value, Mapping):
            continue
        step = copy.deepcopy(dict(value))
        if _is_audit_gate_step(step):
            skip_confirmation = True
            continue
        if skip_confirmation and _is_gate_confirmation_step(step):
            skip_confirmation = False
            continue
        skip_confirmation = False
        filtered.append(step)
    return filtered


def apply_audit_gate_policy(
    config: Dict[str, Any],
    package_name: str,
    enabled: bool,
    channel: str = "",
    status: str = "audit",
) -> None:
    """Apply one package-relative audit policy to matched and generic runs.

    ``channel``/``status`` come from the console form.  An empty channel keeps
    the historical behaviour: only the audit-state radio is selected.
    """
    scenario_value = config.get("scenario")
    scenario = copy.deepcopy(dict(scenario_value)) if isinstance(scenario_value, Mapping) else {}
    steps = _without_audit_gate_steps(scenario.get("navigation_steps", []))
    if enabled:
        insert_at = 0
        while insert_at < len(steps):
            action = str(steps[insert_at].get("action") or "").strip().lower()
            if action not in {"launch", "launch_app"}:
                break
            insert_at += 1
        steps.insert(insert_at, _audit_gate_step(package_name))
    scenario["navigation_steps"] = steps

    per_device = scenario.get("navigation_steps_by_device")
    if isinstance(per_device, Mapping):
        updated_by_device: Dict[str, List[Dict[str, Any]]] = {}
        for serial, device_steps in per_device.items():
            normalized = _without_audit_gate_steps(device_steps)
            if enabled:
                insert_at = 0
                while insert_at < len(normalized):
                    action = str(normalized[insert_at].get("action") or "").strip().lower()
                    if action not in {"launch", "launch_app"}:
                        break
                    insert_at += 1
                normalized.insert(insert_at, _audit_gate_step(package_name))
            updated_by_device[str(serial)] = normalized
        scenario["navigation_steps_by_device"] = updated_by_device

    status_key = status if status in AUDIT_STATUS_LOCATORS else "audit"
    recovery: Dict[str, Any] = {
        "enabled": enabled,
        "activity": "",
        "audit_resource_id": "%s:id/%s"
        % (package_name, AUDIT_STATUS_LOCATORS[status_key]),
        "confirm_text": "确定",
    }
    channel_key = str(channel or "").strip().lower()
    if channel_key in AUDIT_CHANNEL_LOCATORS:
        recovery["channel_resource_id"] = "%s:id/%s" % (
            package_name,
            AUDIT_CHANNEL_LOCATORS[channel_key],
        )
    scenario["audit_recovery"] = recovery
    config["scenario"] = scenario
