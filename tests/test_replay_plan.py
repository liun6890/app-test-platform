import tempfile
import unittest
from pathlib import Path

from core.replay_plan import build_replay_trajectory


class ReplayPlanTests(unittest.TestCase):
    def _events(self, count=8):
        return [
            {
                "event_id": index,
                "action": "click",
                "x": index,
                "y": index,
                "timestamp": "2026-08-26 10:00:%02d.000" % index,
                "activity": "com.ahzy.base.arch.fragment.FragmentContainActivity",
            }
            for index in range(count)
        ]

    def test_selects_only_recent_time_window_and_event_cap(self):
        result = build_replay_trajectory(
            {"package_name": "com.example.app", "events": self._events(8)},
            {"window_seconds": 120, "max_events": 3},
            package_name="com.example.app",
        )
        self.assertEqual([5, 6, 7], [item["event_id"] for item in result["events"]])
        self.assertEqual(8, result["replay_plan"]["original_event_count"])
        self.assertEqual(3, result["replay_plan"]["selected_event_count"])
        self.assertEqual(5, result["replay_plan"]["omitted_event_count"])
        self.assertEqual(
            "com.example.app/com.ahzy.base.arch.fragment.FragmentContainActivity",
            result["replay_plan"]["page_anchor"]["component"],
        )

    def test_crash_timestamp_excludes_events_after_failure(self):
        events = self._events(5)
        result = build_replay_trajectory(
            {"package_name": "com.example.app", "events": events},
            {"window_seconds": 120, "max_events": 20},
            package_name="com.example.app",
            crash_at="2026-08-26T10:00:02.500000+00:00",
        )
        self.assertEqual([0, 1, 2], [item["event_id"] for item in result["events"]])
        self.assertTrue(result["replay_plan"]["crash_cutoff_applied"])

    def test_malformed_existing_plan_is_rebuilt(self):
        result = build_replay_trajectory(
            {
                "package_name": "com.example.app",
                "events": self._events(2),
                "replay_plan": {
                    "strategy": "crash_tail",
                    "requested_window_seconds": "not-a-number",
                    "max_events": None,
                },
            },
            {"window_seconds": 120, "max_events": 400},
            package_name="com.example.app",
        )
        self.assertEqual(2, result["event_count"])
        self.assertEqual("crash_tail", result["replay_plan"]["strategy"])

    def test_activity_log_fallback_ignores_known_external_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "fastbot.log"
            log.write_text(
                "// current activity is com.android.settings.Settings\n"
                "// current activity is com.ahzy.base.arch.fragment.FragmentContainActivity\n",
                encoding="utf-8",
            )
            result = build_replay_trajectory(
                {
                    "package_name": "com.example.app",
                    "events": [{"action": "click", "log_offset": log.stat().st_size}],
                },
                {"max_events": 10},
                package_name="com.example.app",
                pressure_log_path=log,
            )
        self.assertEqual(
            "com.example.app/com.ahzy.base.arch.fragment.FragmentContainActivity",
            result["replay_plan"]["page_anchor"]["component"],
        )


    def test_anchor_prefers_target_owned_page_over_late_sdk_page(self):
        package = "com.nwczizrjyets.moneysaving"
        events = [
            {
                "event_id": 0,
                "action": "click",
                "x": 1,
                "y": 1,
                "timestamp": "2026-08-26 10:00:00.000",
                "activity": package + ".module.activity.MainActivity",
            },
            {
                "event_id": 1,
                "action": "click",
                "x": 2,
                "y": 2,
                "timestamp": "2026-08-26 10:00:01.000",
                "activity": "com.qq.e.ads.ADActivity",
            },
            {
                "event_id": 2,
                "action": "click",
                "x": 3,
                "y": 3,
                "timestamp": "2026-08-26 10:00:02.000",
                "activity": "com.qq.e.ads.ADActivity",
            },
        ]
        result = build_replay_trajectory(
            {"package_name": package, "events": events},
            {"window_seconds": 120, "max_events": 2},
            package_name=package,
        )
        self.assertEqual([1, 2], [item["event_id"] for item in result["events"]])
        self.assertEqual(
            package + "/" + package + ".module.activity.MainActivity",
            result["replay_plan"]["page_anchor"]["component"],
        )

    def test_plan_enables_task_relaunch_when_anchor_is_missing(self):
        result = build_replay_trajectory(
            {"package_name": "com.example.app", "events": self._events(2)},
            {"max_events": 10},
            package_name="com.example.app",
        )
        self.assertTrue(result["replay_plan"]["relaunch_when_anchor_missing"])


    def test_plan_carries_repeat_rounds(self):
        result = build_replay_trajectory(
            {"package_name": "com.example.app", "events": self._events(2)},
            {"max_events": 10, "repeat_rounds": 4},
            package_name="com.example.app",
        )
        self.assertEqual(4, result["replay_plan"]["repeat_rounds"])

    def test_repeat_rounds_defaults_to_single_pass(self):
        result = build_replay_trajectory(
            {"package_name": "com.example.app", "events": self._events(2)},
            {"max_events": 10},
            package_name="com.example.app",
        )
        self.assertEqual(1, result["replay_plan"]["repeat_rounds"])


if __name__ == "__main__":
    unittest.main()
