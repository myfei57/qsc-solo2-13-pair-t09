"""机组与测点台账。

一台机组（风机/磨机）挂若干测点：振动速度（mm/s RMS）与轴承温度（°C），
同一台机组的多个轴承各占一个温度测点。阈值先按机组类型给缺省，再允许逐机组
覆盖——同型号机组工况可能不同，台账是「类型缺省 + 单台覆盖」的两层口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..errors import ValidationError
from .thresholds import ThresholdSpec

VIBRATION = "vibration_rms"
BEARING_TEMP = "bearing_temp"

POINT_KINDS: tuple[str, ...] = (VIBRATION, BEARING_TEMP)

# 机组类型缺省阈值。振动区间参考 ISO 20816-3 中型回转机械；
# 温度按滚动轴承常见预警/危险温度，回差统一取危险线的 0.9。
EQUIPMENT_TYPES: Mapping[str, Mapping[str, Any]] = {
    "fan": {
        "label": "风机",
        VIBRATION: {"warn": 4.5, "critical": 7.1, "clear": 3.5},
        BEARING_TEMP: {"warn": 75.0, "critical": 85.0, "clear": 70.0},
    },
    "mill": {
        "label": "磨机",
        VIBRATION: {"warn": 7.1, "critical": 11.0, "clear": 5.6},
        BEARING_TEMP: {"warn": 70.0, "critical": 80.0, "clear": 65.0},
    },
}

# 温度上升率缺省（°C/min），绝对值之外的早期征兆。
DEFAULT_TEMP_RATE = {"warn": 2.0, "critical": 5.0, "clear": 1.0}

_METRIC_META = {
    VIBRATION: ("振动速度", "mm/s RMS"),
    BEARING_TEMP: ("轴承温度", "°C"),
}


@dataclass(frozen=True, slots=True)
class MonitorPoint:
    """一个物理测点：测哪种量、装在哪个位置、用哪组阈值。"""

    id: str
    equipment_id: str
    kind: str
    location: str
    absolute: ThresholdSpec
    rate: ThresholdSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "equipment_id": self.equipment_id,
            "kind": self.kind,
            "location": self.location,
            "metric": self.absolute.metric,
            "unit": self.absolute.unit,
            "thresholds": self.absolute.to_dict(),
            "rate_thresholds": None if self.rate is None else self.rate.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Equipment:
    """一台被监测的大机组。"""

    id: str
    kind: str
    label: str
    points: tuple[MonitorPoint, ...] = field(default_factory=tuple)

    def point(self, point_id: str) -> MonitorPoint:
        for candidate in self.points:
            if candidate.id == point_id:
                return candidate
        raise ValidationError(
            "测点不属于该机组或不存在",
            details={"equipment_id": self.id, "point_id": point_id},
        )

    def has_point(self, point_id: str) -> bool:
        return any(candidate.id == point_id for candidate in self.points)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "points": [point.to_dict() for point in self.points],
        }


def _build_spec(
    kind: str,
    point_kind: str,
    *,
    metric: str,
    unit: str,
    overrides: Mapping[str, float] | None = None,
) -> ThresholdSpec:
    defaults = EQUIPMENT_TYPES[kind][point_kind]
    values = dict(defaults)
    if overrides:
        for key in ("warn", "critical", "clear"):
            if key in overrides:
                values[key] = float(overrides[key])  # type: ignore[arg-type]
    return ThresholdSpec(
        metric=metric,
        unit=unit,
        warn=float(values["warn"]),
        critical=float(values["critical"]),
        clear=float(values["clear"]),
        description=f"{EQUIPMENT_TYPES[kind]['label']}{metric}阈值",
    )


def build_default_equipment(
    equipment_id: str,
    kind: str,
    *,
    label: str | None = None,
    bearing_count: int = 2,
    threshold_overrides: Mapping[str, Mapping[str, float]] | None = None,
) -> Equipment:
    """按类型生成标准测点配置：1 个驱动端振动点 + N 个轴承温度点。

    ``threshold_overrides`` 的键形如 ``vibration``、``bearing_1_temp``，
    值可覆盖 ``warn``/``critical``/``clear`` 任意几项。
    """

    if kind not in EQUIPMENT_TYPES:
        raise ValidationError(
            "未知机组类型", details={"kind": kind, "known": sorted(EQUIPMENT_TYPES)}
        )
    if bearing_count < 1:
        raise ValidationError("轴承数量至少为 1", details={"bearing_count": bearing_count})

    label = label or f"{EQUIPMENT_TYPES[kind]['label']}-{equipment_id}"
    overrides = dict(threshold_overrides or {})
    points: list[MonitorPoint] = []

    vib_metric, vib_unit = _METRIC_META[VIBRATION]
    points.append(
        MonitorPoint(
            id=f"{equipment_id}-vib",
            equipment_id=equipment_id,
            kind=VIBRATION,
            location="驱动端轴承座",
            absolute=_build_spec(
                kind, VIBRATION, metric=vib_metric, unit=vib_unit,
                overrides=overrides.get("vibration"),
            ),
        )
    )
    temp_metric, temp_unit = _METRIC_META[BEARING_TEMP]
    for index in range(1, bearing_count + 1):
        override_key = f"bearing_{index}_temp"
        points.append(
            MonitorPoint(
                id=f"{equipment_id}-brg{index}-temp",
                equipment_id=equipment_id,
                kind=BEARING_TEMP,
                location=f"{index}号轴承",
                absolute=_build_spec(
                    kind, BEARING_TEMP, metric=temp_metric, unit=temp_unit,
                    overrides=overrides.get(override_key),
                ),
                rate=ThresholdSpec(
                    metric="轴承温升率",
                    unit="°C/min",
                    warn=DEFAULT_TEMP_RATE["warn"],
                    critical=DEFAULT_TEMP_RATE["critical"],
                    clear=DEFAULT_TEMP_RATE["clear"],
                ),
            )
        )
    return Equipment(id=equipment_id, kind=kind, label=label, points=tuple(points))


def equipment_from_dict(payload: Mapping[str, Any]) -> Equipment:
    """从落盘台账恢复一台机组（含被覆盖过的阈值）。"""

    equipment_id = str(payload["id"])
    kind = str(payload["kind"])
    if kind not in EQUIPMENT_TYPES:
        raise ValidationError("台账中的机组类型未知", details={"equipment_id": equipment_id, "kind": kind})
    points: list[MonitorPoint] = []
    raw_points: Iterable[Any] = payload.get("points", ())
    for raw in raw_points:
        point_id = str(raw["id"])
        point_kind = str(raw["kind"])
        if point_kind not in POINT_KINDS:
            raise ValidationError("测点类型未知", details={"point_id": point_id, "kind": point_kind})
        thresholds = raw["thresholds"]
        rate_raw = raw.get("rate_thresholds")
        points.append(
            MonitorPoint(
                id=point_id,
                equipment_id=equipment_id,
                kind=point_kind,
                location=str(raw.get("location", "")),
                absolute=ThresholdSpec(
                    metric=str(thresholds.get("metric", raw.get("metric", ""))),
                    unit=str(thresholds.get("unit", raw.get("unit", ""))),
                    warn=float(thresholds["warn"]),
                    critical=float(thresholds["critical"]),
                    clear=float(thresholds["clear"]),
                    description=str(thresholds.get("description", "")),
                ),
                rate=None
                if not rate_raw
                else ThresholdSpec(
                    metric=str(rate_raw.get("metric", "温升率")),
                    unit=str(rate_raw.get("unit", "")),
                    warn=float(rate_raw["warn"]),
                    critical=float(rate_raw["critical"]),
                    clear=float(rate_raw["clear"]),
                ),
            )
        )
    return Equipment(
        id=equipment_id,
        kind=kind,
        label=str(payload.get("label", equipment_id)),
        points=tuple(points),
    )


__all__ = [
    "VIBRATION",
    "BEARING_TEMP",
    "POINT_KINDS",
    "EQUIPMENT_TYPES",
    "Equipment",
    "MonitorPoint",
    "build_default_equipment",
    "equipment_from_dict",
]
