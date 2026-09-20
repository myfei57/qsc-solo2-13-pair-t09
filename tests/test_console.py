"""控制台端到端：路由、错误映射、请求体限制与审计查询。"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from flashsmelter.console import ConsoleApp, ConsoleServer

from .helpers import make_app


class ConsoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = make_app()
        cls.console = ConsoleApp(cls.app)
        cls.server = ConsoleServer(cls.console, host="127.0.0.1", port=0)
        cls.host, cls.port = cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    # ------------------------------------------------------------------ 工具
    def _request(self, method: str, path: str, body: dict | None = None, raw: bytes | None = None):
        url = f"http://{self.host}:{self.port}{path}"
        data = raw if raw is not None else (json.dumps(body or {}).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    # ------------------------------------------------------------------ 用例
    def test_health_and_root_endpoints(self) -> None:
        status, payload = self._request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertEqual("smelter/line1", payload["namespace"])
        status, root = self._request("GET", "/")
        self.assertEqual(200, status)
        self.assertTrue(any(route["path"] == "/api/actions" for route in root["endpoints"]))

    def test_start_furnace_via_http(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/furnace/start",
            {
                "actor": "http-test",
                "drum_level": 0.6,
                "fuel_pressure_kpa": 200.0,
                "air_flow_nm3h": 5200.0,
                "oxygen_baseline": 0.62,
                "oxygen_baseline_source": "analyzer-a",
                "oxygen_target": 0.62,
                "oxygen_flow_nm3h": 9000.0,
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("oxygen_ready", payload["result"]["state"])
        status, component = self._request("GET", "/api/components/furnace")
        self.assertEqual(200, status)
        self.assertEqual("furnace", component["name"])
        self.assertEqual("oxygen_ready", component["status"]["state"])

    def test_guard_failure_maps_to_conflict(self) -> None:
        status, payload = self._request("POST", "/api/conc/inject", {"rate_tph": 100.0, "tons": 10.0})
        self.assertEqual(409, status)
        self.assertIn(payload["error"], {"state-transition-rejected", "guard-violation", "latch-engaged"})
        self.assertIn("details", payload)

    def test_unknown_routes_and_methods(self) -> None:
        status, payload = self._request("GET", "/api/nope")
        self.assertEqual(404, status)
        self.assertEqual("not-found", payload["error"])
        status, payload = self._request("GET", "/api/conc/inject")
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", payload["error"])
        status, payload = self._request("GET", "/api/components/unknown")
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])

    def test_invalid_body_and_size_limit(self) -> None:
        status, payload = self._request("POST", "/api/furnace/start", raw=b"{not-json")
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])
        big = json.dumps({"actor": "x" * (self.app.settings.max_body_bytes + 10)}).encode("utf-8")
        status, payload = self._request("POST", "/api/furnace/start", raw=big)
        self.assertEqual(413, status)
        self.assertEqual("payload-too-large", payload["error"])

    def test_missing_required_param_is_reported(self) -> None:
        status, payload = self._request("POST", "/api/waste/start", {"actor": "ops"})
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])
        self.assertEqual("drum_level", payload["details"]["param"])

    def test_actions_listing_and_audit_query(self) -> None:
        status, listing = self._request("GET", "/api/actions")
        self.assertEqual(200, status)
        names = {item["action"] for item in listing["actions"]}
        self.assertIn("furnace.start", names)
        self.assertIn("waste.update", names)
        status, audit = self._request("GET", "/api/audit?limit=5&outcome=ok")
        self.assertEqual(200, status)
        self.assertLessEqual(audit["count"], 5)
        self.assertTrue(all(event["outcome"] == "ok" for event in audit["events"]))

    def test_state_and_metrics_views(self) -> None:
        status, state = self._request("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertIn("furnace", state["components"])
        self.assertIn("heat", state)
        self.assertEqual("smelter/line1", state["service"]["namespace"]["prefix"])
        status, metrics = self._request("GET", "/api/metrics")
        self.assertEqual(200, status)
        self.assertIn("counters", metrics)
        self.assertIn("actions_ok", metrics["selected"])
        status, heats = self._request("GET", "/api/heats?limit=3")
        self.assertEqual(200, status)
        self.assertIn("current", heats)

    def test_zone_grouping(self) -> None:
        status, payload = self._request("GET", "/api/zones")
        self.assertEqual(200, status)
        self.assertEqual("smelter/line1", payload["namespace"])
        self.assertIn("conc", payload["zones"]["reactor"])
        self.assertIn("matte", payload["zones"]["settler"])
        self.assertIn("conv", payload["zones"]["converter"])


class RotatingConsoleTest(unittest.TestCase):
    """转动设备监测的 HTTP 端到端：采样、报警、确认、处置、趋势与历史。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = make_app()
        cls.console = ConsoleApp(cls.app)
        cls.server = ConsoleServer(cls.console, host="127.0.0.1", port=0)
        cls.host, cls.port = cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"http://{self.host}:{self.port}{path}"
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def _ingest(self, value: float) -> tuple[int, dict]:
        return self._request(
            "POST",
            "/api/rotating/ingest",
            {"machine_id": "fan-ID01", "point_id": "temp-de", "value": value, "actor": "daq"},
        )

    def test_fleet_empty_initially(self) -> None:
        status, payload = self._request("GET", "/api/rotating/fleet")
        self.assertEqual(200, status)
        self.assertEqual(2, payload["machine_count"])
        self.assertEqual(0, payload["active_alarm_count"])

    def test_ingest_alarm_acknowledge_dispose_flow(self) -> None:
        # 正常采样无报警。
        status, result = self._ingest(55.0)
        self.assertEqual(200, status)
        self.assertEqual([], result["result"]["notifications"])
        # 连续超限 30s 挂出绝对阈值预警；平台值（增量 0）不会挂变化率报警。
        for _ in range(4):
            self.app.clock.advance(10)
            status, result = self._ingest(78.0)
        self.assertEqual(200, status)
        self.assertEqual(1, len(result["result"]["notifications"]))

        status, alarms = self._request("GET", "/api/rotating/alarms")
        self.assertEqual(200, status)
        self.assertEqual(1, alarms["count"])
        alarm_id = alarms["alarms"][0]["alarm_id"]

        # 同类报警不再刷：继续超限，raised 通知总数不再增加。
        self.app.clock.advance(60)
        self._ingest(78.0)
        status, notifications = self._request("GET", "/api/rotating/notifications")
        self.assertEqual(1, sum(1 for n in notifications["notifications"] if n["kind"] == "raised"))

        # 确认 + 降负荷处置。
        status, ack = self._request(
            "POST",
            "/api/rotating/acknowledge",
            {
                "machine_id": "fan-ID01",
                "point_id": "temp-de",
                "rule_code": "bearing-temp-high",
                "note": "已知悉",
                "actor": "ops",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("acknowledged", ack["result"]["status"])
        status, disposed = self._request(
            "POST",
            "/api/rotating/dispose",
            {"alarm_id": alarm_id, "disposition": "reduce-load", "note": "风门70%", "actor": "ops"},
        )
        self.assertEqual(200, status)
        self.assertIsNotNone(disposed["result"]["last_disposition_seq"])

        # 报警详情带处置记录。
        status, detail = self._request(f"GET", f"/api/rotating/alarms/{alarm_id}")
        self.assertEqual(200, status)
        self.assertEqual(
            ["acknowledge", "reduce-load"],
            [item["disposition"] for item in detail["dispositions"]],
        )

    def test_trend_and_machine_history(self) -> None:
        self._ingest(60.0)
        status, trend = self._request(
            "GET", "/api/rotating/machines/fan-ID01/points/temp-de/trend?limit=10"
        )
        self.assertEqual(200, status)
        self.assertGreaterEqual(trend["count"], 1)
        self.assertEqual("temp-de", trend["point_id"])
        status, history = self._request("GET", "/api/rotating/machines/fan-ID01/history")
        self.assertEqual(200, status)
        self.assertGreaterEqual(history["sample_count"], 1)

    def test_unknown_machine_rejected(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/rotating/ingest",
            {"machine_id": "ghost", "point_id": "x", "value": 1.0},
        )
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
