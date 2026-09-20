"""设备台账：机台、测点与报警阈值。

阈值是逐台设备配置的——引风机与煤磨的转速、轴承形式不同，报警线也不同。
这里给出的是一组可直接投产评审的工程缺省值（参考 ISO 10816/20816 的
振动速度分区与滚动轴承温度常用联锁线），现场应以厂家手册与实测基线校准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..errors import ConfigurationError

# 测点度量类型。振动取轴承座振动速度有效值（mm/s RMS），温度取轴承温度（℃）。
METRIC_VIBRATION = "vibration_mm_s"
METRIC_TEMPERATURE = "temperature_c"

# 设备大类：决定危险级时给出的降负荷建议。
KIND_FAN = "fan"
KIND_MILL = "mill"


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """一条绝对阈值规则：预警线、危险线与单位。"""

    code: str
    label: str
    unit: str
    warn: float
    danger: float

    def validate(self) -> None:
        if self.warn <= 0 or self.danger <= 0:
            raise ConfigurationError("报警阈值必须为正", details={"rule": self.code})
        if self.warn >= self.danger:
            raise ConfigurationError(
                "预警线必须低于危险线",
                details={"rule": self.code, "warn": self.warn, "danger": self.danger},
            )


@dataclass(frozen=True, slots=True)
class PointSpec:
    """一个测点：一个绝对阈值规则，可再叠加一条变化率规则。

    ``rate_warn_per_min`` 为每分钟增量报警线（温度单位 ℃/min，振动 mm/s/min）；
    变化率报警只抓「快速爬升」，因此没有单独的危险线，持续未确认会走升级流程。
    ``rate_floor`` 为该测点的最小有效增量，过滤低位噪声算出的伪速率。
    """

    point_id: str
    label: str
    metric: str
    absolute: ThresholdRule
    rate_warn_per_min: float | None = None
    rate_floor: float = 0.0

    def validate(self) -> None:
        if not self.point_id:
            raise ConfigurationError("测点 ID 不能为空")
        if self.metric not in (METRIC_VIBRATION, METRIC_TEMPERATURE):
            raise ConfigurationError(
                "测点度量类型不支持",
                details={"point": self.point_id, "metric": self.metric},
            )
        self.absolute.validate()
        if self.rate_warn_per_min is not None and self.rate_warn_per_min <= 0:
            raise ConfigurationError(
                "变化率预警线必须为正",
                details={"point": self.point_id, "rate": self.rate_warn_per_min},
            )
        if self.rate_floor < 0:
            raise ConfigurationError(
                "变化率最小有效增量不能为负", details={"point": self.point_id}
            )


@dataclass(frozen=True, slots=True)
class MachineSpec:
    """一台转动设备：机台 ID、名称、大类、测点清单与危险级建议负荷比例。"""

    machine_id: str
    name: str
    kind: str
    points: tuple[PointSpec, ...]
    danger_target_load_pct: float = 60.0

    def validate(self) -> None:
        if not self.machine_id:
            raise ConfigurationError("设备 ID 不能为空")
        if self.kind not in (KIND_FAN, KIND_MILL):
            raise ConfigurationError(
                "设备大类不支持", details={"machine": self.machine_id, "kind": self.kind}
            )
        if not 10.0 <= self.danger_target_load_pct <= 90.0:
            raise ConfigurationError(
                "危险级建议负荷比例必须落在 10%~90%",
                details={"machine": self.machine_id, "target": self.danger_target_load_pct},
            )
        if not self.points:
            raise ConfigurationError("设备至少要有一个测点", details={"machine": self.machine_id})
        seen: set[str] = set()
        for point in self.points:
            point.validate()
            if point.point_id in seen:
                raise ConfigurationError(
                    "设备测点 ID 重复",
                    details={"machine": self.machine_id, "point": point.point_id},
                )
            seen.add(point.point_id)

    def point(self, point_id: str) -> PointSpec:
        for candidate in self.points:
            if candidate.point_id == point_id:
                return candidate
        raise ConfigurationError(
            "设备上不存在该测点",
            details={"machine": self.machine_id, "point": point_id},
        )


# 缺省机台：一台高温排烟引风机 + 一台煤磨，各含驱动端/非驱动端轴承的
# 振动与温度测点。阈值见模块文档说明，投产前用实测基线校准。
DEFAULT_FLEET: tuple[MachineSpec, ...] = (
    MachineSpec(
        machine_id="fan-ID01",
        name="高温排烟引风机",
        kind=KIND_FAN,
        danger_target_load_pct=70.0,
        points=(
            PointSpec(
                point_id="vib-de",
                label="驱动端轴承振动",
                metric=METRIC_VIBRATION,
                absolute=ThresholdRule("vibration-high", "轴承振动速度高", "mm/s", 4.5, 7.1),
                rate_warn_per_min=1.5,
                rate_floor=0.3,
            ),
            PointSpec(
                point_id="vib-nde",
                label="非驱动端轴承振动",
                metric=METRIC_VIBRATION,
                absolute=ThresholdRule("vibration-high", "轴承振动速度高", "mm/s", 4.5, 7.1),
                rate_warn_per_min=1.5,
                rate_floor=0.3,
            ),
            PointSpec(
                point_id="temp-de",
                label="驱动端轴承温度",
                metric=METRIC_TEMPERATURE,
                absolute=ThresholdRule("bearing-temp-high", "轴承温度高", "℃", 75.0, 85.0),
                rate_warn_per_min=3.0,
                rate_floor=1.0,
            ),
            PointSpec(
                point_id="temp-nde",
                label="非驱动端轴承温度",
                metric=METRIC_TEMPERATURE,
                absolute=ThresholdRule("bearing-temp-high", "轴承温度高", "℃", 75.0, 85.0),
                rate_warn_per_min=3.0,
                rate_floor=1.0,
            ),
        ),
    ),
    MachineSpec(
        machine_id="mill-M01",
        name="煤磨",
        kind=KIND_MILL,
        danger_target_load_pct=60.0,
        points=(
            PointSpec(
                point_id="vib-pinion-de",
                label="小齿轮驱动端轴承振动",
                metric=METRIC_VIBRATION,
                # 低速重载磨机的轴承座振动速度分区适当放宽，另需关注低速轴承专有指标。
                absolute=ThresholdRule("vibration-high", "轴承振动速度高", "mm/s", 7.1, 11.0),
                rate_warn_per_min=2.0,
                rate_floor=0.5,
            ),
            PointSpec(
                point_id="temp-pinion-de",
                label="小齿轮驱动端轴承温度",
                metric=METRIC_TEMPERATURE,
                absolute=ThresholdRule("bearing-temp-high", "轴承温度高", "℃", 70.0, 80.0),
                rate_warn_per_min=2.5,
                rate_floor=1.0,
            ),
            PointSpec(
                point_id="temp-motor-de",
                label="主电机驱动端轴承温度",
                metric=METRIC_TEMPERATURE,
                absolute=ThresholdRule("bearing-temp-high", "轴承温度高", "℃", 75.0, 85.0),
                rate_warn_per_min=3.0,
                rate_floor=1.0,
            ),
        ),
    ),
)


def build_default_fleet() -> dict[str, MachineSpec]:
    """校验后返回以机台 ID 为键的缺省台账。"""

    catalog: dict[str, MachineSpec] = {}
    for machine in DEFAULT_FLEET:
        machine.validate()
        if machine.machine_id in catalog:
            raise ConfigurationError("设备 ID 重复", details={"machine": machine.machine_id})
        catalog[machine.machine_id] = machine
    return catalog


def default_catalog() -> Mapping[str, MachineSpec]:
    return build_default_fleet()


__all__ = [
    "ThresholdRule",
    "PointSpec",
    "MachineSpec",
    "DEFAULT_FLEET",
    "KIND_FAN",
    "KIND_MILL",
    "METRIC_VIBRATION",
    "METRIC_TEMPERATURE",
    "build_default_fleet",
    "default_catalog",
]
