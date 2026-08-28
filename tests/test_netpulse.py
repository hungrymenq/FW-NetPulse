import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import Mock


app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from core.pinger import Win32Pinger
from core.lag_logger import LagHistoryLogger
from core.process_tracker import GameProcessTracker
from core.tcp_pinger import TCPGamePinger
from core.tracer import VisualMTREngine
from core.route_optimizer import (
    CalibrationStore,
    DpiBypassManager,
    RouteAnalysisEngine,
    WireGuardRelayManager,
)
from run_monitor import GameEndpointMonitor, NetPulseApp
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

    def test_multi_client_payload_reuses_same_endpoint_measurements(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.endpoint_monitors_lock = threading.RLock()
        app.max_endpoint_monitors = 8
        first_monitor = object()
        second_monitor = object()
        app.endpoint_monitors = {
            "51.77.68.91:29001": first_monitor,
            "51.77.68.92:29002": second_monitor,
        }

        def endpoint_payload(monitor, timeframe):
            ping = 41.0 if monitor is first_monitor else 58.0
            return {
                "ping": {"last_rtt": ping - 8, "avg_rtt": ping - 7},
                "tcp_ping": {"last_rtt": ping, "avg_rtt": ping + 1},
                "chart": {"labels": [], "icmp": [], "tcp": []},
                "route": [],
                "recent_freezes": [],
            }

        app._get_endpoint_payload = Mock(side_effect=endpoint_payload)
        process_info = {
            "pid": 100,
            "instances": [
                {"index": 1, "pid": 100, "remote_ip": "51.77.68.91", "remote_port": 29001, "has_connection": True},
                {"index": 2, "pid": 200, "remote_ip": "51.77.68.91", "remote_port": 29001, "has_connection": True},
                {"index": 3, "pid": 300, "remote_ip": "51.77.68.92", "remote_port": 29002, "has_connection": True},
            ],
        }

        multi_client, enriched = app._build_multi_client_payload(process_info, 60)

        self.assertEqual(app._get_endpoint_payload.call_count, 2)
        self.assertEqual(len(multi_client["windows"]), 3)
        self.assertEqual(len(multi_client["endpoints"]), 2)
        self.assertEqual([item["ping_ms"] for item in enriched["instances"]], [41.0, 41.0, 58.0])
        self.assertEqual(multi_client["active_window_id"], "pid-100")

    def test_lag_history_can_be_filtered_by_game_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = LagHistoryLogger(os.path.join(temp_dir, "history.db"))
            logger.log_freeze_event({
                "target": "51.77.68.91",
                "target_port": 29001,
                "window_pid": 100,
                "window_title": "Окно #1",
                "reason": "Потеря пакета",
            })
            logger.log_freeze_event({
                "target": "51.77.68.92",
                "target_port": 29002,
                "window_pid": 200,
                "window_title": "Окно #2",
                "reason": "Скачок задержки",
            })

            first_endpoint = logger.get_recent_events(
                target_ip="51.77.68.91",
                target_port=29001,
            )

        self.assertEqual(len(first_endpoint), 1)
        self.assertEqual(first_endpoint[0]["target_port"], 29001)
        self.assertEqual(first_endpoint[0]["window_pid"], 100)

    def test_old_lag_database_is_migrated_without_losing_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "old-history.db")
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("""
                    CREATE TABLE freeze_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        datetime_str TEXT NOT NULL,
                        target_ip TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        peak_rtt REAL,
                        root_cause TEXT,
                        suspect_hop INTEGER,
                        suspect_ip TEXT
                    )
                """)
                connection.execute("""
                    INSERT INTO freeze_events (
                        timestamp, datetime_str, target_ip, reason, peak_rtt,
                        root_cause, suspect_hop, suspect_ip
                    ) VALUES (1, '2026-08-25 00:00:01', '51.77.68.91', 'Старое событие', 150, 'Анализ', 3, '192.0.2.1')
                """)
                connection.commit()

            logger = LagHistoryLogger(db_path)
            events = logger.get_recent_events(target_ip="51.77.68.91", target_port=29000)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "Старое событие")
        self.assertIsNone(events[0]["target_port"])

    def test_stale_unselected_endpoint_monitor_is_stopped(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.endpoint_monitors_lock = threading.RLock()
        app.active_endpoint_id = "51.77.68.91:29000"
        app.default_endpoint_id = "51.77.68.91:29000"
        primary = Mock()
        stale = Mock()
        app.endpoint_monitors = {
            "51.77.68.91:29000": primary,
            "51.77.68.92:29001": stale,
        }
        app.endpoint_last_seen = {
            "51.77.68.91:29000": time.time(),
            "51.77.68.92:29001": time.time() - 61,
        }

        app._sync_endpoint_monitors({"instances": []})

        self.assertNotIn("51.77.68.92:29001", app.endpoint_monitors)
        stale.stop.assert_called_once()
        primary.stop.assert_not_called()

    def test_unlisted_game_port_gets_its_own_monitor(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.endpoint_monitors_lock = threading.RLock()
        app.active_endpoint_id = "51.77.68.91:29000"
        app.default_endpoint_id = app.active_endpoint_id
        app.endpoint_monitors = {}
        app.endpoint_last_seen = {}
        monitor = Mock(endpoint_id="89.111.154.169:29003")
        app._ensure_endpoint_monitor = Mock(return_value=monitor)

        app._sync_endpoint_monitors({
            "instances": [{
                "pid": 16740,
                "remote_ip": "89.111.154.169",
                "remote_port": 29003,
                "has_connection": True,
                "is_game_endpoint": False,
            }]
        })

        app._ensure_endpoint_monitor.assert_called_once_with("89.111.154.169", 29003)
        self.assertIn("89.111.154.169:29003", app.endpoint_last_seen)

    def test_endpoint_monitor_start_and_stop_are_idempotent(self):
        monitor = GameEndpointMonitor.__new__(GameEndpointMonitor)
        monitor.lock = threading.Lock()
        monitor.started = False
        monitor.pinger = Mock()
        monitor.tcp_pinger = Mock()
        monitor.mtr_engine = Mock()

        monitor.start()
        monitor.start()
        monitor.stop()
        monitor.stop()

        for service in (monitor.pinger, monitor.tcp_pinger, monitor.mtr_engine):
            service.start.assert_called_once()
            service.stop.assert_called_once()
        self.assertFalse(monitor.started)

    def test_endpoint_monitor_serializes_concurrent_start_and_stop(self):
        monitor = GameEndpointMonitor.__new__(GameEndpointMonitor)
        monitor.lock = threading.Lock()
        monitor.started = False
        monitor.pinger = Mock()
        monitor.tcp_pinger = Mock()
        monitor.mtr_engine = Mock()
        start_entered = threading.Event()
        continue_start = threading.Event()

        def blocked_start():
            start_entered.set()
            self.assertTrue(continue_start.wait(timeout=1.0))

        monitor.pinger.start.side_effect = blocked_start
        start_thread = threading.Thread(target=monitor.start)
        stop_thread = threading.Thread(target=monitor.stop)
        start_thread.start()
        self.assertTrue(start_entered.wait(timeout=1.0))
        stop_thread.start()
        self.assertTrue(stop_thread.is_alive(), "stop() должен дождаться завершения start()")
        continue_start.set()
        start_thread.join(timeout=1.0)
        stop_thread.join(timeout=1.0)

        self.assertFalse(start_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertFalse(monitor.started)
        for service in (monitor.pinger, monitor.tcp_pinger, monitor.mtr_engine):
            service.stop.assert_called_once()

    def test_endpoint_monitor_rolls_back_partial_start(self):
        monitor = GameEndpointMonitor.__new__(GameEndpointMonitor)
        monitor.lock = threading.Lock()
        monitor.started = False
        monitor.pinger = Mock()
        monitor.tcp_pinger = Mock()
        monitor.tcp_pinger.start.side_effect = RuntimeError("тестовый сбой")
        monitor.mtr_engine = Mock()

        with self.assertRaises(RuntimeError):
            monitor.start()

        self.assertFalse(monitor.started)
        monitor.pinger.stop.assert_called_once()
        monitor.tcp_pinger.stop.assert_not_called()
        monitor.mtr_engine.start.assert_not_called()

    def test_failed_endpoint_monitor_is_removed_for_retry(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.endpoint_monitors_lock = threading.RLock()
        app.endpoint_monitors = {}
        app.endpoint_last_seen = {}
        app.max_endpoint_monitors = 8
        app.running = True
        broken_monitor = Mock(endpoint_id="51.77.68.92:29002")
        broken_monitor.start.side_effect = RuntimeError("тестовый сбой")
        app._create_endpoint_monitor = Mock(return_value=broken_monitor)

        result = app._ensure_endpoint_monitor("51.77.68.92", 29002)

        self.assertIsNone(result)
        self.assertNotIn("51.77.68.92:29002", app.endpoint_monitors)
        self.assertNotIn("51.77.68.92:29002", app.endpoint_last_seen)

    def test_endpoint_monitor_limit_rejects_new_target(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.endpoint_monitors_lock = threading.RLock()
        app.endpoint_monitors = {"51.77.68.91:29000": Mock()}
        app.endpoint_last_seen = {"51.77.68.91:29000": time.time()}
        app.max_endpoint_monitors = 1
        app.running = False
        app._create_endpoint_monitor = Mock()

        result = app._ensure_endpoint_monitor("51.77.68.92", 29002)

        self.assertIsNone(result)
        app._create_endpoint_monitor.assert_not_called()

    def test_select_process_switches_primary_endpoint_immediately(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.process_info_lock = threading.Lock()
        app.process_selection_lock = threading.RLock()
        app.process_tracker = Mock(selected_pid=None)
        app.change_target = Mock()
        app.active_game_info = {
            "instances": [
                {"pid": 200, "remote_ip": "51.77.68.92", "remote_port": 29002, "has_connection": True},
            ]
        }

        selected = app.select_process(200)

        self.assertTrue(selected)
        self.assertEqual(app.process_tracker.selected_pid, 200)
        app.change_target.assert_called_once_with("51.77.68.92", 29002)

    def test_select_process_rejects_unknown_pid(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.process_info_lock = threading.Lock()
        app.process_selection_lock = threading.RLock()
        app.process_tracker = Mock(selected_pid=None)
        app.change_target = Mock()
        app.active_game_info = {"instances": []}

        selected = app.select_process(999)

        self.assertFalse(selected)
        self.assertIsNone(app.process_tracker.selected_pid)
        app.change_target.assert_not_called()

    def test_select_process_without_connection_keeps_current_target(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.process_info_lock = threading.Lock()
        app.process_selection_lock = threading.RLock()
        app.process_tracker = Mock(selected_pid=None)
        app.change_target = Mock()
        app.active_game_info = {
            "instances": [
                {"pid": 200, "remote_ip": "51.77.68.92", "remote_port": 29002, "has_connection": False},
            ]
        }

        selected = app.select_process(200)

        self.assertTrue(selected)
        self.assertEqual(app.process_tracker.selected_pid, 200)
        app.change_target.assert_not_called()

    def test_select_process_keeps_previous_pid_when_target_change_fails(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.process_info_lock = threading.Lock()
        app.process_selection_lock = threading.RLock()
        app.process_tracker = Mock(selected_pid=100)
        app.change_target = Mock(side_effect=RuntimeError("тестовый сбой"))
        app.active_game_info = {
            "instances": [
                {"pid": 200, "remote_ip": "51.77.68.92", "remote_port": 29002, "has_connection": True},
            ]
        }

        with self.assertRaises(RuntimeError):
            app.select_process(200)

        self.assertEqual(app.process_tracker.selected_pid, 100)

    def test_focus_process_validates_window_before_native_switch(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.select_process = Mock(return_value=True)
        app.process_tracker = Mock()
        app.process_tracker.focus_window.return_value = {
            "success": True,
            "pid": 200,
            "title": "Forsaken World",
        }

        result = app.focus_process_window(200)

        self.assertTrue(result["success"])
        app.select_process.assert_called_once_with(200)
        app.process_tracker.focus_window.assert_called_once_with(200)

    def test_focus_process_rejects_closed_window(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.select_process = Mock(return_value=False)
        app.process_tracker = Mock()

        result = app.focus_process_window(200)

        self.assertFalse(result["success"])
        self.assertIn("не найдено", result["error"])
        app.process_tracker.focus_window.assert_not_called()

    def test_interface_contains_per_window_graphs_and_tabs(self):
        index_path = os.path.join(app_dir, "web", "static", "index.html")
        script_path = os.path.join(app_dir, "web", "static", "app.js")
        with open(index_path, "r", encoding="utf-8") as file:
            index_html = file.read()
        with open(script_path, "r", encoding="utf-8") as file:
            script = file.read()

        self.assertIn('id="windowChartsGrid"', index_html)
        self.assertIn('id="mtrWindowTabs"', index_html)
        self.assertIn('id="historyWindowTabs"', index_html)
        self.assertIn("renderWindowCharts", script)
        self.assertIn("updateMultiClientPanels", script)
        self.assertIn('id="toggleMainChartVisibility"', index_html)
        self.assertIn('id="toggleWindowChartsVisibility"', index_html)
        self.assertIn("shouldPauseUiRefresh", script)
        self.assertIn('/api/focus_process', script)
        self.assertIn("const clientCards = new Map()", script)
        self.assertIn("requestedSelectedPid", script)
        self.assertIn('card.addEventListener("click"', script)
        self.assertNotIn('onclick="selectProcessPid', script)
        self.assertNotIn('onclick="event.stopPropagation(); focusProcessWindow', script)

    def test_auto_detection_switches_when_only_port_changed(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.auto_detect = True
        app.process_selection_lock = threading.RLock()
        app.process_tracker = Mock(selected_pid=None)
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

    def test_auto_detection_respects_pid_selected_after_stale_scan(self):
        app = NetPulseApp.__new__(NetPulseApp)
        app.auto_detect = True
        app.process_selection_lock = threading.RLock()
        app.process_tracker = Mock(selected_pid=200)
        app.target_ip = "51.77.68.91"
        app.target_port = 29000
        app.change_target = Mock()
        process_info = {
            "detected": True,
            "instances_count": 2,
            "primary_instance": {
                "pid": 100,
                "remote_ip": "51.77.68.91",
                "remote_port": 29000,
                "has_connection": True,
            },
            "instances": [
                {"pid": 100, "remote_ip": "51.77.68.91", "remote_port": 29000, "has_connection": True},
                {"pid": 200, "remote_ip": "51.77.68.92", "remote_port": 29002, "has_connection": True},
            ],
        }

        changed = app._select_process_target(process_info)

        self.assertTrue(changed)
        app.change_target.assert_called_once_with("51.77.68.92", 29002)

    def test_mtr_does_not_close_icmp_handle_while_thread_is_alive(self):
        engine = VisualMTREngine.__new__(VisualMTREngine)
        engine.running = True
        engine.lifecycle_lock = threading.Lock()
        engine.handle_lock = threading.Lock()
        engine.stop_event = threading.Event()
        engine.thread = Mock()
        engine.thread.is_alive.return_value = True
        engine.h_icmp = 123
        engine.iphlpapi = Mock()

        engine.stop()

        engine.thread.join.assert_called_once_with(timeout=1.5)
        engine.iphlpapi.IcmpCloseHandle.assert_not_called()
        self.assertEqual(engine.h_icmp, 123)

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
