import ctypes
import struct
import socket
import time
import threading
from typing import Dict, List, Any, Optional
from core.pinger import IP_OPTION_INFORMATION, ICMP_ECHO_REPLY
from core.geoip import GeoIPResolver

class HopStats:
    def __init__(self, hop_num: int):
        self.hop_num = hop_num
        self.ip: Optional[str] = None
        self.hostname: Optional[str] = None
        self.sent: int = 0
        self.received: int = 0
        self.lost: int = 0
        self.last_rtt: Optional[float] = None
        self.min_rtt: float = 9999.0
        self.max_rtt: float = 0.0
        self.avg_rtt: float = 0.0
        self.rtt_history: List[float] = []
        self.category: str = "Unknown"
        self.country: str = "Unknown"
        self.flag: str = "🌐"
        self.city: str = "Магистраль"
        self.is_rate_limited: bool = False
        self.resolving_hostname: bool = False

    def update(self, ip: Optional[str], rtt: Optional[float]):
        self.sent += 1
        if ip:
            self.ip = ip
            self._classify()

        if rtt is not None and rtt >= 0:
            self.received += 1
            self.last_rtt = rtt
            self.min_rtt = min(self.min_rtt, rtt)
            self.max_rtt = max(self.max_rtt, rtt)
            if self.received == 1:
                self.avg_rtt = rtt
            else:
                self.avg_rtt = (self.avg_rtt * 0.9) + (rtt * 0.1)
            self.rtt_history.append(rtt)
            if len(self.rtt_history) > 30:
                self.rtt_history.pop(0)
        else:
            self.lost += 1
            self.last_rtt = None

    def _classify(self):
        if not self.ip:
            self.category = "Unknown"
            return

        geo = GeoIPResolver.resolve(self.ip, self.hostname or "")
        self.flag = geo["flag"]
        self.country = geo["country"]
        self.city = geo["city"]

        # Local router / Private IP ranges
        if self.ip.startswith("192.168.") or self.ip.startswith("10.") or self.ip.startswith("172.16.") or self.ip == "127.0.0.1":
            self.category = "Домашний роутер / LAN" if self.hop_num == 1 else "Внутренняя сеть провайдера (CGNAT)"
        elif self.hop_num in (2, 3, 4) and not self.ip.startswith("178.") and not self.ip.startswith("51."):
            self.category = "Шлюз / Магистраль провайдера"
        elif "ovh" in (self.hostname or "").lower() or self.ip.startswith("178.33.") or self.ip.startswith("37.59."):
            self.category = "OVH Датацентр / Магистраль"
        elif self.ip == "51.77.68.91":
            self.category = "Сервер FW-Rebirth (Цель)"
        else:
            self.category = "Транзитный узел"

    def to_dict(self) -> Dict[str, Any]:
        loss_pct = round((self.lost / max(1, self.sent)) * 100.0, 1)
        return {
            "hop": self.hop_num,
            "ip": self.ip if self.ip else "* * *",
            "hostname": self.hostname if self.hostname else (self.ip if self.ip else "Request timed out"),
            "category": self.category,
            "country": self.country,
            "flag": self.flag,
            "city": self.city,
            "is_rate_limited": self.is_rate_limited,
            "sent": self.sent,
            "received": self.received,
            "lost": self.lost,
            "loss_pct": loss_pct,
            "last_rtt": round(self.last_rtt, 1) if self.last_rtt is not None else None,
            "min_rtt": round(self.min_rtt, 1) if self.min_rtt != 9999.0 else None,
            "max_rtt": round(self.max_rtt, 1) if self.max_rtt != 0.0 else None,
            "avg_rtt": round(self.avg_rtt, 1) if self.avg_rtt != 0.0 else None,
            "rtt_history": self.rtt_history[-10:]
        }


