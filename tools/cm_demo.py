#!/usr/bin/env python3
"""机组在线监测离线演示：在单进程里模拟采集网关连续推送。

生产部署时由常驻控制台（``flashsmelter serve``）+ 现场采集网关代替这里的
模拟循环；脚本只用来向值班人员演示完整闭环：

  正常趋势 → 振动越预警线（提醒）→ 升危险（建议降负荷）→ 确认/降负荷
  → 回落解除（同类不刷屏）→ 趋势与处置记录可按机组调阅。

用法：
    python3 tools/cm_demo.py [--root DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flashsmelter.application import Application  # noqa: E402
from flashsmelter.cm import InMemoryNotifier  # noqa: E402
from flashsmelter.config import Settings  # noqa: E402
from flashsmelter.runtime import ManualClock  # noqa: E402

EQUIPMENT = "F-1001"
POINT = "F-1001-vib"
PERIOD_S = 5.0


def banner(title: str) -> None:
    print(f"\n{'─' * 64}\n{title}\n{'─' * 64}")


def show_status(app: Application) -> None:
    status = app.cm.status()
    print(
        f"监测状态={status['state']}  活动报警={status['active_alarm_count']}"
        f"（危险 {status['critical_count']} / 预警 {status['warning_count']}）"
        f"  待确认={status['unacked_count']}"
    )
    for alarm in status["active_alarms"]:
        print(f"  ! {alarm['alarm_id']} [{alarm['level']}] 当前值 {alarm['last_value']}")
        print(f"    理由：{'; '.join(alarm['reasons'])}")
        print(f"    建议：{alarm['recommendation']}")


def feed(app: Application, values: list[float], *, actor: str = "gw-a") -> None:
    for value in values:
        result = app.cm.ingest(
            actor, equipment_id=EQUIPMENT, point_id=POINT, value=value
        )
        app.clock.advance(PERIOD_S)
        level = result["evaluation"]["level"]
        marker = {"normal": " ", "warning": "△", "critical": "▲"}[level]
        print(f"  {marker} 采样 {value:5.1f} mm/s  判定={level}")


def main() -> int:
    parser = argparse.ArgumentParser(description="机组在线监测离线演示")
    parser.add_argument("--root", help="状态目录（默认临时目录，演示完即弃）")
    args = parser.parse_args()

    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="cm-demo-"))
    clock = ManualClock()
    app = Application(Settings(root=root), clock=clock)
    app.cm.bind_notifier(InMemoryNotifier())

    banner("① 台账：默认登记的机组与测点")
    for item in app.cm.list_equipment():
        equipment = item["equipment"]
        points = ", ".join(f"{p['id']}({p['metric']})" for p in item["points"])
        print(f"  {equipment['id']} {equipment['label']}：{points}")

    banner("② 正常运行趋势（2 mm/s）")
    feed(app, [2.0, 2.1, 2.0])
    show_status(app)

    banner("③ 振动爬升，越过预警线 4.5（连续 3 次才提醒，滤抖动）")
    feed(app, [4.7, 4.8, 4.9])
    show_status(app)

    banner("④ 持续劣化到危险线 7.1 以上（同一报警只升级，不重复刷）")
    feed(app, [7.6, 7.8, 8.2])
    show_status(app)

    banner("⑤ 值班员确认，并登记降负荷处置")
    alarm_id = app.cm.status()["active_alarms"][0]["alarm_id"]
    app.cm.acknowledge("调度-王", alarm_id=alarm_id, note="听到异响，通知现场")
    app.cm.dispose("调度-王", alarm_id=alarm_id, action="derate", note="负荷降至 70%")
    show_status(app)

    banner("⑥ 降负荷后振动回落（回差 + 连续 3 次确认才解除）")
    feed(app, [6.5, 3.2, 3.1, 2.9, 2.8])
    show_status(app)

    banner(f"⑦ {EQUIPMENT} 的报警历史与处置记录")
    history = app.cm.alarm_history(equipment_id=EQUIPMENT)
    for alarm in history["alarms"]:
        print(f"  报警 {alarm['alarm_id']}（活动中={alarm['active']}）")
        for event in alarm["events"]:
            detail = f" {event['detail']}" if event["detail"] else ""
            print(f"    {event['at'][11:19]} {event['event']:<11} {event['actor']} "
                  f"值={event['value']:<5}{detail}")

    banner(f"⑧ {EQUIPMENT} 最近趋势采样（前 5 条 / 共 "
           f"{app.cm.trend_report(equipment_id=EQUIPMENT, point_id=POINT)['count']} 条）")
    trend = app.cm.trend_report(equipment_id=EQUIPMENT, point_id=POINT, limit=500)
    for sample in trend["samples"][:5]:
        print(f"    {sample['at'][11:19]}  {sample['value']:5.1f} mm/s  {sample['level']}")

    banner(f"提醒出口共推送 {len(app.cm.notifier)} 条（预警 1 + 升级 1，持续越线未刷屏）")
    for item in app.cm.notifier.recent(10):
        print(f"  [{item['payload']['event']}] {item['title']}")

    print(f"\n演示状态落盘在：{root}（重启后活动报警与历史仍可恢复）")
    print(json.dumps({"root": str(root), "alarm_count": history["count"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
