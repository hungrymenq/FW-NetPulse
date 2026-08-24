import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock


app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from core.pinger import Win32Pinger
from core.process_tracker import GameProcessTracker
from core.tcp_pinger import TCPGamePinger
from core.route_optimizer import (
    CalibrationStore,
    DpiBypassManager,
    RouteAnalysisEngine,
    WireGuardRelayManager,
)
from run_monitor import NetPulseApp
from web.server import NetPulseHTTPRequestHandler


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class NetPulseTests(unittest.TestCase):
    def setUp(self):
        self.pinger = Win32Pinger(target_ip="127.0.0.1", interval_ms=500)

    def tearDown(self):
        self.pinger.stop()

    def test_get_summary_does_not_deadlock(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(self.pinger.get_summary()), daemon=True)
        worker.start()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive(), "get_summary() завис при повторном захвате блокировки")
        self.assertEqual(len(result), 1)
        self.assertIn("history", result[0])

    def test_history_is_filtered_by_timeframe(self):
        now = time.time()
        with self.pinger.lock:
            self.pinger.history.extend([
                {"timestamp": now - 120, "rtt": 99.0},
                {"timestamp": now - 10, "rtt": 20.0},
            ])

        history = self.pinger.get_history_by_timeframe(60)

        self.assertEqual([item["rtt"] for item in history], [20.0])

    def test_target_change_resets_accumulated_statistics(self):
        with self.pinger.lock:
            self.pinger.total_sent = 10
            self.pinger.total_received = 8
            self.pinger.total_lost = 2
            self.pinger.last_rtt = 55.0
            self.pinger.sliding_window.extend([1, 0])

        self.pinger.set_target("127.0.0.2")
        summary = self.pinger.get_summary()

        self.assertEqual(summary["total_sent"], 0)
        self.assertEqual(summary["total_lost"], 0)
        self.assertIsNone(summary["last_rtt"])
        self.assertEqual(summary["rolling_loss_pct"], 0.0)

    def test_process_tracker_uses_configured_exact_names(self):
        tracker = GameProcessTracker(target_names=["PEM.exe", "pemv.exe"])

        self.assertTrue(tracker._is_target_process("pem.exe"))
        self.assertTrue(tracker._is_target_process(r"C:\Games\pemv.exe"))
        self.assertFalse(tracker._is_target_process("gamebar.exe"))

    def test_process_tracker_prefers_established_game_port(self):
        tracker = GameProcessTracker(
            target_names=["pem.exe"],
            fallback_ip="51.77.68.91",
            fallback_port=29000,
            preferred_ports=[29000, 29001, 29002],
        )
        sockets = [
            {"remote_ip": "203.0.113.10", "remote_port": 443, "is_established": True},
            {"remote_ip": "51.77.68.91", "remote_port": 29001, "is_established": True},
            {"remote_ip": "51.77.68.91", "remote_port": 29002, "is_established": False},
        ]

        selected = min(sockets, key=tracker._socket_priority)

        self.assertEqual(selected["remote_port"], 29001)

    def test_local_api_rejects_cross_site_origin(self):
        handler = NetPulseHTTPRequestHandler.__new__(NetPulseHTTPRequestHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"}

        self.assertFalse(handler._is_trusted_local_origin())

        handler.headers = {"Origin": "http://127.0.0.1:8899", "Sec-Fetch-Site": "same-origin"}
        self.assertTrue(handler._is_trusted_local_origin())

    def test_web_interface_uses_bundled_chart_library(self):
        index_path = os.path.join(app_dir, "web", "static", "index.html")
        chart_path = os.path.join(app_dir, "web", "static", "vendor", "chart.umd.js")
        with open(index_path, "r", encoding="utf-8") as file:
            index_html = file.read()

        self.assertIn('/vendor/chart.umd.js', index_html)
        self.assertNotIn('cdn.jsdelivr.net', index_html)
        self.assertTrue(os.path.isfile(chart_path))

    def test_client_ping_is_added_to_all_windows_on_same_endpoint(self):
        process_info = {
            "detected": True,
            "pid": 100,
            "instances": [
                {"pid": 100, "remote_ip": "51.77.68.91", "remote_port": 29001, "has_connection": True},
                {"pid": 200, "remote_ip": "51.77.68.91", "remote_port": 29001, "has_connection": True},
            ],
        }
        tcp = {"target": "51.77.68.91:29001", "last_rtt": 42.5, "avg_rtt": 44.0}
        icmp = {"target_ip": "51.77.68.91", "last_rtt": 31.0, "avg_rtt": 32.0}

        result = NetPulseApp._attach_client_pings(process_info, icmp, tcp)

        self.assertEqual([item["ping_ms"] for item in result["instances"]], [42.5, 42.5])
        self.assertEqual([item["ping_type"] for item in result["instances"]], ["TCP", "TCP"])
        self.assertEqual(result["ping_ms"], 42.5)

    def test_auto_detection_switches_when_only_port_changed(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.auto_detect = True
        app.target_ip = "51.77.68.91"
        app.target_port = 29000
        app.change_target = Mock()
        process_info = {
            "detected": True,
            "instances_count": 2,
            "primary_instance": {
                "pid": 100,
                "remote_ip": "51.77.68.91",
                "remote_port": 29001,
                "has_connection": True,
            },
        }

        changed = app._select_process_target(process_info)

        self.assertTrue(changed)
        app.change_target.assert_called_once_with("51.77.68.91", 29001)

    def test_chart_series_aligns_tcp_by_timestamp_and_marks_spike(self):
        ping_history = [
            {"timestamp": 901.0, "rtt": 34.0},
            {"timestamp": 902.0, "rtt": 35.0},
            {"timestamp": 903.0, "rtt": 80.0},
            {"timestamp": 951.0, "rtt": 36.0},
        ]
        tcp_history = [{"timestamp": 951.0, "rtt": 50.0}]

        chart = NetPulseApp._build_chart_series(
            ping_history,
            tcp_history,
            timeframe_seconds=900,
            end_timestamp=1000.0,
        )

        self.assertEqual(chart["bucket_seconds"], 10)
        self.assertEqual(chart["icmp"][0], 35.0)
        self.assertEqual(chart["spikes"][0], 80.0)
        self.assertIsNone(chart["tcp"][0])
        self.assertEqual(chart["tcp"][5], 50.0)
        self.assertEqual(len(chart["labels"]), len(chart["icmp"]))
        self.assertEqual(len(chart["labels"]), len(chart["tcp"]))
        self.assertEqual(len(chart["labels"]), len(chart["icmp_loss"]))
        self.assertEqual(len(chart["labels"]), len(chart["tcp_loss"]))

    def test_chart_marks_only_real_packet_loss(self):
        ping_history = [
            {"timestamp": 1001.2, "rtt": 34.0, "success": True},
            {"timestamp": 1002.2, "rtt": None, "success": False},
            {"timestamp": 1003.2, "rtt": 35.0, "success": True},
        ]
        tcp_history = [
            {"timestamp": 1001.2, "rtt": 45.0, "success": True},
            {"timestamp": 1002.2, "rtt": None, "success": False},
            # В корзине 1003 нет пробы: это технический пропуск, а не потеря.
            {"timestamp": 1004.2, "rtt": 47.0, "success": True},
        ]

        chart = NetPulseApp._build_chart_series(
            ping_history,
            tcp_history,
            timeframe_seconds=60,
            end_timestamp=1005.0,
        )

        self.assertEqual(chart["icmp_loss"][:4], [False, True, False, False])
        self.assertEqual(chart["tcp_loss"][:4], [False, True, False, False])
        self.assertIsNone(chart["tcp"][2])
        self.assertFalse(chart["tcp_loss"][2])

    def test_tcp_history_respects_selected_timeframe(self):
        tcp_pinger = TCPGamePinger(target_ip="127.0.0.1", history_seconds=900)
        now = time.time()
        with tcp_pinger.lock:
            tcp_pinger.history.extend([
                {"timestamp": now - 120, "time_str": "00:00:00", "rtt": 40.0, "success": True},
                {"timestamp": now - 10, "time_str": "00:01:50", "rtt": 42.0, "success": True},
            ])

        summary = tcp_pinger.get_summary(timeframe_seconds=60)

        self.assertEqual([item["rtt"] for item in summary["history"]], [42.0])
        self.assertGreaterEqual(tcp_pinger.history.maxlen, 900)

    def test_partial_config_keeps_default_values(self):
        app = NetPulseApp.__new__(NetPulseApp)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"monitoring": {"ping_interval_ms": 250}}, config_file)

            config = app._load_config(config_path)

        self.assertEqual(config["monitoring"]["ping_interval_ms"], 250)
        self.assertEqual(config["monitoring"]["max_hops"], 18)
        self.assertEqual(config["target_server"]["port"], 29000)

    def test_dpi_manager_builds_target_only_command_and_stops_owned_process(self):
        captured = {}
        fake_process = FakeProcess()

        def process_factory(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return fake_process

        with tempfile.TemporaryDirectory() as temp_dir:
            winws_path = os.path.join(temp_dir, "winws.exe")
            with open(winws_path, "wb") as file:
                file.write(b"test")
            manager = DpiBypassManager(
                temp_dir,
                {"zapret_path": winws_path},
                process_factory=process_factory,
                admin_provider=lambda: True,
                startup_delay=0,
            )

            result = manager.start("multisplit", ["51.77.68.91"], [29002, 29001, 29002])
            self.assertTrue(result["success"])
            self.assertIn("--wf-tcp=29001,29002", captured["command"])
            self.assertIn("--filter-tcp=29001,29002", captured["command"])
            self.assertFalse(any("1024-65535" in arg for arg in captured["command"]))
            self.assertNotIn("shell", captured["kwargs"])

            target_arg = next(arg for arg in captured["command"] if arg.startswith("--ipset="))
            with open(target_arg.split("=", 1)[1], "r", encoding="ascii") as file:
                self.assertEqual(file.read(), "51.77.68.91\n")

            manager.stop()
            self.assertTrue(fake_process.terminated)

    def test_dpi_manager_refuses_start_without_admin_rights(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            winws_path = os.path.join(temp_dir, "winws.exe")
            with open(winws_path, "wb") as file:
                file.write(b"test")
            manager = DpiBypassManager(
                temp_dir,
                {"zapret_path": winws_path},
                admin_provider=lambda: False,
                startup_delay=0,
            )

            result = manager.start("multisplit", ["51.77.68.91"], [29001])

            self.assertFalse(result["success"])
            self.assertIn("администратора", result["error"])
            self.assertFalse(manager.get_status()["active"])

    def test_route_analyzer_distinguishes_local_dpi_and_transit_problems(self):
        stable_ping = {
            "target_ip": "51.77.68.91",
            "avg_rtt": 35.0,
            "jitter": 1.0,
            "rolling_loss_pct": 0.0,
            "history": [{"rtt": 35.0, "success": True}] * 10,
        }
        stable_tcp = {
            "last_rtt": 45.0,
            "total_probes": 10,
            "history": [{"rtt": 45.0, "success": True}] * 10,
        }

        local_route = [
            {"hop": 1, "ip": "192.168.1.1", "sent": 10, "loss_pct": 10.0, "avg_rtt": 12.0, "is_rate_limited": False},
        ]
        local = RouteAnalysisEngine.analyze(local_route, stable_ping, stable_tcp)
        self.assertEqual(local["kind"], "local")
        self.assertFalse(local["can_use_dpi"])
        self.assertFalse(local["can_use_relay"])

        dpi_tcp = {
            "last_rtt": None,
            "total_probes": 10,
            "history": [
                *[{"rtt": 45.0, "success": True}] * 8,
                *[{"rtt": None, "success": False}] * 2,
            ],
        }
        dpi_route = [
            {"hop": 1, "ip": "192.168.1.1", "sent": 10, "loss_pct": 0.0, "avg_rtt": 1.0, "is_rate_limited": False},
            {"hop": 2, "ip": "10.0.0.1", "sent": 10, "loss_pct": 0.0, "avg_rtt": 5.0, "is_rate_limited": False},
            {"hop": 3, "ip": "51.77.68.91", "sent": 10, "loss_pct": 0.0, "avg_rtt": 35.0, "is_rate_limited": False},
        ]
        dpi = RouteAnalysisEngine.analyze(dpi_route, stable_ping, dpi_tcp)
        self.assertEqual(dpi["kind"], "dpi")
        self.assertTrue(dpi["can_use_dpi"])

        transit_route = [
            {"hop": 1, "ip": "192.168.1.1", "sent": 10, "loss_pct": 0.0, "avg_rtt": 1.0, "is_rate_limited": False},
            {"hop": 2, "ip": "10.0.0.1", "sent": 10, "loss_pct": 0.0, "avg_rtt": 5.0, "is_rate_limited": False},
            {"hop": 3, "ip": "203.0.113.1", "sent": 10, "loss_pct": 0.0, "avg_rtt": 42.0, "is_rate_limited": False},
            {"hop": 4, "ip": "51.77.68.91", "sent": 10, "loss_pct": 0.0, "avg_rtt": 48.0, "is_rate_limited": False},
        ]
        degraded_tcp = {
            "last_rtt": 105.0,
            "total_probes": 10,
            "history": [{"rtt": 105.0, "success": True}] * 10,
        }
        transit = RouteAnalysisEngine.analyze(transit_route, stable_ping, degraded_tcp)
        self.assertEqual(transit["kind"], "transit")
        self.assertTrue(transit["can_use_relay"])

    def test_wireguard_relay_requires_exact_game_split_route(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            safe_config = os.path.join(temp_dir, "safe.conf")
            broad_config = os.path.join(temp_dir, "broad.conf")
            with open(safe_config, "w", encoding="utf-8") as file:
                file.write("[Peer]\nAllowedIPs = 51.77.68.91/32\n")
            with open(broad_config, "w", encoding="utf-8") as file:
                file.write("[Peer]\nAllowedIPs = 0.0.0.0/0\n")

            WireGuardRelayManager._validate_split_tunnel(safe_config, ["51.77.68.91"])
            with self.assertRaises(ValueError):
                WireGuardRelayManager._validate_split_tunnel(broad_config, ["51.77.68.91"])

    def test_calibration_selects_profile_with_lower_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CalibrationStore(os.path.join(temp_dir, "calibration.json"))
            ping = {
                "jitter": 2.0,
                "history": [{"rtt": 35.0, "success": True}] * 12,
            }
            direct_tcp = {
                "history": [
                    *[{"rtt": 70.0, "success": True}] * 10,
                    *[{"rtt": None, "success": False}] * 2,
                ]
            }
            optimized_tcp = {"history": [{"rtt": 45.0, "success": True}] * 12}

            store.capture("51.77.68.91", "direct", "direct", ping, direct_tcp)
            store.capture("51.77.68.91", "dpi", "multisplit", ping, optimized_tcp)
            summary = store.get_summary("51.77.68.91")

            self.assertEqual(summary["best"]["profile"], "multisplit")
            self.assertLess(summary["best"]["score"], max(item["score"] for item in summary["records"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
