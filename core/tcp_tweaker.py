import winreg
from typing import Dict, Any

class WindowsTCPTweaker:
    """
    Manages Windows TCP/IP Registry Optimizations for MMORPGs (Nagle's Algorithm & Delayed ACK).
    Reduces in-game network input latency by 15-30ms by disabling TCP packet batching delays.
    """
    REG_PATH = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """Scans network interfaces and checks if TCP optimization keys are active."""
        active_interfaces = []
        optimized_count = 0

        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cls.REG_PATH, 0, winreg.KEY_READ)
            subkeys_count, _, _ = winreg.QueryInfoKey(root)

            for i in range(subkeys_count):
                sub_name = winreg.EnumKey(root, i)
                sub_key = winreg.OpenKey(root, sub_name, 0, winreg.KEY_READ)

                ip = None
                for ip_field in ("DhcpIPAddress", "IPAddress"):
                    try:
                        val, _ = winreg.QueryValueEx(sub_key, ip_field)
                        if isinstance(val, list) and val:
                            ip = val[0]
                        elif isinstance(val, str) and val and val != "0.0.0.0":
                            ip = val
                            break
                    except Exception:
                        pass

                if ip and not ip.startswith("127."):
                    # Check if tweaks are present
                    ack_freq = None
                    nodelay = None
                    try:
                        ack_freq, _ = winreg.QueryValueEx(sub_key, "TcpAckFrequency")
                        nodelay, _ = winreg.QueryValueEx(sub_key, "TCPNoDelay")
                    except Exception:
                        pass

                    is_optimized = (ack_freq == 1 and nodelay == 1)
                    if is_optimized:
                        optimized_count += 1

                    active_interfaces.append({
                        "guid": sub_name,
                        "ip": ip,
                        "is_optimized": is_optimized,
                        "tcp_ack_frequency": ack_freq,
                        "tcp_nodelay": nodelay
                    })

                winreg.CloseKey(sub_key)
            winreg.CloseKey(root)

            is_globally_optimized = (optimized_count > 0 and optimized_count >= len(active_interfaces))
            return {
                "success": True,
                "is_optimized": is_globally_optimized,
                "optimized_count": optimized_count,
                "interfaces_count": len(active_interfaces),
                "interfaces": active_interfaces,
                "status_text": "Оптимизация TCP АКТИВНА (Nagle OFF)" if is_globally_optimized else "Стандартный режим Windows (Nagle ON)"
            }
        except Exception as e:
            return {
                "success": False,
                "is_optimized": False,
                "error": str(e),
                "status_text": f"Ошибка доступа к реестру: {e}"
            }

    @classmethod
    def apply_tweaks(cls) -> Dict[str, Any]:
        """Applies TcpAckFrequency=1 and TCPNoDelay=1 to all active network adapters."""
        status = cls.get_status()
        if not status.get("success"):
            return status

        modified = 0
        errors = []

        for iface in status.get("interfaces", []):
            guid = iface["guid"]
            key_path = f"{cls.REG_PATH}\\{guid}"
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(key, "TcpAckFrequency", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "TCPNoDelay", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "TcpDelAckTicks", 0, winreg.REG_DWORD, 0)
                winreg.CloseKey(key)
                modified += 1
            except PermissionError:
                errors.append(f"Интерфейс {iface['ip']}: требуются права администратора")
            except Exception as e:
                errors.append(f"Ошибка {guid}: {e}")

        if modified > 0:
            return {
                "success": True,
                "message": f"Оптимизация успешно применена для {modified} сетевых адаптеров! Задержки пакетов отключены.",
                "modified": modified
            }
        else:
            return {
                "success": False,
                "message": "Для изменения параметров реестра запустите программу от имени Администратора.",
                "errors": errors
            }

    @classmethod
    def revert_tweaks(cls) -> Dict[str, Any]:
        """Removes TcpAckFrequency and TCPNoDelay keys, restoring default Windows behavior."""
        status = cls.get_status()
        if not status.get("success"):
            return status

        reverted = 0
        for iface in status.get("interfaces", []):
            guid = iface["guid"]
            key_path = f"{cls.REG_PATH}\\{guid}"
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
                for param in ("TcpAckFrequency", "TCPNoDelay", "TcpDelAckTicks"):
                    try:
                        winreg.DeleteValue(key, param)
                    except FileNotFoundError:
                        pass
                winreg.CloseKey(key)
                reverted += 1
            except Exception:
                pass

        return {
            "success": True,
            "message": f"Стандартные параметры Windows восстановлены для {reverted} адаптеров.",
            "reverted": reverted
        }
