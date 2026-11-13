"""Build bounded, page-aware replay plans from pressure trajectories."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


DEFAULT_WINDOW_SECONDS = 120
DEFAULT_MAX_EVENTS = 400
MAX_WINDOW_SECONDS = 15 * 60
MAX_REPLAY_EVENTS = 5000
ANCHOR_SCAN_BYTES = 8 * 1024 * 1024

_CURRENT_ACTIVITY_RE = re.compile(
    r"//\s*current\s+activity\s+is\s+"
    r"(?P<activity>[A-Za-z_$][A-Za-z0-9_.$]*)",
    re.IGNORECASE,
)
_TOP_ACTIVITY_RE = re.compile(
    r"//\s*the\s+top\s+activity\s+is\s+"
    r"(?P<activity>[A-Za-z_$][A-Za-z0-9_.$]*)",
    re.IGNORECASE,
)


def _integer_option(
    options: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = options.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, normalized))


def _timestamp(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    # Reports use ISO-8601 while Fastbot uses a space-separated local-looking
    # timestamp.  Normalize both forms so a crash timestamp can bound the
    # replay window reliably.
    try:
        iso_value = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso_value)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _parsed_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")
    except (TypeError, ValueError, OverflowError):
        return None


def _aligned_crash_timestamp(
    crash_at: Any, events: Sequence[Mapping[str, Any]]
) -> Tuple[Optional[float], Optional[str]]:
    """Map an ISO report timestamp onto Fastbot's device-local event clock.

    Fastbot logs commonly omit a timezone (for example ``22:05`` on an
    Asia/Shanghai device), while the report records UTC.  Compare wall-clock
    values only after trying the usual half-hour timezone offsets, choosing the
    candidate nearest the final event.  If the clocks cannot be aligned, skip
    the crash cutoff rather than accidentally dropping the whole trajectory.
    """
    parsed_crash = _parsed_datetime(crash_at)
    if parsed_crash is None:
        return None, None
    event_times = [
        _parsed_datetime(event.get("timestamp"))
        for event in events
        if isinstance(event, Mapping)
    ]
    event_times = [item for item in event_times if item is not None]
    if not event_times:
        return _timestamp(crash_at), "direct"
    # Make all event values naive wall-clock values.  Fastbot's timestamps are
    # normally naive; an explicitly zoned event is converted to its wall value
    # because the sequence itself is the authoritative device clock.
    event_walls = [item.replace(tzinfo=None) for item in event_times]
    crash_wall = parsed_crash.replace(tzinfo=None)
    if parsed_crash.tzinfo is None:
        return crash_wall.timestamp(), "direct"
    event_min = min(event_walls)
    event_max = max(event_walls)
    candidates: List[Tuple[float, datetime, int]] = []
    for half_hour in range(-28, 29):
        offset = timedelta(minutes=30 * half_hour)
        candidate = crash_wall + offset
        distance = abs((candidate - event_max).total_seconds())
        if event_min - timedelta(minutes=5) <= candidate <= event_max + timedelta(minutes=5):
            candidates.append((distance, candidate, half_hour))
    if candidates:
        _, selected, half_hour = min(candidates, key=lambda item: item[0])
        return selected.timestamp(), "offset_%+d_minutes" % (half_hour * 30)
    return None, None


def _event_matches_context(
    event: Mapping[str, Any], cycle: Optional[int], phase: Optional[int]
) -> bool:
    for key, expected in (("cycle", cycle), ("phase", phase)):
        if expected is None or event.get(key) is None:
            continue
        try:
            if int(event[key]) != expected:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _normalize_activity(package_name: str, activity: Any) -> Optional[Dict[str, str]]:
    package = str(package_name or "").strip()
    value = str(activity or "").strip()
    if not package or not value:
        return None
    if "/" in value:
        component_package, separator, value = value.partition("/")
        if not separator or component_package != package:
            return None
    if value.startswith("."):
        value = package + value
    elif "." not in value:
        value = package + "." + value
    elif value.lower().startswith(
        (
            "android.",
            "com.android.",
            "com.google.android.",
            "com.huawei.",
            "com.hihonor.",
            "com.vivo.",
            "com.xiaomi.",
            "com.miui.",
        )
    ):
        # Fastbot also logs system/vendor activities without a component
        # package.  A target application's own libraries may use a different
        # Java namespace (for example com.ahzy.*), so reject only well-known
        # platform/vendor namespaces here.
        return None
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$]*", value):
        return None
    return {"activity": value, "component": "%s/%s" % (package, value)}


def _activity_is_owned(package_name: str, activity: Any) -> bool:
    """Return whether a recorded Activity belongs to the target package.

    Ad SDKs, trackers and AndroidX stubs log their own classes (``com.qq.e.*``,
    ``com.byazt.*``).  They are merged into the APK, so the component name looks
    valid, but the page is transient: it cannot be reopened after a restart and
    an anchor built from it can never be restored.
    """
    package = str(package_name or "").strip()
    value = str(activity or "").strip()
    if not package or not value:
        return False
    if "/" in value:
        component_package, _, _ = value.partition("/")
        return component_package == package
    # A relative class name (``.ui.MainActivity``) is always package-owned.
    if value.startswith("."):
        return True
    return value == package or value.startswith(package + ".")


def _activity_from_events(
    all_events: Sequence[Mapping[str, Any]], selected_start: int, package_name: str
) -> Optional[Dict[str, Any]]:
    """Return the most recent page anchor, preferring target-owned Activities.

    The nearest recorded Activity is not necessarily a restorable page: apps
    often spend their last seconds on an ad or a stub Activity.  Prefer the
    closest owned page so the replay starts somewhere the app can actually
    return to, and keep the previous permissive result as a fallback for apps
    whose own screens live in another namespace (for example ``com.ahzy.*``).
    """
    fallback: Optional[Dict[str, Any]] = None
    for index in range(min(selected_start, len(all_events) - 1), -1, -1):
        event = all_events[index]
        raw_activity = event.get("activity") or event.get("component")
        normalized = _normalize_activity(package_name, raw_activity)
        if normalized is None:
            continue
        candidate = dict(normalized)
        candidate.update(
            {
                "source": "structured_event",
                "observed_at": event.get("timestamp"),
                "event_id": event.get("event_id"),
                "confidence": "activity",
            }
        )
        if _activity_is_owned(package_name, raw_activity):
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback


def _activity_before_offset(
    log_path: Optional[Path], offset: Any, package_name: str
) -> Optional[Dict[str, Any]]:
    if log_path is None:
        return None
    path = Path(log_path)
    try:
        end = max(0, int(offset))
        size = path.stat().st_size
    except (OSError, TypeError, ValueError, OverflowError):
        return None
    end = min(size, end if end else size)
    start = max(0, end - ANCHOR_SCAN_BYTES)
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            content = stream.read(end - start).decode("utf-8", errors="replace")
    except OSError:
        return None
    matches = list(_CURRENT_ACTIVITY_RE.finditer(content))
    source = "fastbot_current_activity"
    if not matches:
        matches = list(_TOP_ACTIVITY_RE.finditer(content))
        source = "fastbot_top_activity"
    for match in reversed(matches):
        normalized = _normalize_activity(package_name, match.group("activity"))
        if normalized is None:
            continue
        normalized.update({"source": source, "confidence": "activity"})
        return normalized
    return None


def _context_number(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_plan_int(value: Any, default: int = -1) -> int:
    """Read legacy plan counters without allowing malformed JSON to abort."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _boolean_option(options: Mapping[str, Any], key: str, default: bool) -> bool:
    """Read a boolean option without treating the string ``"false"`` as true."""
    value = options.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _refresh_plan_metadata(
    plan: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Synchronize counters and timing after reusing or trimming a legacy plan."""
    refreshed = dict(plan)
    selected_count = len(events)
    original_count = _safe_plan_int(refreshed.get("original_event_count"), -1)
    omitted_count = _safe_plan_int(refreshed.get("omitted_event_count"), -1)
    if original_count < selected_count:
        original_count = (
            selected_count + omitted_count
            if omitted_count >= 0
            else selected_count
        )
    original_count = max(selected_count, original_count)
    refreshed["original_event_count"] = original_count
    refreshed["selected_event_count"] = selected_count
    refreshed["omitted_event_count"] = max(0, original_count - selected_count)

    first_at = str(events[0].get("timestamp") or "") if events else ""
    last_at = str(events[-1].get("timestamp") or "") if events else ""
    first_value = _timestamp(first_at)
    last_value = _timestamp(last_at)
    refreshed["start_at"] = first_at or None
    refreshed["end_at"] = last_at or None
    refreshed["actual_window_seconds"] = (
        round(max(0.0, last_value - first_value), 3)
        if first_value is not None and last_value is not None
        else None
    )
    return refreshed


def build_replay_trajectory(
    trajectory: Mapping[str, Any],
    options: Optional[Mapping[str, Any]] = None,
    *,
    package_name: str = "",
    pressure_log_path: Optional[Path] = None,
    cycle: Any = None,
    phase: Any = None,
    crash_at: Any = None,
) -> Dict[str, Any]:
    """Return a crash-tail trajectory plus an Activity-level start anchor.

    Android screenshots and UI XML are evidence, not restorable process state.
    The plan therefore keeps a short event tail and records the Activity that
    owned the first retained event. The replay controller can first try to
    recover the still-existing task; if that state is gone, callers must use an
    explicit page route or let an operator position the device.
    """

    settings = options if isinstance(options, Mapping) else {}
    window_seconds = _integer_option(
        settings,
        "window_seconds",
        DEFAULT_WINDOW_SECONDS,
        10,
        MAX_WINDOW_SECONDS,
    )
    max_events = _integer_option(
        settings, "max_events", DEFAULT_MAX_EVENTS, 1, MAX_REPLAY_EVENTS
    )
    strict_anchor = _boolean_option(settings, "require_page_anchor", True)
    restore_timeout = _integer_option(
        settings, "activity_restore_timeout_seconds", 8, 1, 30
    )
    allow_cold_activity_start = _boolean_option(
        settings, "allow_cold_activity_start", False
    )
    # When the recorded Activity cannot be reopened but the task still exists,
    # resume that task (a launcher-style activation) instead of refusing to
    # replay.  This never clears application data.
    relaunch_when_anchor_missing = _boolean_option(
        settings, "relaunch_when_anchor_missing", True
    )
    # Data-dependent crashes (an empty list, an exhausted pager) only appear
    # after the recorded tail is repeated. 1 keeps the historical single pass.
    repeat_rounds = _integer_option(settings, "repeat_rounds", 1, 1, 10)
    # Off by default: a recorded touch that no current node owns is normally
    # rejected. Enabling this replays it anyway once the target app is proven
    # foreground, so a single odd coordinate cannot hide the trigger event.
    allow_unowned_touch = _boolean_option(settings, "allow_unowned_touch", False)
    # Off by default: the replay guard refuses to type while an input method is
    # visible or its state is unconfirmed. A crash that requires touching a text
    # field needs that escape hatch, still gated on target foreground.
    allow_ime_visible = _boolean_option(settings, "allow_ime_visible", False)
    cycle_number = _context_number(cycle)
    phase_number = _context_number(phase)

    existing_plan = trajectory.get("replay_plan", {})
    if (
        isinstance(existing_plan, Mapping)
        and existing_plan.get("strategy") == "crash_tail"
        and _safe_plan_int(existing_plan.get("requested_window_seconds"))
        == window_seconds
        and _safe_plan_int(existing_plan.get("max_events")) == max_events
        and (
            _timestamp(crash_at) is None
            or _timestamp(existing_plan.get("crash_at")) == _timestamp(crash_at)
        )
    ):
        result = dict(trajectory)
        plan = dict(existing_plan)
        raw_events = result.get("events", [])
        if not isinstance(raw_events, list):
            raw_events = []
        raw_events = [dict(event) for event in raw_events if isinstance(event, Mapping)]
        selected_start = max(0, len(raw_events) - max_events)
        events = raw_events[selected_start:]
        if len(raw_events) > max_events:
            result["events_truncated"] = True
        result["events"] = events
        target_package = str(
            package_name or trajectory.get("package_name") or ""
        ).strip()
        if events:
            # Legacy cached plans may carry an anchor parsed from a stale
            # activity line (for example MainActivity) even though the first
            # retained gesture was recorded on a nested Activity. Derive the
            # anchor from the retained structured events so current-page replay
            # starts on the page that actually owns the crash tail.
            anchor = _activity_from_events(raw_events, selected_start, target_package)
            if anchor is None and isinstance(events[0], Mapping):
                anchor = _activity_before_offset(
                    pressure_log_path,
                    events[0].get("log_offset"),
                    target_package,
                )
            if anchor is not None:
                plan["page_anchor"] = anchor
        # The caller's options win over a cached plan: the stored trajectory
        # records the values that were used at collection time, so a debugging
        # run that explicitly enables a bypass must not be ignored by
        # ``setdefault``.
        plan["relaunch_when_anchor_missing"] = relaunch_when_anchor_missing
        plan["repeat_rounds"] = repeat_rounds
        plan["allow_unowned_touch"] = allow_unowned_touch
        plan["allow_ime_visible"] = allow_ime_visible
        plan = _refresh_plan_metadata(plan, events)
        result["replay_plan"] = plan
        result["event_count"] = len(events)
        result["replayable"] = bool(events)
        return result

    raw_events = trajectory.get("events", [])
    contextual_events: List[Dict[str, Any]] = []
    if isinstance(raw_events, list):
        contextual_events = [
            dict(event)
            for event in raw_events
            if isinstance(event, Mapping)
            and _event_matches_context(event, cycle_number, phase_number)
        ]

    # A recovered phase can append a few actions after the crash was detected.
    # Exclude those actions when the report gives us a trustworthy crash time.
    crash_timestamp, crash_alignment = _aligned_crash_timestamp(
        crash_at, contextual_events
    )
    crash_cutoff_applied = False
    if crash_timestamp is not None:
        bounded_events: List[Dict[str, Any]] = []
        timestamped_events = False
        for event in contextual_events:
            event_timestamp = _timestamp(event.get("timestamp"))
            if event_timestamp is None:
                bounded_events.append(event)
                continue
            timestamped_events = True
            # A small allowance covers logcat detection latency without
            # pulling a subsequent Fastbot gesture into the reproduction.
            if event_timestamp <= crash_timestamp + 0.25:
                bounded_events.append(event)
        if timestamped_events and bounded_events:
            contextual_events = bounded_events
            crash_cutoff_applied = True

    timed: List[Tuple[int, float]] = []
    for index, event in enumerate(contextual_events):
        value = _timestamp(event.get("timestamp"))
        if value is not None:
            timed.append((index, value))

    selected_start = max(0, len(contextual_events) - max_events)
    selected_by_time = False
    if timed:
        # Anchor the window on the crash instant when the clocks aligned.  Using
        # the last logged event instead lets a late detection or post-crash
        # recovery actions push the failing sequence out of the window.
        window_end = timed[-1][1]
        if crash_timestamp is not None and crash_timestamp >= timed[0][1]:
            window_end = crash_timestamp
        cutoff = window_end - window_seconds
        selected_start = next(
            (index for index, timestamp in timed if timestamp >= cutoff),
            timed[-1][0],
        )
        selected_by_time = True
    if len(contextual_events) - selected_start > max_events:
        selected_start = len(contextual_events) - max_events
    selected = contextual_events[selected_start:]

    target_package = str(
        package_name or trajectory.get("package_name") or ""
    ).strip()
    anchor = _activity_from_events(contextual_events, selected_start, target_package)
    if anchor is None and selected:
        anchor = _activity_before_offset(
            pressure_log_path, selected[0].get("log_offset"), target_package
        )
        if anchor is not None:
            anchor["observed_at"] = selected[0].get("timestamp")
            anchor["event_id"] = selected[0].get("event_id")

    first_at = str(selected[0].get("timestamp") or "") if selected else ""
    last_at = str(selected[-1].get("timestamp") or "") if selected else ""
    first_value = _timestamp(first_at)
    last_value = _timestamp(last_at)
    actual_seconds = (
        round(max(0.0, last_value - first_value), 3)
        if first_value is not None and last_value is not None
        else None
    )
    plan: Dict[str, Any] = {
        "version": 1,
        "strategy": "crash_tail",
        "requested_window_seconds": window_seconds,
        "max_events": max_events,
        "original_event_count": len(contextual_events),
        "selected_event_count": len(selected),
        "omitted_event_count": max(0, len(contextual_events) - len(selected)),
        "selected_by_time": selected_by_time,
        "start_at": first_at or None,
        "end_at": last_at or None,
        "actual_window_seconds": actual_seconds,
        "require_page_anchor": strict_anchor,
        "activity_restore_timeout_seconds": restore_timeout,
        "allow_cold_activity_start": allow_cold_activity_start,
        "relaunch_when_anchor_missing": relaunch_when_anchor_missing,
        "repeat_rounds": repeat_rounds,
        "allow_unowned_touch": allow_unowned_touch,
        "allow_ime_visible": allow_ime_visible,
        "crash_at": str(crash_at).strip() if crash_at is not None else None,
        "crash_timestamp_alignment": crash_alignment,
        "crash_cutoff_applied": crash_cutoff_applied,
        "page_anchor": anchor,
    }

    result = dict(trajectory)
    result["events"] = selected
    result["event_count"] = len(selected)
    result["replayable"] = bool(selected)
    result["replay_plan"] = plan
    result["replay_note"] = (
        "Replay uses the final %s events (%ss requested window) instead of the "
        "entire pressure phase."
        % (len(selected), window_seconds)
        if selected
        else "No structured events are available in the crash replay window."
    )
    return result


__all__ = [
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_WINDOW_SECONDS",
    "MAX_REPLAY_EVENTS",
    "MAX_WINDOW_SECONDS",
    "build_replay_trajectory",
]
