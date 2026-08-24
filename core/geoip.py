from typing import Dict, Any, Tuple

class GeoIPResolver:
    """
    Lightweight, ultra-fast GeoIP & IXP resolver for route hops.
    Resolves country flag, city, and ISP identity without external network latency.
    """
    _cache: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def resolve(cls, ip: str, hostname: str = "") -> Dict[str, Any]:
        if not ip or ip == "* * *":
            return {"country": "Unknown", "flag": "🌐", "city": "Неизвестно", "asn": "Unknown"}

        if ip in cls._cache:
            return cls._cache[ip]

        host_lower = (hostname or "").lower()
        ip_parts = [int(p) for p in ip.split('.')] if ip.count('.') == 3 and all(p.isdigit() for p in ip.split('.')) else []

        flag = "🌐"
        country = "Транзит"
        city = "Магистраль"
        isp = "Интерконнект"

        # 1. Local Network / Private IP
        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.16.") or ip == "127.0.0.1":
            flag = "🏠"
            country = "Локальная сеть"
            city = "Домашний роутер"
            isp = "LAN"

        # 2. Moldova (.net.md / Moldtelecom / Starnet)
        elif "net.md" in host_lower or "starnet" in host_lower or "moldtelecom" in host_lower or (ip_parts and ip_parts[0] in (83, 77, 178, 212) and "md" in host_lower):
            flag = "🇲🇩"
            country = "Молдова"
            city = "Кишинёв"
            isp = "Интернет-провайдер (MD)"

        # 3. Germany (DE-CIX / Frankfurt / Hetzner)
        elif "decix" in host_lower or "fra" in host_lower or "frankfurt" in host_lower or "de.eu" in host_lower or "hetzner" in host_lower or ip.startswith("80.81.") or ip.startswith("57.128."):
            flag = "🇩🇪"
            country = "Германия"
            city = "Франкфурт (DE-CIX)"
            isp = "DE-CIX / OVH Frankfurt"

        # 4. France (OVH Roubaix / Gravelines / Paris)
        elif "ovh" in host_lower or "gra" in host_lower or "rbx" in host_lower or "par" in host_lower or ip.startswith("51.77.") or ip.startswith("178.33.") or ip.startswith("37.59."):
            flag = "🇫🇷"
            country = "Франция"
            city = "Рубе / Страсбург"
            isp = "OVHcloud Datacenter"

        # 5. Netherlands (Amsterdam / AMS-IX)
        elif "ams" in host_lower or "amsterdam" in host_lower or "nl" in host_lower:
            flag = "🇳🇱"
            country = "Нидерланды"
            city = "Амстердам (AMS-IX)"
            isp = "AMS-IX Transit"

        # 6. Poland (Warsaw / WIX)
        elif "waw" in host_lower or "poland" in host_lower or "pl" in host_lower or ip.startswith("51.89."):
            flag = "🇵🇱"
            country = "Польша"
            city = "Варшава"
            isp = "OVH Poland"

        # 7. Russia (Moscow / SPb / Rostelecom)
        elif "ru" in host_lower or "rt.ru" in host_lower or "ertelecom" in host_lower or "transtelecom" in host_lower:
            flag = "🇷🇺"
            country = "Россия"
            city = "Москва / СПб"
            isp = "Магистральный оператор"

        # 8. United Kingdom
        elif "lon" in host_lower or "london" in host_lower or "uk" in host_lower:
            flag = "🇬🇧"
            country = "Великобритания"
            city = "Лондон (LINX)"
            isp = "LINX Transit"

        res = {
            "country": country,
            "flag": flag,
            "city": city,
            "isp": isp,
            "label": f"{flag} {city}" if flag != "🌐" else city
        }
        cls._cache[ip] = res
        return res
