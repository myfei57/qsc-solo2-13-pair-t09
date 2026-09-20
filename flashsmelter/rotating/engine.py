"""报警判定引擎（纯逻辑，不碰持久化）。

每个「测点 + 规则」（绝对阈值或变化率）由一个 :class:`RuleState` 跟踪。
设计目标对应现场的几条硬要求：

* **不刷同类报警**：一次持续超线只产生一条报警（``raised``），升级时在同一条
  报警上追加 ``escalated``，恢复后 ``recovered`` 关闭；下次再超是新的一条。
* **毛刺不报**：危险/预警各自有确认延时（on-delay），连续超限达到延时才报警；
  回落到释放带内还要持续释放延时（off-delay）才恢复，避免在阈值附近抖动。
* **预警先提醒**：预警 raised 即通知；值班员在
  ``rotating_ack_escalate_seconds`` 内没有确认，:meth:`RuleEngine.sweep` 会
  把它升级到危险；测点本身直接恶化为危险也会升级同一条报警。
* **趋势劣化也要抓**：变化率规则看窗口内的每分钟增量，叠加最小有效增量，
  避免低位噪声算出伪速率。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import ValidationError
from ..runtime import Clock
from .catalog import PointSpec, ThresholdRule

LEVEL_NONE = "none"
LEVEL_WARN = "warn"
LEVEL_DANGER = "danger"
_LEVEL_ORDER = {LEVEL_NONE: 0, LEVEL_WARN: 1, LEVEL_DANGER: 2}

# 报警生命周期事件。
EVENT_RAISED = "raised"
EVENT_ESCALATED = "escalated"
EVENT_RECOVERED = "recovered"
EVENT_SWEEP_ESCALATED = "sweep-escalated"


@dataclass(frozen=True, slots=True)
class AlarmLevel:
    """一次规则评估得到的原始级别。"""

    level: str
    rule: str
    value: float
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> int:
        return _LEVEL_ORDER[self.level]


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    """一条报警生命周期事件（产生/升级/恢复），由组件落盘与通知。"""

    kind: str
    machine_id: str
    point_id: str
    rule_code: str
    level: str
    alarm_id: str
    at: str
    value: float
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """同类报警的去重键：机台 + 测点 + 规则。"""

        return f"{self.machine_id}/{self.point_id}/{self.rule_code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "machine_id": self.machine_id,
            "point_id": self.point_id,
            "rule_code": self.rule_code,
            "level": self.level,
            "alarm_id": self.alarm_id,
            "at": self.at,
            "value": round(self.value, 4),
            "detail": dict(self.detail),
        }


@dataclass(slots=True)
class RuleState:
    """单条规则的状态机。状态分两层：原始超限计数与已挂牌报警。"""

    machine_id: str
    point_id: str
    point_label: str
    rule_code: str
    rule_label: str
    unit: str
    kind: str  # absolute / rate
    over_level: str = LEVEL_NONE
    over_since: float | None = None  # 当前超线段的开始时刻
    clear_since: float | None = None  # 当前回线段的开始时刻（释放延时用）
    alarm_level: str = LEVEL_NONE
    alarm_id: str | None = None
    alarm_started_at: float | None = None
    acknowledged: bool = False
    escalated_by_sweep: bool = False
    last_value: float = 0.0
    escalated_severity: str = LEVEL_NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "point_id": self.point_id,
            "point_label": self.point_label,
            "rule_code": self.rule_code,
            "rule_label": self.rule_label,
            "unit": self.unit,
            "kind": self.kind,
            "over_level": self.over_level,
            "over_since": self.over_since,
            "clear_since": self.clear_since,
            "alarm_level": self.alarm_level,
            "alarm_id": self.alarm_id,
            "alarm_started_at": self.alarm_started_at,
            "acknowledged": self.acknowledged,
            "escalated_by_sweep": self.escalated_by_sweep,
            "last_value": self.last_value,
            "escalated_severity": self.escalated_severity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuleState":
        return cls(
            machine_id=str(payload["machine_id"]),
            point_id=str(payload["point_id"]),
            point_label=str(payload.get("point_label", payload["point_id"])),
            rule_code=str(payload["rule_code"]),
            rule_label=str(payload.get("rule_label", payload["rule_code"])),
            unit=str(payload.get("unit", "")),
            kind=str(payload.get("kind", "absolute")),
            over_level=str(payload.get("over_level", LEVEL_NONE)),
            over_since=payload.get("over_since"),
            clear_since=payload.get("clear_since"),
            alarm_level=str(payload.get("alarm_level", LEVEL_NONE)),
            alarm_id=payload.get("alarm_id"),
            alarm_started_at=payload.get("alarm_started_at"),
            acknowledged=bool(payload.get("acknowledged", False)),
            escalated_by_sweep=bool(payload.get("escalated_by_sweep", False)),
            last_value=float(payload.get("last_value", 0.0)),
            escalated_severity=str(payload.get("escalated_severity", LEVEL_NONE)),
        )


@dataclass(slots=True)
class _Sample:
    at: float
    value: float


class RuleEngine:
    """按机台/测点持有规则状态，评估采样并产出报警事件。"""

    def __init__(
        self,
        points: Mapping[str, Sequence[PointSpec]],
        clock: Clock,
        *,
        warn_on_delay: float,
        danger_on_delay: float,
        off_delay: float,
        ack_escalate_after: float,
        rate_window: float,
        rate_min_interval: float,
        deadband_pct: float,
    ) -> None:
        self._clock = clock
        self._warn_on_delay = warn_on_delay
        self._danger_on_delay = danger_on_delay
        self._off_delay = off_delay
        self._ack_escalate_after = ack_escalate_after
        self._rate_window = rate_window
        self._rate_min_interval = rate_min_interval
        self._deadband_pct = deadband_pct
        self._states: dict[tuple[str, str, str], RuleState] = {}
        self._histories: dict[tuple[str, str], deque[_Sample]] = {}
        for machine_id, specs in points.items():
            for spec in specs:
                self._register(machine_id, spec)

    # ------------------------------------------------------------------ 装配
    def _register(self, machine_id: str, spec: PointSpec) -> None:
        self._states[(machine_id, spec.point_id, spec.absolute.code)] = RuleState(
            machine_id=machine_id,
            point_id=spec.point_id,
            point_label=spec.label,
            rule_code=spec.absolute.code,
            rule_label=spec.absolute.label,
            unit=spec.absolute.unit,
            kind="absolute",
        )
        if spec.rate_warn_per_min is not None:
            rate_code = "rate-of-change"
            self._states[(machine_id, spec.point_id, rate_code)] = RuleState(
                machine_id=machine_id,
                point_id=spec.point_id,
                point_label=spec.label,
                rule_code=rate_code,
                rule_label=f"{spec.label}爬升过快",
                unit="/min",
                kind="rate",
            )
        self._histories[(machine_id, spec.point_id)] = deque()

    def states(self) -> tuple[RuleState, ...]:
        return tuple(self._states.values())

    def active_states(self) -> tuple[RuleState, ...]:
        return tuple(state for state in self._states.values() if state.alarm_level != LEVEL_NONE)

    def acknowledge(self, machine_id: str, point_id: str, rule_code: str) -> RuleState:
        state = self._require_state(machine_id, point_id, rule_code)
        if state.alarm_level == LEVEL_NONE:
            raise ValidationError(
                "该报警当前不活动，无需确认",
                details={"machine": machine_id, "point": point_id, "rule": rule_code},
            )
        state.acknowledged = True
        return state

    def is_active(self, machine_id: str, point_id: str, rule_code: str) -> bool:
        return self._states[(machine_id, point_id, rule_code)].alarm_level != LEVEL_NONE

    # ------------------------------------------------------------------ 评估
    def evaluate(self, machine_id: str, point: PointSpec, value: float) -> list[AlarmEvent]:
        """喂入一个测点的一次采样，返回本次产生的生命周期事件。"""

        if value != value or value < 0:
            raise ValidationError(
                "监测值必须是非负有限数",
                details={"machine": machine_id, "point": point.point_id, "value": value},
            )
        now = self._clock.timestamp()
        events: list[AlarmEvent] = []
        absolute = self._classify_absolute(point.absolute, value, now)
        history = self._histories[(machine_id, point.point_id)]
        history.append(_Sample(now, value))
        self._trim(history)
        rate = self._classify_rate(machine_id, point, history, value, now)
        classified = {point.absolute.code: absolute}
        if point.rate_warn_per_min is not None:
            classified["rate-of-change"] = rate
        for code, candidate in classified.items():
            state = self._states[(machine_id, point.point_id, code)]
            state.last_value = candidate.value
            events.extend(self._advance(state, candidate, now))
        return events

    def sweep(self) -> list[AlarmEvent]:
        """周期巡检：未确认预警超时升级。应由组件定期调用。"""

        now = self._clock.timestamp()
        events: list[AlarmEvent] = []
        for state in self._states.values():
            if (
                state.alarm_level == LEVEL_WARN
                and not state.acknowledged
                and not state.escalated_by_sweep
                and state.alarm_started_at is not None
                and now - state.alarm_started_at >= self._ack_escalate_after
            ):
                state.alarm_level = LEVEL_DANGER
                state.escalated_by_sweep = True
                state.escalated_severity = LEVEL_DANGER
                events.append(
                    self._event(EVENT_SWEEP_ESCALATED, state, state.last_value, now, reason="ack-timeout")
                )
        return events

    # ------------------------------------------------------------- 绝对/速率
    def _classify_absolute(self, rule: ThresholdRule, value: float, now: float) -> AlarmLevel:
        if value >= rule.danger:
            return AlarmLevel(LEVEL_DANGER, rule.code, value, {"threshold": rule.danger})
        if value >= rule.warn:
            return AlarmLevel(LEVEL_WARN, rule.code, value, {"threshold": rule.warn})
        # 死带：回落到预警线的 (1-deadband) 以下才算真正恢复，抑制边界抖动。
        release = rule.warn * (1.0 - self._deadband_pct)
        return AlarmLevel(LEVEL_NONE, rule.code, value, {"release_below": round(release, 4)})

    def _trim(self, history: deque[_Sample]) -> None:
        cutoff = self._clock.timestamp() - self._rate_window
        while history and history[0].at < cutoff:
            history.popleft()

    def _classify_rate(
        self,
        machine_id: str,
        point: PointSpec,
        history: deque[_Sample],
        value: float,
        now: float,
    ) -> AlarmLevel:
        assert point.rate_warn_per_min is not None
        # 取窗口内「距当前最近、且至少间隔 rate_min_interval」的基准点：它最能
        # 反映当前的爬升速度。基准太旧会把近期快速爬升被历史平稳段稀释掉。
        base: _Sample | None = None
        for sample in reversed(history):
            if now - sample.at >= self._rate_min_interval:
                base = sample
                break
        if base is None or now <= base.at:
            return AlarmLevel(LEVEL_NONE, "rate-of-change", 0.0, {"reason": "insufficient-history"})
        delta = value - base.value
        per_minute = delta * 60.0 / (now - base.at)
        detail = {
            "delta": round(delta, 4),
            "span_seconds": round(now - base.at, 3),
            "window_seconds": self._rate_window,
            "warn_per_min": point.rate_warn_per_min,
            "floor": point.rate_floor,
        }
        # 既要爬得快，也要有足够绝对增量；低位噪声不再误报。
        if per_minute >= point.rate_warn_per_min and delta >= point.rate_floor:
            return AlarmLevel(LEVEL_WARN, "rate-of-change", per_minute, detail)
        # 速率规则的恢复带：降到预警线的死带以下才视为平稳。
        detail["release_below"] = round(point.rate_warn_per_min * (1.0 - self._deadband_pct), 4)
        return AlarmLevel(LEVEL_NONE, "rate-of-change", per_minute, detail)

    # ------------------------------------------------------------- 状态推进
    def _advance(self, state: RuleState, candidate: AlarmLevel, now: float) -> list[AlarmEvent]:
        # 死带滞后只对绝对阈值规则生效：报警已挂出时，回落到「预警线以下但仍
        # 在释放带内」不算恢复，按当前报警级别维持，取消释放计时，也不重复通知。
        # 变化率规则的候选本身就带自己的 release_below，不能混用。
        if (
            state.kind == "absolute"
            and candidate.severity == 0
            and state.alarm_level != LEVEL_NONE
            and candidate.value >= float(candidate.detail.get("release_below", float("inf")))
        ):
            candidate = AlarmLevel(state.alarm_level, candidate.rule, candidate.value, candidate.detail)
        if candidate.severity > 0:
            return self._on_over(state, candidate, now)
        return self._on_clear(state, candidate, now)

    def _on_over(self, state: RuleState, candidate: AlarmLevel, now: float) -> list[AlarmEvent]:
        events: list[AlarmEvent] = []
        # 报警已挂出且处于释放观察期：瞬时再超限按噪声处理，保留首次回落
        # 计时（否则测点在释放带边缘反复抖动会让恢复永远无法完成）。
        if state.alarm_level != LEVEL_NONE and state.clear_since is not None:
            state.last_value = candidate.value
            return events
        state.clear_since = None
        if state.over_level == LEVEL_NONE:
            state.over_level = candidate.level
            state.over_since = now
        elif _LEVEL_ORDER[candidate.level] > _LEVEL_ORDER[state.over_level]:
            state.over_level = candidate.level
            state.over_since = now
        required = (
            self._danger_on_delay if state.over_level == LEVEL_DANGER else self._warn_on_delay
        )
        assert state.over_since is not None
        sustained = now - state.over_since >= required
        if not sustained:
            return events
        if state.alarm_level == LEVEL_NONE:
            state.alarm_level = state.over_level
            state.alarm_started_at = state.over_since
            state.alarm_id = self._alarm_id(state, now)
            state.acknowledged = False
            state.escalated_by_sweep = False
            state.escalated_severity = state.over_level
            state.clear_since = None  # 新报警挂出，旧的释放计时作废
            events.append(self._event(EVENT_RAISED, state, candidate.value, now, **candidate.detail))
        elif _LEVEL_ORDER[state.over_level] > _LEVEL_ORDER[state.alarm_level]:
            previous = state.alarm_level
            state.alarm_level = state.over_level
            state.escalated_severity = state.over_level
            # 工况直接恶化导致的升级：已确认也重新提醒一次（同一条报警）。
            events.append(
                self._event(
                    EVENT_ESCALATED,
                    state,
                    candidate.value,
                    now,
                    from_level=previous,
                    reason="severity-worsened",
                    **candidate.detail,
                )
            )
        return events

    def _on_clear(self, state: RuleState, candidate: AlarmLevel, now: float) -> list[AlarmEvent]:
        events: list[AlarmEvent] = []
        # 报警尚未挂出：on-delay 计时立即作废，单个毛刺不与后续超限合并。
        state.over_level = LEVEL_NONE
        state.over_since = None
        if state.alarm_level == LEVEL_NONE:
            state.clear_since = None
            return events
        # 报警已挂出：回落后要持续释放延时才允许恢复，期间再超则取消恢复。
        if state.clear_since is None:
            state.clear_since = now
        if now - state.clear_since < self._off_delay:
            return events
        events.append(self._event(EVENT_RECOVERED, state, candidate.value, now, **candidate.detail))
        self._reset_alarm(state)
        return events

    @staticmethod
    def _reset_alarm(state: RuleState) -> None:
        state.alarm_level = LEVEL_NONE
        state.alarm_id = None
        state.alarm_started_at = None
        state.acknowledged = False
        state.escalated_by_sweep = False
        state.escalated_severity = LEVEL_NONE
        state.clear_since = None

    def _event(
        self,
        kind: str,
        state: RuleState,
        value: float,
        now: float,
        **detail: Any,
    ) -> AlarmEvent:
        return AlarmEvent(
            kind=kind,
            machine_id=state.machine_id,
            point_id=state.point_id,
            rule_code=state.rule_code,
            level=state.alarm_level,
            alarm_id=state.alarm_id or self._alarm_id(state, now),
            at=self._clock.timestamp_iso(),
            value=value,
            detail={"rule_label": state.rule_label, "unit": state.unit, **detail},
        )

    def _alarm_id(self, state: RuleState, now: float) -> str:
        return f"{state.machine_id}-{state.point_id}-{state.rule_code}-{int(now * 1000)}"

    def _require_state(self, machine_id: str, point_id: str, rule_code: str) -> RuleState:
        try:
            return self._states[(machine_id, point_id, rule_code)]
        except KeyError as exc:
            raise ValidationError(
                "未知的报警规则",
                details={"machine": machine_id, "point": point_id, "rule": rule_code},
            ) from exc

    # ------------------------------------------------------------------ 持久
    def dump(self) -> dict[str, Any]:
        return {
            "states": [state.to_dict() for state in self._states.values()],
            "histories": [
                {
                    "machine_id": key[0],
                    "point_id": key[1],
                    "samples": [{"at": sample.at, "value": sample.value} for sample in history],
                }
                for key, history in self._histories.items()
            ],
        }

    def load(self, payload: Mapping[str, Any]) -> None:
        for raw in payload.get("states", ()):  # type: ignore[union-attr]
            if not isinstance(raw, Mapping):
                continue
            state = RuleState.from_dict(raw)
            key = (state.machine_id, state.point_id, state.rule_code)
            if key in self._states:
                self._states[key] = state
        for block in payload.get("histories", ()):  # type: ignore[union-attr]
            if not isinstance(block, Mapping):
                continue
            key = (str(block["machine_id"]), str(block["point_id"]))
            history = self._histories.get(key)
            if history is None:
                continue
            history.clear()
            for raw_sample in block.get("samples", ()):
                if isinstance(raw_sample, Mapping):
                    history.append(_Sample(at=float(raw_sample["at"]), value=float(raw_sample["value"])))
            self._trim(history)


__all__ = [
    "AlarmLevel",
    "AlarmEvent",
    "RuleEngine",
    "RuleState",
    "LEVEL_NONE",
    "LEVEL_WARN",
    "LEVEL_DANGER",
    "EVENT_RAISED",
    "EVENT_ESCALATED",
    "EVENT_RECOVERED",
    "EVENT_SWEEP_ESCALATED",
]