class VisualMTREngine:
    """
    Continuous visual MTR route prober with Smart MTR classification & GeoIP integration.
    """
    def __init__(self, target_ip: str = "51.77.68.91", max_hops: int = 18, interval_s: int = 3):
        self.target_ip = target_ip
        self.max_hops = max_hops
        self.interval_s = interval_s
        self.running = False
        self.smart_filter_enabled = True
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        self.hops: Dict[int, HopStats] = {i: HopStats(i) for i in range(1, max_hops + 1)}
        self.last_update_time: float = 0
        self.dns_cache: Dict[str, str] = {}

        # Win32 ICMP Handle
        self.iphlpapi = ctypes.windll.iphlpapi
        self.iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
        self.iphlpapi.IcmpSendEcho.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ushort,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong
        ]
        self.iphlpapi.IcmpSendEcho.restype = ctypes.c_ulong
        self.iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        self.h_icmp = self.iphlpapi.IcmpCreateFile()

    def set_target(self, new_ip: str):
        with self.lock:
            if new_ip != self.target_ip:
                self.target_ip = new_ip
                self.hops = {i: HopStats(i) for i in range(1, self.max_hops + 1)}

    def _resolve_dns_async(self, ip: str, hop: HopStats):
        if ip in self.dns_cache:
            hop.hostname = self.dns_cache[ip]
            hop._classify()
            return

        def worker():
            try:
                host, _, _ = socket.gethostbyaddr(ip)
                self.dns_cache[ip] = host
                hop.hostname = host
            except Exception:
                self.dns_cache[ip] = ip
                hop.hostname = ip
            hop._classify()

        threading.Thread(target=worker, daemon=True).start()

    def _probe_single_ttl(self, ttl: int, timeout_ms: int = 1000) -> tuple:
        if not self.h_icmp:
            self.h_icmp = self.iphlpapi.IcmpCreateFile()

        try:
            target_ip_little = struct.unpack('<I', socket.inet_aton(self.target_ip))[0]
        except Exception:
            return None, None

        send_data = b"MTRTrace"
        reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(send_data) + 8
        reply_buf = ctypes.create_string_buffer(reply_size)
        opt = IP_OPTION_INFORMATION(Ttl=ttl, Tos=0, Flags=0, OptionsSize=0, OptionsData=None)

        start_t = time.perf_counter()
        ret = self.iphlpapi.IcmpSendEcho(
            self.h_icmp,
            target_ip_little,
            send_data,
            len(send_data),
            ctypes.byref(opt),
            reply_buf,
            reply_size,
            timeout_ms
        )
        end_t = time.perf_counter()
        measured_rtt = (end_t - start_t) * 1000.0

        if ret > 0:
            reply = ctypes.cast(reply_buf, ctypes.POINTER(ICMP_ECHO_REPLY)).contents
            if reply.Address != 0:
                resp_ip = socket.inet_ntoa(struct.pack('<I', reply.Address))
                rtt = float(reply.RoundTripTime)
                if rtt == 0:
                    rtt = max(0.5, round(measured_rtt, 1))
                return resp_ip, rtt
        return None, None

    def _mtr_loop(self):
        while self.running:
            dest_reached = False
            for ttl in range(1, self.max_hops + 1):
                if not self.running:
                    break

                resp_ip, rtt = self._probe_single_ttl(ttl, timeout_ms=800)

                with self.lock:
                    hop = self.hops[ttl]
                    hop.update(resp_ip, rtt)

                    if resp_ip and not hop.hostname and not hop.resolving_hostname:
                        hop.resolving_hostname = True
                        self._resolve_dns_async(resp_ip, hop)

                if resp_ip == self.target_ip:
                    dest_reached = True
                    break

            # Smart MTR Rate-Limit check: If destination has 0% loss, intermediate loss is rate-limiting
            with self.lock:
                dest_hop = None
                for i in range(self.max_hops, 0, -1):
                    if self.hops[i].ip == self.target_ip:
                        dest_hop = self.hops[i]
                        break

                if dest_hop and dest_hop.sent >= 3 and (dest_hop.lost / dest_hop.sent) < 0.05:
                    for i in range(1, dest_hop.hop_num):
                        h = self.hops[i]
                        if h.sent >= 3 and (h.lost / h.sent) > 0.15:
                            h.is_rate_limited = True
                        else:
                            h.is_rate_limited = False

                self.last_update_time = time.time()

            time.sleep(max(1.0, float(self.interval_s)))

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._mtr_loop, daemon=True, name="VisualMTREngineThread")
            self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self.h_icmp:
                self.iphlpapi.IcmpCloseHandle(self.h_icmp)
                self.h_icmp = None
        except Exception:
            pass

    def get_route_snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            result = []
            for i in range(1, self.max_hops + 1):
                hop = self.hops[i]
                if hop.sent > 0:
                    d = hop.to_dict()
                    result.append(d)
                    if d["ip"] == self.target_ip:
                        break
            return result
