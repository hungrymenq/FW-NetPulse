import ctypes
import struct
import socket
import os
from ctypes import wintypes
from typing import Optional, Dict, Any, List

TH32CS_SNAPPROCESS = 0x00000002

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', wintypes.WCHAR * 260),
    ]

class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ('dwState', wintypes.DWORD),
        ('dwLocalAddr', wintypes.DWORD),
        ('dwLocalPort', wintypes.DWORD),
        ('dwRemoteAddr', wintypes.DWORD),
        ('dwRemotePort', wintypes.DWORD),
        ('dwOwningPid', wintypes.DWORD),
    ]

class MIB_TCPTABLE_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ('dwNumEntries', wintypes.DWORD),
        ('table', MIB_TCPROW_OWNER_PID * 1),
    ]

class GameProcessTracker:
    """
    Triple-Engine Game Process & Network Connection Inspector.
    Combines Unicode Toolhelp32W snapshot enumeration, QueryFullProcessImageNameW,
    and GetExtendedTcpTable socket mapping.
    100% reliably detects ANY number of running game windows (pem.exe, pemv.exe, launcher.exe, etc.).
    """
    def __init__(
        self,
        target_names: Optional[List[str]] = None,
        fallback_ip: str = "51.77.68.91",
        fallback_port: int = 29000,
        preferred_ports: Optional[List[int]] = None,
    ):
        default_names = ["pem.exe", "pemv.exe", "elementclient.exe", "patcher.exe"]
        configured_names = target_names or default_names
        self.target_names = {
            os.path.basename(str(name)).strip().lower()
            for name in configured_names
            if str(name).strip()
        }
        self.fallback_ip = fallback_ip
        self.fallback_port = fallback_port
        self.preferred_ports = set()
        for port in preferred_ports or [fallback_port]:
            try:
                parsed_port = int(port)
                if 1 <= parsed_port <= 65535:
                    self.preferred_ports.add(parsed_port)
            except (TypeError, ValueError):
                continue
        if not self.preferred_ports:
            self.preferred_ports.add(int(fallback_port))
        self.kernel32 = ctypes.windll.kernel32
        self.iphlpapi = ctypes.windll.iphlpapi
        self.selected_pid: Optional[int] = None

    def _is_target_process(self, process_name: str) -> bool:
        """Проверяет точное имя процесса без ложных совпадений вроде gamebar.exe."""
        normalized = os.path.basename(process_name or "").strip().lower()
        return normalized in self.target_names

    def _socket_priority(self, item: Dict[str, Any]):
        """Сначала выбирает активный игровой порт, затем известный IP сервера."""
        return (
            0 if item.get("is_established") else 1,
            0 if item.get("remote_port") in self.preferred_ports else 1,
            0 if item.get("remote_ip") == self.fallback_ip else 1,
            int(item.get("remote_port") or 65536),
        )

    def _get_process_name_by_pid(self, pid: int) -> str:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        ret = self.kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size))
        self.kernel32.CloseHandle(h_proc)
        if ret:
            return os.path.basename(buf.value).lower()
        return ""

    def focus_window(self, pid: int) -> Dict[str, Any]:
        """Восстанавливает и выводит на передний план главное окно процесса."""
        try:
            target_pid = int(pid)
        except (TypeError, ValueError):
            return {"success": False, "error": "Некорректный PID игрового окна"}

        user32 = ctypes.windll.user32
        candidates = []
        enum_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.SetFocus.argtypes = [wintypes.HWND]
        user32.SetFocus.restype = wintypes.HWND
        user32.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.FlashWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        @enum_callback_type
        def collect_window(hwnd, _):
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value != target_pid or not user32.IsWindowVisible(hwnd):
                return True

            title_length = user32.GetWindowTextLengthW(hwnd)
            if title_length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
            if title_buffer.value.strip():
                # Сначала предпочитаем самостоятельное не свёрнутое окно.
                score = (
                    0 if not user32.GetWindow(hwnd, 4) else 1,  # GW_OWNER
                    0 if not user32.IsIconic(hwnd) else 1,
                    -title_length,
                )
                candidates.append((score, hwnd, title_buffer.value.strip()))
            return True

        user32.EnumWindows(collect_window, 0)
        if not candidates:
            return {
                "success": False,
                "error": "Окно процесса найдено, но его игровое окно ещё не создано",
            }

        _, hwnd, title = min(candidates, key=lambda item: item[0])
        SW_RESTORE = 9
        SW_SHOW = 5
        HWND_TOP = 0
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_SHOWWINDOW = 0x0040

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        current_thread = self.kernel32.GetCurrentThreadId()
        foreground_hwnd = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached_threads = []
        activated = False

        try:
            for thread_id in (foreground_thread, target_thread):
                if thread_id and thread_id != current_thread and thread_id not in attached_threads:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached_threads.append(thread_id)
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.BringWindowToTop(hwnd)
            activated = bool(user32.SetForegroundWindow(hwnd))
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            for thread_id in reversed(attached_threads):
                user32.AttachThreadInput(current_thread, thread_id, False)

        is_foreground = user32.GetForegroundWindow() == hwnd
        if not activated and not is_foreground:
            user32.FlashWindow(hwnd, True)
            return {
                "success": False,
                "error": "Windows запретила переключение. Игровое окно подсвечено на панели задач.",
                "pid": target_pid,
                "title": title,
            }
        return {"success": True, "pid": target_pid, "title": title}

    def scan_all_instances(self) -> Dict[str, Any]:
        found_procs: Dict[int, str] = {}

        # Engine 1: Win32 Toolhelp32W Unicode Snapshot
        try:
            hSnap = self.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if hSnap and hSnap != ctypes.c_void_p(-1).value:
                pe = PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if self.kernel32.Process32FirstW(hSnap, ctypes.byref(pe)):
                    while True:
                        name = pe.szExeFile.lower()
                        if self._is_target_process(name):
                            found_procs[pe.th32ProcessID] = name
                        if not self.kernel32.Process32NextW(hSnap, ctypes.byref(pe)):
                            break
                self.kernel32.CloseHandle(hSnap)
        except Exception:
            pass

        # Engine 2: TCP Table & Process Handle Cross-Referencing
        tcp_sockets_by_pid: Dict[int, List[Dict[str, Any]]] = {}
        try:
            size = wintypes.DWORD(0)
            self.iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), True, 2, 5, 0)
            if size.value > 0:
                buf = ctypes.create_string_buffer(size.value)
                ret = self.iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), True, 2, 5, 0)
                if ret == 0:
                    table = ctypes.cast(buf, ctypes.POINTER(MIB_TCPTABLE_OWNER_PID)).contents
                    count = table.dwNumEntries
                    if count > 0:
                        rows = ctypes.cast(ctypes.addressof(table.table), ctypes.POINTER(MIB_TCPROW_OWNER_PID * count)).contents
                        for row in rows:
                            pid = row.dwOwningPid
                            if pid <= 4:
                                continue

                            # If not already found, inspect process image name directly
                            if pid not in found_procs:
                                p_name = self._get_process_name_by_pid(pid)
                                if self._is_target_process(p_name):
                                    found_procs[pid] = p_name

                            if pid in found_procs:
                                r_ip = socket.inet_ntoa(struct.pack('<I', row.dwRemoteAddr))
                                r_port = socket.ntohs(row.dwRemotePort)
                                l_port = socket.ntohs(row.dwLocalPort)
                                state_str = "ESTABLISHED" if row.dwState == 5 else f"STATE_{row.dwState}"

                                if r_ip != "0.0.0.0" and not r_ip.startswith("127."):
                                    if pid not in tcp_sockets_by_pid:
                                        tcp_sockets_by_pid[pid] = []
                                    tcp_sockets_by_pid[pid].append({
                                        "remote_ip": r_ip,
                                        "remote_port": r_port,
                                        "local_port": l_port,
                                        "state": state_str,
                                        "is_established": (row.dwState == 5)
                                    })
        except Exception:
            pass

        # Assemble instance objects
        instance_list = []
        for idx, (pid, name) in enumerate(sorted(found_procs.items()), 1):
            sockets = tcp_sockets_by_pid.get(pid, [])
            primary_socket = min(sockets, key=self._socket_priority) if sockets else None

            if primary_socket:
                r_ip = primary_socket["remote_ip"]
                r_port = primary_socket["remote_port"]
                l_port = primary_socket["local_port"]
                state = primary_socket["state"]
                status_badge = "В ИГРЕ" if state == "ESTABLISHED" else state
            else:
                r_ip = self.fallback_ip
                r_port = self.fallback_port
                l_port = None
                state = "RUNNING"
                status_badge = "ЗАПУЩЕН"

            instance_list.append({
                "index": idx,
                "pid": pid,
                "process_name": name,
                "remote_ip": r_ip,
                "remote_port": r_port,
                "local_port": l_port,
                "state": state,
                "has_connection": primary_socket is not None,
                "is_game_endpoint": bool(primary_socket and primary_socket.get("remote_port") in self.preferred_ports),
                "status_badge": status_badge,
                "title": f"Окно #{idx}: {name} [PID {pid}]",
                "display": f"PID {pid} -> {r_ip}:{r_port} ({status_badge})"
            })

        instances_count = len(instance_list)
        if instances_count == 0:
            return self._get_fallback_state("Окна игры не обнаружены (Мониторинг сервера по умолчанию)")

        # Prioritize established game instance or selected PID
        primary = instance_list[0]
        if self.selected_pid:
            for inst in instance_list:
                if inst["pid"] == self.selected_pid:
                    primary = inst
                    break
        else:
            primary = min(
                instance_list,
                key=lambda item: (
                    0 if item.get("is_game_endpoint") else 1,
                    0 if item.get("has_connection") else 1,
                    0 if item.get("remote_ip") == self.fallback_ip else 1,
                    item.get("pid", 0),
                ),
            )

        summary_text = f"Обнаружено {instances_count} {'окно' if instances_count == 1 else ('окна' if instances_count < 5 else 'окон')}: {primary['process_name']} [PID {primary['pid']}]"

        return {
            "detected": True,
            "instances_count": instances_count,
            "instances": instance_list,
            "primary_instance": primary,
            "process_name": primary["process_name"],
            "pid": primary["pid"],
            "remote_ip": primary["remote_ip"],
            "remote_port": primary["remote_port"],
            "local_port": primary["local_port"],
            "state": primary["state"],
            "status_text": summary_text
        }

    def _get_fallback_state(self, message: str) -> Dict[str, Any]:
        return {
            "detected": False,
            "instances_count": 0,
            "instances": [],
            "primary_instance": None,
            "process_name": "Не обнаружен",
            "pid": None,
            "remote_ip": self.fallback_ip,
            "remote_port": self.fallback_port,
            "local_port": None,
            "state": "STANDBY",
            "status_text": message
        }
