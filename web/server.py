import http.server
import ipaddress
import json
import os
import sys
import subprocess
import mimetypes
import urllib.parse
from typing import Dict, Any, Optional

class NetPulseHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    MAX_REQUEST_BODY = 64 * 1024

    def __init__(self, *args, monitor_app=None, **kwargs):
        self.monitor_app = monitor_app
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # API Routes
        if path == "/api/status":
            try:
                timeframe = int(query.get("timeframe", [60])[0])
            except (TypeError, ValueError):
                self.send_json_response({"success": False, "error": "Некорректный интервал графика"}, status=400)
                return
            try:
                self.send_json_response(self.monitor_app.get_live_telemetry(timeframe_seconds=timeframe))
            except Exception as error:
                self.send_json_response({"success": False, "error": f"Не удалось получить телеметрию: {error}"}, status=500)
            return
        elif path == "/api/history":
            self.send_json_response(self.monitor_app.lag_logger.get_recent_events(limit=100))
            return
        elif path == "/api/tweaks/status":
            from core.tcp_tweaker import WindowsTCPTweaker
            self.send_json_response(WindowsTCPTweaker.get_status())
            return
        elif path == "/api/export/csv":
            csv_data = self.monitor_app.lag_logger.export_csv()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=fw_netpulse_lag_history.csv")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(csv_data.encode("utf-8"))
            return
        elif path == "/api/export/json":
            json_data = self.monitor_app.lag_logger.export_json()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=fw_netpulse_lag_history.json")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(json_data.encode("utf-8"))
            return

        # Static Web Files
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        if path in ("/", "/index.html"):
            file_path = os.path.join(static_dir, "index.html")
        elif path == "/overlay.html":
            file_path = os.path.join(static_dir, "overlay.html")
        elif path == "/overlay.css":
            file_path = os.path.join(static_dir, "overlay.css")
        elif path == "/overlay.js":
            file_path = os.path.join(static_dir, "overlay.js")
        elif path == "/style.css":
            file_path = os.path.join(static_dir, "style.css")
        elif path == "/app.js":
            file_path = os.path.join(static_dir, "app.js")
        else:
            requested_path = urllib.parse.unquote(path).lstrip("/")
            file_path = os.path.abspath(os.path.join(static_dir, requested_path))

        static_root = os.path.abspath(static_dir)
        try:
            is_inside_static = os.path.commonpath([static_root, os.path.abspath(file_path)]) == static_root
        except ValueError:
            is_inside_static = False

        if not is_inside_static:
            self.send_error(404, "Файл не найден")
            return

        if os.path.isfile(file_path):
            self.serve_file(file_path)
        else:
            self.send_error(404, "Файл не найден")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._is_trusted_local_origin():
            self.send_json_response({"success": False, "error": "Запрос отклонён: разрешено управление только из локальной панели"}, status=403)
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_json_response({"success": False, "error": "Некорректный размер запроса"}, status=400)
            return
        if content_length < 0 or content_length > self.MAX_REQUEST_BODY:
            self.send_json_response({"success": False, "error": "Запрос слишком большой"}, status=413)
            return
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            data = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(data, dict):
                raise ValueError("JSON должен быть объектом")
        except Exception:
            self.send_json_response({"success": False, "error": "Тело запроса должно содержать корректный JSON"}, status=400)
            return

        if path == "/api/target":
            new_ip = data.get("ip", "").strip()
            try:
                parsed_ip = ipaddress.ip_address(new_ip)
                new_port = int(data.get("port", 29000))
                if parsed_ip.version != 4:
                    raise ValueError("поддерживается только IPv4")
                if not 1 <= new_port <= 65535:
                    raise ValueError("порт должен быть от 1 до 65535")
            except Exception as error:
                self.send_json_response({"success": False, "error": f"Некорректная цель: {error}"}, status=400)
                return
            self.monitor_app.change_target(str(parsed_ip), new_port)
            self.send_json_response({"success": True, "message": f"Цель изменена: {parsed_ip}:{new_port}"})
            return

        elif path == "/api/hud/launch":
            try:
                if getattr(sys, "frozen", False):
                    hud_program = os.path.join(os.path.dirname(sys.executable), "FW-NetPulse-HUD.exe")
                    if not os.path.isfile(hud_program):
                        raise FileNotFoundError("Рядом с программой не найден FW-NetPulse-HUD.exe")
                    subprocess.Popen([hud_program])
                else:
                    hud_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hud_overlay.py")
                    subprocess.Popen([sys.executable, hud_script])
                self.send_json_response({"success": True, "message": "Настольный HUD запущен"})
            except Exception as e:
                self.send_json_response({"success": False, "error": f"Не удалось запустить HUD: {e}"}, status=500)
            return

        elif path == "/api/select_process":
            try:
                pid = int(data.get("pid"))
                if pid <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.send_json_response({"success": False, "error": "Некорректный PID процесса"}, status=400)
                return
            try:
                if hasattr(self.monitor_app, "select_process"):
                    if not self.monitor_app.select_process(pid):
                        self.send_json_response({"success": False, "error": "Игровое окно с таким PID не найдено"}, status=404)
                        return
                else:
                    self.monitor_app.process_tracker.selected_pid = pid
            except Exception as error:
                self.send_json_response({"success": False, "error": f"Не удалось выбрать игровое окно: {error}"}, status=409)
                return
            self.send_json_response({"success": True, "selected_pid": pid})
            return

        elif path == "/api/focus_process":
            try:
                pid = int(data.get("pid"))
                if pid <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.send_json_response({"success": False, "error": "Некорректный PID процесса"}, status=400)
                return
            if not hasattr(self.monitor_app, "focus_process_window"):
                self.send_json_response({"success": False, "error": "Переключение окон недоступно"}, status=501)
                return
            try:
                result = self.monitor_app.focus_process_window(pid)
            except Exception as error:
                self.send_json_response({"success": False, "error": f"Не удалось показать игровое окно: {error}"}, status=409)
                return
            self.send_json_response(result, status=200 if result.get("success") else 400)
            return

        elif path == "/api/tweaks/apply":
            from core.tcp_tweaker import WindowsTCPTweaker
            res = WindowsTCPTweaker.apply_tweaks()
            self.send_json_response(res)
            return

        elif path == "/api/tweaks/revert":
            from core.tcp_tweaker import WindowsTCPTweaker
            res = WindowsTCPTweaker.revert_tweaks()
            self.send_json_response(res)
            return

        elif path == "/api/optimizer/dpi/start":
            if data.get("confirm") is not True:
                self.send_json_response({"success": False, "error": "Требуется подтверждение запуска обхода"}, status=400)
                return
            result = self.monitor_app.route_optimizer.start_dpi(str(data.get("profile") or "multisplit"))
            self.send_json_response(result, status=200 if result.get("success") else 400)
            return

        elif path == "/api/optimizer/dpi/stop":
            self.send_json_response(self.monitor_app.route_optimizer.stop_dpi())
            return

        elif path == "/api/optimizer/relay/start":
            if data.get("confirm") is not True:
                self.send_json_response({"success": False, "error": "Требуется подтверждение включения релея"}, status=400)
                return
            result = self.monitor_app.route_optimizer.start_relay(str(data.get("relay_id") or ""))
            self.send_json_response(result, status=200 if result.get("success") else 400)
            return

        elif path == "/api/optimizer/relay/stop":
            result = self.monitor_app.route_optimizer.stop_relay()
            self.send_json_response(result, status=200 if result.get("success") else 400)
            return

        elif path == "/api/optimizer/calibration/capture":
            try:
                ping_summary = self.monitor_app.pinger.get_summary(timeframe_seconds=60)
                tcp_summary = self.monitor_app.tcp_pinger.get_summary(timeframe_seconds=60)
                self.send_json_response(self.monitor_app.route_optimizer.capture_calibration(ping_summary, tcp_summary))
            except Exception as error:
                self.send_json_response({"success": False, "error": str(error)}, status=400)
            return

        self.send_error(404, "Not Found")

    def _is_trusted_local_origin(self) -> bool:
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return False
        except Exception:
            return False

        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlparse(origin)
            return parsed.hostname in ("127.0.0.1", "localhost", "::1")
        except Exception:
            return False

    def serve_file(self, file_path: str):
        content_type, _ = mimetypes.guess_type(file_path)
        if not content_type:
            content_type = "text/plain"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")

    def send_json_response(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def log_message(self, format, *args):
        pass


def create_server(host: str, port: int, monitor_app):
    def handler(*args, **kwargs):
        return NetPulseHTTPRequestHandler(*args, monitor_app=monitor_app, **kwargs)

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    http.server.ThreadingHTTPServer.daemon_threads = True
    server = http.server.ThreadingHTTPServer((host, port), handler)
    return server
