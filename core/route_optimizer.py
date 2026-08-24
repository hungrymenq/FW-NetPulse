import ctypes
import ipaddress
import json
import math
import os
import statistics
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


DPI_PROFILES: Dict[str, Dict[str, Any]] = {
    "multisplit": {
        "name": "Аккуратное разделение TCP",
        "description": "Разделяет первые пакеты игрового соединения. Самый безопасный профиль для начала.",
        "risk": "низкий",
        "args": [
            "--dpi-desync=multisplit",
            "--dpi-desync-any-protocol=1",
            "--dpi-desync-cutoff=n3",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-split-seqovl=568",
        ],
    },
    "fake_badseq": {
        "name": "Имитация с неверной последовательностью",
        "description": "Добавляет ложные начальные пакеты для DPI, не принимаемые игровым сервером.",
        "risk": "средний",
        "args": [
            "--dpi-desync=fake,multidisorder",
            "--dpi-desync-any-protocol=1",
            "--dpi-desync-cutoff=n4",
            "--dpi-desync-repeats=6",
            "--dpi-desync-fooling=badseq",
            "--dpi-desync-fake-unknown=0x00000000",
        ],
    },
    "fake_timestamp": {
        "name": "Имитация с временной меткой",
        "description": "Альтернативный профиль для провайдеров, где разделение пакетов не помогает.",
        "risk": "средний",
        "args": [
            "--dpi-desync=fake,multidisorder",
            "--dpi-desync-any-protocol=1",
            "--dpi-desync-cutoff=n4",
            "--dpi-desync-repeats=6",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-fake-unknown=0x00000000",
        ],
    },
}


def _is_windows_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _safe_percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 1)


