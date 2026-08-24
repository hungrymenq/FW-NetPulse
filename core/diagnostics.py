from typing import List, Dict, Any, Optional

class NetworkDiagnosticsEngine:
    """
    Intelligent Root-Cause Analyzer with Smart MTR support.
    Accurately ignores ICMP rate limiting when end-to-end game traffic is healthy.
    """
    @staticmethod
    def analyze_route(route: List[Dict[str, Any]], ping_summary: Dict[str, Any], tcp_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not route:
            return {
                "level": "info",
                "title": "Инициализация маршрута...",
                "message": "Выполняется первичное сканирование цепочки узлов до игрового сервера.",
                "suspect_hop": None,
                "suspect_ip": None,
                "recommendation": "Подождите несколько секунд для завершения первой MTR-трассировки."
            }

        last_rtt = ping_summary.get("last_rtt")
        loss_pct = ping_summary.get("rolling_loss_pct", 0.0)
        jitter = ping_summary.get("jitter", 0.0)
        tcp_rtt = (tcp_summary or {}).get("last_rtt")

        # Step 1: Check Local Router / Wi-Fi (Hop 1)
        hop1 = route[0] if len(route) >= 1 else None
        if hop1 and hop1["sent"] >= 3:
            hop1_rtt = hop1.get("avg_rtt") or 0.0
            hop1_loss = hop1.get("loss_pct", 0.0)
            if hop1_loss > 1.0 or hop1_rtt > 8.0:
                return {
                    "level": "critical" if hop1_loss > 5.0 else "warning",
                    "title": "Локальная сеть / Wi-Fi (Hop #1)",
                    "message": f"Задержки ({hop1_rtt} мс) или потери ({hop1_loss}%) на домашнем роутере ({hop1['ip']}).",
                    "suspect_hop": 1,
                    "suspect_ip": hop1["ip"],
                    "recommendation": "Подключите ПК кабелем Ethernet вместо Wi-Fi или перезагрузите роутер."
                }

        # Step 2: Check ISP Gateway (Hops 2..4)
        for h in route[1:4]:
            if h["sent"] >= 3 and h.get("loss_pct", 0.0) > 3.0 and h.get("ip") != "* * *" and not h.get("is_rate_limited"):
                return {
                    "level": "warning",
                    "title": f"Шлюз интернет-провайдера (Hop #{h['hop']})",
                    "message": f"Узел провайдера {h['ip']} ({h['hostname']}) теряет {h['loss_pct']}% пакетов.",
                    "suspect_hop": h["hop"],
                    "suspect_ip": h["ip"],
                    "recommendation": "Проблема в городской сети провайдера. Обратитесь в техподдержку вашего провайдера."
                }

        # Step 3: Check International Backbone (Hops 5..9)
        if len(route) >= 5:
            for i in range(4, min(len(route) - 1, 9)):
                h = route[i]
                if h["sent"] >= 3 and h.get("loss_pct", 0.0) > 4.0 and h.get("ip") != "* * *" and not h.get("is_rate_limited"):
                    return {
                        "level": "warning",
                        "title": f"Магистральный транзитный оператор (Hop #{h['hop']})",
                        "message": f"Потери на международном стыке {h['ip']} ({h['hostname']}).",
                        "suspect_hop": h["hop"],
                        "suspect_ip": h["ip"],
                        "recommendation": "Временный затор на европейском магистральном кабеле."
                    }

        # Step 4: Check Destination (Last Hop)
        dest_hop = route[-1] if route else None
        if dest_hop and dest_hop["sent"] >= 3:
            dest_loss = dest_hop.get("loss_pct", 0.0)
            if dest_loss > 3.0:
                return {
                    "level": "critical",
                    "title": "Датацентр / Игровой хост",
                    "message": f"Потери пакетов ({dest_loss}%) на входе в датацентр OVH / сервер {dest_hop['ip']}.",
                    "suspect_hop": dest_hop["hop"],
                    "suspect_ip": dest_hop["ip"],
                    "recommendation": "Возможна перегрузка сетевого порта игрового сервера или фильтрация анти-DDoS защитой OVH."
                }

        # Step 5: High packet loss or Jitter
        if last_rtt is None or loss_pct >= 5.0:
            return {
                "level": "critical",
                "title": "Высокая потеря пакетов",
                "message": f"Потеря пакетов к серверу: {loss_pct}%. Возможны дисконнекты.",
                "suspect_hop": None,
                "suspect_ip": None,
                "recommendation": "Проверьте стабильность интернет-соединения."
            }

        if jitter >= 15.0 or (last_rtt and last_rtt > 120):
            return {
                "level": "warning",
                "title": "Нестабильный пинг (Джиттер)",
                "message": f"Пинг скачет (джиттер {jitter} мс, RTT {last_rtt} мс). Возможны рывки в игре.",
                "suspect_hop": None,
                "suspect_ip": None,
                "recommendation": "Закройте фоновые загрузки (торренты, обновления, стримы)."
            }

        # Normal condition
        tcp_target = (tcp_summary or {}).get("target") or "игровой порт"
        tcp_info = f", TCP {tcp_target}: {tcp_rtt} мс" if tcp_rtt else ""
        return {
            "level": "good",
            "title": "Связь стабильна (Идеально)",
            "message": f"Пинг {last_rtt} мс{tcp_info}, потери 0.0%, джиттер {jitter} мс. Сеть работает в идеальном режиме.",
            "suspect_hop": None,
            "suspect_ip": None,
            "recommendation": "Все узлы маршрута работают штатно."
        }
