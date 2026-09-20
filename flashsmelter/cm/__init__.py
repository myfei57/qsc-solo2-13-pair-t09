"""大机组在线监测：振动与轴承温度趋势、分级报警与处置记录。

子模块分工：

* :mod:`thresholds` —— 测点阈值（ISO 20816 振动速度区间、轴承温度区间、
  温升率）与单次采样评估，纯函数、无副作用；
* :mod:`equipment` —— 机组与测点台账，阈值按机组类型给缺省、逐机组可覆盖；
* :mod:`journal` —— 趋势采样与报警生命周期事件的两条追加流水；
* :mod:`monitor` —— 监测组件：收样、趋势判断、报警去抖/抑制/升级、
  确认与处置记录、历史查询。
"""

from __future__ import annotations

from .equipment import (
    BEARING_TEMP,
    EQUIPMENT_TYPES,
    VIBRATION,
    Equipment,
    MonitorPoint,
)
from .journal import ALARM_STREAM, TREND_STREAM, AlarmJournal, TrendJournal
from .monitor import (
    DISPOSITION_EVENTS,
    DISPOSITION_LABELS,
    RECOMMENDATIONS,
    ConditionMonitor,
    InMemoryNotifier,
    Monitor,
    Notifier,
)
from .thresholds import (
    CRITICAL,
    NORMAL,
    WARNING,
    LEVELS,
    ThresholdSpec,
    evaluate,
    level_rank,
)

__all__ = [
    "ConditionMonitor",
    "Monitor",
    "Equipment",
    "MonitorPoint",
    "EQUIPMENT_TYPES",
    "VIBRATION",
    "BEARING_TEMP",
    "ThresholdSpec",
    "evaluate",
    "level_rank",
    "LEVELS",
    "NORMAL",
    "WARNING",
    "CRITICAL",
    "TrendJournal",
    "AlarmJournal",
    "TREND_STREAM",
    "ALARM_STREAM",
]
