"""大机组状态监测组件。

职责对应值班口径：

1. **连续盯梢**：采集网关把振动速度/轴承温度采样送进来（``ingest``），
   逐点做阈值与温升率判定，全部进趋势流水；
2. **先提醒、后处置**：连续 ``cm_debounce_samples`` 次越线才产生报警并提醒，
   预警只提示巡检，危险才建议降负荷——系统只建议，降不降由人决定并留痕；
3. **同类不刷屏**：同一测点在一次活动报警期间，持续越线不重复建报警；
   预警升危险只追加一次升级；回落有回差 + 连续 ``cm_clear_samples`` 次
   确认才解除；解除后 ``cm_renotify_cooldown_seconds`` 内再越线记为同一次
   报警再激活、不重复提醒；
4. **处置闭环**：确认（ack）、已降负荷（derate）、现场检查（inspect）、
   消音抑制（suppress）、备注（note）、关闭（close）全部是带操作者的
   生命周期事件，跟趋势一样可按机组逐条调出。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ..component import Component, ensure_actor
from ..errors import NotFoundError, ValidationError
from ..runtime import RuntimeContext, epoch_from_iso
from .equipment import (
    Equipment,
    MonitorPoint,
    build_default_equipment,
    equipment_from_dict,
)
from .journal import (
    ALARM_STREAM,
    EVENT_ACKED,
    EVENT_CLEARED,
    EVENT_CLOSED,
    EVENT_DERATED,
    EVENT_DETECTED,
    EVENT_ESCALATED,
    EVENT_NOTE,
    EVENT_RAISED,
    EVENT_REACTIVATED,
    EVENT_SUPPRESSED,
    TREND_STREAM,
    AlarmEvent,
    AlarmJournal,
    TrendJournal,
)
from .thresholds import CRITICAL, NORMAL, WARNING, evaluate, level_rank

# 处置动作 → 生命周期事件。
DISPOSITION_EVENTS: Mapping[str, str] = {
    "derate": EVENT_DERATED,
    "inspect": EVENT_DETECTED,
    "suppress": EVENT_SUPPRESSED,
    "close": EVENT_CLOSED,
}

DISPOSITION_LABELS: Mapping[str, str] = {
    "derate": "已降负荷",
    "inspect": "现场检查确认",
    "suppress": "消音抑制（继续监测，不再重复提醒）",
    "close": "关闭报警",
}

RECOMMENDATIONS: Mapping[str, str] = {
    WARNING: "已越过预警线：请关注趋势变化，安排现场巡检",
    CRITICAL: "已越过危险线：建议立即降负荷并安排现场检查，必要时停机",
}


@runtime_checkable
class Notifier(Protocol):
    """提醒出口：控制台/短信/声光报警器各自实现，监测组件只负责调用。"""

    def notify(self, title: str, payload: Mapping[str, Any]) -> None: ...


class InMemoryNotifier:
    """缺省提醒出口：进程内留存最近若干条，供控制台/自检查看。"""

    def __init__(self, limit: int = 100) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def notify(self, title: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._items.append({"title": title, "payload": dict(payload)})

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)[-limit:]

    def __len__(self) -> int:
        return len(self._items)


@dataclass(slots=True)
class _PointRuntime:
    """一个测点的瞬时判定状态（不落盘，重启后从流水重建）。"""

    last_value: float | None = None
    last_epoch: float | None = None
    abnormal_run: int = 0
    escalation_run: int = 0
    clear_run: int = 0
    occurrence: int = 0


@dataclass(slots=True)
class ActiveAlarm:
    alarm_id: str
    equipment_id: str
    point_id: str
    kind: str
    level: str
    first_at: str
    last_at: str
    last_value: float
    reasons: list[str] = field(default_factory=list)
    samples: int = 1
    acked: bool = False
    acked_by: str | None = None
    acked_at: str | None = None
    suppressed: bool = False
    derated: bool = False
    escalation_run: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "alarm_id": self.alarm_id,
            "equipment_id": self.equipment_id,
            "point_id": self.point_id,
            "kind": self.kind,
            "level": self.level,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "last_value": self.last_value,
            "reasons": list(self.reasons),
            "samples": self.samples,
            "acked": self.acked,
            "acked_by": self.acked_by,
            "acked_at": self.acked_at,
            "suppressed": self.suppressed,
            "derated": self.derated,
            "recommendation": RECOMMENDATIONS[self.level],
        }


class ConditionMonitor(Component):
    """机组振动/温度在线监测与报警管理。"""

    name = "cm"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self.trend = TrendJournal(ctx.store, TREND_STREAM)
        self.alarms_journal = AlarmJournal(ctx.store, ALARM_STREAM)
        self.notifier: Notifier = InMemoryNotifier()
        self._equipment: dict[str, Equipment] = {}
        self._points: dict[str, MonitorPoint] = {}
        self._runtime: dict[str, _PointRuntime] = {}
        self._active: dict[str, ActiveAlarm] = {}
        # 已解除但仍在再通知冷却窗内的报警：point_id -> ActiveAlarm 快照
        self._cleared: dict[str, ActiveAlarm] = {}
        self._lock = threading.RLock()
        self._restore_catalog()
        self._reconstruct()
        self._refresh_gauges()

    def bind_notifier(self, notifier: Notifier) -> None:
        """注入真实提醒出口（短信网关、声光报警、控制台推送等）。"""

        self.notifier = notifier

    # ------------------------------------------------------------- 台账管理
    def bootstrap_defaults(self) -> list[Equipment]:
        """首启时播种典型机组：一台风机、一台磨机；已有台账则不动。"""

        with self._lock:
            if self._equipment:
                return list(self._equipment.values())
            created = [
                self._build_and_register("F-1001", "fan", label="一次风机 F-1001", bearing_count=2),
                self._build_and_register("M-2001", "mill", label="球磨机 M-2001", bearing_count=2),
            ]
            self._persist_catalog(reason="bootstrap_defaults")
            return created

    def register_equipment(
        self,
        actor: str,
        *,
        equipment_id: str,
        kind: str,
        label: str | None = None,
        bearing_count: int = 2,
        threshold_overrides: Mapping[str, Mapping[str, float]] | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "register_equipment", equipment_id, actor, correlation_id=correlation_id
        ) as trace:
            with self._lock:
                equipment = self._build_and_register(
                    equipment_id,
                    kind,
                    label=label,
                    bearing_count=bearing_count,
                    threshold_overrides=threshold_overrides,
                )
                record = self._persist_catalog(reason="register_equipment")
                trace.attach(record).note("kind", kind).note("points", len(equipment.points))
            self._refresh_gauges()
            return self.equipment_status(equipment_id)

    def _build_and_register(
        self,
        equipment_id: str,
        kind: str,
        *,
        label: str | None = None,
        bearing_count: int = 2,
        threshold_overrides: Mapping[str, Mapping[str, float]] | None = None,
    ) -> Equipment:
        if equipment_id in self._equipment:
            raise ValidationError(
                "机组已登记，重复登记会覆盖历史关联", details={"equipment_id": equipment_id}
            )
        equipment = build_default_equipment(
            equipment_id,
            kind,
            label=label,
            bearing_count=bearing_count,
            threshold_overrides=threshold_overrides,
        )
        self._equipment[equipment_id] = equipment
        for point in equipment.points:
            self._points[point.id] = point
            self._runtime.setdefault(point.id, _PointRuntime())
        return equipment

    # ------------------------------------------------------------- 采样评估
    def ingest(
        self,
        actor: str,
        *,
        equipment_id: str,
        point_id: str,
        value: float,
        observed_at: str | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """收一个传感器采样：进趋势流水，再驱动报警生命周期。"""

        actor = ensure_actor(actor)
        with self.action(
            "ingest", point_id, actor, correlation_id=correlation_id
        ) as trace:
            with self._lock:
                equipment, point = self._require_point(equipment_id, point_id)
                value = float(value)
                self._require_finite(value, point_id)
                now = self.clock.timestamp()
                at = observed_at or self.clock.timestamp_iso()
                epoch = now if observed_at is None else epoch_from_iso(observed_at)
                if epoch > now:
                    raise ValidationError(
                        "采样观测时间晚于当前时刻", details={"point_id": point_id}
                    )

                runtime = self._runtime[point_id]
                previous = runtime.last_value
                elapsed = None if runtime.last_epoch is None else epoch - runtime.last_epoch
                if elapsed is not None and elapsed < 0:
                    # 允许网关补传乱序样本，但不拿它算温升率。
                    elapsed = None
                result = evaluate(
                    point_id,
                    value,
                    absolute=point.absolute,
                    rate=point.rate,
                    previous_value=previous,
                    elapsed_seconds=elapsed,
                )

                sample = self.trend.append(
                    at=at,
                    equipment_id=equipment_id,
                    point_id=point_id,
                    value=value,
                    level=result.level,
                    rate_per_min=result.rate_per_min,
                    actor=actor,
                )
                self.metrics.inc("cm.samples.total")
                self._advance_lifecycle(
                    equipment=equipment,
                    point=point,
                    value=value,
                    epoch=epoch,
                    at=at,
                    level=result.level,
                    reasons=result.reasons,
                    actor=actor,
                )
                runtime.last_value = value
                runtime.last_epoch = epoch
                self._refresh_gauges()
                trace.note("level", result.level).note("value", value)
                return {
                    "sample": sample.to_dict(),
                    "evaluation": result.to_dict(),
                    "alarm": self._alarm_view(point_id),
                }

    def _advance_lifecycle(
        self,
        *,
        equipment: Equipment,
        point: MonitorPoint,
        value: float,
        epoch: float,
        at: str,
        level: str,
        reasons: tuple[str, ...],
        actor: str,
    ) -> None:
        runtime = self._runtime[point.id]
        active = self._active.get(point.id)

        # 冷却窗已过的解除报警，正式关闭，下次越线算新报警。
        self._expire_cleared(point.id, epoch)

        if level != NORMAL:
            runtime.abnormal_run += 1
            runtime.clear_run = 0
        else:
            runtime.abnormal_run = 0

        if active is None:
            if level != NORMAL:
                self._consider_raise(
                    equipment=equipment,
                    point=point,
                    value=value,
                    epoch=epoch,
                    at=at,
                    level=level,
                    reasons=reasons,
                    actor=actor,
                )
            # 正常采样：什么都不发生，这是绝大多数时刻的路径。
            return

        # 已有活动报警：刷新现场值，持续越线不产生任何新事件/提醒。
        active.last_at = at
        active.last_value = round(value, 4)
        active.samples += 1
        if reasons:
            active.reasons = list(reasons)

        if level_rank(level) > level_rank(active.level):
            runtime.escalation_run += 1
            if runtime.escalation_run >= self.settings.cm_debounce_samples:
                self._escalate(active, to_level=level, value=value, at=at, reasons=reasons)
        else:
            runtime.escalation_run = 0

        if self._is_below_clear(point, value, epoch):
            runtime.clear_run += 1
            if runtime.clear_run >= self.settings.cm_clear_samples:
                self._clear(active, value=value, at=at)
        else:
            runtime.clear_run = 0

    def _consider_raise(
        self,
        *,
        equipment: Equipment,
        point: MonitorPoint,
        value: float,
        epoch: float,
        at: str,
        level: str,
        reasons: tuple[str, ...],
        actor: str,
    ) -> None:
        runtime = self._runtime[point.id]
        if runtime.abnormal_run < self.settings.cm_debounce_samples:
            return

        # 解除冷却窗内再越线：同一次报警再激活，不重复提醒。
        cooled = self._cleared.pop(point.id, None)
        if cooled is not None:
            alarm = ActiveAlarm(
                alarm_id=cooled.alarm_id,
                equipment_id=equipment.id,
                point_id=point.id,
                kind=point.kind,
                level=level,
                first_at=cooled.first_at,
                last_at=at,
                last_value=round(value, 4),
                reasons=list(reasons),
                samples=cooled.samples + 1,
                acked=cooled.acked,
                acked_by=cooled.acked_by,
                acked_at=cooled.acked_at,
            )
            self._active[point.id] = alarm
            self._append_event(
                alarm,
                EVENT_REACTIVATED,
                value=value,
                at=at,
                level=level,
                reasons=reasons,
                actor=actor,
                detail={"within_cooldown": True},
            )
            self.metrics.inc("cm.alarms.reactivated")
            return

        runtime.occurrence += 1
        alarm_id = f"{point.id}#A{runtime.occurrence:03d}"
        alarm = ActiveAlarm(
            alarm_id=alarm_id,
            equipment_id=equipment.id,
            point_id=point.id,
            kind=point.kind,
            level=level,
            first_at=at,
            last_at=at,
            last_value=round(value, 4),
            reasons=list(reasons),
        )
        self._active[point.id] = alarm
        self._append_event(
            alarm,
            EVENT_RAISED,
            value=value,
            at=at,
            level=level,
            reasons=reasons,
            actor=actor,
        )
        self.metrics.inc("cm.alarms.raised")
        self._notify(alarm, EVENT_RAISED)

    def _escalate(
        self,
        alarm: ActiveAlarm,
        *,
        to_level: str,
        value: float,
        at: str,
        reasons: tuple[str, ...],
    ) -> None:
        previous = alarm.level
        alarm.level = to_level
        alarm.reasons = list(reasons)
        self._runtime[alarm.point_id].escalation_run = 0
        self._append_event(
            alarm,
            EVENT_ESCALATED,
            value=value,
            at=at,
            level=to_level,
            reasons=reasons,
            actor="rule-engine",
            detail={"from_level": previous},
        )
        self.metrics.inc("cm.alarms.escalated")
        self._notify(alarm, EVENT_ESCALATED, detail={"from_level": previous})

    def _clear(self, alarm: ActiveAlarm, *, value: float, at: str) -> None:
        self._append_event(
            alarm,
            EVENT_CLEARED,
            value=value,
            at=at,
            level=NORMAL,
            reasons=(),
            actor="rule-engine",
        )
        self._active.pop(alarm.point_id, None)
        self._cleared[alarm.point_id] = alarm
        runtime = self._runtime[alarm.point_id]
        runtime.clear_run = 0
        runtime.escalation_run = 0
        self.metrics.inc("cm.alarms.cleared")

    def _expire_cleared(self, point_id: str, epoch: float) -> None:
        alarm = self._cleared.get(point_id)
        if alarm is None:
            return
        cleared_epoch = self._event_epoch(alarm.last_at)
        if cleared_epoch is None:
            return
        if epoch - cleared_epoch >= self.settings.cm_renotify_cooldown_seconds:
            self._append_event(
                alarm,
                EVENT_CLOSED,
                value=alarm.last_value,
                at=self.clock.timestamp_iso(),
                level=NORMAL,
                reasons=(),
                actor="system",
                detail={"reason": "cooldown_elapsed_auto_close"},
            )
            self._cleared.pop(point_id, None)
            self.metrics.inc("cm.alarms.closed")

    def _is_below_clear(self, point: MonitorPoint, value: float, epoch: float) -> bool:
        if not point.absolute.cleared_for(value):
            return False
        # 温度测点还要看温升率是否回到解除线以下（按本次采样时刻计算）。
        runtime = self._runtime[point.id]
        if point.rate is not None and runtime.last_value is not None and runtime.last_epoch is not None:
            elapsed = epoch - runtime.last_epoch
            if elapsed >= 1.0:
                rate = (value - runtime.last_value) * 60.0 / elapsed
                if rate > point.rate.clear:
                    return False
        return True

    # ------------------------------------------------------------- 人工处置
    def acknowledge(
        self,
        actor: str,
        *,
        alarm_id: str,
        note: str | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action("acknowledge", alarm_id, actor, correlation_id=correlation_id) as trace:
            with self._lock:
                alarm = self._require_active(alarm_id)
                if alarm.acked:
                    raise ValidationError(
                        "该报警已确认，无需重复确认",
                        details={"alarm_id": alarm_id, "acked_by": alarm.acked_by},
                    )
                at = self.clock.timestamp_iso()
                alarm.acked = True
                alarm.acked_by = actor
                alarm.acked_at = at
                detail = {"note": note} if note else {}
                self._append_event(
                    alarm, EVENT_ACKED, value=alarm.last_value, at=at,
                    level=alarm.level, reasons=(), actor=actor, detail=detail,
                )
                self._persist_catalog(reason="acknowledge")
                trace.attach(self.state_record()).note("point_id", alarm.point_id)
                self._refresh_gauges()
                return self.alarm_view(alarm_id)

    def dispose(
        self,
        actor: str,
        *,
        alarm_id: str,
        action: str,
        note: str | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """登记一条处置：已降负荷 / 现场检查 / 消音抑制 / 关闭。"""

        actor = ensure_actor(actor)
        with self.action("dispose", alarm_id, actor, correlation_id=correlation_id) as trace:
            event = DISPOSITION_EVENTS.get(action)
            if event is None:
                raise ValidationError(
                    "未知处置动作",
                    details={"action": action, "allowed": sorted(DISPOSITION_EVENTS)},
                )
            with self._lock:
                alarm = self._find_alarm(alarm_id)
                at = self.clock.timestamp_iso()
                detail: dict[str, Any] = {"label": DISPOSITION_LABELS[action]}
                if note:
                    detail["note"] = note
                if action == "derate":
                    alarm.derated = True
                elif action == "suppress":
                    alarm.suppressed = True
                self._append_event(
                    alarm, event, value=alarm.last_value, at=at,
                    level=alarm.level, reasons=(), actor=actor, detail=detail,
                )
                if action == "close":
                    self._active.pop(alarm.point_id, None)
                    self._cleared.pop(alarm.point_id, None)
                    self.metrics.inc("cm.alarms.closed")
                trace.note("disposition", action)
                self._refresh_gauges()
                return self.alarm_view(alarm_id)

    def add_note(
        self,
        actor: str,
        *,
        alarm_id: str,
        note: str,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action("add_note", alarm_id, actor, correlation_id=correlation_id):
            if not note or not note.strip():
                raise ValidationError("备注内容不能为空", details={"alarm_id": alarm_id})
            with self._lock:
                alarm = self._find_alarm(alarm_id)
                self._append_event(
                    alarm, EVENT_NOTE, value=alarm.last_value,
                    at=self.clock.timestamp_iso(), level=alarm.level,
                    reasons=(), actor=actor, detail={"note": note.strip()},
                )
                return self.alarm_view(alarm_id)

    # ------------------------------------------------------------- 查询视图
    def status(self) -> Mapping[str, Any]:
        with self._lock:
            active = [alarm.to_dict() for alarm in self._active.values()]
            active.sort(key=lambda item: (-level_rank(item["level"]), item["first_at"]))
            if any(item["level"] == CRITICAL for item in active):
                state = "critical"
            elif active:
                state = "alarm"
            else:
                state = "watching"
            return {
                "state": state,
                "equipment_count": len(self._equipment),
                "point_count": len(self._points),
                "active_alarm_count": len(active),
                "critical_count": sum(1 for item in active if item["level"] == CRITICAL),
                "warning_count": sum(1 for item in active if item["level"] == WARNING),
                "unacked_count": sum(1 for item in active if not item["acked"]),
                "active_alarms": active,
                "recent_notifications": self.notifier.recent(10)
                if isinstance(self.notifier, InMemoryNotifier)
                else [],
            }

    def list_equipment(self) -> list[Mapping[str, Any]]:
        with self._lock:
            return [self.equipment_status(equipment_id) for equipment_id in sorted(self._equipment)]

    def equipment_status(self, equipment_id: str) -> Mapping[str, Any]:
        with self._lock:
            equipment = self._require_equipment(equipment_id)
            points: list[dict[str, Any]] = []
            for point in equipment.points:
                runtime = self._runtime[point.id]
                alarm = self._active.get(point.id)
                points.append(
                    {
                        **point.to_dict(),
                        "last_value": runtime.last_value,
                        "last_at": None
                        if runtime.last_epoch is None
                        else self._iso(runtime.last_epoch),
                        "level": None if alarm is None else alarm.level,
                        "alarm_id": None if alarm is None else alarm.alarm_id,
                    }
                )
            active = [
                self._active[point.id].to_dict()
                for point in equipment.points
                if point.id in self._active
            ]
            return {
                "equipment": equipment.to_dict(),
                "points": points,
                "active_alarms": active,
            }

    def trend_report(
        self,
        *,
        equipment_id: str,
        point_id: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        with self._lock:
            self._require_equipment(equipment_id)
            if point_id is not None:
                self._require_point(equipment_id, point_id)
            samples = self.trend.query(
                equipment_id=equipment_id, point_id=point_id,
                limit=self._bounded_limit(limit),
            )
            return {
                "equipment_id": equipment_id,
                "point_id": point_id,
                "count": len(samples),
                "samples": [sample.to_dict() for sample in samples],
            }

    def alarm_history(
        self,
        *,
        equipment_id: str | None = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> Mapping[str, Any]:
        limit = self._bounded_limit(limit, default=200)
        with self._lock:
            if equipment_id is not None:
                self._require_equipment(equipment_id)
            events = self.alarms_journal.query(
                equipment_id=equipment_id, active_only=active_only, limit=limit
            )
            return self._group_alarms(events)

    def alarm_view(self, alarm_id: str) -> Mapping[str, Any]:
        with self._lock:
            events = self.alarms_journal.query(alarm_id=alarm_id, limit=500)
            if not events:
                raise NotFoundError("报警不存在", details={"alarm_id": alarm_id})
            grouped = self._group_alarms(events)
            return grouped["alarms"][0]

    def dispositions(self, *, equipment_id: str | None = None, limit: int = 200) -> Mapping[str, Any]:
        """处置记录：确认、降负荷、现场检查、消音、备注、关闭。"""

        result = self.alarm_history(equipment_id=equipment_id, limit=limit)
        handled = {
            EVENT_ACKED,
            EVENT_DERATED,
            EVENT_DETECTED,
            EVENT_SUPPRESSED,
            EVENT_NOTE,
            EVENT_CLOSED,
        }
        records: list[Mapping[str, Any]] = []
        for alarm in result["alarms"]:
            for event in alarm["events"]:
                if event["event"] in handled:
                    records.append(
                        {
                            "alarm_id": alarm["alarm_id"],
                            "equipment_id": alarm["equipment_id"],
                            "point_id": alarm["point_id"],
                            **event,
                        }
                    )
        records.sort(key=lambda item: item["at"])
        return {"count": len(records), "records": records[-self._bounded_limit(limit, default=200) :]}

    def notifications(self, limit: int = 20) -> Mapping[str, Any]:
        if not isinstance(self.notifier, InMemoryNotifier):
            return {"count": 0, "items": []}
        items = self.notifier.recent(self._bounded_limit(limit, default=20, ceiling=100))
        return {"count": len(items), "items": items}

    # ------------------------------------------------------------- 内部工具
    def _notify(self, alarm: ActiveAlarm, event: str, *, detail: Mapping[str, Any] | None = None) -> None:
        if alarm.suppressed and event != EVENT_RAISED:
            return
        payload = {
            "event": event,
            "alarm_id": alarm.alarm_id,
            "equipment_id": alarm.equipment_id,
            "point_id": alarm.point_id,
            "kind": alarm.kind,
            "level": alarm.level,
            "value": alarm.last_value,
            "at": alarm.last_at,
            "reasons": list(alarm.reasons),
            "recommendation": RECOMMENDATIONS[alarm.level],
            "acked": alarm.acked,
            "derated": alarm.derated,
        }
        if detail:
            payload["detail"] = dict(detail)
        title = f"[{alarm.level.upper()}] {alarm.equipment_id} {alarm.point_id} 越线报警"
        if event == EVENT_ESCALATED:
            title = f"[{alarm.level.upper()}] {alarm.equipment_id} {alarm.point_id} 报警升级"
        try:
            self.notifier.notify(title, payload)
        except Exception:  # 提醒出口故障不得影响监测主链路
            self.metrics.inc("cm.notify.failed")
        self.metrics.inc("cm.notifications.sent")

    def _append_event(
        self,
        alarm: ActiveAlarm,
        event: str,
        *,
        value: float,
        at: str,
        level: str,
        reasons: tuple[str, ...],
        actor: str,
        detail: Mapping[str, Any] | None = None,
    ) -> AlarmEvent:
        return self.alarms_journal.append_event(
            at=at,
            alarm_id=alarm.alarm_id,
            equipment_id=alarm.equipment_id,
            point_id=alarm.point_id,
            kind=alarm.kind,
            event=event,
            actor=actor,
            level=level,
            value=value,
            reasons=reasons,
            detail=detail,
        )

    def _group_alarms(self, events: list[AlarmEvent]) -> Mapping[str, Any]:
        grouped: dict[str, dict[str, Any]] = {}
        for event in events:
            bucket = grouped.setdefault(
                event.alarm_id,
                {
                    "alarm_id": event.alarm_id,
                    "equipment_id": event.equipment_id,
                    "point_id": event.point_id,
                    "kind": event.kind,
                    "events": [],
                },
            )
            bucket["events"].append(event.to_dict())
        alarms: list[dict[str, Any]] = []
        for bucket in grouped.values():
            lifecycle = bucket["events"]
            terminal = {item["event"] for item in lifecycle}
            active_now = bucket["alarm_id"] in {a.alarm_id for a in self._active.values()}
            last = lifecycle[-1]
            first = next(item for item in lifecycle if item["event"] == EVENT_RAISED)
            bucket.update(
                {
                    "active": active_now,
                    "level": self._active[bucket["point_id"]].level
                    if bucket["point_id"] in self._active
                    else last["level"],
                    "first_at": first["at"],
                    "last_at": last["at"],
                    "acked": EVENT_ACKED in terminal,
                    "derated": EVENT_DERATED in terminal,
                    "suppressed": EVENT_SUPPRESSED in terminal,
                    "closed": EVENT_CLOSED in terminal,
                    "cleared": EVENT_CLEARED in terminal,
                }
            )
            alarms.append(bucket)
        alarms.sort(key=lambda item: item["first_at"], reverse=True)
        return {"count": len(alarms), "alarms": alarms}

    def _alarm_view(self, point_id: str) -> dict[str, Any] | None:
        alarm = self._active.get(point_id)
        return None if alarm is None else alarm.to_dict()

    def _require_equipment(self, equipment_id: str) -> Equipment:
        try:
            return self._equipment[equipment_id]
        except KeyError as exc:
            raise NotFoundError("机组未登记", details={"equipment_id": equipment_id}) from exc

    def _require_point(self, equipment_id: str, point_id: str) -> tuple[Equipment, MonitorPoint]:
        equipment = self._require_equipment(equipment_id)
        point = self._points.get(point_id)
        if point is None or point.equipment_id != equipment_id:
            raise NotFoundError(
                "测点不存在或不属于该机组",
                details={"equipment_id": equipment_id, "point_id": point_id},
            )
        return equipment, point

    def _require_active(self, alarm_id: str) -> ActiveAlarm:
        alarm = self._find_alarm_object(alarm_id)
        if alarm is None:
            raise ValidationError(
                "报警已关闭或不存在，无法确认", details={"alarm_id": alarm_id}
            )
        return alarm

    def _find_alarm(self, alarm_id: str) -> ActiveAlarm:
        alarm = self._find_alarm_object(alarm_id)
        if alarm is None:
            raise NotFoundError("报警不存在", details={"alarm_id": alarm_id})
        return alarm

    def _find_alarm_object(self, alarm_id: str) -> ActiveAlarm | None:
        for alarm in self._active.values():
            if alarm.alarm_id == alarm_id:
                return alarm
        return self._cleared.get(
            next((pid for pid, a in self._cleared.items() if a.alarm_id == alarm_id), ""),
            None,
        )

    def _bounded_limit(
        self, value: int, *, default: int = 500, ceiling: int | None = None
    ) -> int:
        ceiling = ceiling or self.settings.cm_history_max_limit
        if value is None:
            return default
        value = int(value)
        if value < 1:
            raise ValidationError("limit 必须为正", details={"limit": value})
        return min(value, ceiling)

    @staticmethod
    def _require_finite(value: float, point_id: str) -> None:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValidationError("采样值必须是有限数值", details={"point_id": point_id, "value": repr(value)})

    def _iso(self, epoch: float) -> str:
        from ..runtime import iso_from_epoch

        return iso_from_epoch(epoch)

    def _event_epoch(self, iso_text: str) -> float | None:
        try:
            return epoch_from_iso(iso_text)
        except ValidationError:
            return None

    # ------------------------------------------------------------- 落盘/恢复
    def _persist_catalog(self, *, reason: str) -> Any:
        payload = {
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "equipment": [equipment.to_dict() for equipment in self._equipment.values()],
        }
        return self.persist_state(payload)

    def _restore_catalog(self) -> None:
        payload = self.load_state()
        if payload is None:
            return
        for raw in payload.get("equipment", ()):
            equipment = equipment_from_dict(raw)
            self._equipment[equipment.id] = equipment
            for point in equipment.points:
                self._points[point.id] = point
                self._runtime.setdefault(point.id, _PointRuntime())

    def _reconstruct(self) -> None:
        """从两条流水重建瞬时状态：最近采样、活动报警与发生序号。

        流水本身带校验和，重建过程读到的就是现场真实发生过的序列；去抖计数等
        纯瞬时量不重建（重启相当于重新观察），这对安全性只有好处。
        """

        samples = self.trend.query(limit=self.settings.cm_history_max_limit)
        for sample in samples:
            runtime = self._runtime.get(sample.point_id)
            if runtime is None:
                continue
            epoch = self._event_epoch(sample.at)
            if epoch is None:
                continue
            if runtime.last_epoch is None or epoch >= runtime.last_epoch:
                runtime.last_value = sample.value
                runtime.last_epoch = epoch

        events = self.alarms_journal.query(limit=self.settings.cm_history_max_limit)
        working: dict[str, ActiveAlarm] = {}
        for event in events:
            point_id = event.point_id
            occurrence = self._occurrence_from_id(event.alarm_id)
            runtime = self._runtime.get(point_id)
            if runtime is not None:
                runtime.occurrence = max(runtime.occurrence, occurrence)
            if event.event == EVENT_RAISED:
                working[point_id] = self._alarm_from_event(event)
                continue
            alarm = working.get(point_id)
            if alarm is None:
                # REACTIVATED 是同一次报警从解除态回到活动态；其余事件则可能
                # 来自本次重建窗口之前已存在的报警，都补一个骨架。
                alarm = self._alarm_from_event(event)
                working[point_id] = alarm
                if event.event == EVENT_REACTIVATED:
                    self._cleared.pop(point_id, None)
            alarm.last_at = event.at
            alarm.last_value = event.value
            if level_rank(event.level) > level_rank(NORMAL):
                alarm.level = event.level
            if event.event == EVENT_ACKED:
                alarm.acked = True
                alarm.acked_by = event.actor
                alarm.acked_at = event.at
            elif event.event == EVENT_DERATED:
                alarm.derated = True
            elif event.event == EVENT_SUPPRESSED:
                alarm.suppressed = True
            elif event.event == EVENT_CLEARED:
                alarm.level = NORMAL
                self._cleared[point_id] = alarm
                working.pop(point_id, None)
            elif event.event == EVENT_CLOSED:
                working.pop(point_id, None)
                self._cleared.pop(point_id, None)
        self._active = working

    @staticmethod
    def _alarm_from_event(event: AlarmEvent) -> ActiveAlarm:
        return ActiveAlarm(
            alarm_id=event.alarm_id,
            equipment_id=event.equipment_id,
            point_id=event.point_id,
            kind=event.kind,
            level=event.level,
            first_at=event.at,
            last_at=event.at,
            last_value=event.value,
            reasons=list(event.reasons),
        )

    @staticmethod
    def _occurrence_from_id(alarm_id: str) -> int:
        try:
            _, suffix = alarm_id.rsplit("#A", 1)
            return int(suffix)
        except (ValueError, AttributeError):
            return 0

    def _refresh_gauges(self) -> None:
        self.metrics.observe("cm.equipment.count", float(len(self._equipment)))
        self.metrics.observe("cm.points.count", float(len(self._points)))
        self.metrics.observe("cm.alarms.active", float(len(self._active)))
        self.metrics.observe(
            "cm.alarms.unacked",
            float(sum(1 for alarm in self._active.values() if not alarm.acked)),
        )


# 组件对外的简名。
Monitor = ConditionMonitor

__all__ = [
    "ConditionMonitor",
    "Monitor",
    "Notifier",
    "InMemoryNotifier",
    "DISPOSITION_EVENTS",
    "DISPOSITION_LABELS",
    "RECOMMENDATIONS",
]
