"""监测模块的 HTTP 端到端：收样、报警、确认处置、趋势与历史查询。"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from flashsmelter.console import ConsoleApp, ConsoleServer

from .helpers import make_app

FAN = "F-1001"
VIB = "F-1001-vib"


class MonitorConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一套独立台账，避免报警状态在方法间串扰。
        self.app = make_app()
        self.console = ConsoleApp(self.app)
        self.server = ConsoleServer(self.console, host="127.0.0.1", port=0)
        self.host, self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"http://{self.host}:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def _ingest(self, value: float) -> tuple[int, dict]:
        self.app.clock.advance(5)
        return self._request(
            "POST", "/api/cm/ingest",
            {"equipment_id": FAN, "point_id": VIB, "value": value, "actor": "gw-a"},
        )

    def test_monitor_overview_and_catalog(self) -> None:
        status, payload = self._request("GET", "/api/monitor")
        self.assertEqual(200, status)
        self.assertEqual("watching", payload["state"])
        self.assertEqual(2, payload["equipment_count"])
        status, listing = self._request("GET", "/api/monitor/equipment")
        self.assertEqual(200, status)
        self.assertEqual({FAN, "M-2001"}, {e["equipment"]["id"] for e in listing["equipment"]})

    def test_ingest_alert_ack_dispose_trend_chain(self) -> None:
        for _ in range(3):
            status, payload = self._ingest(8.0)
        self.assertEqual(200, status)
        alarm_id = payload["result"]["alarm"]["alarm_id"]
        self.assertEqual("critical", payload["result"]["alarm"]["level"])

        status, active = self._request("GET", "/api/monitor/alarms?active_only=true")
        self.assertEqual(200, status)
        self.assertEqual(1, active["count"])

        status, _ = self._request(
            "POST", "/api/cm/acknowledge", {"alarm_id": alarm_id, "actor": "ops-chen"}
        )
        self.assertEqual(200, status)
        status, _ = self._request(
            "POST", "/api/cm/dispose",
            {"alarm_id": alarm_id, "actor": "ops-chen", "action": "derate", "note": "降负荷至70%"},
        )
        self.assertEqual(200, status)

        status, dispositions = self._request(
            "GET", f"/api/monitor/dispositions?equipment_id={FAN}"
        )
        self.assertEqual(200, status)
        self.assertEqual(["acked", "derated"], [item["event"] for item in dispositions["records"]])

        status, trend = self._request(
            "GET", f"/api/monitor/equipment/{FAN}/trend?point_id={VIB}&limit=10"
        )
        self.assertEqual(200, status)
        self.assertEqual(3, trend["count"])
        self.assertTrue(all(sample["value"] == 8.0 for sample in trend["samples"]))

        # 解除报警收尾
        for _ in range(3):
            self._ingest(2.0)
        status, active = self._request("GET", "/api/monitor/alarms?active_only=true")
        self.assertEqual(0, active["count"])

    def test_unknown_equipment_maps_to_404(self) -> None:
        status, payload = self._request("GET", "/api/monitor/equipment/NOPE/detail")
        self.assertEqual(404, status)
        self.assertEqual("not-found", payload["error"])

    def test_invalid_disposition_maps_to_400(self) -> None:
        for _ in range(3):
            status, payload = self._ingest(5.0)
        alarm_id = payload["result"]["alarm"]["alarm_id"]
        status, payload = self._request(
            "POST", "/api/cm/dispose",
            {"alarm_id": alarm_id, "actor": "ops-chen", "action": "nope"},
        )
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])

    def test_register_custom_equipment_via_http(self) -> None:
        body = {
            "equipment_id": "F-9101",
            "kind": "fan",
            "label": "引风机 F-9101",
            "threshold_overrides": {"vibration": {"warn": 3.5, "critical": 6.0, "clear": 2.8}},
        }
        status, payload = self._request("POST", "/api/cm/register_equipment", body)
        self.assertEqual(200, status)
        vib = next(p for p in payload["result"]["points"] if p["id"] == "F-9101-vib")
        self.assertEqual(3.5, vib["thresholds"]["warn"])


if __name__ == "__main__":
    unittest.main()
