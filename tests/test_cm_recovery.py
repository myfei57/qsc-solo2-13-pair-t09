"""状态监测的重启恢复：台账、活动报警、最近采样都从落盘流水重建。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.config import Settings
from flashsmelter.runtime import ManualClock

from .helpers import make_root

FAN = "F-1001"
VIB = "F-1001-vib"


class MonitorRecoveryTest(unittest.TestCase):
    def _fresh(self, root, *, start: float = 1_700_000_000.0) -> Application:
        clock = ManualClock(start)
        return Application(Settings(root=root), clock=clock)

    def test_active_alarm_and_ack_survive_restart(self) -> None:
        root = make_root()
        app = self._fresh(root)
        for _ in range(3):
            app.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=5.4)
            app.clock.advance(5)
        alarm_id = app.cm.status()["active_alarms"][0]["alarm_id"]
        app.cm.acknowledge("ops-zhao", alarm_id=alarm_id, note="接班注意")
        epoch = app.clock.timestamp()

        restarted = self._fresh(root, start=epoch)
        status = restarted.cm.status()
        self.assertEqual(2, status["equipment_count"])
        self.assertEqual(1, status["active_alarm_count"])
        active = status["active_alarms"][0]
        self.assertEqual(alarm_id, active["alarm_id"])
        self.assertEqual("warning", active["level"])
        self.assertTrue(active["acked"])
        self.assertEqual("ops-zhao", active["acked_by"])
        point = restarted.cm.equipment_status(FAN)["points"][0]
        self.assertEqual(5.4, point["last_value"])

    def test_cleared_alarm_not_active_after_restart_but_kept_in_history(self) -> None:
        root = make_root()
        app = self._fresh(root)
        for _ in range(3):
            app.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=5.4)
            app.clock.advance(5)
        for _ in range(3):
            app.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=2.0)
            app.clock.advance(5)
        self.assertEqual(0, app.cm.status()["active_alarm_count"])
        epoch = app.clock.timestamp()

        restarted = self._fresh(root, start=epoch)
        self.assertEqual(0, restarted.cm.status()["active_alarm_count"])
        history = restarted.cm.alarm_history(equipment_id=FAN)
        self.assertEqual(1, history["count"])
        self.assertTrue(history["alarms"][0]["cleared"])

    def test_occurrence_counter_continues_after_restart(self) -> None:
        root = make_root()
        app = self._fresh(root)
        # 产生 #A001，解除，再让冷却窗到期自动关闭
        for _ in range(3):
            app.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=5.4)
            app.clock.advance(5)
        for _ in range(3):
            app.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=2.0)
            app.clock.advance(5)
        app.clock.advance(app.settings.cm_renotify_cooldown_seconds + 1)
        for _ in range(3):
            app.cm.ingest("gw", equipment_id=FAN, point_id=VIB, value=5.4)
            app.clock.advance(5)
        self.assertTrue(
            app.cm.status()["active_alarms"][0]["alarm_id"].endswith("#A002")
        )
        epoch = app.clock.timestamp()

        restarted = self._fresh(root, start=epoch)
        for _ in range(3):
            restarted.cm.ingest("gw", equipment_id="M-2001", point_id="M-2001-vib", value=8.0)
        # 风机侧下一次报警继续编号，不与历史撞号
        for _ in range(3):
            result = restarted.cm.ingest(
                "gw", equipment_id="M-2001", point_id="M-2001-vib", value=8.0
            )
        self.assertEqual("M-2001-vib#A001", result["alarm"]["alarm_id"])

    def test_custom_thresholds_persist_in_catalog(self) -> None:
        root = make_root()
        app = self._fresh(root)
        app.cm.register_equipment(
            "ops",
            equipment_id="F-9001",
            kind="fan",
            label="引风机 F-9001",
            threshold_overrides={"vibration": {"warn": 3.5, "critical": 6.0, "clear": 2.8}},
        )
        epoch = app.clock.timestamp()
        restarted = self._fresh(root, start=epoch)
        equipment = restarted.cm.equipment_status("F-9001")
        vib = next(p for p in equipment["points"] if p["id"] == "F-9001-vib")
        self.assertEqual(3.5, vib["thresholds"]["warn"])
        self.assertEqual(6.0, vib["thresholds"]["critical"])


if __name__ == "__main__":
    unittest.main()
