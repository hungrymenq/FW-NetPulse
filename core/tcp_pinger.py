import socket
import time
import threading
from typing import Dict, Any, Optional
from collections import deque

class TCPGamePinger:
    """
    Measures true TCP 3-way handshake round-trip latency directly to the game gateway daemon (e.g., port 29000).
    Provides exact TCP socket connection latency comparison alongside ICMP ping.
    """
    def __init__(
        self,
        target_ip: str = "51.77.68.91",
        target_port: int = 29000,
        interval_s: float = 1.0,
        history_seconds: int = 900,
    ):
        self.target_ip = target_ip
        self.target_port = target_port
        self.interval_s = interval_s
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        # Stats
        self.last_rtt: Optional[float] = None
        self.min_rtt: float = 9999.0
        self.max_rtt: float = 0.0
        self.avg_rtt: float = 0.0
        self.total_probes = 0
        self.successful_probes = 0
        safe_interval = max(0.2, float(interval_s))
        self.history = deque(maxlen=max(60, int(max(60, history_seconds) / safe_interval)))

    def set_target(self, ip: str, port: int = 29000):
        with self.lock:
            if ip != self.target_ip or port != self.target_port:
                self.target_ip = ip
                self.target_port = port
                self.last_rtt = None
                self.min_rtt = 9999.0
                self.max_rtt = 0.0
                self.avg_rtt = 0.0
                self.total_probes = 0
                self.successful_probes = 0
                self.history.clear()

    def probe_once(self, timeout: float = 1.2) -> Dict[str, Any]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        t0 = time.perf_counter()
        ts = time.time()
        try:
            s.connect((self.target_ip, self.target_port))
            t1 = time.perf_counter()
            rtt = (t1 - t0) * 1000.0
            s.close()
            return {
                "success": True,
                "rtt": round(rtt, 1),
                "timestamp": ts,
                "target": f"{self.target_ip}:{self.target_port}",
                "status": "OK"
            }
        except Exception as e:
            try:
                s.close()
            except Exception:
                pass
            return {
                "success": False,
                "rtt": None,
                "timestamp": ts,
                "target": f"{self.target_ip}:{self.target_port}",
                "status": str(e)
            }

    def _loop(self):
        while self.running:
            res = self.probe_once()
            with self.lock:
                self.total_probes += 1
                if res["success"]:
                    rtt = res["rtt"]
                    self.successful_probes += 1
                    self.last_rtt = rtt
                    self.min_rtt = min(self.min_rtt, rtt)
                    self.max_rtt = max(self.max_rtt, rtt)
                    if self.successful_probes == 1:
                        self.avg_rtt = rtt
                    else:
                        self.avg_rtt = (self.avg_rtt * 0.9) + (rtt * 0.1)
                else:
                    self.last_rtt = None

                self.history.append({
                    "timestamp": res["timestamp"],
                    "time_str": time.strftime("%H:%M:%S", time.localtime(res["timestamp"])),
                    "rtt": self.last_rtt,
                    "success": res["success"]
                })

            time.sleep(max(0.2, self.interval_s))

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True, name="TCPGamePingerThread")
            self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=max(1.5, self.interval_s + 1.3))
        except Exception:
            pass

    def get_summary(self, timeframe_seconds: int = 60) -> Dict[str, Any]:
        with self.lock:
            loss_pct = round(((self.total_probes - self.successful_probes) / max(1, self.total_probes)) * 100.0, 1)
            cutoff = time.time() - max(1, int(timeframe_seconds))
            history = [dict(item) for item in self.history if item["timestamp"] >= cutoff]
            return {
                "target": f"{self.target_ip}:{self.target_port}",
                "last_rtt": self.last_rtt,
                "min_rtt": round(self.min_rtt, 1) if self.min_rtt != 9999.0 else None,
                "max_rtt": round(self.max_rtt, 1) if self.max_rtt != 0.0 else None,
                "avg_rtt": round(self.avg_rtt, 1) if self.avg_rtt != 0.0 else None,
                "total_probes": self.total_probes,
                "loss_pct": loss_pct,
                "history": history
            }
