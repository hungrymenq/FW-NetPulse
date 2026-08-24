import ctypes
import struct
import socket
import time
import threading
from collections import deque
from typing import Optional, Dict, Any, Callable, List

# Win32 Data Structures for ICMP
class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('Ttl', ctypes.c_ubyte),
        ('Tos', ctypes.c_ubyte),
        ('Flags', ctypes.c_ubyte),
        ('OptionsSize', ctypes.c_ubyte),
        ('OptionsData', ctypes.c_void_p),
    ]

class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ('Address', ctypes.c_ulong),
        ('Status', ctypes.c_ulong),
        ('RoundTripTime', ctypes.c_ulong),
        ('DataSize', ctypes.c_ushort),
        ('Reserved', ctypes.c_ushort),
        ('Data', ctypes.c_void_p),
        ('Options', IP_OPTION_INFORMATION),
    ]

class Win32Pinger:
    """
    High-precision ICMP prober utilizing native Windows iphlpapi.dll.
    Operates without administrator rights and maintains up to 15 minutes of granular telemetry.
    """
    def __init__(
        self,
        target_ip: str = "51.77.68.91",
        interval_ms: int = 500,
        history_length: int = 1800,
        loss_window_seconds: int = 60,
    ):
        self.target_ip = target_ip
        self.interval_ms = interval_ms
        self.history_length = history_length  # 1800 samples @ 500ms = 15 minutes
        self.running = False
        self.thread: Optional[threading.Thread] = None

        # Callbacks
        self.on_ping_result: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_freeze: Optional[Callable[[Dict[str, Any]], None]] = None
        self.freeze_threshold_ms = 120
        self.freeze_event_cooldown_s = 10.0
        self._last_freeze_event_at = 0.0

        # Stats tracking
        self.lock = threading.Lock()
        self.history = deque(maxlen=history_length)
        self.total_sent = 0
        self.total_received = 0
        self.total_lost = 0

        # Latency statistics
        self.last_rtt: Optional[float] = None
        self.min_rtt: float = 9999.0
        self.max_rtt: float = 0.0
        self.avg_rtt: float = 0.0
        self.jitter: float = 0.0
        self._prev_rtt: Optional[float] = None

        # Sliding window for rolling packet loss
        safe_interval_s = max(0.05, interval_ms / 1000.0)
        safe_window_s = max(1, int(loss_window_seconds))
        self.sliding_window = deque(maxlen=max(1, int(safe_window_s / safe_interval_s)))

        # Initialize Windows ICMP Handle
        self.iphlpapi = ctypes.windll.iphlpapi
        self.iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
        self.iphlpapi.IcmpSendEcho.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ushort,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong
        ]
        self.iphlpapi.IcmpSendEcho.restype = ctypes.c_ulong
        self.iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        self.iphlpapi.IcmpCloseHandle.restype = ctypes.c_bool
        self.h_icmp = self.iphlpapi.IcmpCreateFile()

    def set_target(self, new_ip: str):
        with self.lock:
            if new_ip != self.target_ip:
                self.target_ip = new_ip
                self.history.clear()
                self.sliding_window.clear()
                self.total_sent = 0
                self.total_received = 0
                self.total_lost = 0
                self.last_rtt = None
                self.min_rtt = 9999.0
                self.max_rtt = 0.0
                self.avg_rtt = 0.0
                self.jitter = 0.0
                self._prev_rtt = None
                self._last_freeze_event_at = 0.0

    def send_single_probe(self, timeout_ms: int = 1500, ttl: int = 128) -> Dict[str, Any]:
        if not self.h_icmp:
            self.h_icmp = self.iphlpapi.IcmpCreateFile()

        try:
            target_ip_little = struct.unpack('<I', socket.inet_aton(self.target_ip))[0]
        except Exception:
            return {"success": False, "rtt": None, "status": "INVALID_IP", "timestamp": time.time()}

        send_data = b"FWNetPulse"
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
        ts = time.time()

        if ret > 0:
            reply = ctypes.cast(reply_buf, ctypes.POINTER(ICMP_ECHO_REPLY)).contents
            if reply.Status == 0:
                rtt = float(reply.RoundTripTime)
                if rtt == 0:
                    rtt = max(0.5, round(measured_rtt, 1))
                return {
                    "success": True,
                    "rtt": rtt,
                    "status": "OK",
                    "timestamp": ts,
                    "address": self.target_ip,
                    "ttl": reply.Options.Ttl
                }
            else:
                return {
                    "success": False,
                    "rtt": None,
                    "status": f"ERR_{reply.Status}",
                    "timestamp": ts,
                    "address": self.target_ip
                }
        else:
            return {
                "success": False,
                "rtt": None,
                "status": "TIMEOUT",
                "timestamp": ts,
                "address": self.target_ip
            }

    def _probe_loop(self):
        while self.running:
            res = self.send_single_probe(timeout_ms=1200)

            with self.lock:
                self.total_sent += 1
                is_freeze = False
                freeze_reason = ""

                if res["success"]:
                    rtt = res["rtt"]
                    self.total_received += 1
                    self.last_rtt = rtt
                    self.min_rtt = min(self.min_rtt, rtt)
                    self.max_rtt = max(self.max_rtt, rtt)

                    if self.total_received == 1:
                        self.avg_rtt = rtt
                    else:
                        self.avg_rtt = (self.avg_rtt * 0.95) + (rtt * 0.05)

                    # RFC 3550 Jitter
                    if self._prev_rtt is not None:
                        diff = abs(rtt - self._prev_rtt)
                        self.jitter = self.jitter + (diff - self.jitter) / 16.0
                    self._prev_rtt = rtt

                    self.sliding_window.append(1)

                    if rtt >= self.freeze_threshold_ms:
                        is_freeze = True
                        freeze_reason = f"Скачок задержки ({round(rtt)} мс ≥ {self.freeze_threshold_ms} мс)"
                else:
                    self.total_lost += 1
                    self.last_rtt = None
                    self.sliding_window.append(0)
                    is_freeze = True
                    freeze_reason = f"Потеря пакета или тайм-аут ({res['status']})"

                if len(self.sliding_window) > 0:
                    loss_count = self.sliding_window.count(0)
                    rolling_loss_pct = round((loss_count / len(self.sliding_window)) * 100.0, 1)
                else:
                    rolling_loss_pct = 0.0

                sample = {
                    "timestamp": res["timestamp"],
                    "time_str": time.strftime("%H:%M:%S", time.localtime(res["timestamp"])),
                    "rtt": self.last_rtt,
                    "success": res["success"],
                    "status": res["status"],
                    "jitter": round(self.jitter, 1),
                    "loss_pct": rolling_loss_pct,
                    "target": self.target_ip
                }
                self.history.append(sample)

            should_report_freeze = (
                is_freeze
                and self.on_freeze
                and (res["timestamp"] - self._last_freeze_event_at >= self.freeze_event_cooldown_s)
            )
            if should_report_freeze:
                try:
                    self._last_freeze_event_at = res["timestamp"]
                    freeze_payload = {
                        "timestamp": res["timestamp"],
                        "time_str": time.strftime("%H:%M:%S", time.localtime(res["timestamp"])),
                        "reason": freeze_reason,
                        "rtt": self.last_rtt,
                        "target": self.target_ip
                    }
                    self.on_freeze(freeze_payload)
                except Exception:
                    pass

            time.sleep(max(0.05, self.interval_ms / 1000.0))

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._probe_loop, daemon=True, name="Win32PingerThread")
            self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=max(2.0, self.interval_ms / 1000.0 + 1.5))
        except Exception:
            pass
        try:
            if self.h_icmp:
                self.iphlpapi.IcmpCloseHandle(self.h_icmp)
                self.h_icmp = None
        except Exception:
            pass

    def _get_history_by_timeframe_unlocked(self, timeframe_seconds: int = 60) -> List[Dict[str, Any]]:
        """Возвращает историю. Вызывается только когда ``self.lock`` уже захвачен."""
        safe_timeframe = max(1, int(timeframe_seconds))
        cutoff = time.time() - safe_timeframe
        samples = [dict(s) for s in self.history if s["timestamp"] >= cutoff]

        # Уменьшаем число точек на длинном графике, чтобы браузер не тормозил.
        if safe_timeframe > 300 and len(samples) > 200:
            step = 2 if safe_timeframe <= 600 else 3
            return samples[::step]
        return samples

    def get_history_by_timeframe(self, timeframe_seconds: int = 60) -> List[Dict[str, Any]]:
        with self.lock:
            return self._get_history_by_timeframe_unlocked(timeframe_seconds)

    def get_summary(self, timeframe_seconds: int = 60) -> Dict[str, Any]:
        with self.lock:
            overall_loss = round((self.total_lost / max(1, self.total_sent)) * 100.0, 1)
            rolling_loss = round((self.sliding_window.count(0) / max(1, len(self.sliding_window))) * 100.0, 1) if self.sliding_window else 0.0
            history = self._get_history_by_timeframe_unlocked(timeframe_seconds)
            return {
                "target_ip": self.target_ip,
                "last_rtt": round(self.last_rtt, 1) if self.last_rtt is not None else None,
                "min_rtt": round(self.min_rtt, 1) if self.min_rtt != 9999.0 else None,
                "max_rtt": round(self.max_rtt, 1) if self.max_rtt != 0.0 else None,
                "avg_rtt": round(self.avg_rtt, 1) if self.avg_rtt != 0.0 else None,
                "jitter": round(self.jitter, 1),
                "total_sent": self.total_sent,
                "total_received": self.total_received,
                "total_lost": self.total_lost,
                "overall_loss_pct": overall_loss,
                "rolling_loss_pct": rolling_loss,
                "history": history
            }
