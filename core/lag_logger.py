import sqlite3
import os
import time
import json
import csv
import io
import threading
from typing import List, Dict, Any, Optional

class LagHistoryLogger:
    """
    Persistent SQLite storage for lag events, freeze incidents, and network summaries.
    """
    def __init__(self, db_path: str = "data/network_history.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.lock:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS freeze_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    datetime_str TEXT NOT NULL,
                    target_ip TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    peak_rtt REAL,
                    root_cause TEXT,
                    suspect_hop INTEGER,
                    suspect_ip TEXT
                )
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS hourly_aggregates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_hour TEXT UNIQUE NOT NULL,
                    samples_count INTEGER DEFAULT 0,
                    avg_ping REAL,
                    min_ping REAL,
                    max_ping REAL,
                    avg_jitter REAL,
                    lost_samples INTEGER DEFAULT 0,
                    freeze_events_count INTEGER DEFAULT 0
                )
                """)
                conn.commit()

    def log_freeze_event(self, event: Dict[str, Any]):
        ts = event.get("timestamp", time.time())
        dt_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        target_ip = event.get("target", "51.77.68.91")
        reason = event.get("reason", "Unknown spike")
        peak_rtt = event.get("rtt")
        root_cause = event.get("root_cause", "Анализ маршрута...")
        suspect_hop = event.get("suspect_hop")
        suspect_ip = event.get("suspect_ip")

        with self.lock:
            try:
                with self._get_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                    INSERT INTO freeze_events (timestamp, datetime_str, target_ip, reason, peak_rtt, root_cause, suspect_hop, suspect_ip)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (ts, dt_str, target_ip, reason, peak_rtt, root_cause, suspect_hop, suspect_ip))
                    conn.commit()
            except Exception as e:
                print(f"[LagLogger Error]: {e}")

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            try:
                with self._get_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                    SELECT id, timestamp, datetime_str, target_ip, reason, peak_rtt, root_cause, suspect_hop, suspect_ip
                    FROM freeze_events
                    ORDER BY id DESC
                    LIMIT ?
                    """, (limit,))
                    rows = cursor.fetchall()
                    return [dict(r) for r in rows]
            except Exception as e:
                print(f"[LagLogger Error]: {e}")
                return []

    def get_stats_summary(self) -> Dict[str, Any]:
        with self.lock:
            try:
                with self._get_conn() as conn:
                    cursor = conn.cursor()

                    # Total events count
                    cursor.execute("SELECT COUNT(*) as cnt FROM freeze_events")
                    total_events = cursor.fetchone()["cnt"]

                    # Events in last 24h
                    one_day_ago = time.time() - 86400
                    cursor.execute("SELECT COUNT(*) as cnt FROM freeze_events WHERE timestamp >= ?", (one_day_ago,))
                    events_24h = cursor.fetchone()["cnt"]

                    return {
                        "total_recorded_events": total_events,
                        "events_last_24h": events_24h
                    }
            except Exception:
                return {"total_recorded_events": 0, "events_last_24h": 0}

    def export_csv(self) -> str:
        events = self.get_recent_events(limit=500)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["id", "datetime_str", "target_ip", "reason", "peak_rtt", "root_cause", "suspect_hop", "suspect_ip", "timestamp"])
        writer.writeheader()
        for ev in events:
            writer.writerow(ev)
        return output.getvalue()

    def export_json(self) -> str:
        events = self.get_recent_events(limit=500)
        return json.dumps(events, indent=2, ensure_ascii=False)
