"""转动设备监测。

风机、磨机这类大机组没有人能 24 小时盯着振动和轴承温度。本组件把各测点的
在线采样收进来做趋势判断：

* 绝对阈值两级报警（预警/危险），带确认延时与释放延时，避免单点毛刺刷屏；
* 温升率/振值爬升率报警，抓「还没超线但在快速劣化」的早期故障；
* 同一设备同一测点同一类报警在一次持续期间只发一条（再提醒由升级产生）；
* 预警先通知值班员；未在时限内确认的预警自动升级；危险级直接给出降负荷建议；
* 每台设备的历史趋势、报警全过程与处置记录都可按机台调阅。

监测组件是只读建议型的：它不直接驱动执行机构，降负荷仍由值班员确认后通过
原有工艺指令执行。
"""

from .catalog import (
    DEFAULT_FLEET,
    MachineSpec,
    PointSpec,
    build_default_fleet,
    default_catalog,
)
from .component import RotatingMonitor
from .engine import AlarmEvent, AlarmLevel, RuleEngine, ThresholdRule

__all__ = [
    "RotatingMonitor",
    "RuleEngine",
    "ThresholdRule",
    "AlarmLevel",
    "AlarmEvent",
    "MachineSpec",
    "PointSpec",
    "DEFAULT_FLEET",
    "build_default_fleet",
    "default_catalog",
]
