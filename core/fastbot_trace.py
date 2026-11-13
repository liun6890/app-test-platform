"""Parse replayable actions from Fastbot's human-readable log output.

Fastbot commonly writes a binary ``.fbm`` model and a text log.  The model is
useful evidence, but it is not a portable replay script.  This module keeps the
parser deliberately small and tolerant of output differences between Fastbot
builds.  It is safe to use on a byte range of a large log file, which is
important because a long run can produce gigabytes of output.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import re
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Set, Tuple


_TIMESTAMP_RE = re.compile(
    r"\[Fastbot\]\[(?P<timestamp>\d{4}-\d{2}-\d{2}\s+"
    r"\d{2}:\d{2}:\d{2}\.\d+)\]"
)
_EVENT_ID_RE = re.compile(r"//\s*Event\s+id\s*:\s*(?P<id>\d+)", re.I)
_SENDING_EVENT_RE = re.compile(r"//\s*Sending\s+event\s+#(?P<id>\d+)", re.I)
_ACTION_RE = re.compile(r"action\s+type\s*:\s*(?P<action>[A-Za-z0-9_ -]+)", re.I)
_TOUCH_RE = re.compile(
    r"Sending\s+Touch\s*\(ACTION_(?P<phase>DOWN|MOVE|UP)\)\s*:\s*"
    r"(?:\d+\s*:\s*)?\(\s*(?P<x>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*,\s*(?P<y>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*\)",
    re.I,
)
_WAIT_RE = re.compile(r"Wait\s+Event\s+for\s+(?P<ms>\d+(?:\.\d+)?)\s+milliseconds", re.I)
_SLEEP_RE = re.compile(
    r"Sleeping\s+for\s+(?P<ms>\d+(?:\.\d+)?)\s+milliseconds", re.I
)
_INPUT_RE = re.compile(r"Input\s+text\s+is\s*(?P<text>.*)$", re.I)
_KEY_RE = re.compile(
    r"Sending\s+Key(?:Event)?\s*\([^)]*\)\s*:\s*"
    r"(?:keyCode\s*[=:]\s*)?(?P<key>[A-Za-z0-9_+-]+)",
    re.I,
)
_CURRENT_ACTIVITY_RE = re.compile(
    r"//\s*current\s+activity\s+is\s+"
    r"(?P<activity>[A-Za-z_$][A-Za-z0-9_.$]*)",
    re.I,
)

_SCROLL_ACTIONS = {
    "SCROLL_LEFT_RIGHT",
    "SCROLL_RIGHT_LEFT",
    "SCROLL_TOP_DOWN",
    "SCROLL_BOTTOM_UP",
}
_KEY_ACTIONS = {
    "BACK": "BACK",
    "HOME": "HOME",
    "MENU": "MENU",
    "RECENTS": "APP_SWITCH",
    "APP_SWITCH": "APP_SWITCH",
}


def _number(value: str) -> Any:
    number = float(value)
    if number.is_integer():
        return int(number)
    return round(number, 4)


def _timestamp_value(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_input(value: str) -> str:
    # The value is intentionally not shell-decoded.  Fastbot's input text is
    # already the text it attempted to send and may contain spaces or unicode.
    return value.rstrip("\r\n")


class _Builder:
    def __init__(
        self, event_id: Optional[int] = None, activity: Optional[str] = None
    ) -> None:
        self.event_id = event_id
        self.activity = str(activity or "")
        self.engine_event_number: Optional[int] = None
        self.raw_action = ""
        self.timestamp = ""
        self.timestamp_value: Optional[float] = None
        self.points: List[Tuple[str, Any, Any, Optional[float]]] = []
        self.waits_ms: List[float] = []
        self.delay_ms: Optional[float] = None
        self.input_text: Optional[str] = None
        self.key: Optional[str] = None
        self.start_offset: Optional[int] = None
        self.end_offset: Optional[int] = None

    @property
    def meaningful(self) -> bool:
        return bool(
            self.raw_action
            or self.points
            or self.input_text is not None
            or self.key
        )


def _action_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", raw.strip()).strip("_").upper()


def _event_signature(event: Mapping[str, Any]) -> Tuple[Any, ...]:
    action = str(event.get("action") or "")
    values: List[Any] = [action]
    for name in ("x", "y", "x1", "y1", "x2", "y2", "key", "text"):
        values.append(event.get(name))
    return tuple(values)


def _build_events(builder: _Builder) -> List[Dict[str, Any]]:
    """Turn one Fastbot event block into one or two replay events."""
    if not builder.meaningful:
        return []
    raw_action = _action_name(builder.raw_action)
    points = builder.points
    if points:
        first = points[0]
        last = points[-1]
    else:
        first = last = ("", 0, 0, builder.timestamp_value)

    if raw_action in _SCROLL_ACTIONS or len(points) >= 3:
        action = "swipe"
    elif raw_action == "LONG_CLICK":
        action = "long_click"
    elif raw_action in _KEY_ACTIONS or raw_action in {"BACK", "HOME", "MENU"}:
        action = "key"
    elif raw_action in {"INPUT", "TEXT", "SET_TEXT"} and not points:
        action = "input"
    elif points or raw_action in {"CLICK", "TAP", "TOUCH", ""}:
        action = "click"
    elif builder.key:
        action = "key"
    elif builder.input_text is not None:
        action = "input"
    else:
        return []

    event: Dict[str, Any] = {
        "action": action,
        "source": "fastbot.log",
    }
    if builder.event_id is not None:
        event["event_id"] = builder.event_id
    if builder.engine_event_number is not None:
        event["engine_event_number"] = builder.engine_event_number
    if builder.timestamp:
        event["timestamp"] = builder.timestamp
    if builder.activity:
        event["activity"] = builder.activity
    if builder.start_offset is not None:
        event["log_offset"] = builder.start_offset
    if raw_action:
        event["raw_action"] = raw_action

    if action in {"click", "long_click"}:
        event["x"] = first[1]
        event["y"] = first[2]
    elif action == "swipe":
        event["x1"] = first[1]
        event["y1"] = first[2]
        event["x2"] = last[1]
        event["y2"] = last[2]
        start_time = first[3]
        end_time = last[3]
        duration = (
            max(0.0, end_time - start_time)
            if start_time is not None and end_time is not None
            else 0.0
        )
        if duration <= 0:
            duration = 0.2
        event["duration"] = round(min(30.0, max(0.05, duration)), 3)
    elif action == "key":
        event["key"] = builder.key or _KEY_ACTIONS.get(raw_action, raw_action)

    if action == "long_click":
        duration_ms = max(builder.waits_ms or [1000.0])
        if len(points) >= 2 and points[0][3] is not None and points[-1][3] is not None:
            duration_ms = max(duration_ms, (points[-1][3] - points[0][3]) * 1000.0)
        event["duration"] = round(min(30.0, max(0.1, duration_ms / 1000.0)), 3)

    if builder.delay_ms is not None:
        event["delay_ms"] = int(min(10000, max(0, round(builder.delay_ms))))
    else:
        event["delay_ms"] = 200

    output = [event]
    if builder.input_text is not None and action != "input":
        input_event: Dict[str, Any] = {
            "action": "input",
            "text": _clean_input(builder.input_text),
            "delay_ms": 0,
            "source": "fastbot.log",
            "sequence": 2,
        }
        if builder.event_id is not None:
            # Keep the parent id visible while making the sub-event unique for
            # clients that deduplicate event identifiers.
            input_event["event_id"] = "%s:input" % builder.event_id
        if builder.timestamp:
            input_event["timestamp"] = builder.timestamp
        if builder.activity:
            input_event["activity"] = builder.activity
        output.append(input_event)
    elif action == "input":
        event["text"] = _clean_input(builder.input_text or "")
    return output


def _parse_lines(
    lines: Iterable[Tuple[int, str]],
    *,
    max_events: int,
    start_offset: int = 0,
    end_offset: Optional[int] = None,
) -> Dict[str, Any]:
    max_events = max(1, int(max_events))
    retained: Deque[Dict[str, Any]] = deque(maxlen=max_events)
    current: Optional[_Builder] = None
    seen_ids: Set[str] = set()
    seen_id_order: Deque[str] = deque(maxlen=max_events * 4)
    anonymous_signatures: Deque[Tuple[Any, ...]] = deque(maxlen=512)
    first_id: Optional[int] = None
    last_id: Optional[int] = None
    first_at: Optional[str] = None
    last_at: Optional[str] = None
    parsed_blocks = 0
    current_activity = ""

    def remember_id(value: str) -> None:
        if value in seen_ids:
            return
        if len(seen_id_order) == seen_id_order.maxlen:
            old = seen_id_order.popleft()
            seen_ids.discard(old)
        seen_id_order.append(value)
        seen_ids.add(value)

    def emit(builder: Optional[_Builder]) -> None:
        nonlocal parsed_blocks, first_id, last_id, first_at, last_at
        if builder is None or not builder.meaningful:
            return
        parsed_blocks += 1
        built = _build_events(builder)
        for event in built:
            event_id = event.get("event_id")
            if event_id is not None:
                event_key = str(event_id).split(":", 1)[0]
                # Input is a deliberate child event and must not be discarded
                # merely because it shares the parent id.
                if ":" not in str(event_id):
                    if event_key in seen_ids:
                        continue
                    remember_id(event_key)
                    try:
                        numeric_id = int(event_key)
                    except ValueError:
                        numeric_id = None
                    if numeric_id is not None:
                        if first_id is None:
                            first_id = numeric_id
                        last_id = numeric_id
            else:
                signature = _event_signature(event)
                if signature in anonymous_signatures:
                    continue
                anonymous_signatures.append(signature)
            if event.get("timestamp"):
                if first_at is None:
                    first_at = str(event["timestamp"])
                last_at = str(event["timestamp"])
            retained.append(event)

    for offset, line in lines:
        if end_offset is not None and offset >= end_offset:
            break
        timestamp_match = _TIMESTAMP_RE.search(line)
        timestamp = timestamp_match.group("timestamp") if timestamp_match else ""
        timestamp_value = _timestamp_value(timestamp)

        activity_match = _CURRENT_ACTIVITY_RE.search(line)
        if activity_match:
            # The line describes the page reached by the preceding input. Emit
            # that input with its old page, then attach the new Activity to
            # subsequent events.
            emit(current)
            current = None
            current_activity = activity_match.group("activity")
            continue

        event_match = _EVENT_ID_RE.search(line)
        if event_match:
            event_id = int(event_match.group("id"))
            # A few builds print a short preamble of touch lines before the
            # first Event id.  Attach that small preamble to the first id.
            if current is not None and current.event_id is None and current.meaningful:
                if not current.raw_action and len(current.points) <= 2:
                    current.event_id = event_id
                else:
                    emit(current)
                    current = _Builder(event_id, current_activity)
            else:
                emit(current)
                current = _Builder(event_id, current_activity)
            current.timestamp = timestamp or current.timestamp
            current.timestamp_value = timestamp_value or current.timestamp_value
            current.start_offset = offset
            continue

        sending_match = _SENDING_EVENT_RE.search(line)
        if sending_match:
            try:
                sending_id = int(sending_match.group("id"))
            except ValueError:
                sending_id = None
            if current is None:
                current = _Builder(sending_id, current_activity)
                current.start_offset = offset
            elif current.engine_event_number is None:
                current.engine_event_number = sending_id
            continue

        action_match = _ACTION_RE.search(line)
        if action_match:
            if current is None:
                current = _Builder(activity=current_activity)
                current.start_offset = offset
            elif current.raw_action and current.meaningful:
                emit(current)
                current = _Builder(activity=current_activity)
                current.start_offset = offset
            current.raw_action = action_match.group("action").strip()
            current.timestamp = timestamp or current.timestamp
            current.timestamp_value = timestamp_value or current.timestamp_value
            continue

        touch_match = _TOUCH_RE.search(line)
        if touch_match:
            if current is None:
                current = _Builder(activity=current_activity)
                current.start_offset = offset
            current.points.append(
                (
                    touch_match.group("phase").upper(),
                    _number(touch_match.group("x")),
                    _number(touch_match.group("y")),
                    timestamp_value,
                )
            )
            if not current.timestamp and timestamp:
                current.timestamp = timestamp
                current.timestamp_value = timestamp_value
            continue

        wait_match = _WAIT_RE.search(line)
        if wait_match and current is not None:
            current.waits_ms.append(float(wait_match.group("ms")))
            continue

        sleep_match = _SLEEP_RE.search(line)
        if sleep_match and current is not None:
            current.delay_ms = float(sleep_match.group("ms"))
            continue

        input_match = _INPUT_RE.search(line)
        if input_match:
            if current is None:
                current = _Builder(activity=current_activity)
                current.start_offset = offset
            current.input_text = input_match.group("text")
            current.timestamp = timestamp or current.timestamp
            current.timestamp_value = timestamp_value or current.timestamp_value
            continue

        key_match = _KEY_RE.search(line)
        if key_match:
            if current is None:
                current = _Builder(activity=current_activity)
                current.start_offset = offset
            current.key = key_match.group("key")

    emit(current)
    events = list(retained)
    duration: Optional[float] = None
    if first_at and last_at:
        first_value = _timestamp_value(first_at)
        last_value = _timestamp_value(last_at)
        if first_value is not None and last_value is not None:
            duration = max(0.0, last_value - first_value)
    return {
        "events": events,
        "event_count": len(events),
        "parsed_blocks": parsed_blocks,
        "first_event_id": first_id,
        "last_event_id": last_id,
        "first_event_at": first_at,
        "last_event_at": last_at,
        "event_elapsed_seconds": round(duration, 3) if duration is not None else None,
        "truncated": parsed_blocks > len(events),
        "start_offset": max(0, int(start_offset)),
        "end_offset": int(end_offset) if end_offset is not None else None,
        "source": "fastbot.log",
    }


def parse_fastbot_log_text(
    text: str, *, max_events: int = 10000, start_offset: int = 0
) -> Dict[str, Any]:
    """Parse an in-memory Fastbot log fragment."""
    lines = []
    offset = max(0, int(start_offset))
    for line in str(text or "").splitlines(True):
        lines.append((offset, line.rstrip("\r\n")))
        offset += len(line.encode("utf-8", errors="replace"))
    return _parse_lines(lines, max_events=max_events, start_offset=start_offset)


def parse_fastbot_log(
    path: Path,
    *,
    start_offset: int = 0,
    end_offset: Optional[int] = None,
    max_events: int = 10000,
) -> Dict[str, Any]:
    """Parse a bounded byte range from a Fastbot log without loading it all."""
    path = Path(path)
    start = max(0, int(start_offset))
    end = None if end_offset is None else max(start, int(end_offset))

    def iter_lines() -> Iterable[Tuple[int, str]]:
        with path.open("rb") as stream:
            stream.seek(start)
            if start:
                # A phase offset can point into the previous line.  Discard
                # only that partial line; offsets recorded immediately after a
                # newline are already at a complete line boundary.
                stream.seek(start - 1)
                previous = stream.read(1)
                stream.seek(start)
                if previous != b"\n":
                    stream.readline()
            while True:
                line_offset = stream.tell()
                if end is not None and line_offset >= end:
                    break
                raw = stream.readline()
                if not raw:
                    break
                if end is not None and line_offset + len(raw) > end:
                    raw = raw[: max(0, end - line_offset)]
                yield line_offset, raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if end is not None and line_offset + len(raw) >= end:
                    break

    try:
        result = _parse_lines(
            iter_lines(),
            max_events=max_events,
            start_offset=start,
            end_offset=end,
        )
    except (OSError, ValueError):
        return {
            "events": [],
            "event_count": 0,
            "parsed_blocks": 0,
            "first_event_id": None,
            "last_event_id": None,
            "first_event_at": None,
            "last_event_at": None,
            "event_elapsed_seconds": None,
            "truncated": False,
            "start_offset": start,
            "end_offset": end,
            "source": "fastbot.log",
            "error": "unable to read Fastbot log",
        }
    return result


def parse_fastbot_events(
    text: str, *, max_events: int = 10000
) -> List[Dict[str, Any]]:
    """Convenience wrapper returning only the replay events."""
    return list(parse_fastbot_log_text(text, max_events=max_events).get("events", []))


__all__ = [
    "parse_fastbot_events",
    "parse_fastbot_log",
    "parse_fastbot_log_text",
]