def calculate_quality_metrics(
    ping_summary: Dict[str, Any],
    tcp_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Строит сравнимую оценку качества по последнему окну измерений."""
    tcp_history = list(tcp_summary.get("history") or [])
    ping_history = list(ping_summary.get("history") or [])
    preferred_history = tcp_history if tcp_history else ping_history
    successful_rtts = [
        float(item["rtt"])
        for item in preferred_history
        if item.get("rtt") is not None and item.get("success") is not False
    ]
    failed_count = sum(1 for item in preferred_history if item.get("success") is False)
    sample_count = len(preferred_history)
    loss_pct = round((failed_count / max(1, sample_count)) * 100.0, 2)

    median_rtt = round(float(statistics.median(successful_rtts)), 1) if successful_rtts else None
    p95_rtt = _safe_percentile(successful_rtts, 0.95)
    jitter = float(ping_summary.get("jitter") or 0.0)

    if median_rtt is None or p95_rtt is None:
        score = 100000.0
    else:
        # Чем меньше итоговая оценка, тем лучше. Потери намеренно имеют большой вес.
        score = median_rtt + max(0.0, p95_rtt - median_rtt) * 2.0 + jitter * 2.5 + loss_pct * 150.0

    return {
        "samples": sample_count,
        "median_rtt": median_rtt,
        "p95_rtt": p95_rtt,
        "jitter": round(jitter, 1),
        "loss_pct": loss_pct,
        "score": round(score, 1),
    }


class RouteAnalysisEngine:
    """Определяет, где возникла проблема и какой тип оптимизации уместен."""

    @staticmethod
    def analyze(
        route: List[Dict[str, Any]],
        ping_summary: Dict[str, Any],
        tcp_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        metrics = calculate_quality_metrics(ping_summary, tcp_summary)
        icmp_loss = float(ping_summary.get("rolling_loss_pct") or 0.0)
        tcp_loss = float(metrics.get("loss_pct") or 0.0)
        icmp_rtt = ping_summary.get("avg_rtt")
        tcp_rtt = metrics.get("median_rtt")

        result = {
            "kind": "collecting",
            "level": "info",
            "title": "Собираем данные маршрута",
            "message": "Для уверенного вывода нужно несколько ответов от каждого узла.",
            "action": "wait",
            "can_use_dpi": False,
            "can_use_relay": False,
            "suspect_hop": None,
            "suspect_ip": None,
            "metrics": metrics,
        }
        if not route:
            return result

        hop1 = route[0]
        if hop1.get("sent", 0) >= 3 and (
            float(hop1.get("loss_pct") or 0.0) > 1.0
            or float(hop1.get("avg_rtt") or 0.0) > 8.0
        ):
            return {
                **result,
                "kind": "local",
                "level": "critical" if float(hop1.get("loss_pct") or 0.0) > 5.0 else "warning",
                "title": "Проблема в домашней сети",
                "message": "Потери или задержка начинаются между компьютером и роутером. Обход и релей это не исправят.",
                "action": "local_fix",
                "suspect_hop": hop1.get("hop"),
                "suspect_ip": hop1.get("ip"),
            }

        destination = route[-1]
        destination_is_real = destination.get("ip") == ping_summary.get("target_ip")
        destination_loss = float(destination.get("loss_pct") or 0.0)
        if destination_is_real and destination.get("sent", 0) >= 3 and destination_loss >= 5.0:
            return {
                **result,
                "kind": "destination",
                "level": "critical",
                "title": "Потери у игрового сервера или его защиты",
                "message": "Потери сохраняются на конечном узле. Смена маршрута может помочь только если проблема находится перед датацентром.",
                "action": "server_check",
                "can_use_relay": True,
                "suspect_hop": destination.get("hop"),
                "suspect_ip": destination.get("ip"),
            }

        # Ищем резкий прирост задержки между отвечающими узлами, игнорируя ICMP Rate-Limit.
        previous: Optional[Dict[str, Any]] = None
        largest_jump: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
        for hop in route:
            if hop.get("is_rate_limited") or hop.get("ip") in (None, "* * *") or hop.get("avg_rtt") is None:
                continue
            if previous is not None:
                jump = float(hop["avg_rtt"]) - float(previous["avg_rtt"])
                if jump > largest_jump[0]:
                    largest_jump = (jump, hop)
            previous = hop

        jump_ms, jump_hop = largest_jump
        # Когда ICMP идёт чисто, а игровой TCP теряется, это более сильный
        # признак фильтрации порта, чем обычный географический прирост RTT.
        strong_dpi_signal = tcp_loss >= 2.0 or (
            tcp_summary.get("last_rtt") is None and icmp_loss < 2.0
        )
        if tcp_summary.get("total_probes", 0) >= 3 and strong_dpi_signal and icmp_loss < 2.0:
            return {
                **result,
                "kind": "dpi",
                "level": "warning",
                "title": "Возможна фильтрация игрового TCP",
                "message": "ICMP-маршрут отвечает нормально, но соединения с игровым портом теряются или заметно задерживаются. Можно проверить точечный DPI-профиль.",
                "action": "dpi",
                "can_use_dpi": True,
            }

        route_is_degraded = (
            metrics.get("p95_rtt") is not None
            and float(metrics["p95_rtt"]) >= 80.0
        ) or icmp_loss >= 2.0
        if jump_hop and jump_hop.get("hop", 0) >= 3 and jump_ms >= 25.0 and route_is_degraded:
            return {
                **result,
                "kind": "transit",
                "level": "warning",
                "title": "Неудачный международный маршрут",
                "message": f"На узле #{jump_hop.get('hop')} задержка увеличилась примерно на {round(jump_ms, 1)} мс. Здесь полезнее игровой релей, а не DPI-обход.",
                "action": "relay",
                "can_use_relay": True,
                "suspect_hop": jump_hop.get("hop"),
                "suspect_ip": jump_hop.get("ip"),
            }

        if tcp_summary.get("total_probes", 0) >= 3 and (
            tcp_rtt is not None
            and icmp_rtt is not None
            and float(tcp_rtt) - float(icmp_rtt) >= 35.0
        ) and icmp_loss < 2.0:
            return {
                **result,
                "kind": "dpi",
                "level": "warning",
                "title": "Возможна фильтрация игрового TCP",
                "message": "Игровой TCP заметно медленнее обычного ICMP, при этом явного плохого участка маршрута не найдено. Можно проверить точечный DPI-профиль.",
                "action": "dpi",
                "can_use_dpi": True,
            }

        if metrics["samples"] >= 5:
            return {
                **result,
                "kind": "stable",
                "level": "good",
                "title": "Маршрут сейчас стабилен",
                "message": "Явных признаков фильтрации или проблемного транзитного узла не обнаружено. Профиль обхода без необходимости лучше не включать.",
                "action": "none",
            }
        return result


class DpiBypassManager:
    """Запускает изолированный winws только для выбранной игры и только по запросу."""

    def __init__(
        self,
        app_dir: str,
        config: Dict[str, Any],
        process_factory: Callable[..., Any] = subprocess.Popen,
        admin_provider: Callable[[], bool] = _is_windows_admin,
        startup_delay: float = 0.7,
    ):
        self.app_dir = os.path.abspath(app_dir)
        self.config = config
        self.process_factory = process_factory
        self.admin_provider = admin_provider
        self.startup_delay = max(0.0, float(startup_delay))
        self.lock = threading.RLock()
        self.process = None
        self.active_profile: Optional[str] = None
        self.active_targets: List[str] = []
        self.active_ports: List[int] = []
        self.last_error: Optional[str] = None
        self.winws_path = self._find_winws()

    def _find_winws(self) -> Optional[str]:
        configured = str(self.config.get("zapret_path") or "").strip()
        candidates = []
        if configured:
            candidates.append(configured)
        environment_dir = os.environ.get("FW_NETPULSE_ZAPRET_DIR", "").strip()
        if environment_dir:
            candidates.append(environment_dir)
        candidates.extend([
            os.path.join(self.app_dir, "tools", "zapret", "bin", "winws.exe"),
            os.path.join(self.app_dir, "zapret-discord-youtube-main", "bin", "winws.exe"),
            os.path.join(os.path.dirname(self.app_dir), "zapret-discord-youtube-main", "bin", "winws.exe"),
        ])

        for candidate in candidates:
            full_path = os.path.abspath(os.path.expandvars(candidate))
            if os.path.isdir(full_path):
                full_path = os.path.join(full_path, "bin", "winws.exe")
            if os.path.isfile(full_path):
                return full_path
        return None

    @staticmethod
    def _validate_targets(targets: Iterable[str], ports: Iterable[int]) -> Tuple[List[str], List[int]]:
        safe_targets = []
        for value in targets:
            ip = ipaddress.ip_address(str(value).strip())
            if ip.version != 4 or ip.is_unspecified or ip.is_multicast:
                raise ValueError(f"Недопустимый игровой IP: {value}")
            safe_targets.append(str(ip))
        safe_ports = sorted({int(port) for port in ports if 1 <= int(port) <= 65535})
        if not safe_targets:
            raise ValueError("Не найден IP игрового сервера")
        if not safe_ports:
            raise ValueError("Не найдены игровые TCP-порты")
        return sorted(set(safe_targets)), safe_ports

    def _write_target_file(self, targets: Sequence[str]) -> str:
        target_dir = os.path.join(self.app_dir, "data", "optimizer")
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, "game-targets.txt")
        temporary_path = path + ".tmp"
        with open(temporary_path, "w", encoding="ascii", newline="\n") as file:
            file.write("\n".join(targets) + "\n")
        os.replace(temporary_path, path)
        return path

    def build_command(self, profile_id: str, targets: Iterable[str], ports: Iterable[int]) -> List[str]:
        if profile_id not in DPI_PROFILES:
            raise ValueError("Неизвестный профиль обхода")
        if not self.winws_path:
            raise FileNotFoundError("winws.exe не найден. Укажите папку zapret в config.json")
        safe_targets, safe_ports = self._validate_targets(targets, ports)
        target_file = self._write_target_file(safe_targets)
        ports_text = ",".join(str(port) for port in safe_ports)
        return [
            self.winws_path,
            f"--wf-tcp={ports_text}",
            f"--filter-tcp={ports_text}",
            f"--ipset={target_file}",
            *DPI_PROFILES[profile_id]["args"],
        ]

    def start(self, profile_id: str, targets: Iterable[str], ports: Iterable[int]) -> Dict[str, Any]:
        with self.lock:
            if not self.admin_provider():
                return {"success": False, "error": "Запустите FW-NetPulse от имени администратора"}
            try:
                safe_targets, safe_ports = self._validate_targets(targets, ports)
                command = self.build_command(profile_id, safe_targets, safe_ports)
                self.stop()
                self.process = self.process_factory(
                    command,
                    cwd=os.path.dirname(self.winws_path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if self.startup_delay:
                    time.sleep(self.startup_delay)
                return_code = self.process.poll()
                if return_code is not None:
                    self.process = None
                    raise RuntimeError(f"winws завершился с кодом {return_code}")
                self.active_profile = profile_id
                self.active_targets = safe_targets
                self.active_ports = safe_ports
                self.last_error = None
                return {
                    "success": True,
                    "message": "Точечный игровой обход запущен. Переподключитесь к серверу игры.",
                    "profile": profile_id,
                }
            except Exception as error:
                self.last_error = str(error)
                self.active_profile = None
                self.active_targets = []
                self.active_ports = []
                return {"success": False, "error": str(error)}

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            owned_process = self.process
            self.process = None
            if owned_process is not None and owned_process.poll() is None:
                try:
                    owned_process.terminate()
                    owned_process.wait(timeout=3.0)
                except Exception:
                    try:
                        owned_process.kill()
                        owned_process.wait(timeout=2.0)
                    except Exception:
                        pass
            self.active_profile = None
            self.active_targets = []
            self.active_ports = []
            return {"success": True, "message": "Игровой обход отключён"}

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            if self.process is not None and self.process.poll() is not None:
                self.last_error = f"winws неожиданно завершился с кодом {self.process.poll()}"
                self.process = None
                self.active_profile = None
            return {
                "available": self.winws_path is not None,
                "winws_path": self.winws_path,
                "is_admin": self.admin_provider(),
                "active": self.process is not None,
                "active_profile": self.active_profile,
                "active_profile_name": DPI_PROFILES.get(self.active_profile or "", {}).get("name"),
                "targets": list(self.active_targets),
                "ports": list(self.active_ports),
                "last_error": self.last_error,
                "profiles": [
                    {"id": profile_id, **{key: value for key, value in profile.items() if key != "args"}}
                    for profile_id, profile in DPI_PROFILES.items()
                ],
            }


class WireGuardRelayManager:
    """Управляет только явно настроенными WireGuard-релеями для игрового /32."""

    def __init__(
        self,
        config: Dict[str, Any],
        admin_provider: Callable[[], bool] = _is_windows_admin,
        command_runner: Callable[..., Any] = subprocess.run,
    ):
        self.config = config
        self.admin_provider = admin_provider
        self.command_runner = command_runner
        self.lock = threading.RLock()
        self.active_relay: Optional[str] = None
        self.last_error: Optional[str] = None
        configured_path = str(config.get("wireguard_path") or "").strip()
        candidates = [
            configured_path,
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WireGuard", "wireguard.exe"),
        ]
        self.wireguard_path = next((os.path.abspath(path) for path in candidates if path and os.path.isfile(path)), None)

    def _relay_by_id(self, relay_id: str) -> Dict[str, Any]:
        relay = next((item for item in self.config.get("relays", []) if item.get("id") == relay_id), None)
        if not relay:
            raise ValueError("Неизвестный игровой релей")
        return relay

    @staticmethod
    def _validate_split_tunnel(config_path: str, targets: Iterable[str]) -> None:
        if not os.path.isfile(config_path):
            raise FileNotFoundError("Файл конфигурации WireGuard не найден")
        with open(config_path, "r", encoding="utf-8-sig") as file:
            allowed_lines = [line.split("=", 1)[1].strip() for line in file if line.strip().lower().startswith("allowedips") and "=" in line]
        allowed_networks = []
        for line in allowed_lines:
            for value in line.split(","):
                allowed_networks.append(ipaddress.ip_network(value.strip(), strict=False))
        expected = {ipaddress.ip_network(f"{ip}/32") for ip in targets}
        if not expected or set(allowed_networks) != expected:
            raise ValueError("WireGuard AllowedIPs должен содержать только игровые адреса /32")

    def start(self, relay_id: str, targets: Iterable[str]) -> Dict[str, Any]:
        with self.lock:
            if not self.wireguard_path:
                return {"success": False, "error": "WireGuard для Windows не найден"}
            if not self.admin_provider():
                return {"success": False, "error": "Запустите FW-NetPulse от имени администратора"}
            try:
                relay = self._relay_by_id(relay_id)
                config_path = os.path.abspath(os.path.expandvars(relay.get("config_path", "")))
                self._validate_split_tunnel(config_path, targets)
                result = self.command_runner(
                    [self.wireguard_path, "/installtunnelservice", config_path],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout or "WireGuard вернул ошибку").strip())
                self.active_relay = relay_id
                self.last_error = None
                return {"success": True, "message": f"Игровой релей «{relay.get('name', relay_id)}» включён"}
            except Exception as error:
                self.last_error = str(error)
                return {"success": False, "error": str(error)}

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            if not self.active_relay:
                return {"success": True, "message": "Игровой релей уже выключен"}
            relay = self._relay_by_id(self.active_relay)
            config_name = os.path.splitext(os.path.basename(relay.get("config_path", "")))[0]
            try:
                result = self.command_runner(
                    [self.wireguard_path, f"/uninstalltunnelservice", config_name],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout or "WireGuard вернул ошибку").strip())
                self.active_relay = None
                return {"success": True, "message": "Игровой релей отключён"}
            except Exception as error:
                self.last_error = str(error)
                return {"success": False, "error": str(error)}

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "available": self.wireguard_path is not None,
                "is_admin": self.admin_provider(),
                "configured": bool(self.config.get("relays")),
                "active": self.active_relay is not None,
                "active_relay": self.active_relay,
                "last_error": self.last_error,
                "relays": [
                    {"id": item.get("id"), "name": item.get("name", item.get("id")), "location": item.get("location", "Не указано")}
                    for item in self.config.get("relays", [])
                ],
            }


class CalibrationStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.records: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            return loaded if isinstance(loaded, list) else []
        except Exception:
            return []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary_path = self.path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(self.records[-100:], file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, self.path)

    def capture(
        self,
        target: str,
        mode: str,
        profile: str,
        ping_summary: Dict[str, Any],
        tcp_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        metrics = calculate_quality_metrics(ping_summary, tcp_summary)
        if metrics["samples"] < 10:
            raise ValueError("Недостаточно данных: подождите не менее 10 измерений")
        record = {
            "timestamp": time.time(),
            "time_str": time.strftime("%d.%m.%Y %H:%M:%S"),
            "target": target,
            "mode": mode,
            "profile": profile,
            **metrics,
        }
        with self.lock:
            self.records.append(record)
            self.records = self.records[-100:]
            self._save()
        return record

    def get_summary(self, target: str) -> Dict[str, Any]:
        with self.lock:
            relevant = [record for record in self.records if record.get("target") == target]
        latest_by_profile: Dict[str, Dict[str, Any]] = {}
        for record in relevant:
            latest_by_profile[f"{record.get('mode')}:{record.get('profile')}"] = record
        candidates = list(latest_by_profile.values())
        best = min(candidates, key=lambda item: float(item.get("score", 100000.0))) if candidates else None
        return {
            "records": sorted(candidates, key=lambda item: item.get("timestamp", 0), reverse=True),
            "best": best,
            "instruction": "После смены DPI-профиля переподключитесь к игре, поиграйте не менее минуты и сохраните замер.",
        }


class GameRouteOptimizer:
    def __init__(self, app_dir: str, config: Dict[str, Any]):
        self.app_dir = app_dir
        self.config = config
        self.lock = threading.RLock()
        self.target_ips: List[str] = []
        self.target_ports: List[int] = []
        self.dpi = DpiBypassManager(app_dir, config)
        self.relay = WireGuardRelayManager(config)
        self.calibration = CalibrationStore(os.path.join(app_dir, "data", "optimizer", "calibration.json"))

    def update_targets(self, process_info: Dict[str, Any], fallback_ip: str, fallback_port: int) -> None:
        ips = set()
        ports = set()
        for instance in process_info.get("instances", []):
            if not instance.get("has_connection"):
                continue
            remote_ip = instance.get("remote_ip")
            remote_port = instance.get("remote_port")
            try:
                parsed_ip = ipaddress.ip_address(str(remote_ip))
                if parsed_ip.version == 4 and not parsed_ip.is_unspecified:
                    ips.add(str(parsed_ip))
                if remote_port is not None and 1 <= int(remote_port) <= 65535:
                    ports.add(int(remote_port))
            except Exception:
                continue

        if not ips:
            try:
                ips.add(str(ipaddress.ip_address(fallback_ip)))
            except Exception:
                pass
        if not ports:
            ports.update(int(port) for port in self.config.get("game_ports", [fallback_port]) if 1 <= int(port) <= 65535)

        with self.lock:
            self.target_ips = sorted(ips)
            self.target_ports = sorted(ports)

    def get_status(
        self,
        route: List[Dict[str, Any]],
        ping_summary: Dict[str, Any],
        tcp_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self.lock:
            target_ips = list(self.target_ips)
            target_ports = list(self.target_ports)
        target_key = ",".join(target_ips) or "не определён"
        return {
            "enabled": bool(self.config.get("enabled", True)),
            "targets": target_ips,
            "ports": target_ports,
            "analysis": RouteAnalysisEngine.analyze(route, ping_summary, tcp_summary),
            "dpi": self.dpi.get_status(),
            "relay": self.relay.get_status(),
            "calibration": self.calibration.get_summary(target_key),
        }

    def start_dpi(self, profile_id: str) -> Dict[str, Any]:
        if self.relay.get_status()["active"]:
            return {"success": False, "error": "Сначала отключите игровой релей"}
        with self.lock:
            return self.dpi.start(profile_id, self.target_ips, self.target_ports)

    def stop_dpi(self) -> Dict[str, Any]:
        return self.dpi.stop()

    def start_relay(self, relay_id: str) -> Dict[str, Any]:
        if self.dpi.get_status()["active"]:
            return {"success": False, "error": "Сначала отключите DPI-обход"}
        with self.lock:
            return self.relay.start(relay_id, self.target_ips)

    def stop_relay(self) -> Dict[str, Any]:
        return self.relay.stop()

    def capture_calibration(
        self,
        ping_summary: Dict[str, Any],
        tcp_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self.lock:
            target_key = ",".join(self.target_ips) or "не определён"
        dpi_status = self.dpi.get_status()
        relay_status = self.relay.get_status()
        if relay_status["active"]:
            mode = "relay"
            profile = relay_status["active_relay"] or "unknown"
        elif dpi_status["active"]:
            mode = "dpi"
            profile = dpi_status["active_profile"] or "unknown"
        else:
            mode = "direct"
            profile = "direct"
        record = self.calibration.capture(target_key, mode, profile, ping_summary, tcp_summary)
        return {"success": True, "record": record, "calibration": self.calibration.get_summary(target_key)}

    def shutdown(self) -> None:
        # Останавливаем только процессы и релей, запущенные этим экземпляром программы.
        self.dpi.stop()
        self.relay.stop()
