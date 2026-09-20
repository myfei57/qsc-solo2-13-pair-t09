"""转动设备在线监测组件。

组件把 :mod:`~flashsmelter.rotating.engine` 的纯逻辑判定接到平台的持久化与
审计设施上：

* 采样（``rotating.ingest``）进每台设备自己的趋势流水；
* 报警生命周期事件写全局通知流水——同一条报警只会在「产生/升级/恢复」三个
  时刻各产生一条通知，从根上杜绝同类报警反复刷屏；
* 每条报警是一份独立文档（状态、最新值、确认/处置/恢复全过程），处置记录
  另有追加流水，可逐台调阅；
* 组件只给「建议负荷比例」，真正降负荷由值班员走工艺指令执行。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..component import Component, ensure_actor
from ..errors import ConfigurationError, GuardViolation, NotFoundError, ValidationError
from ..runtime import RuntimeContext
from .catalog import (
    KIND_FAN,
    KIND_MILL,
    MachineSpec,
    PointSpec,
    default_catalog,
)
from .engine import (
    EVENT_ESCALATED,
    EVENT_RAISED,
    EVENT_RECOVERED,
    EVENT_SWEEP_ESCALATED,
    LEVEL_DANGER,
    LEVEL_WARN,
    AlarmEvent,
    RuleEngine,
)

NOTIFY_STREAM = "rotating/notifications"
DISPOSITION_STREAM = "rotating/dispositions"

SAMPLES_STREAM = "rotating/samples/{machine}"
ALARM_KEY = "rotating/alarm/{alarm_id}"

STATUS_ACTIVE = "active"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_RECOVERED = "recovered"
STATUS_CLOSED = "closed"

DISPOSITION_ACKNOWLEDGE = "acknowledge"
DISPOSITION_REDUCE_LOAD = "reduce-load"
DISPOSITION_INSPECT = "inspect"
DISPOSITION_FINALIZE = "finalize"
DISPOSITIONS = (
    DISPOSITION_ACKNOWLEDGE,
    DISPOSITION_REDUCE_LOAD,
    DISPOSITION_INSPECT,
    DISPOSITION_FINALIZE,
)

# 处置动作 → 中文说明，写进审计与处置流水。
DISPOSITION_LABELS = {
    DISPOSITION_ACKNOWLEDGE: "确认报警",
    DISPOSITION_REDUCE_LOAD: "降负荷",
    DISPOSITION_INSPECT: "现场检查",
    DISPOSITION_FINALIZE: "关闭归档",
}

_KIND_LOAD_LABEL = {
    KIND_FAN: "建议降低风门/转速",
    KIND_MILL: "建议降低给料量",
}


class RotatingMonitor(Component):
    """风机/磨机振动与轴承温度的在线趋势监测。"""

    name = "rotating"
    state_key = "engine-state"

    def __init__(
        self,
        ctx: RuntimeContext,
        catalog: Mapping[str, MachineSpec] | None = None,
    ) -> None:
        super().__init__(ctx)
        self._catalog: dict[str, MachineSpec] = dict(catalog or default_catalog())
        for machine in self._catalog.values():
            machine.validate()
        self._engine = RuleEngine(
            self._catalog_points(),
            self.clock,
            warn_on_delay=self.settings.rotating_warn_on_delay_seconds,
            danger_on_delay=self.settings.rotating_danger_on_delay_seconds,
            off_delay=self.settings.rotating_off_delay_seconds,
            ack_escalate_after=self.settings.rotating_ack_escalate_seconds,
            rate_window=self.settings.rotating_rate_window_seconds,
            rate_min_interval=self.settings.rotating_rate_min_interval_seconds,
            deadband_pct=self.settings.rotating_alarm_deadband_pct,
        )
        restored = self.restore()
        if restored is not None:
            self._engine.load(restored)
        self._rebuild_histories()
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def ingest(
        self,
        actor: str,
        *,
        machine_id: str,
        point_id: str,
        value: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """收进一个测点的一次采样：记趋势、跑判定、必要时产生通知。"""

        actor = ensure_actor(actor)
        with self.action(
            "ingest",
            machine_id,
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            machine, point = self._require_point(machine_id, point_id)
            value = float(value)
            events = self._engine.evaluate(machine.machine_id, point, value)
            sample = self._append_sample(machine, point, value)
            notifications = [self._handle_event(event, machine) for event in events]
            self._persist_engine(reason="ingest")
            self._refresh_gauges()
            trace.note("point", point_id).note("value", value).note("alarms", len(events))
            return {
                "machine_id": machine.machine_id,
                "point_id": point.point_id,
                "value": value,
                "sample_seq": sample.seq,
                "notifications": [item["alarm_id"] for item in notifications],
                "advisory": self.machine_advisory(machine.machine_id),
            }

    def acknowledge(
        self,
        actor: str,
        *,
        machine_id: str,
        point_id: str,
        rule_code: str,
        note: str = "",
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "acknowledge",
            machine_id,
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            alarm = self._require_active_alarm(machine_id, point_id, rule_code)
            self._engine.acknowledge(machine_id, point_id, rule_code)
            record = self._append_disposition(
                alarm,
                DISPOSITION_ACKNOWLEDGE,
                actor,
                note=note or "值班员已确认",
            )
            alarm["status"] = STATUS_ACKNOWLEDGED
            alarm["acknowledged_by"] = actor
            alarm["acknowledged_at"] = self.clock.timestamp_iso()
            alarm["last_disposition_seq"] = record.seq
            self._put_alarm(alarm)
            self._persist_engine(reason="acknowledge")
            trace.note("alarm_id", alarm["alarm_id"]).note("disposition_seq", record.seq)
            return self._alarm_view(alarm)

    def dispose(
        self,
        actor: str,
        *,
        alarm_id: str,
        disposition: str,
        note: str = "",
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """在报警上记录一次处置（降负荷/现场检查/关闭归档）。"""

        actor = ensure_actor(actor)
        if disposition not in DISPOSITIONS or disposition == DISPOSITION_ACKNOWLEDGE:
            raise ValidationError(
                "不支持的处置类型",
                details={"disposition": disposition, "allowed": [item for item in DISPOSITIONS if item != DISPOSITION_ACKNOWLEDGE]},
            )
        with self.action(
            "dispose",
            alarm_id,
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            alarm = self._require_alarm(alarm_id)
            if disposition == DISPOSITION_FINALIZE:
                self._require_recovered(alarm)
            elif alarm["status"] == STATUS_CLOSED:
                raise GuardViolation("报警已关闭归档，不能再记录处置", details={"alarm_id": alarm_id})
            label = DISPOSITION_LABELS[disposition]
            record = self._append_disposition(
                alarm,
                disposition,
                actor,
                note=note or label,
            )
            alarm["last_disposition_seq"] = record.seq
            if disposition == DISPOSITION_FINALIZE:
                alarm["status"] = STATUS_CLOSED
                alarm["closed_at"] = self.clock.timestamp_iso()
                alarm["closed_by"] = actor
            stored = self._put_alarm(alarm)
            trace.attach(stored).note("disposition", disposition).note("disposition_seq", record.seq)
            return self._alarm_view(alarm)

    def sweep(
        self,
        actor: str = "monitor-scan",
        *,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """巡检：把超时未确认的预警升级为危险（建议由扫描周期调用）。"""

        actor = ensure_actor(actor)
        with self.action("sweep", "rotating", actor, correlation_id=correlation_id) as trace:
            events = self._engine.sweep()
            notifications = []
            for event in events:
                machine = self._catalog[event.machine_id]
                notifications.append(self._handle_event(event, machine))
            if events:
                self._persist_engine(reason="sweep")
            self._refresh_gauges()
            trace.note("escalated", len(events))
            return {
                "escalated": [item["alarm_id"] for item in notifications],
                "fleet": self.fleet_status(),
            }

    # ------------------------------------------------------------------ 查询
    def fleet_status(self) -> Mapping[str, Any]:
        """全部机组的当前监测概览：每个测点最新值、活动报警与降负荷建议。"""

        alarms = self.list_alarms(include_closed=False)
        machines = []
        pending_review = sum(1 for item in alarms if item["status"] == STATUS_RECOVERED)
        for machine in self._catalog.values():
            point_views = []
            for point in machine.points:
                latest = self._latest_sample(machine.machine_id, point.point_id)
                point_alarms = [
                    item
                    for item in alarms
                    if item["machine_id"] == machine.machine_id
                    and item["point_id"] == point.point_id
                    and item["status"] in (STATUS_ACTIVE, STATUS_ACKNOWLEDGED)
                ]
                worst = LEVEL_DANGER if any(item["level"] == LEVEL_DANGER for item in point_alarms) else (
                    LEVEL_WARN if point_alarms else "none"
                )
                point_views.append(
                    {
                        "point_id": point.point_id,
                        "label": point.label,
                        "metric": point.metric,
                        "latest": latest,
                        "alarm_level": worst,
                        "alarms": [item["alarm_id"] for item in point_alarms],
                    }
                )
            machines.append(
                {
                    "machine_id": machine.machine_id,
                    "name": machine.name,
                    "kind": machine.kind,
                    "advisory": self._advisory_for(machine, point_views),
                    "points": point_views,
                }
            )
        return {
            "at": self.clock.timestamp_iso(),
            "machine_count": len(machines),
            "active_alarm_count": sum(
                1
                for item in alarms
                if item["status"] in (STATUS_ACTIVE, STATUS_ACKNOWLEDGED)
            ),
            "pending_review_count": pending_review,
            "unread_notifications": self._unread_count(),
            "machines": machines,
        }

    def machine_advisory(self, machine_id: str) -> Mapping[str, Any] | None:
        machine = self._require_machine(machine_id)
        alarms = self.list_alarms(machine_id=machine_id, include_closed=False)
        live = [item for item in alarms if item["status"] in (STATUS_ACTIVE, STATUS_ACKNOWLEDGED)]
        if not live:
            return None
        return self._advisory_payload(machine, live)

    def trend(
        self,
        machine_id: str,
        point_id: str,
        *,
        limit: int = 600,
        window_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        """调出单台设备单个测点的历史趋势。

        默认只返回最近 ``rotating_trend_window_seconds``（配置项，缺省 1 小时）
        内、且不超过 ``limit`` 条采样；需要更长历史时显式给更大的
        ``window_seconds``，传 0 表示不按时间截断。
        """

        self._require_point(machine_id, point_id)
        if not 1 <= limit <= 5000:
            raise ValidationError("趋势条数必须落在 1~5000", details={"limit": limit})
        if window_seconds is None:
            window_seconds = self.settings.rotating_trend_window_seconds
        if window_seconds < 0:
            raise ValidationError("趋势窗口不能为负", details={"window_seconds": window_seconds})
        entries = self.store.read_stream(
            SAMPLES_STREAM.format(machine=machine_id), limit=max(limit, 2000)
        )
        samples = [dict(entry.payload) for entry in entries]
        if window_seconds > 0:
            cutoff = self.clock.timestamp() - window_seconds
            samples = [item for item in samples if float(item.get("epoch", 0.0)) >= cutoff]
        samples = samples[-limit:]
        return {
            "machine_id": machine_id,
            "point_id": point_id,
            "window_seconds": window_seconds,
            "count": len(samples),
            "samples": samples,
        }

    def list_alarms(
        self,
        *,
        machine_id: str | None = None,
        status: str | None = None,
        include_closed: bool = True,
        include_recovered: bool = True,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """调阅报警：可按机台、状态过滤。

        ``include_closed=False`` 排除已归档关闭；``include_recovered=False``
        进一步排除「已恢复待复盘」的报警（用于当前活动告警视图）。
        """

        if not 1 <= limit <= 2000:
            raise ValidationError("报警查询条数必须落在 1~2000", details={"limit": limit})
        keys = self.store.list_keys(prefix=self.namespace.key("rotating", "alarm"))
        alarms: list[dict[str, Any]] = []
        for key in keys:
            record = self.store.get(key)
            if record is None:
                continue
            payload = dict(record.payload)
            if machine_id is not None and payload.get("machine_id") != machine_id:
                continue
            current = str(payload.get("status"))
            if not include_closed and current == STATUS_CLOSED:
                continue
            if not include_recovered and current == STATUS_RECOVERED:
                continue
            if status is not None and current != status:
                continue
            alarms.append(payload)
        alarms.sort(key=lambda item: str(item.get("raised_at", "")))
        return alarms[-limit:]

    def alarm_detail(self, alarm_id: str) -> Mapping[str, Any]:
        """单条报警全过程：状态字段 + 历次处置记录。"""

        alarm = self._require_alarm(alarm_id)
        dispositions = [
            dict(entry.payload)
            for entry in self.store.read_stream(DISPOSITION_STREAM, limit=2000)
            if entry.payload.get("alarm_id") == alarm_id
        ]
        view = self._alarm_view(alarm)
        view["dispositions"] = dispositions
        return view

    def notifications(self, *, limit: int = 100, only_unread: bool = False) -> Mapping[str, Any]:
        entries = self.store.read_stream(NOTIFY_STREAM, limit=limit)
        if only_unread:
            unread = self._unread_items(limit=max(limit, 2000))
            items = [item for item in unread if item.get("kind") != "read-marker"][-limit:]
        else:
            items = [dict(entry.payload) for entry in entries]
        return {"count": len(items), "notifications": items}

    def mark_all_read(self, actor: str = "operator", *, correlation_id: str | None = None) -> Mapping[str, Any]:
        """通知流水本身是只追加的：已读标记用一条 read-marker 表达。

        查询时以最近一条 read-marker 为界，它之前的通知视为已读，避免改写
        已落盘的流水行。
        """

        actor = ensure_actor(actor)
        with self.action("mark_all_read", "rotating", actor, correlation_id=correlation_id):
            entry = self.store.append(
                NOTIFY_STREAM,
                {
                    "at": self.clock.timestamp_iso(),
                    "kind": "read-marker",
                    "read": True,
                    "actor": actor,
                    "machine_id": "",
                    "point_id": "",
                    "rule_code": "",
                    "alarm_id": "",
                    "level": "none",
                    "value": 0.0,
                    "notification_seq_key": "read-marker",
                    "title": "通知全部已读",
                    "detail": {},
                },
            )
            self._refresh_gauges()
            return {"read_marker_seq": entry.seq, "unread": self._unread_count()}

    def _unread_items(self, limit: int = 2000) -> list[dict[str, Any]]:
        entries = self.store.read_stream(NOTIFY_STREAM, limit=limit)
        last_marker = 0
        for entry in entries:
            if entry.payload.get("kind") == "read-marker":
                last_marker = entry.seq
        return [
            dict(entry.payload)
            for entry in entries
            if entry.seq > last_marker and entry.payload.get("kind") != "read-marker"
        ]

    def _unread_count(self) -> int:
        return len(self._unread_items())

    def history_for_machine(self, machine_id: str, *, limit: int = 100) -> Mapping[str, Any]:
        """一台设备的完整历史：趋势采样 + 报警 + 处置记录，交班/复盘用。"""

        self._require_machine(machine_id)
        alarms = self.list_alarms(machine_id=machine_id, include_closed=True, limit=limit)
        dispositions = [
            dict(entry.payload)
            for entry in self.store.read_stream(DISPOSITION_STREAM, limit=2000)
            if entry.payload.get("machine_id") == machine_id
        ]
        sample_total = self.store.stream_length(SAMPLES_STREAM.format(machine=machine_id))
        return {
            "machine_id": machine_id,
            "sample_count": sample_total,
            "alarms": alarms,
            "dispositions": dispositions,
        }

    # ------------------------------------------------------------------ 内部
    def _catalog_points(self) -> Mapping[str, tuple[PointSpec, ...]]:
        return {machine.machine_id: machine.points for machine in self._catalog.values()}

    def _require_machine(self, machine_id: str) -> MachineSpec:
        machine = self._catalog.get(machine_id)
        if machine is None:
            raise ValidationError("未登记的转动设备", details={"machine": machine_id, "known": sorted(self._catalog)})
        return machine

    def _require_point(self, machine_id: str, point_id: str) -> tuple[MachineSpec, PointSpec]:
        machine = self._require_machine(machine_id)
        try:
            return machine, machine.point(point_id)
        except ConfigurationError as exc:
            raise ValidationError(
                "设备上不存在该测点",
                details={"machine": machine_id, "point": point_id},
            ) from exc

    def _append_sample(self, machine: MachineSpec, point: PointSpec, value: float) -> Any:
        return self.store.append(
            SAMPLES_STREAM.format(machine=machine.machine_id),
            {
                "at": self.clock.timestamp_iso(),
                "epoch": self.clock.timestamp(),
                "machine_id": machine.machine_id,
                "point_id": point.point_id,
                "metric": point.metric,
                "value": round(float(value), 4),
            },
        )

    def _rebuild_histories(self) -> None:
        """重启后用各机台趋势流水的尾部重建速率窗口。"""

        window = self.settings.rotating_rate_window_seconds
        payload = {"states": [], "histories": []}
        for machine_id in self._catalog:
            stream = SAMPLES_STREAM.format(machine=machine_id)
            entries = self.store.read_stream(stream, limit=10_000)
            by_point: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                item = entry.payload
                by_point.setdefault(str(item["point_id"]), []).append(
                    {"at": float(item["epoch"]), "value": float(item["value"])}
                )
            for point_id, samples in by_point.items():
                cutoff = self.clock.timestamp() - window
                payload["histories"].append(
                    {
                        "machine_id": machine_id,
                        "point_id": point_id,
                        "samples": [sample for sample in samples if sample["at"] >= cutoff],
                    }
                )
        self._engine.load(payload)

    def _latest_sample(self, machine_id: str, point_id: str) -> Mapping[str, Any] | None:
        entries = self.store.read_stream(
            SAMPLES_STREAM.format(machine=machine_id), limit=2000
        )
        for entry in reversed(entries):
            if entry.payload.get("point_id") == point_id:
                return dict(entry.payload)
        return None

    # ------------------------------------------------------------- 报警落盘
    def _handle_event(self, event: AlarmEvent, machine: MachineSpec) -> dict[str, Any]:
        if event.kind in (EVENT_RAISED,):
            return self._open_alarm(event, machine)
        if event.kind in (EVENT_ESCALATED, EVENT_SWEEP_ESCALATED):
            return self._escalate_alarm(event, machine)
        return self._recover_alarm(event, machine)

    def _open_alarm(self, event: AlarmEvent, machine: MachineSpec) -> dict[str, Any]:
        point = machine.point(event.point_id)
        now = self.clock.timestamp_iso()
        alarm = {
            "alarm_id": event.alarm_id,
            "machine_id": machine.machine_id,
            "machine_name": machine.name,
            "machine_kind": machine.kind,
            "point_id": point.point_id,
            "point_label": point.label,
            "metric": point.metric,
            "rule_code": event.rule_code,
            "rule_label": str(event.detail.get("rule_label", event.rule_code)),
            "unit": str(event.detail.get("unit", "")),
            "level": event.level,
            "status": STATUS_ACTIVE,
            "raised_at": now,
            "raised_value": round(event.value, 4),
            "latest_value": round(event.value, 4),
            "latest_at": now,
            "updated_at": now,
            "acknowledged_by": None,
            "acknowledged_at": None,
            "recovered_at": None,
            "closed_at": None,
            "closed_by": None,
            "escalations": [],
            "notifications": 1,
            "unread": True,
            "last_disposition_seq": None,
            "detail": dict(event.detail),
        }
        self._put_alarm(alarm)
        self._notify(event, alarm, machine)
        self.metrics.inc("rotating.alarms.raised")
        if event.level == LEVEL_DANGER:
            self.metrics.inc("rotating.alarms.danger")
        return alarm

    def _escalate_alarm(self, event: AlarmEvent, machine: MachineSpec) -> dict[str, Any]:
        alarm = self._require_alarm(event.alarm_id)
        now = self.clock.timestamp_iso()
        previous = alarm["level"]
        alarm["level"] = LEVEL_DANGER
        # 工况恶化升级时，此前的确认失效，需要值班员针对危险级重新处置。
        alarm["status"] = STATUS_ACTIVE
        alarm["acknowledged_by"] = None
        alarm["acknowledged_at"] = None
        alarm["latest_value"] = round(event.value, 4)
        alarm["latest_at"] = now
        alarm["updated_at"] = now
        alarm["unread"] = True
        alarm["notifications"] = int(alarm.get("notifications", 1)) + 1
        alarm.setdefault("escalations", []).append(
            {"at": now, "from_level": previous, "reason": event.kind, "value": round(event.value, 4)}
        )
        self._put_alarm(alarm)
        self._notify(event, alarm, machine)
        self.metrics.inc("rotating.alarms.escalated")
        return alarm

    def _recover_alarm(self, event: AlarmEvent, machine: MachineSpec) -> dict[str, Any]:
        alarm = self._require_alarm(event.alarm_id)
        now = self.clock.timestamp_iso()
        was_ack = alarm["status"] == STATUS_ACKNOWLEDGED
        alarm["status"] = STATUS_CLOSED if was_ack else STATUS_RECOVERED
        alarm["latest_value"] = round(event.value, 4)
        alarm["latest_at"] = now
        alarm["recovered_at"] = now
        alarm["updated_at"] = now
        alarm["unread"] = not was_ack
        alarm["notifications"] = int(alarm.get("notifications", 1)) + 1
        self._put_alarm(alarm)
        self._notify(event, alarm, machine)
        self.metrics.inc("rotating.alarms.recovered")
        return alarm

    def _notify(self, event: AlarmEvent, alarm: Mapping[str, Any], machine: MachineSpec) -> None:
        advisory = None
        if event.kind != EVENT_RECOVERED and event.level == LEVEL_DANGER:
            advisory = self._advisory_payload(machine, [alarm])
        self.store.append(
            NOTIFY_STREAM,
            {
                "at": self.clock.timestamp_iso(),
                "notification_seq_key": event.dedup_key,
                "alarm_id": event.alarm_id,
                "machine_id": event.machine_id,
                "point_id": event.point_id,
                "rule_code": event.rule_code,
                "kind": event.kind,
                "level": event.level,
                "value": round(event.value, 4),
                "title": f"{machine.name} {alarm.get('point_label', event.point_id)} {self._event_phrase(event, alarm)}",
                "read": False,
                "advisory": advisory,
                "detail": dict(event.detail),
            },
        )

    @staticmethod
    def _event_phrase(event: AlarmEvent, alarm: Mapping[str, Any]) -> str:
        if event.kind == EVENT_RAISED:
            if event.rule_code == "rate-of-change":
                return f"变化率过快（{round(event.value, 2)} {alarm.get('unit', '/min')}）"
            return f"越限报警（{round(event.value, 2)} {alarm.get('unit', '')}）"
        if event.kind == EVENT_ESCALATED:
            return f"升级为危险（{round(event.value, 2)} {alarm.get('unit', '')}）"
        if event.kind == EVENT_SWEEP_ESCALATED:
            return "超时未确认，升级为危险"
        return f"已恢复（{round(event.value, 2)} {alarm.get('unit', '')}）"

    def _append_disposition(
        self,
        alarm: Mapping[str, Any],
        disposition: str,
        actor: str,
        *,
        note: str,
    ) -> Any:
        return self.store.append(
            DISPOSITION_STREAM,
            {
                "at": self.clock.timestamp_iso(),
                "alarm_id": alarm["alarm_id"],
                "machine_id": alarm["machine_id"],
                "point_id": alarm["point_id"],
                "rule_code": alarm["rule_code"],
                "disposition": disposition,
                "label": DISPOSITION_LABELS.get(disposition, disposition),
                "actor": actor,
                "note": note,
                "level_at_time": alarm["level"],
                "status_at_time": alarm["status"],
            },
        )

    def _advisory_for(self, machine: MachineSpec, point_views: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        live = []
        for view in point_views:
            if view["alarm_level"] != "none":
                live.append({"level": view["alarm_level"], "point_id": view["point_id"]})
        if not live:
            return None
        return self._advisory_payload(machine, live)

    def _advisory_payload(
        self,
        machine: MachineSpec,
        alarms: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        danger = [item for item in alarms if item.get("level") == LEVEL_DANGER]
        if danger:
            return {
                "severity": LEVEL_DANGER,
                "action": "reduce-load",
                "load_hint": _KIND_LOAD_LABEL.get(machine.kind, "建议降负荷"),
                "target_load_pct": machine.danger_target_load_pct,
                "message": (
                    f"{machine.name}存在危险级报警，{_KIND_LOAD_LABEL.get(machine.kind, '建议降负荷')}"
                    f"至额定的 {machine.danger_target_load_pct:.0f}% 并安排现场检查"
                ),
                "point_ids": sorted({str(item.get("point_id", "")) for item in danger if item.get("point_id")}),
            }
        return {
            "severity": LEVEL_WARN,
            "action": "watch",
            "load_hint": "维持当前负荷，加密关注趋势",
            "target_load_pct": None,
            "message": f"{machine.name}存在预警，先提醒值班员关注趋势变化",
            "point_ids": sorted({str(item.get("point_id", "")) for item in alarms if item.get("point_id")}),
        }

    # ------------------------------------------------------------- 存取辅助
    def _alarm_key(self, alarm_id: str) -> str:
        return self.key("alarm", alarm_id)

    def _put_alarm(self, alarm: Mapping[str, Any]) -> Any:
        return self.store.put(self._alarm_key(str(alarm["alarm_id"])), alarm)

    def _require_alarm(self, alarm_id: str) -> dict[str, Any]:
        record = self.store.get(self._alarm_key(alarm_id))
        if record is None:
            raise NotFoundError("报警不存在", details={"alarm_id": alarm_id})
        return dict(record.payload)

    def _require_active_alarm(self, machine_id: str, point_id: str, rule_code: str) -> dict[str, Any]:
        if not self._engine.is_active(machine_id, point_id, rule_code):
            raise GuardViolation(
                "该测点规则当前没有活动报警",
                details={"machine": machine_id, "point": point_id, "rule": rule_code},
            )
        for alarm in self.list_alarms(
            machine_id=machine_id, include_closed=False, include_recovered=False
        ):
            if (
                alarm["point_id"] == point_id
                and alarm["rule_code"] == rule_code
                and alarm["status"] in (STATUS_ACTIVE, STATUS_ACKNOWLEDGED)
            ):
                return alarm
        raise NotFoundError(
            "活动报警缺少落盘记录",
            details={"machine": machine_id, "point": point_id, "rule": rule_code},
        )

    @staticmethod
    def _require_recovered(alarm: Mapping[str, Any]) -> None:
        if alarm["status"] in (STATUS_ACTIVE, STATUS_ACKNOWLEDGED):
            raise GuardViolation(
                "报警尚未恢复，禁止关闭归档",
                details={"alarm_id": alarm["alarm_id"], "status": alarm["status"]},
            )

    def _persist_engine(self, *, reason: str) -> Any:
        payload = self._engine.dump()
        payload["reason"] = reason
        payload["written_at"] = self.clock.timestamp_iso()
        return self.persist_state(payload)

    def _alarm_view(self, alarm: Mapping[str, Any]) -> dict[str, Any]:
        view = dict(alarm)
        view.pop("detail", None)
        return view

    def _refresh_gauges(self) -> None:
        active = self._engine.active_states()
        dangers = sum(1 for state in active if state.alarm_level == LEVEL_DANGER)
        self.metrics.observe("rotating.active_alarms", float(len(active)))
        self.metrics.observe("rotating.danger_alarms", float(dangers))

    def status(self) -> Mapping[str, Any]:
        fleet = self.fleet_status()
        return {
            "state": "monitoring",
            "machine_count": fleet["machine_count"],
            "active_alarm_count": fleet["active_alarm_count"],
            "unread_notifications": fleet["unread_notifications"],
            "machines": [
                {
                    "machine_id": item["machine_id"],
                    "name": item["name"],
                    "active_points": [
                        point["point_id"]
                        for point in item["points"]
                        if point["alarm_level"] != "none"
                    ],
                    "advisory": item["advisory"],
                }
                for item in fleet["machines"]
            ],
        }


__all__ = ["RotatingMonitor"]
