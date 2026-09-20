"""机组状态监测组件：去抖、升级、回差、抑制与处置闭环。"""

from __future__ import annotations

import unittest

from flashsmelter.cm.journal import (
    EVENT_ACKED,
    EVENT_CLEARED,
    EVENT_CLOSED,
    EVENT_DERATED,
    EVENT_ESCALATED,
    EVENT_RAISED,
    EVENT_REACTIVATED,
    EVENT_SUPPRESSED,
)
from flashsmelter.cm.thresholds import CRITICAL, NORMAL, WARNING
from flashsmelter.errors import NotFoundError, ValidationError

from .helpers import make_app

FAN = "F-1001"
VIB = "F-1001-vib"
TEMP = "F-1001-brg1-temp"
DT = 5.0


class ConditionMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.cm = self.app.cm

    def push(self, point_id: str, value: float, *, times: int = 1, dt: float = DT) -> list[dict]:
        results = []
        for _ in range(times):
            results.append(
                self.cm.ingest("collector", equipment_id=FAN, point_id=point_id, value=value)
            )
            self.app.clock.advance(dt)
        return results

    def event_names(self, equipment_id: str = FAN) -> list[str]:
        report = self.cm.alarm_history(equipment_id=equipment_id)
        return [event["event"] for event in report["alarms"][0]["events"]]

    # ------------------------------------------------------------- 去抖/升级
    def test_normal_samples_raise_nothing(self) -> None:
        results = self.push(VIB, 2.0, times=5)
        self.assertTrue(all(item["alarm"] is None for item in results))
        self.assertEqual("watching", self.cm.status()["state"])

    def test_alarm_requires_consecutive_abnormal_samples(self) -> None:
        self.push(VIB, 5.0, times=2)
        self.assertEqual(0, self.cm.status()["active_alarm_count"])
        result = self.push(VIB, 5.0)[0]
        self.assertEqual(WARNING, result["alarm"]["level"])
        self.assertTrue(result["alarm"]["alarm_id"].startswith(f"{VIB}#A"))

    def test_single_normal_sample_resets_debounce(self) -> None:
        self.push(VIB, 5.0, times=2)
        self.push(VIB, 2.0)
        self.push(VIB, 5.0, times=2)
        self.assertEqual(0, self.cm.status()["active_alarm_count"])

    def test_sustained_abnormal_does_not_spam_new_alarms(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        results = self.push(VIB, 5.3, times=10)
        self.assertTrue(all(item["alarm"]["alarm_id"] == alarm_id for item in results))
        self.assertEqual(1, self.cm.status()["active_alarm_count"])

    def test_warning_escalates_to_critical(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.push(VIB, 8.0, times=2)
        self.assertEqual(WARNING, self.cm.status()["active_alarms"][0]["level"])
        self.push(VIB, 8.0)
        active = self.cm.status()["active_alarms"][0]
        self.assertEqual(CRITICAL, active["level"])
        self.assertEqual(alarm_id, active["alarm_id"])
        self.assertIn(EVENT_ESCALATED, self.event_names())
        self.assertEqual("critical", self.cm.status()["state"])

    def test_critical_recommendation_mentions_load_reduction(self) -> None:
        self.push(VIB, 8.0, times=3)
        recommendation = self.cm.status()["active_alarms"][0]["recommendation"]
        self.assertIn("降负荷", recommendation)

    # ------------------------------------------------------------- 回差/冷却
    def test_clear_requires_hysteresis_and_consecutive_samples(self) -> None:
        self.push(VIB, 5.0, times=3)
        # 4.0 仍高于解除线 3.5，不清
        self.push(VIB, 4.0, times=5)
        self.assertEqual(1, self.cm.status()["active_alarm_count"])
        self.push(VIB, 3.0, times=2)
        self.assertEqual(1, self.cm.status()["active_alarm_count"])
        self.push(VIB, 3.0)
        self.assertEqual(0, self.cm.status()["active_alarm_count"])
        self.assertIn(EVENT_CLEARED, self.event_names())

    def test_rebreach_within_cooldown_reactivates_same_alarm_without_notify(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        notifications = len(self.cm.notifier)
        self.push(VIB, 3.0, times=3)
        # 立刻再越线：同一次报警再激活，且不重复提醒
        self.push(VIB, 5.0, times=3)
        active = self.cm.status()["active_alarms"][0]
        self.assertEqual(alarm_id, active["alarm_id"])
        self.assertEqual(len(self.cm.notifier), notifications)
        self.assertIn(EVENT_REACTIVATED, self.event_names())

    def test_new_breach_after_cooldown_is_a_new_alarm(self) -> None:
        self.push(VIB, 5.0, times=3)
        first = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.push(VIB, 3.0, times=3)
        self.app.clock.advance(self.app.settings.cm_renotify_cooldown_seconds + 1)
        self.push(VIB, 5.0, times=3)
        second = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.assertNotEqual(first, second)
        history = self.cm.alarm_history(equipment_id=FAN)
        self.assertEqual(2, history["count"])
        self.assertTrue(history["alarms"][1]["closed"])

    # ------------------------------------------------------------- 处置闭环
    def test_acknowledge_and_dispositions_are_recorded(self) -> None:
        self.push(VIB, 8.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.cm.acknowledge("ops-wang", alarm_id=alarm_id, note="去现场看看")
        self.cm.dispose("ops-wang", alarm_id=alarm_id, action="derate", note="降到70%")
        active = self.cm.status()["active_alarms"][0]
        self.assertTrue(active["acked"])
        self.assertTrue(active["derated"])
        records = self.cm.dispositions(equipment_id=FAN)["records"]
        names = [item["event"] for item in records]
        self.assertEqual([EVENT_ACKED, EVENT_DERATED], names)

    def test_double_acknowledge_rejected(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.cm.acknowledge("ops-wang", alarm_id=alarm_id)
        with self.assertRaises(ValidationError):
            self.cm.acknowledge("ops-li", alarm_id=alarm_id)

    def test_unknown_disposition_rejected(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        with self.assertRaises(ValidationError):
            self.cm.dispose("ops-wang", alarm_id=alarm_id, action="shutdown-plant")

    def test_suppress_keeps_alarm_active_but_stops_escalation_notify(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.cm.dispose("ops-wang", alarm_id=alarm_id, action="suppress", note="检修中已知")
        notifications = len(self.cm.notifier)
        self.push(VIB, 8.0, times=3)
        self.assertEqual(CRITICAL, self.cm.status()["active_alarms"][0]["level"])
        self.assertEqual(len(self.cm.notifier), notifications)
        self.assertIn(EVENT_SUPPRESSED, self.event_names())

    def test_manual_close_finishes_alarm(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        self.cm.dispose("ops-wang", alarm_id=alarm_id, action="close", note="确认误报，探头松动")
        self.assertEqual(0, self.cm.status()["active_alarm_count"])
        view = self.cm.alarm_view(alarm_id)
        self.assertFalse(view["active"])
        self.assertTrue(view["closed"])
        self.assertEqual([EVENT_CLOSED], [
            event["event"] for event in view["events"] if event["event"] == EVENT_CLOSED
        ])

    def test_note_requires_content(self) -> None:
        self.push(VIB, 5.0, times=3)
        alarm_id = self.cm.status()["active_alarms"][0]["alarm_id"]
        with self.assertRaises(ValidationError):
            self.cm.add_note("ops-wang", alarm_id=alarm_id, note="   ")

    # ------------------------------------------------------------- 查询/校验
    def test_history_isolated_by_equipment(self) -> None:
        self.push(VIB, 5.0, times=3)
        self.assertEqual(0, self.cm.alarm_history(equipment_id="M-2001")["count"])
        trend = self.cm.trend_report(equipment_id="M-2001")
        self.assertEqual(0, trend["count"])

    def test_unknown_equipment_and_point_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.cm.ingest("gw", equipment_id="NOPE", point_id="NOPE-vib", value=1.0)
        with self.assertRaises(NotFoundError):
            self.cm.ingest("gw", equipment_id=FAN, point_id="M-2001-vib", value=1.0)

    def test_non_finite_value_rejected(self) -> None:
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(ValidationError):
                self.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=value)

    def test_duplicate_registration_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.cm.register_equipment("ops", equipment_id=FAN, kind="fan")

    def test_temperature_point_alarm_independently(self) -> None:
        # 温度快速上升：每 30 秒 +3°C（6°C/min 危险），与振动互不影响。
        # 第一个样本无前值不判温升率，所以推 4 个拿到连续 3 次危险。
        results = []
        value = 50.0
        for _ in range(4):
            results.append(
                self.cm.ingest("collector", equipment_id=FAN, point_id=TEMP, value=value)
            )
            value += 3.0
            self.app.clock.advance(30)
        self.assertEqual(CRITICAL, results[-1]["alarm"]["level"])
        self.assertEqual(0, self.cm.trend_report(equipment_id=FAN, point_id=VIB)["count"])
        self.assertEqual(4, self.cm.trend_report(equipment_id=FAN, point_id=TEMP)["count"])


if __name__ == "__main__":
    unittest.main()
