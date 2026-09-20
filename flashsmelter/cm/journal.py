"""趋势与报警事件的追加流水。

两类历史都走 :class:`DurableStore` 的 JSONL 追加流水（带逐行校验和）：

* 趋势流：每条采样一行，按测点分段，供画趋势曲线；
* 报警流：报警生命周期事件（raised/escalated/acked/detected/cleared/suppressed/
  reactivated/closed + 处置记录），同一台设备同一测点的同类报警共用一个
  ``alarm_id``，反复越线只追加升级/再激活事件，绝不重复产生新报警。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from ..store import DurableStore, JournalEntry

TREND_STREAM = "cm/trend"
ALARM_STREAM = "cm/alarms"

# 报警生命周期事件类型（顺序即一般流转顺序）。
EVENT_RAISED = "raised"
EVENT_ESCALATED = "escalated"
EVENT_ACKED = "acked"
EVENT_DERATED = "derated"
EVENT_DETECTED = "detected"
EVENT_SUPPRESSED = "suppressed"
EVENT_REACTIVATED = "reactivated"
EVENT_CLEARED = "cleared"
EVENT_CLOSED = "closed"
EVENT_NOTE = "note"

LIFECYCLE_EVENTS: tuple[str, ...] = (
    EVENT_RAISED,
    EVENT_ESCALATED,
    EVENT_ACKED,
    EVENT_DERATED,
    EVENT_DETECTED,
    EVENT_SUPPRESSED,
    EVENT_REACTIVATED,
    EVENT_CLEARED,
    EVENT_CLOSED,
    EVENT_NOTE,
)


@dataclass(frozen=True, slots=True)
class TrendSample:
    seq: int
    at: str
    equipment_id: str
    point_id: str
    value: float
    level: str
    rate_per_min: float | None
    actor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "equipment_id": self.equipment_id,
            "point_id": self.point_id,
            "value": self.value,
            "level": self.level,
            "rate_per_min": self.rate_per_min,
            "actor": self.actor,
        }


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    seq: int
    at: str
    alarm_id: str
    equipment_id: str
    point_id: str
    kind: str
    event: str
    actor: str
    level: str
    value: float
    reasons: tuple[str, ...]
    detail: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "alarm_id": self.alarm_id,
            "equipment_id": self.equipment_id,
            "point_id": self.point_id,
            "kind": self.kind,
            "event": self.event,
            "actor": self.actor,
            "level": self.level,
            "value": self.value,
            "reasons": list(self.reasons),
            "detail": dict(self.detail),
        }


def _to_sample(entry: JournalEntry) -> TrendSample:
    payload = entry.payload
    return TrendSample(
        seq=entry.seq,
        at=str(payload.get("at", entry.written_at)),
        equipment_id=str(payload.get("equipment_id", "")),
        point_id=str(payload.get("point_id", "")),
        value=float(payload.get("value", 0.0)),
        level=str(payload.get("level", "normal")),
        rate_per_min=payload.get("rate_per_min"),
        actor=str(payload.get("actor", "sensor")),
    )


def _to_alarm_event(entry: JournalEntry) -> AlarmEvent:
    payload = entry.payload
    reasons = payload.get("reasons", ())
    return AlarmEvent(
        seq=entry.seq,
        at=str(payload.get("at", entry.written_at)),
        alarm_id=str(payload.get("alarm_id", "")),
        equipment_id=str(payload.get("equipment_id", "")),
        point_id=str(payload.get("point_id", "")),
        kind=str(payload.get("kind", "")),
        event=str(payload.get("event", "")),
        actor=str(payload.get("actor", "system")),
        level=str(payload.get("level", "")),
        value=float(payload.get("value", 0.0)),
        reasons=tuple(reasons) if isinstance(reasons, (list, tuple)) else (str(reasons),),
        detail=payload.get("detail", {}) or {},
    )


class _Stream:
    def __init__(self, store: DurableStore, name: str) -> None:
        self._store = store
        self._name = name

    def _read(
        self,
        *,
        limit: int,
        since_seq: int = 0,
        **filters: str,
    ) -> list[JournalEntry]:
        # 过滤在内存里做：单厂站大机组数量有限，拉取窗口放大到 limit 的若干倍
        # 即可覆盖大部分查询；超长历史应由外层归档，不靠这里全表扫。
        fetch = max(limit * 32, 500)
        entries = self._store.read_stream(self._name, limit=fetch, since_seq=since_seq)
        if filters:
            kept = []
            for entry in entries:
                payload = entry.payload
                if all(str(payload.get(key)) == expected for key, expected in filters.items()):
                    kept.append(entry)
            entries = kept
        return entries[-limit:]


class TrendJournal(_Stream):
    """趋势采样流水。"""

    def append(
        self,
        *,
        at: str,
        equipment_id: str,
        point_id: str,
        value: float,
        level: str,
        rate_per_min: float | None,
        actor: str,
    ) -> TrendSample:
        entry = self._store.append(
            TREND_STREAM,
            {
                "at": at,
                "equipment_id": equipment_id,
                "point_id": point_id,
                "value": round(float(value), 4),
                "level": level,
                "rate_per_min": rate_per_min,
                "actor": actor,
            },
        )
        return _to_sample(entry)

    def query(
        self,
        *,
        equipment_id: str | None = None,
        point_id: str | None = None,
        limit: int = 500,
        since_seq: int = 0,
    ) -> list[TrendSample]:
        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        filters: dict[str, str] = {}
        if equipment_id is not None:
            filters["equipment_id"] = equipment_id
        if point_id is not None:
            filters["point_id"] = point_id
        return [_to_sample(entry) for entry in self._read(limit=limit, since_seq=since_seq, **filters)]


class AlarmJournal(_Stream):
    """报警生命周期事件流水。"""

    def append_event(
        self,
        *,
        at: str,
        alarm_id: str,
        equipment_id: str,
        point_id: str,
        kind: str,
        event: str,
        actor: str,
        level: str,
        value: float,
        reasons: tuple[str, ...] | list[str] | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> AlarmEvent:
        if event not in LIFECYCLE_EVENTS:
            raise ValidationError(
                "非法的报警生命周期事件",
                details={"event": event, "allowed": list(LIFECYCLE_EVENTS)},
            )
        entry = self._store.append(
            ALARM_STREAM,
            {
                "at": at,
                "alarm_id": alarm_id,
                "equipment_id": equipment_id,
                "point_id": point_id,
                "kind": kind,
                "event": event,
                "actor": actor or "system",
                "level": level,
                "value": round(float(value), 4),
                "reasons": list(reasons or []),
                "detail": dict(detail or {}),
            },
        )
        return _to_alarm_event(entry)

    def query(
        self,
        *,
        equipment_id: str | None = None,
        point_id: str | None = None,
        alarm_id: str | None = None,
        active_only: bool = False,
        limit: int = 200,
        since_seq: int = 0,
    ) -> list[AlarmEvent]:
        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        filters: dict[str, str] = {}
        if equipment_id is not None:
            filters["equipment_id"] = equipment_id
        if point_id is not None:
            filters["point_id"] = point_id
        if alarm_id is not None:
            filters["alarm_id"] = alarm_id
        events = [_to_alarm_event(entry) for entry in self._read(
            limit=limit * 4 if active_only else limit, since_seq=since_seq, **filters
        )]
        if active_only:
            active = self._active_alarm_ids(events)
            events = [event for event in events if event.alarm_id in active]
        return events[-limit:]

    @staticmethod
    def _active_alarm_ids(events: list[AlarmEvent]) -> set[str]:
        """按时间顺序回放事件，找出仍处于活动态（未 cleared/closed）的报警。"""

        active: set[str] = set()
        for event in events:
            if event.event == EVENT_RAISED or event.event == EVENT_REACTIVATED:
                active.add(event.alarm_id)
            elif event.event in (EVENT_CLEARED, EVENT_CLOSED):
                active.discard(event.alarm_id)
        return active


__all__ = [
    "TrendJournal",
    "AlarmJournal",
    "TrendSample",
    "AlarmEvent",
    "TREND_STREAM",
    "ALARM_STREAM",
    "LIFECYCLE_EVENTS",
    "EVENT_RAISED",
    "EVENT_ESCALATED",
    "EVENT_ACKED",
    "EVENT_DERATED",
    "EVENT_DETECTED",
    "EVENT_SUPPRESSED",
    "EVENT_REACTIVATED",
    "EVENT_CLEARED",
    "EVENT_CLOSED",
    "EVENT_NOTE",
]
