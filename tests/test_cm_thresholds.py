"""阈值与单次采样评估：振动绝对量、轴承温度与温升率。"""

from __future__ import annotations

import unittest

from flashsmelter.cm.thresholds import (
    CRITICAL,
    NORMAL,
    WARNING,
    ThresholdSpec,
    evaluate,
    level_rank,
)


def temp_spec() -> ThresholdSpec:
    return ThresholdSpec(metric="轴承温度", unit="°C", warn=75.0, critical=85.0, clear=70.0)


class ThresholdTest(unittest.TestCase):
    def test_level_partitions(self) -> None:
        spec = ThresholdSpec(metric="振动速度", unit="mm/s", warn=4.5, critical=7.1, clear=3.5)
        self.assertEqual(NORMAL, spec.level_for(4.49))
        self.assertEqual(WARNING, spec.level_for(4.5))
        self.assertEqual(CRITICAL, spec.level_for(7.1))

    def test_hysteresis_clear_line(self) -> None:
        spec = temp_spec()
        self.assertFalse(spec.cleared_for(72.0))
        self.assertTrue(spec.cleared_for(70.0))

    def test_invalid_spec_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ThresholdSpec(metric="x", unit="u", warn=8.0, critical=5.0, clear=1.0)
        with self.assertRaises(ValueError):
            ThresholdSpec(metric="x", unit="u", warn=4.0, critical=5.0, clear=4.5)

    def test_level_rank_ordering(self) -> None:
        self.assertLess(level_rank(NORMAL), level_rank(WARNING))
        self.assertLess(level_rank(WARNING), level_rank(CRITICAL))


class EvaluateTest(unittest.TestCase):
    def test_temperature_rate_triggers_early_warning(self) -> None:
        rate = ThresholdSpec(metric="温升率", unit="°C/min", warn=2.0, critical=5.0, clear=1.0)
        # 绝对值 60°C 正常，但 30 秒涨 3 度 => 6°C/min 危险
        result = evaluate(
            "p",
            60.0,
            absolute=temp_spec(),
            rate=rate,
            previous_value=57.0,
            elapsed_seconds=30.0,
        )
        self.assertEqual(CRITICAL, result.level)
        self.assertEqual(6.0, result.rate_per_min)
        self.assertTrue(any("温升率" in reason for reason in result.reasons))

    def test_absolute_and_rate_take_worse_level(self) -> None:
        rate = ThresholdSpec(metric="温升率", unit="°C/min", warn=2.0, critical=5.0, clear=1.0)
        result = evaluate(
            "p",
            76.0,
            absolute=temp_spec(),
            rate=rate,
            previous_value=75.5,
            elapsed_seconds=30.0,
        )
        self.assertEqual(WARNING, result.level)
        self.assertTrue(any("轴承温度" in reason for reason in result.reasons))

    def test_cooling_down_does_not_alarm_on_rate(self) -> None:
        rate = ThresholdSpec(metric="温升率", unit="°C/min", warn=2.0, critical=5.0, clear=1.0)
        result = evaluate(
            "p",
            60.0,
            absolute=temp_spec(),
            rate=rate,
            previous_value=65.0,
            elapsed_seconds=30.0,
        )
        self.assertEqual(NORMAL, result.level)
        self.assertEqual(-10.0, result.rate_per_min)

    def test_short_interval_skips_rate(self) -> None:
        rate = ThresholdSpec(metric="温升率", unit="°C/min", warn=2.0, critical=5.0, clear=1.0)
        result = evaluate(
            "p",
            60.0,
            absolute=temp_spec(),
            rate=rate,
            previous_value=50.0,
            elapsed_seconds=0.2,
        )
        self.assertIsNone(result.rate_per_min)


if __name__ == "__main__":
    unittest.main()
