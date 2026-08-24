#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  FW-NetPulse - мониторинг и диагностика игровой сети
  Designed for Forsaken World (FW-Rebirth) and Angelica 3D Engine Games
================================================================================
"""

import sys
import os

# Fix Windows console encoding immediately
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import time
import json
import webbrowser
import threading
import copy
import statistics
from typing import Dict, Any

# В обычном режиме ресурсы лежат рядом со скриптом. В собранном EXE PyInstaller
# распаковывает встроенные ресурсы во временную папку, а пользовательские данные
# должны оставаться рядом с EXE, чтобы история не исчезала после закрытия.
resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else resource_dir
if resource_dir not in sys.path:
    sys.path.insert(0, resource_dir)


def _load_app_version() -> str:
    for base_dir in (app_dir, resource_dir):
        version_path = os.path.join(base_dir, "VERSION")
        try:
            with open(version_path, "r", encoding="utf-8") as version_file:
                version = version_file.read().strip()
            if version:
                return version
        except OSError:
            continue
    return "1.0.0.0"


APP_VERSION = _load_app_version()

from core.pinger import Win32Pinger
from core.tcp_pinger import TCPGamePinger
from core.tracer import VisualMTREngine
from core.process_tracker import GameProcessTracker
from core.lag_logger import LagHistoryLogger
from core.diagnostics import NetworkDiagnosticsEngine
from core.route_optimizer import GameRouteOptimizer
from web.server import create_server

class NetPulseApp:
    def __init__(self, config_path: str = "config.json"):
        full_config_path = os.path.join(app_dir, config_path)
        if not os.path.exists(full_config_path):
            full_config_path = os.path.join(resource_dir, config_path)
        self.config = self._load_config(full_config_path)
        self.target_ip = self.config["target_server"]["ip"]
        self.target_port = self.config["target_server"]["port"]
        self.target_name = self.config["target_server"]["name"]
        self.version = APP_VERSION

        print("=" * 68)
        print(f"   FW-NetPulse v{self.version} | Мониторинг и диагностика игровой сети")
        print(f"   Целевой сервер: {self.target_name} ({self.target_ip}:{self.target_port})")
        print("=" * 68)

        # 1. Initialize Log History
        db_path = os.path.join(app_dir, "data", "network_history.db")
        self.lag_logger = LagHistoryLogger(db_path=db_path)

        # 2. Initialize ICMP Pinger (with 15m buffer: 1800 samples)
        ping_int_ms = self.config["monitoring"].get("ping_interval_ms", 500)
        loss_window_s = self.config["monitoring"].get("packet_loss_window_seconds", 60)
        self.pinger = Win32Pinger(
            target_ip=self.target_ip,
            interval_ms=ping_int_ms,
            history_length=1800,
            loss_window_seconds=loss_window_s,
        )
        self.pinger.freeze_threshold_ms = self.config["monitoring"].get("freeze_threshold_ms", 120)
        self.pinger.freeze_event_cooldown_s = self.config["monitoring"].get("freeze_event_cooldown_s", 10)
        self.pinger.on_freeze = self._on_freeze_detected

        # 3. Initialize TCP Game Port Pinger (:29000)
        self.tcp_pinger = TCPGamePinger(
            target_ip=self.target_ip,
            target_port=self.target_port,
            interval_s=1.0,
            history_seconds=900,
        )

        # 4. Initialize Visual Smart MTR Tracer
        mtr_int_s = self.config["monitoring"].get("mtr_interval_s", 3)
        max_hops = self.config["monitoring"].get("max_hops", 18)
        self.mtr_engine = VisualMTREngine(target_ip=self.target_ip, max_hops=max_hops, interval_s=mtr_int_s)

        # 5. Initialize Dynamic Multi-Client Process Tracker
        proc_names = self.config["monitoring"].get("game_process_names", ["pem.exe", "pemv.exe", "elementclient.exe"])
        preferred_ports = self.config.get("optimizer", {}).get("game_ports", [self.target_port])
        self.process_tracker = GameProcessTracker(
            target_names=proc_names,
            fallback_ip=self.target_ip,
            fallback_port=self.target_port,
            preferred_ports=preferred_ports,
        )

        # 6. Безопасный оптимизатор маршрута и точечный DPI-обход.
        self.route_optimizer = GameRouteOptimizer(app_dir=app_dir, config=self.config.get("optimizer", {}))
        self.route_optimizer.update_targets({}, self.target_ip, self.target_port)

        # 7. Runtime State
        self.running = False
        self.auto_detect = self.config["monitoring"].get("auto_detect_game_process", True)
        self.active_game_info: Dict[str, Any] = {}
        self.process_info_lock = threading.Lock()
        self.tracker_thread = None

    def _load_config(self, path: str) -> Dict[str, Any]:
        default_config = {
            "target_server": {"ip": "51.77.68.91", "port": 29000, "name": "FW-Rebirth Game Server"},
            "monitoring": {
                "ping_interval_ms": 500,
                "mtr_interval_s": 3,
                "max_hops": 18,
                "freeze_threshold_ms": 120,
                "auto_detect_game_process": True,
                "game_process_names": ["pem.exe", "pemv.exe", "elementclient.exe"]
            },
            "web": {"host": "127.0.0.1", "port": 8899, "auto_open_browser": True},
            "optimizer": {
                "enabled": True,
                "zapret_path": "",
                "wireguard_path": "",
                "game_ports": [29000, 29001, 29002],
                "relays": []
            }
        }
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)

                merged = copy.deepcopy(default_config)
                for section, value in loaded.items():
                    if isinstance(value, dict) and isinstance(merged.get(section), dict):
                        merged[section].update(value)
                    else:
                        merged[section] = value
                return merged
            except Exception as e:
                print(f"[Предупреждение конфигурации] Не удалось прочитать {path}: {e}")
        return default_config

    def _on_freeze_detected(self, freeze_event: Dict[str, Any]):
        route_snap = self.mtr_engine.get_route_snapshot()
        ping_snap = self.pinger.get_summary()
        tcp_snap = self.tcp_pinger.get_summary()
        diag = NetworkDiagnosticsEngine.analyze_route(route_snap, ping_snap, tcp_snap)

        freeze_event["root_cause"] = f"{diag['title']}: {diag['message']}"
        freeze_event["suspect_hop"] = diag.get("suspect_hop")
        freeze_event["suspect_ip"] = diag.get("suspect_ip")

        self.lag_logger.log_freeze_event(freeze_event)
        print(f"\n[Обнаружен сетевой фриз] {freeze_event['time_str']} | {freeze_event['reason']} | Причина: {diag['title']}")

    def change_target(self, new_ip: str, new_port: int = 29000):
        print(f"\n[Цель изменена] Мониторинг адреса: {new_ip}:{new_port}")
        self.target_ip = new_ip
        self.target_port = new_port
        self.pinger.set_target(new_ip)
        self.tcp_pinger.set_target(new_ip, new_port)
        self.mtr_engine.set_target(new_ip)

    def _select_process_target(self, info: Dict[str, Any]) -> bool:
        """Переключает монитор на реальный IP и порт выбранного игрового окна."""
        if not self.auto_detect or not info.get("detected"):
            return False

        primary = info.get("primary_instance") or {}
        game_ip = primary.get("remote_ip")
        game_port = primary.get("remote_port")
        if not primary.get("has_connection") or not game_ip or game_ip == "0.0.0.0" or game_port is None:
            return False

        target_changed = game_ip != self.target_ip or int(game_port) != int(self.target_port)
        if not target_changed:
            return False

        print(f"\n[Автопоиск игры] Найдено окон: {info['instances_count']}. PID {primary.get('pid')} подключён к {game_ip}:{game_port}")
        self.change_target(game_ip, int(game_port))
        return True

    def _process_tracking_loop(self):
        while self.running:
            try:
                info = self.process_tracker.scan_all_instances()
                with self.process_info_lock:
                    self.active_game_info = info

                self.route_optimizer.update_targets(info, self.target_ip, self.target_port)
                self._select_process_target(info)
            except Exception as e:
                print(f"[Ошибка поиска игровых окон] {e}")
            time.sleep(1.5)

    @staticmethod
    def _attach_client_pings(
        process_info: Dict[str, Any],
        ping_summary: Dict[str, Any],
        tcp_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Добавляет к каждому окну пинг его сетевого адреса.

        Windows не хранит отдельный RTT для каждого процесса. Если окна подключены
        к одному адресу и порту, их сетевой пинг одинаков и берётся из TCP-пробы.
        """
        result = copy.deepcopy(process_info)
        tcp_target = tcp_summary.get("target")
        icmp_target = ping_summary.get("target_ip")

        for instance in result.get("instances", []):
            endpoint = f"{instance.get('remote_ip')}:{instance.get('remote_port')}"
            if instance.get("has_connection") and endpoint == tcp_target:
                instance["ping_ms"] = tcp_summary.get("last_rtt")
                instance["avg_ping_ms"] = tcp_summary.get("avg_rtt")
                instance["ping_type"] = "TCP"
            elif instance.get("has_connection") and instance.get("remote_ip") == icmp_target:
                instance["ping_ms"] = ping_summary.get("last_rtt")
                instance["avg_ping_ms"] = ping_summary.get("avg_rtt")
                instance["ping_type"] = "ICMP"
            else:
                instance["ping_ms"] = None
                instance["avg_ping_ms"] = None
                instance["ping_type"] = None

        selected_pid = result.get("pid")
        selected = next((item for item in result.get("instances", []) if item.get("pid") == selected_pid), None)
        if selected:
            result["ping_ms"] = selected.get("ping_ms")
            result["ping_type"] = selected.get("ping_type")
        return result

    @staticmethod
    def _build_chart_series(
        ping_history,
        tcp_history,
        timeframe_seconds: int,
        end_timestamp: float = None,
    ) -> Dict[str, Any]:
        """Выравнивает ICMP и TCP по времени и успокаивает длинные графики.

        На длинном интервале несколько сырых измерений объединяются в одну
        временную корзину. Основная линия показывает медиану, а редкий высокий
        замер остаётся отдельной точкой «Скачок» и не теряется.
        """
        safe_timeframe = min(900, max(60, int(timeframe_seconds)))
        bucket_seconds = 1 if safe_timeframe <= 60 else (3 if safe_timeframe <= 300 else (5 if safe_timeframe <= 600 else 10))
        end_ts = float(end_timestamp if end_timestamp is not None else time.time())
        cutoff = end_ts - safe_timeframe

        valid_timestamps = [
            float(item["timestamp"])
            for item in list(ping_history) + list(tcp_history)
            if item.get("timestamp") is not None and cutoff <= float(item["timestamp"]) <= end_ts
        ]
        if valid_timestamps:
            start_ts = max(cutoff, min(valid_timestamps))
        else:
            start_ts = end_ts - bucket_seconds

        start_ts = int(start_ts // bucket_seconds) * bucket_seconds
        end_bucket = (int(end_ts // bucket_seconds) + 1) * bucket_seconds
        bucket_count = max(1, int((end_bucket - start_ts) / bucket_seconds))

        icmp_buckets = [[] for _ in range(bucket_count)]
        tcp_buckets = [[] for _ in range(bucket_count)]
        icmp_loss_buckets = [False for _ in range(bucket_count)]
        tcp_loss_buckets = [False for _ in range(bucket_count)]

        def add_samples(history, buckets, loss_buckets):
            for item in history:
                timestamp = item.get("timestamp")
                if timestamp is None:
                    continue
                index = int((float(timestamp) - start_ts) // bucket_seconds)
                if 0 <= index < bucket_count:
                    if item.get("success") is False:
                        loss_buckets[index] = True
                    rtt = item.get("rtt")
                    if rtt is not None:
                        buckets[index].append(float(rtt))

        add_samples(ping_history, icmp_buckets, icmp_loss_buckets)
        add_samples(tcp_history, tcp_buckets, tcp_loss_buckets)

        def bucket_median(values):
            return round(float(statistics.median(values)), 1) if values else None

        icmp_values = [bucket_median(values) for values in icmp_buckets]
        tcp_values = [bucket_median(values) for values in tcp_buckets]
        spike_values = []
        for values, median_value in zip(icmp_buckets, icmp_values):
            if not values or median_value is None:
                spike_values.append(None)
                continue
            peak = max(values)
            threshold = max(8.0, median_value * 0.25)
            spike_values.append(round(peak, 1) if peak - median_value >= threshold else None)

        average_values = []
        for index, value in enumerate(icmp_values):
            if value is None:
                average_values.append(None)
                continue
            window = [item for item in icmp_values[max(0, index - 4):index + 1] if item is not None]
            average_values.append(round(sum(window) / len(window), 1))

        labels = [
            time.strftime("%H:%M:%S", time.localtime(start_ts + (index + 1) * bucket_seconds))
            for index in range(bucket_count)
        ]
        return {
            "labels": labels,
            "icmp": icmp_values,
            "tcp": tcp_values,
            "icmp_loss": icmp_loss_buckets,
            "tcp_loss": tcp_loss_buckets,
            "average": average_values,
            "spikes": spike_values,
            "bucket_seconds": bucket_seconds,
        }

    def get_live_telemetry(self, timeframe_seconds: int = 60) -> Dict[str, Any]:
        safe_timeframe = min(900, max(60, int(timeframe_seconds)))
        ping_summary = self.pinger.get_summary(timeframe_seconds=safe_timeframe)
        tcp_summary = self.tcp_pinger.get_summary(timeframe_seconds=safe_timeframe)
        route_snapshot = self.mtr_engine.get_route_snapshot()
        diagnostics = NetworkDiagnosticsEngine.analyze_route(route_snapshot, ping_summary, tcp_summary)
        recent_freezes = self.lag_logger.get_recent_events(limit=20)
        chart_series = self._build_chart_series(
            ping_summary.get("history", []),
            tcp_summary.get("history", []),
            safe_timeframe,
        )

        # Always return real-time process info
        with self.process_info_lock:
            cached_process_info = copy.deepcopy(self.active_game_info)
        proc_info = cached_process_info if cached_process_info else self.process_tracker.scan_all_instances()
        proc_info = self._attach_client_pings(proc_info, ping_summary, tcp_summary)
        self.route_optimizer.update_targets(proc_info, self.target_ip, self.target_port)
        optimizer_status = self.route_optimizer.get_status(route_snapshot, ping_summary, tcp_summary)

        return {
            "app": {
                "name": "FW-NetPulse",
                "version": self.version,
            },
            "target": {
                "ip": self.target_ip,
                "port": self.target_port,
                "name": self.target_name
            },
            "ping": ping_summary,
            "tcp_ping": tcp_summary,
            "chart": chart_series,
            "route": route_snapshot,
            "game_process": proc_info,
            "diagnostics": diagnostics,
            "optimizer": optimizer_status,
            "recent_freezes": recent_freezes
        }

    def start(self):
        self.running = True

        # Start pinger, TCP pinger & MTR
        self.pinger.start()
        self.tcp_pinger.start()
        self.mtr_engine.start()

        # Start process tracker thread
        self.tracker_thread = threading.Thread(target=self._process_tracking_loop, daemon=True, name="ProcTracker")
        self.tracker_thread.start()

        # Start Web Server
        host = self.config["web"].get("host", "127.0.0.1")
        configured_port = self.config["web"].get("port", 8899)
        port = int(os.environ.get("FW_NETPULSE_PORT", configured_port))
        server = create_server(host, port, self)

        server_url = f"http://{host}:{port}"
        print(f"\n[Веб-панель] Интерфейс доступен по адресу: {server_url}")
        print(f"[Игровой HUD] Оверлей доступен по адресу: {server_url}/overlay.html")
        print("   (Для остановки монитора нажмите Ctrl+C)\n")

        browser_disabled = os.environ.get("FW_NETPULSE_NO_BROWSER", "").strip() == "1"
        if self.config["web"].get("auto_open_browser", True) and not browser_disabled:
            threading.Timer(0.8, lambda: webbrowser.open(server_url)).start()

        try:
            server.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            print("\n[Завершение работы] Останавливаем службы FW-NetPulse...")
            self.running = False
            try:
                if self.tracker_thread and self.tracker_thread.is_alive():
                    self.tracker_thread.join(timeout=2.0)
            except Exception:
                pass
            for s in (self.pinger, self.tcp_pinger, self.mtr_engine):
                try:
                    s.stop()
                except Exception:
                    pass
            try:
                self.route_optimizer.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            print("[Готово] Все службы остановлены.")

if __name__ == "__main__":
    try:
        app = NetPulseApp("config.json")
        app.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n[Готово] FW-NetPulse остановлен.")
        sys.exit(0)
    except Exception as error:
        print(f"\n[Ошибка запуска] {error}")
        print("Проверьте, что порт веб-панели свободен, а config.json содержит корректные значения.")
        sys.exit(1)
