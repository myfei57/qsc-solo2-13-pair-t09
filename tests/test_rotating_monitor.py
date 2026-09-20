"""转动设备在线监测：阈值延时、死带、报警抑制、升级、处置与历史调阅。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.errors import GuardViolation, NotFoundError, ValidationError
from flashsmelter.rotating.component import (
    DISPOSITION_INSPECT,
    DISPOSITION_REDUCE_LOAD,
    STATUS_ACKNOWLEDGED,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_RECOVERED,
)

from .helpers import make_app, make_root

FAN = "fan-ID01"
FAN_TEMP = "temp-de"
FAN_VIB = "vib-de"
MILL = "mill-M01"
TEMP_RULE = "bearing-temp-high"
RATE_RULE = "rate-of-change"


def feed(app: Application, machine: str, point: str, value: float, *, step: float = 10.0, times: int = 1):
    """连续喂 ``times`` 个同值采样：首点立即喂，之后每 ``step`` 秒一个。

    共 ``times`` 个采样、覆盖 ``step*(times-1)`` 秒，末点喂完不再推进时钟。
    确认延时 30s 需要 times=4（t=0/10/20/30 均超限）。
    """

    last = app.rotating.ingest("daq", machine_id=machine, point_id=point, value=value)
    for _ in range(times - 1):
        app.clock.advance(step)
        last = app.rotating.ingest("daq", machine_id=machine, point_id=point, value=value)
    return last


def steady(app: Application, machine: str, point: str, value: float, *, step: float = 10.0, times: int = 1):
    """每个采样都先推进 ``step`` 秒（用于描述「再过多久」的场景）。"""

    last = None
    for _ in range(times):
        app.clock.advance(step)
        last = app.rotating.ingest("daq", machine_id=machine, point_id=point, value=value)
    return last


def active_alarms(app: Application, machine: str = FAN):
    """当前仍挂着的报警：不包含已关闭与已恢复待复盘。"""

    return app.rotating.list_alarms(
        machine_id=machine, include_closed=False, include_recovered=False
    )


class AbsoluteThresholdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_normal_readings_raise_nothing(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 55.0, times=10)
        self.assertEqual(0, self.app.rotating.fleet_status()["active_alarm_count"])

    def test_short_glitch_below_on_delay_does_not_alarm(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 78.0, times=2)  # 连续 20s < 预警确认延时 30s
        steady(self.app, FAN, FAN_TEMP, 55.0)
        self.assertEqual(0, self.app.rotating.notifications(limit=50)["count"])

    def test_sustained_warn_raises_single_alarm(self) -> None:
        first = feed(self.app, FAN, FAN_TEMP, 78.0, times=4)  # 连续 30s
        self.assertEqual(1, len(first["notifications"]))
        # 继续超限不再产生同类报警。
        steady(self.app, FAN, FAN_TEMP, 78.0, times=6)
        alarms = active_alarms(self.app)
        self.assertEqual(1, len(alarms))
        self.assertEqual(STATUS_ACTIVE, alarms[0]["status"])
        self.assertEqual("warn", alarms[0]["level"])
        notifications = [
            n for n in self.app.rotating.notifications(limit=50)["notifications"]
            if n.get("kind") != "read-marker"
        ]
        self.assertEqual(1, len(notifications))

    def test_danger_uses_shorter_on_delay(self) -> None:
        first = feed(self.app, FAN, FAN_TEMP, 90.0)  # t0 即超限，未达 5s 确认延时
        self.assertEqual([], first["notifications"])
        result = steady(self.app, FAN, FAN_TEMP, 90.0)  # +10s >= 5s
        self.assertEqual(1, len(result["notifications"]))
        self.assertEqual("danger", active_alarms(self.app)[0]["level"])

    def test_warn_worsening_to_danger_escalates_same_alarm(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 78.0, times=4)
        alarm_id = active_alarms(self.app)[0]["alarm_id"]
        steady(self.app, FAN, FAN_TEMP, 90.0)
        steady(self.app, FAN, FAN_TEMP, 90.0)  # 危险确认延时 5s 已过
        alarms = active_alarms(self.app)
        self.assertEqual(1, len(alarms))
        self.assertEqual(alarm_id, alarms[0]["alarm_id"])
        self.assertEqual("danger", alarms[0]["level"])
        self.assertEqual(1, len(alarms[0]["escalations"]))

    def test_deadband_holds_active_alarm_near_threshold(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 78.0, times=4)
        # 73℃ 低于预警线 75，但高于释放带 75*0.95≈71.25：活动报警保持。
        steady(self.app, FAN, FAN_TEMP, 73.0, times=2)
        self.assertEqual(1, len(active_alarms(self.app)))
        # 全新状态下 73℃ 本身不构成报警。
        other = make_app()
        feed(other, MILL, "temp-motor-de", 73.0, times=4)
        self.assertEqual(0, other.rotating.fleet_status()["active_alarm_count"])

    def test_off_delay_delays_recovery(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 78.0, times=4)
        # 首个回落点在 t=40，释放观察期到 t=100。
        steady(self.app, FAN, FAN_TEMP, 60.0, times=6)  # 到 t=90 仍未恢复
        self.assertEqual(1, len(active_alarms(self.app)))
        steady(self.app, FAN, FAN_TEMP, 60.0)  # t=100，回落满 60s
        self.assertEqual(0, len(active_alarms(self.app)))

    def test_re_alarm_after_recovery_is_new_record(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 78.0, times=4)
        first = active_alarms(self.app)[0]["alarm_id"]
        steady(self.app, FAN, FAN_TEMP, 60.0, times=7)  # 回落满 60s 才恢复
        feed(self.app, FAN, FAN_TEMP, 78.0, times=4)
        absolute = [
            alarm
            for alarm in active_alarms(self.app)
            if alarm["rule_code"] == TEMP_RULE
        ]
        self.assertEqual(1, len(absolute))
        self.assertNotEqual(first, absolute[0]["alarm_id"])
        history = self.app.rotating.list_alarms(
            machine_id=FAN, include_closed=True, include_recovered=True
        )
        # 第一条（已关闭）+ 新的绝对阈值报警；重报阶跃可能另带一条变化率报警。
        ids = {item["alarm_id"] for item in history if item["rule_code"] == TEMP_RULE}
        self.assertEqual(2, len(ids))


class AcknowledgeAndEscalationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        feed(self.app, FAN, FAN_TEMP, 78.0, times=4)

    def test_unacknowledged_warn_swept_to_danger(self) -> None:
        self.assertEqual("warn", active_alarms(self.app)[0]["level"])
        self.app.clock.advance(self.app.settings.rotating_ack_escalate_seconds)
        result = self.app.rotating.sweep("scan")
        self.assertEqual(1, len(result["escalated"]))
        self.assertEqual("danger", active_alarms(self.app)[0]["level"])

    def test_acknowledged_warn_is_not_swept(self) -> None:
        self.app.rotating.acknowledge(
            "ops", machine_id=FAN, point_id=FAN_TEMP, rule_code=TEMP_RULE, note="看到了"
        )
        self.app.clock.advance(self.app.settings.rotating_ack_escalate_seconds + 1)
        self.assertEqual([], self.app.rotating.sweep("scan")["escalated"])
        self.assertEqual("warn", active_alarms(self.app)[0]["level"])

    def test_acknowledge_requires_active_alarm(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.rotating.acknowledge(
                "ops", machine_id=MILL, point_id="temp-motor-de", rule_code=TEMP_RULE
            )

    def test_worsening_after_acknowledgement_reopens_attention(self) -> None:
        self.app.rotating.acknowledge(
            "ops", machine_id=FAN, point_id=FAN_TEMP, rule_code=TEMP_RULE
        )
        steady(self.app, FAN, FAN_TEMP, 90.0, times=2)  # 危险确认延时 5s 已过
        alarm = active_alarms(self.app)[0]
        self.assertEqual("danger", alarm["level"])
        self.assertEqual(STATUS_ACTIVE, alarm["status"])
        self.assertIsNone(alarm["acknowledged_at"])


class RateOfChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_slow_rise_does_not_alarm(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 51.0)  # t0=51
        steady(self.app, FAN, FAN_TEMP, 52.0, times=2)  # +10/+20s：≤3℃/min
        self.assertEqual(0, self.app.rotating.fleet_status()["active_alarm_count"])

    def test_fast_rise_alarms_after_on_delay(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 50.0)
        # 20s 后 +5℃ => 15℃/min，越过 3℃/min 预警线。
        self.app.clock.advance(20)
        first = self.app.rotating.ingest("daq", machine_id=FAN, point_id=FAN_TEMP, value=55.0)
        self.assertEqual([], first["notifications"])  # 变化率也要持续确认 30s
        steady(self.app, FAN, FAN_TEMP, 60.0, times=2)  # +10s、+20s 仍快
        result = steady(self.app, FAN, FAN_TEMP, 65.0)  # +30s 持续
        self.assertEqual(1, len(result["notifications"]))
        alarm = active_alarms(self.app)[0]
        self.assertEqual(RATE_RULE, alarm["rule_code"])

    def test_low_level_noise_filtered_by_floor(self) -> None:
        feed(self.app, FAN, FAN_VIB, 1.0)
        self.app.clock.advance(20)
        steady(self.app, FAN, FAN_VIB, 1.2, times=3)  # 比例再高，增量 < floor 0.3
        self.assertEqual(0, self.app.rotating.fleet_status()["active_alarm_count"])

    def test_absolute_and_rate_alarms_coexist(self) -> None:
        # 全程保持在预警带（75~85℃）内，每 10s 爬升 1℃（6℃/min ≥ 3℃/min）。
        feed(self.app, FAN, FAN_TEMP, 76.0)  # 绝对值 t0 起超限
        steady(self.app, FAN, FAN_TEMP, 77.0)  # t10：间隔不足，速率不出
        steady(self.app, FAN, FAN_TEMP, 78.0)  # t20：变化率开始超限
        steady(self.app, FAN, FAN_TEMP, 79.0)  # t30：绝对阈值持续 30s 挂出
        self.assertEqual({TEMP_RULE}, {a["rule_code"] for a in active_alarms(self.app)})
        steady(self.app, FAN, FAN_TEMP, 80.0)  # t40
        steady(self.app, FAN, FAN_TEMP, 81.0)  # t50：变化率持续 30s 挂出
        rules = {alarm["rule_code"] for alarm in active_alarms(self.app)}
        self.assertIn(TEMP_RULE, rules)
        self.assertIn(RATE_RULE, rules)
        # 不同规则各一条，互不抑制。
        self.assertEqual(2, len(rules))


class DispositionAndHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        feed(self.app, FAN, FAN_TEMP, 90.0, times=2)  # t=0/10，危险确认延时 5s
        self.alarm_id = active_alarms(self.app)[0]["alarm_id"]

    def test_danger_carries_load_reduction_advisory(self) -> None:
        advisory = self.app.rotating.machine_advisory(FAN)
        self.assertEqual("danger", advisory["severity"])
        self.assertEqual("reduce-load", advisory["action"])
        self.assertEqual(70.0, advisory["target_load_pct"])

    def test_dispositions_recorded_and_finalize_requires_recovery(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.rotating.dispose("ops", alarm_id=self.alarm_id, disposition="finalize")
        self.app.rotating.acknowledge(
            "ops", machine_id=FAN, point_id=FAN_TEMP, rule_code=TEMP_RULE, note="已知悉"
        )
        self.app.rotating.dispose(
            "ops", alarm_id=self.alarm_id, disposition=DISPOSITION_REDUCE_LOAD, note="风门70%"
        )
        self.app.rotating.dispose(
            "ops", alarm_id=self.alarm_id, disposition=DISPOSITION_INSPECT, note="现场测振正常"
        )
        detail = self.app.rotating.alarm_detail(self.alarm_id)
        self.assertEqual(
            ["acknowledge", DISPOSITION_REDUCE_LOAD, DISPOSITION_INSPECT],
            [item["disposition"] for item in detail["dispositions"]],
        )
        steady(self.app, FAN, FAN_TEMP, 60.0, times=7)  # 释放延时 60s（首回落点起算）
        # 已确认的报警恢复后自动归档关闭。
        self.assertEqual(STATUS_CLOSED, self.app.rotating.alarm_detail(self.alarm_id)["status"])

    def test_unacknowledged_recovery_waits_for_review_then_close(self) -> None:
        steady(self.app, FAN, FAN_TEMP, 60.0, times=7)
        detail = self.app.rotating.alarm_detail(self.alarm_id)
        self.assertEqual(STATUS_RECOVERED, detail["status"])
        closed = self.app.rotating.dispose(
            "ops", alarm_id=self.alarm_id, disposition="finalize", note="复盘关闭"
        )
        self.assertEqual(STATUS_CLOSED, closed["status"])
        # 已关闭的报警默认不出现在活动列表。
        self.assertEqual([], active_alarms(self.app))
        self.assertEqual(
            1, len(self.app.rotating.list_alarms(machine_id=FAN, include_closed=True))
        )

    def test_closed_alarm_rejects_further_disposition(self) -> None:
        steady(self.app, FAN, FAN_TEMP, 60.0, times=7)
        self.app.rotating.dispose("ops", alarm_id=self.alarm_id, disposition="finalize")
        with self.assertRaises(GuardViolation):
            self.app.rotating.dispose(
                "ops", alarm_id=self.alarm_id, disposition=DISPOSITION_INSPECT
            )

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.rotating.ingest("daq", machine_id="ghost", point_id="x", value=1.0)
        with self.assertRaises(ValidationError):
            self.app.rotating.ingest("daq", machine_id=FAN, point_id="ghost", value=1.0)
        with self.assertRaises(ValidationError):
            self.app.rotating.ingest("daq", machine_id=FAN, point_id=FAN_TEMP, value=-1.0)
        with self.assertRaises(NotFoundError):
            self.app.rotating.alarm_detail("no-such-alarm")
        with self.assertRaises(ValidationError):
            self.app.rotating.dispose("ops", alarm_id=self.alarm_id, disposition="explode")

    def test_machine_history_contains_trend_alarms_and_dispositions(self) -> None:
        self.app.rotating.acknowledge(
            "ops", machine_id=FAN, point_id=FAN_TEMP, rule_code=TEMP_RULE
        )
        history = self.app.rotating.history_for_machine(FAN)
        self.assertGreaterEqual(history["sample_count"], 2)
        self.assertEqual(1, len(history["alarms"]))
        self.assertEqual(1, len(history["dispositions"]))
        trend = self.app.rotating.trend(FAN, FAN_TEMP, limit=10)
        self.assertEqual(history["sample_count"], trend["count"])

    def test_notification_read_marker(self) -> None:
        self.assertGreaterEqual(self.app.rotating.fleet_status()["unread_notifications"], 1)
        self.app.rotating.mark_all_read("ops")
        self.assertEqual(0, self.app.rotating.fleet_status()["unread_notifications"])
        self.assertEqual(0, self.app.rotating.notifications(only_unread=True)["count"])


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_active_alarm_survives_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        feed(app, FAN, FAN_TEMP, 78.0, times=4)
        alarm_id = active_alarms(app)[0]["alarm_id"]

        restarted = Application(app.settings, clock=app.clock)
        self.assertEqual(
            alarm_id,
            restarted.rotating.list_alarms(machine_id=FAN, include_closed=False)[0]["alarm_id"],
        )
        # 重启后恢复状态机，恢复流程仍可走完。
        steady(restarted, FAN, FAN_TEMP, 60.0, times=7)
        statuses = {
            item["alarm_id"]: item["status"]
            for item in restarted.rotating.list_alarms(machine_id=FAN, include_closed=True)
        }
        self.assertEqual(STATUS_RECOVERED, statuses[alarm_id])
        self.assertTrue(restarted.store.verify().ok)

    def test_audit_records_ingest_and_disposition(self) -> None:
        feed(self.app, FAN, FAN_TEMP, 90.0, times=2)  # 危险确认延时 5s
        alarm_id = active_alarms(self.app)[0]["alarm_id"]
        self.app.rotating.acknowledge(
            "ops", machine_id=FAN, point_id=FAN_TEMP, rule_code=TEMP_RULE
        )
        self.app.rotating.dispose(
            "ops", alarm_id=alarm_id, disposition=DISPOSITION_INSPECT, note="检查"
        )
        actions = {event.action for event in self.app.audit.query(limit=200)}
        self.assertIn("ingest", actions)
        self.assertIn("acknowledge", actions)
        events = self.app.audit.query(limit=200, action="dispose")
        self.assertIn(alarm_id, [event.target for event in events])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
