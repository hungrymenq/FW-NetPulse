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
from typing import Dict, Any, Callable, Optional

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
    return "1.1.2.0"


APP_VERSION = _load_app_version()

from core.pinger import Win32Pinger
from core.tcp_pinger import TCPGamePinger
from core.tracer import VisualMTREngine
from core.process_tracker import GameProcessTracker
from core.lag_logger import LagHistoryLogger
from core.diagnostics import NetworkDiagnosticsEngine
from core.route_optimizer import GameRouteOptimizer
from web.server import create_server


class GameEndpointMonitor:
    """Независимые измерения для одного игрового адреса и TCP-порта."""

    def __init__(
        self,
        target_ip: str,
        target_port: int,
        ping_interval_ms: int,
        loss_window_seconds: int,
        freeze_threshold_ms: int,
        freeze_event_cooldown_s: int,
        mtr_interval_s: int,
        max_hops: int,
        on_freeze: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.target_ip = str(target_ip)
        self.target_port = int(target_port)
        self.endpoint_id = f"{self.target_ip}:{self.target_port}"
        self.started = False
        self.lock = threading.Lock()

        self.pinger = Win32Pinger(
            target_ip=self.target_ip,
            interval_ms=ping_interval_ms,
            history_length=1800,
            loss_window_seconds=loss_window_seconds,
        )
        self.pinger.freeze_threshold_ms = freeze_threshold_ms
        self.pinger.freeze_event_cooldown_s = freeze_event_cooldown_s
        if on_freeze:
            self.pinger.on_freeze = lambda event: on_freeze(self.endpoint_id, event)

        self.tcp_pinger = TCPGamePinger(
            target_ip=self.target_ip,
            target_port=self.target_port,
            interval_s=1.0,
            history_seconds=900,
        )
        self.mtr_engine = VisualMTREngine(
            target_ip=self.target_ip,
            max_hops=max_hops,
            interval_s=mtr_interval_s,
        )

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
            started_services = []
            try:
                for service in (self.pinger, self.tcp_pinger, self.mtr_engine):
                    service.start()
                    started_services.append(service)
            except Exception:
                self.started = False
                for service in reversed(started_services):
                    try:
                        service.stop()
                    except Exception:
                        pass
                raise

    def stop(self):
        with self.lock:
            if not self.started:
                return
            self.started = False
            for service in (self.pinger, self.tcp_pinger, self.mtr_engine):
                try:
                    service.stop()
                except Exception:
                    pass


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

        # 1. Локальная история сетевых событий.
        db_path = os.path.join(app_dir, "data", "network_history.db")
        self.lag_logger = LagHistoryLogger(db_path=db_path)

        # 2. Пул независимых измерителей для разных игровых зеркал.
        self.running = False
        self.endpoint_monitors: Dict[str, GameEndpointMonitor] = {}
        self.endpoint_last_seen: Dict[str, float] = {}
        self.endpoint_monitors_lock = threading.RLock()
        configured_limit = self.config["monitoring"].get("multi_client_max_endpoints", 8)
        self.max_endpoint_monitors = min(16, max(1, int(configured_limit)))
        self.active_endpoint_id = self._endpoint_id(self.target_ip, self.target_port)
        self.default_endpoint_id = self.active_endpoint_id
        initial_monitor = self._ensure_endpoint_monitor(self.target_ip, self.target_port)
        if initial_monitor is None:
            raise RuntimeError("Не удалось создать основной сетевой измеритель")
        self._activate_endpoint_monitor(initial_monitor)

        # 3. Поиск всех запущенных окон игры и их TCP-соединений.
        proc_names = self.config["monitoring"].get("game_process_names", ["pem.exe", "pemv.exe", "elementclient.exe"])
        preferred_ports = self.config.get("optimizer", {}).get("game_ports", [self.target_port])
        self.process_tracker = GameProcessTracker(
            target_names=proc_names,
            fallback_ip=self.target_ip,
            fallback_port=self.target_port,
            preferred_ports=preferred_ports,
        )
        self.process_selection_lock = threading.RLock()

        # 4. Безопасный оптимизатор маршрута и точечный DPI-обход.
        self.route_optimizer = GameRouteOptimizer(app_dir=app_dir, config=self.config.get("optimizer", {}))
        self.route_optimizer.update_targets({}, self.target_ip, self.target_port)

        # 5. Текущее состояние поиска процессов.
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
                "freeze_event_cooldown_s": 10,
                "packet_loss_window_seconds": 60,
                "multi_client_max_endpoints": 8,
                "auto_detect_game_process": True,
                "game_process_names": ["pem.exe", "pemv.exe", "elementclient.exe"]
            },
            "web": {"host": "127.0.0.1", "port": 8899, "auto_open_browser": True},
            "optimizer": {
                "enabled": True,
                "zapret_path": "",
                "wireguard_path": "",
                "game_ports": [29000, 29001, 29002, 29003],
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

    @staticmethod
    def _endpoint_id(target_ip: str, target_port: int) -> str:
        if not target_ip or target_port is None:
            return ""
        try:
            return f"{str(target_ip)}:{int(target_port)}"
        except (TypeError, ValueError):
            return ""

    def _create_endpoint_monitor(self, target_ip: str, target_port: int) -> GameEndpointMonitor:
        monitoring = self.config["monitoring"]
        return GameEndpointMonitor(
            target_ip=target_ip,
            target_port=target_port,
            ping_interval_ms=int(monitoring.get("ping_interval_ms", 500)),
            loss_window_seconds=int(monitoring.get("packet_loss_window_seconds", 60)),
            freeze_threshold_ms=int(monitoring.get("freeze_threshold_ms", 120)),
            freeze_event_cooldown_s=int(monitoring.get("freeze_event_cooldown_s", 10)),
            mtr_interval_s=int(monitoring.get("mtr_interval_s", 3)),
            max_hops=int(monitoring.get("max_hops", 18)),
            on_freeze=self._on_endpoint_freeze_detected,
        )

    def _ensure_endpoint_monitor(self, target_ip: str, target_port: int) -> Optional[GameEndpointMonitor]:
        endpoint_id = self._endpoint_id(target_ip, target_port)
        with self.endpoint_monitors_lock:
            existing = self.endpoint_monitors.get(endpoint_id)
            if existing:
                return existing
            if len(self.endpoint_monitors) >= self.max_endpoint_monitors:
                print(
                    f"[Лимит зеркал] Адрес {endpoint_id} не добавлен: "
                    f"одновременно разрешено {self.max_endpoint_monitors} целей"
                )
                return None
            monitor = self._create_endpoint_monitor(target_ip, target_port)
            self.endpoint_monitors[endpoint_id] = monitor
            self.endpoint_last_seen[endpoint_id] = time.time()

        if self.running:
            try:
                monitor.start()
            except Exception as error:
                print(f"[Ошибка запуска измерителя {endpoint_id}] {error}")
                # Не оставляем сломанный измеритель в пуле: иначе последующие
                # попытки получили бы тот же объект и больше не пробовали запуск.
                with self.endpoint_monitors_lock:
                    if self.endpoint_monitors.get(endpoint_id) is monitor:
                        self.endpoint_monitors.pop(endpoint_id, None)
                        self.endpoint_last_seen.pop(endpoint_id, None)
                return None
        return monitor

    def _activate_endpoint_monitor(self, monitor: GameEndpointMonitor):
        """Переключает общие карточки на измеритель выбранного окна."""
        with self.endpoint_monitors_lock:
            self.active_endpoint_id = monitor.endpoint_id
            self.target_ip = monitor.target_ip
            self.target_port = monitor.target_port
            self.pinger = monitor.pinger
            self.tcp_pinger = monitor.tcp_pinger
            self.mtr_engine = monitor.mtr_engine

    def _sync_endpoint_monitors(self, process_info: Dict[str, Any]):
        """Создаёт по одному набору проб для каждого уникального игрового зеркала."""
        seen = set()
        now = time.time()
        for instance in process_info.get("instances", []):
            # Процесс игры уже отфильтрован по точному имени. Поэтому отслеживаем
            # его фактический основной TCP-сокет даже на новом порту зеркала.
            if not instance.get("has_connection"):
                continue
            target_ip = instance.get("remote_ip")
            target_port = instance.get("remote_port")
            if not target_ip or target_ip == "0.0.0.0" or target_port is None:
                continue
            endpoint_id = self._endpoint_id(target_ip, target_port)
            if endpoint_id in seen:
                continue
            seen.add(endpoint_id)
            monitor = self._ensure_endpoint_monitor(target_ip, int(target_port))
            if monitor:
                with self.endpoint_monitors_lock:
                    self.endpoint_last_seen[endpoint_id] = now

        stale_monitors = []
        with self.endpoint_monitors_lock:
            for endpoint_id, monitor in list(self.endpoint_monitors.items()):
                if endpoint_id in seen or endpoint_id in (self.active_endpoint_id, self.default_endpoint_id):
                    continue
                if now - self.endpoint_last_seen.get(endpoint_id, now) >= 60:
                    stale_monitors.append(monitor)
                    self.endpoint_monitors.pop(endpoint_id, None)
                    self.endpoint_last_seen.pop(endpoint_id, None)
        for monitor in stale_monitors:
            monitor.stop()

    def _on_endpoint_freeze_detected(self, endpoint_id: str, freeze_event: Dict[str, Any]):
        with self.endpoint_monitors_lock:
            monitor = self.endpoint_monitors.get(endpoint_id)
        if not monitor:
            return

        route_snapshot = monitor.mtr_engine.get_route_snapshot()
        ping_snapshot = monitor.pinger.get_summary()
        tcp_snapshot = monitor.tcp_pinger.get_summary()
        diagnostics = NetworkDiagnosticsEngine.analyze_route(route_snapshot, ping_snapshot, tcp_snapshot)

        event = dict(freeze_event)
        event["target"] = monitor.target_ip
        event["target_port"] = monitor.target_port
        event["root_cause"] = f"{diagnostics['title']}: {diagnostics['message']}"
        event["suspect_hop"] = diagnostics.get("suspect_hop")
        event["suspect_ip"] = diagnostics.get("suspect_ip")

        with self.process_info_lock:
            instances = list(self.active_game_info.get("instances", []))
        matching = [
            item for item in instances
            if self._endpoint_id(item.get("remote_ip"), item.get("remote_port")) == endpoint_id
        ]
        if matching:
            event["window_pid"] = matching[0].get("pid") if len(matching) == 1 else None
            event["window_title"] = ", ".join(item.get("title", "") for item in matching if item.get("title"))

        self.lag_logger.log_freeze_event(event)
        print(
            f"\n[Сетевой фриз {endpoint_id}] {event['time_str']} | "
            f"{event['reason']} | Причина: {diagnostics['title']}"
        )

    def _on_freeze_detected(self, freeze_event: Dict[str, Any]):
        self._on_endpoint_freeze_detected(self.active_endpoint_id, freeze_event)

    def change_target(self, new_ip: str, new_port: int = 29000):
        print(f"\n[Цель изменена] Мониторинг адреса: {new_ip}:{new_port}")
        monitor = self._ensure_endpoint_monitor(new_ip, int(new_port))
        if monitor is None:
            raise RuntimeError("Достигнут лимит одновременно отслеживаемых игровых зеркал")
        self._activate_endpoint_monitor(monitor)

    def _select_process_target(self, info: Dict[str, Any]) -> bool:
        """Переключает монитор на реальный IP и порт выбранного игрового окна."""
        with self.process_selection_lock:
            if not self.auto_detect or not info.get("detected"):
                return False

            # Скан мог начаться до клика пользователя. Перед переключением ещё
            # раз сверяем выбранный PID, чтобы устаревший результат не вернул
            # панель на предыдущее окно.
            selected_pid = self.process_tracker.selected_pid
            primary = info.get("primary_instance") or {}
            if selected_pid is not None:
                selected_instance = next(
                    (item for item in info.get("instances", []) if item.get("pid") == selected_pid),
                    None,
                )
                if selected_instance:
                    primary = selected_instance

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

                self._sync_endpoint_monitors(info)
                self.route_optimizer.update_targets(info, self.target_ip, self.target_port)
                self._select_process_target(info)
            except Exception as e:
                print(f"[Ошибка поиска игровых окон] {e}")
            time.sleep(1.5)

    def select_process(self, pid: int) -> bool:
        """Выбирает окно и сразу переключает общие карточки на его зеркало."""
        selected_pid = int(pid)
        with self.process_selection_lock:
            with self.process_info_lock:
                instance = next(
                    (item for item in self.active_game_info.get("instances", []) if item.get("pid") == selected_pid),
                    None,
                )
            if not instance:
                return False

            # Сначала выполняем потенциально ошибочную смену измерителя и только
            # после успеха фиксируем PID. Так выбранное окно и активная цель не
            # могут разойтись при лимите пула или ошибке запуска службы.
            if instance.get("has_connection") and instance.get("remote_ip") and instance.get("remote_port") is not None:
                self.change_target(instance["remote_ip"], int(instance["remote_port"]))
            self.process_tracker.selected_pid = selected_pid
            return True

    def focus_process_window(self, pid: int) -> Dict[str, Any]:
        """Проверяет PID игры и выводит соответствующее окно на передний план."""
        if not self.select_process(pid):
            return {"success": False, "error": "Игровое окно с таким PID не найдено"}
        return self.process_tracker.focus_window(int(pid))

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

    @staticmethod
    def _without_history(summary: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in summary.items() if key != "history"}

    def _get_endpoint_payload(
        self,
        monitor: GameEndpointMonitor,
        timeframe_seconds: int,
    ) -> Dict[str, Any]:
        ping_summary = monitor.pinger.get_summary(timeframe_seconds=timeframe_seconds)
        tcp_summary = monitor.tcp_pinger.get_summary(timeframe_seconds=timeframe_seconds)
        route_snapshot = monitor.mtr_engine.get_route_snapshot()
        diagnostics = NetworkDiagnosticsEngine.analyze_route(route_snapshot, ping_summary, tcp_summary)
        recent_freezes = self.lag_logger.get_recent_events(
            limit=20,
            target_ip=monitor.target_ip,
            target_port=monitor.target_port,
        )
        return {
            "target": {
                "ip": monitor.target_ip,
                "port": monitor.target_port,
                "endpoint_id": monitor.endpoint_id,
            },
            "ping": self._without_history(ping_summary),
            "tcp_ping": self._without_history(tcp_summary),
            "chart": self._build_chart_series(
                ping_summary.get("history", []),
                tcp_summary.get("history", []),
                timeframe_seconds,
            ),
            "route": route_snapshot,
            "diagnostics": diagnostics,
            "recent_freezes": recent_freezes,
        }

    def _build_multi_client_payload(
        self,
        process_info: Dict[str, Any],
        timeframe_seconds: int,
    ):
        """Формирует окна и данные зеркал без повторения одинаковых измерений."""
        result = copy.deepcopy(process_info)
        endpoints: Dict[str, Dict[str, Any]] = {}

        with self.endpoint_monitors_lock:
            monitors = dict(self.endpoint_monitors)

        for instance in result.get("instances", []):
            endpoint_id = self._endpoint_id(instance.get("remote_ip"), instance.get("remote_port"))
            monitor = monitors.get(endpoint_id) if instance.get("has_connection") else None
            instance["window_id"] = f"pid-{instance.get('pid')}"
            instance["endpoint_id"] = endpoint_id
            instance["monitor_available"] = monitor is not None

            if monitor and endpoint_id not in endpoints:
                endpoints[endpoint_id] = self._get_endpoint_payload(monitor, timeframe_seconds)

            endpoint_data = endpoints.get(endpoint_id, {})
            tcp_summary = endpoint_data.get("tcp_ping", {})
            ping_summary = endpoint_data.get("ping", {})
            if tcp_summary.get("last_rtt") is not None:
                instance["ping_ms"] = tcp_summary.get("last_rtt")
                instance["avg_ping_ms"] = tcp_summary.get("avg_rtt")
                instance["ping_type"] = "TCP"
            elif ping_summary.get("last_rtt") is not None:
                instance["ping_ms"] = ping_summary.get("last_rtt")
                instance["avg_ping_ms"] = ping_summary.get("avg_rtt")
                instance["ping_type"] = "ICMP"
            else:
                instance["ping_ms"] = None
                instance["avg_ping_ms"] = None
                instance["ping_type"] = None

        selected_pid = result.get("pid")
        selected = next(
            (item for item in result.get("instances", []) if item.get("pid") == selected_pid),
            None,
        )
        if selected:
            result["primary_instance"] = selected
            result["ping_ms"] = selected.get("ping_ms")
            result["ping_type"] = selected.get("ping_type")

        return {
            "windows": result.get("instances", []),
            "endpoints": endpoints,
            "active_window_id": f"pid-{selected_pid}" if selected_pid is not None else None,
            "max_endpoints": self.max_endpoint_monitors,
        }, result

    def get_live_telemetry(self, timeframe_seconds: int = 60) -> Dict[str, Any]:
        safe_timeframe = min(900, max(60, int(timeframe_seconds)))

        # Список окон нужен до подготовки графиков, чтобы добавить новые зеркала
        # сразу после появления TCP-соединения игры.
        with self.process_info_lock:
            cached_process_info = copy.deepcopy(self.active_game_info)
        proc_info = cached_process_info if cached_process_info else self.process_tracker.scan_all_instances()
        self._sync_endpoint_monitors(proc_info)

        with self.endpoint_monitors_lock:
            active_monitor = self.endpoint_monitors.get(self.active_endpoint_id)
        if active_monitor is None:
            raise RuntimeError("Активный сетевой измеритель не найден")

        ping_summary = active_monitor.pinger.get_summary(timeframe_seconds=safe_timeframe)
        tcp_summary = active_monitor.tcp_pinger.get_summary(timeframe_seconds=safe_timeframe)
        route_snapshot = active_monitor.mtr_engine.get_route_snapshot()
        diagnostics = NetworkDiagnosticsEngine.analyze_route(route_snapshot, ping_summary, tcp_summary)
        recent_freezes = self.lag_logger.get_recent_events(
            limit=20,
            target_ip=active_monitor.target_ip,
            target_port=active_monitor.target_port,
        )
        chart_series = self._build_chart_series(
            ping_summary.get("history", []),
            tcp_summary.get("history", []),
            safe_timeframe,
        )

        multi_client, proc_info = self._build_multi_client_payload(proc_info, safe_timeframe)
        self.route_optimizer.update_targets(proc_info, active_monitor.target_ip, active_monitor.target_port)
        optimizer_status = self.route_optimizer.get_status(route_snapshot, ping_summary, tcp_summary)

        return {
            "app": {
                "name": "FW-NetPulse",
                "version": self.version,
            },
            "target": {
                "ip": active_monitor.target_ip,
                "port": active_monitor.target_port,
                "name": self.target_name
            },
            "ping": ping_summary,
            "tcp_ping": tcp_summary,
            "chart": chart_series,
            "route": route_snapshot,
            "game_process": proc_info,
            "multi_client": multi_client,
            "diagnostics": diagnostics,
            "optimizer": optimizer_status,
            "recent_freezes": recent_freezes
        }

    def start(self):
        self.running = True

        # Запускаем основной адрес; измерители новых зеркал стартуют автоматически.
        with self.endpoint_monitors_lock:
            monitors = list(self.endpoint_monitors.values())
        for monitor in monitors:
            monitor.start()

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
            with self.endpoint_monitors_lock:
                monitors = list(self.endpoint_monitors.values())
            for monitor in monitors:
                monitor.stop()
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
