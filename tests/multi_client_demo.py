"""Тестовый запуск панели с четырьмя игровыми окнами и разными зеркалами."""

import os
import sys

project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

from run_monitor import NetPulseApp


def build_process_state(selected_pid=None):
    endpoints = [
        (1101, "51.77.68.91", 29000),
        (1102, "51.77.68.91", 29001),
        (1103, "37.59.16.44", 29002),
        # Четвёртый вход использует новый порт, которого нет в старом списке.
        (1104, "89.111.154.169", 29003),
    ]
    instances = []
    for index, (pid, target_ip, target_port) in enumerate(endpoints, 1):
        instances.append({
            "index": index,
            "pid": pid,
            "process_name": "pem.exe",
            "remote_ip": target_ip,
            "remote_port": target_port,
            "local_port": 40000 + index,
            "state": "ESTABLISHED",
            "has_connection": True,
            "is_game_endpoint": target_port != 29003,
            "status_badge": "В ИГРЕ",
            "title": f"Окно #{index}: pem.exe [PID {pid}]",
            "display": f"PID {pid} -> {target_ip}:{target_port} (В ИГРЕ)",
        })
    primary = next((item for item in instances if item["pid"] == selected_pid), instances[0])
    return {
        "detected": True,
        "instances_count": len(instances),
        "instances": instances,
        "primary_instance": primary,
        "process_name": primary["process_name"],
        "pid": primary["pid"],
        "remote_ip": primary["remote_ip"],
        "remote_port": primary["remote_port"],
        "local_port": primary["local_port"],
        "state": primary["state"],
        "status_text": "Тест: обнаружено четыре окна на разных игровых входах",
    }


if __name__ == "__main__":
    app = NetPulseApp("config.json")
    app.process_tracker.scan_all_instances = lambda: build_process_state(app.process_tracker.selected_pid)
    app.start()
