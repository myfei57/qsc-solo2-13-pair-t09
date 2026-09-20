"""测点阈值与单次采样评估。

口径：

* 振动采用 ISO 20816-3 对中型回转机械（15~300 kW、额定 120~15000 r/min）
  的振动速度均方根分区：正常 / 报警（预警）/ 危险（联锁建议）；
* 轴承温度采用滑动/滚动轴承运行经验区间：预警线、危险线，并带回差，
  数值回到危险线的 ``clear_ratio`` 比例以下才解除，避免在阈值附近反复刷；
* 温度上升率（°C/min）作为早期征兆，与绝对值独立判定，取更高级别。

这里只做纯计算，便于单测复现「正常 → 预警 → 危险 → 回落解除」全过程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

NORMAL = "normal"
WARNING = "warning"
CRITICAL = "critical"

# 严重度排序：数值越大越严重，比较与升级都按它来。
LEVELS: tuple[str, ...] = (NORMAL, WARNING, CRITICAL)

_RANK = {NORMAL: 0, WARNING: 1, CRITICAL: 2}


def level_rank(level: str) -> int:
    return _RANK[level]


@dataclass(frozen=True, slots=True)
class ThresholdSpec:
    """一个测点的报警区间。

    warn/critical 是越线值；``clear`` 是回差解除线。绝对类测点（振动速度、
    温度）用「低于 clear 才解除」；温升率没有滞回需求时 clear 取与 warn 相同。
    """

    metric: str
    unit: str
    warn: float
    critical: float
    clear: float
    description: str = ""

    def __post_init__(self) -> None:
        if not self.warn < self.critical:
            raise ValueError("报警线必须严格小于危险线")
        if not 0.0 <= self.clear <= self.warn:
            raise ValueError("回差解除线必须位于 0 与报警线之间")

    def level_for(self, value: float) -> str:
        if value >= self.critical:
            return CRITICAL
        if value >= self.warn:
            return WARNING
        return NORMAL

    def cleared_for(self, value: float) -> bool:
        """配合滞回：数值已回落到解除线以下。"""

        return value <= self.clear

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "unit": self.unit,
            "warn": self.warn,
            "critical": self.critical,
            "clear": self.clear,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class Evaluation:
    """一次采样在单个测点上的判定结果（绝对量与温升率可能各贡献一条理由）。"""

    point_id: str
    value: float
    level: str
    reasons: tuple[str, ...]
    rate_per_min: float | None
    specs: Mapping[str, ThresholdSpec]

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "value": self.value,
            "level": self.level,
            "reasons": list(self.reasons),
            "rate_per_min": self.rate_per_min,
            "thresholds": {key: spec.to_dict() for key, spec in self.specs.items()},
        }


def evaluate(
    point_id: str,
    value: float,
    *,
    absolute: ThresholdSpec,
    rate: ThresholdSpec | None = None,
    previous_value: float | None = None,
    elapsed_seconds: float | None = None,
) -> Evaluation:
    """评估单个测点的一次采样。

    温升率需要上一个采样值与时间间隔（秒）；间隔为零或过短（< 1 秒）时不判
    温升率，避免除零和尖峰误报。
    """

    reasons: list[str] = []
    level = absolute.level_for(value)
    if level != NORMAL:
        reasons.append(
            f"{absolute.metric} {value:.2f} {absolute.unit} 越过"
            f"{'危险线' if level == CRITICAL else '预警线'} "
            f"{absolute.critical if level == CRITICAL else absolute.warn:g}"
        )

    rate_per_min: float | None = None
    specs: dict[str, ThresholdSpec] = {"absolute": absolute}
    if (
        rate is not None
        and previous_value is not None
        and elapsed_seconds is not None
        and elapsed_seconds >= 1.0
    ):
        rate_per_min = (value - previous_value) * 60.0 / elapsed_seconds
        # 只对快速升温告警；快速降温不构成轴承风险。
        if rate_per_min >= rate.critical:
            rate_level = CRITICAL
        elif rate_per_min >= rate.warn:
            rate_level = WARNING
        else:
            rate_level = NORMAL
        specs["rate"] = rate
        if rate_level != NORMAL:
            reasons.append(
                f"温升率 {rate_per_min:+.2f} {rate.unit} 越过"
                f"{'危险线' if rate_level == CRITICAL else '预警线'} "
                f"{rate.critical if rate_level == CRITICAL else rate.warn:g}"
            )
            if _RANK[rate_level] > _RANK[level]:
                level = rate_level

    return Evaluation(
        point_id=point_id,
        value=value,
        level=level,
        reasons=tuple(reasons),
        rate_per_min=None if rate_per_min is None else round(rate_per_min, 3),
        specs=specs,
    )


__all__ = [
    "NORMAL",
    "WARNING",
    "CRITICAL",
    "LEVELS",
    "ThresholdSpec",
    "Evaluation",
    "evaluate",
    "level_rank",
]
