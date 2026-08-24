#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  FW-NetPulse Native Desktop Floating HUD
  Borderless, Transparent, Always-On-Top In-Game Draggable Widget
================================================================================
"""

import sys
import os
import json
import time
import urllib.request
import threading
import tkinter as tk
from tkinter import ttk

# В однофайловой сборке ресурсы распаковываются во временную папку, но настройки
# должны читаться и сохраняться рядом с EXE.
resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else resource_dir
if resource_dir not in sys.path:
    sys.path.insert(0, resource_dir)

CONFIG_PATH = os.path.join(app_dir, "config.json")

class DesktopHudOverlay:
    def __init__(self, api_url: str = None):
        self.root = tk.Tk()
        self.root.title("FW-NetPulse HUD")

        # Load saved position & settings
        self.config = self._load_config()
        web_cfg = self.config.get("web", {})
        try:
            api_port = int(web_cfg.get("port", 8899))
            if not 1 <= api_port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            api_port = 8899
        self.api_url = api_url or f"http://127.0.0.1:{api_port}/api/status"
        hud_cfg = self.config.get("hud", {})
        self.pos_x = hud_cfg.get("x", 40)
        self.pos_y = hud_cfg.get("y", 40)
        self.opacity = hud_cfg.get("opacity", 0.90)
        self.compact_mode = hud_cfg.get("compact", False)

        # Configure window: borderless, topmost, transparent background
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.opacity)
        self.root.geometry(f"+{self.pos_x}+{self.pos_y}")

        # Palette
        self.BG_DARK = "#0f172a"
        self.BG_CARD = "#1e293b"
        self.BORDER_COLOR = "#38bdf8"
        self.TEXT_CYAN = "#38bdf8"
        self.TEXT_PURPLE = "#c084fc"
        self.TEXT_GREEN = "#10b981"
        self.TEXT_AMBER = "#f59e0b"
        self.TEXT_RED = "#ef4444"
        self.TEXT_MUTED = "#94a3b8"

        self.root.configure(bg=self.BG_DARK)

        # Dragging state
        self._drag_start_x = 0
        self._drag_start_y = 0

        self.running = True
        self.latest_data = {}

        self._build_ui()
        self._bind_events()

        # Start telemetry poller thread
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

        # Start UI updater loop
        self.root.after(200, self._update_ui_loop)

    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_config(self):
        try:
            cfg = self._load_config()
            if "hud" not in cfg:
                cfg["hud"] = {}
            cfg["hud"]["x"] = self.root.winfo_x()
            cfg["hud"]["y"] = self.root.winfo_y()
            cfg["hud"]["opacity"] = self.opacity
            cfg["hud"]["compact"] = self.compact_mode
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _build_ui(self):
        # Outer Frame with glowing border
        self.main_frame = tk.Frame(self.root, bg=self.BG_DARK, highlightbackground=self.BORDER_COLOR, highlightthickness=1)
        self.main_frame.pack(fill="both", expand=True)

        # 1. Header Bar (Draggable)
        self.header = tk.Frame(self.main_frame, bg="#1e293b", height=24)
        self.header.pack(fill="x", padx=1, pady=1)

        self.dot = tk.Label(self.header, text="●", fg=self.TEXT_GREEN, bg="#1e293b", font=("Segoe UI", 9, "bold"))
        self.dot.pack(side="left", padx=(6, 2))

        self.title_lbl = tk.Label(self.header, text="FW-NETPULSE HUD", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8, "bold"))
        self.title_lbl.pack(side="left")

        # Controls in header
        self.btn_close = tk.Label(self.header, text="✕", fg="#94a3b8", bg="#1e293b", font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.btn_close.pack(side="right", padx=6)
        self.btn_close.bind("<Button-1>", lambda e: self.close())

        self.btn_opacity = tk.Label(self.header, text="👁", fg="#94a3b8", bg="#1e293b", font=("Segoe UI", 8), cursor="hand2")
        self.btn_opacity.pack(side="right", padx=4)
        self.btn_opacity.bind("<Button-1>", lambda e: self._cycle_opacity())

        self.btn_compact = tk.Label(self.header, text="🗕", fg="#94a3b8", bg="#1e293b", font=("Segoe UI", 8), cursor="hand2")
        self.btn_compact.pack(side="right", padx=4)
        self.btn_compact.bind("<Button-1>", lambda e: self._toggle_compact())

        # 2. Content Frame (Metrics)
        self.content = tk.Frame(self.main_frame, bg=self.BG_DARK, padx=8, pady=6)
        self.content.pack(fill="both", expand=True)

        # Metric Tiles Row
        self.tiles_frame = tk.Frame(self.content, bg=self.BG_DARK)
        self.tiles_frame.pack(fill="x")

        # Tile 1: ICMP Ping
        self.p_box = tk.Frame(self.tiles_frame, bg=self.BG_CARD, padx=6, pady=4, highlightbackground="#334155", highlightthickness=1)
        self.p_box.pack(side="left", fill="both", expand=True, padx=2)
        self.lbl_ping = tk.Label(self.p_box, text="--", fg=self.TEXT_CYAN, bg=self.BG_CARD, font=("Consolas", 14, "bold"))
        self.lbl_ping.pack()
        self.lbl_ping_sub = tk.Label(self.p_box, text="ICMP ms", fg=self.TEXT_MUTED, bg=self.BG_CARD, font=("Segoe UI", 6, "bold"))
        self.lbl_ping_sub.pack()

        # Tile 2: TCP Game Ping (:29000)
        self.t_box = tk.Frame(self.tiles_frame, bg=self.BG_CARD, padx=6, pady=4, highlightbackground="#334155", highlightthickness=1)
        self.t_box.pack(side="left", fill="both", expand=True, padx=2)
        self.lbl_tcp = tk.Label(self.t_box, text="--", fg=self.TEXT_PURPLE, bg=self.BG_CARD, font=("Consolas", 14, "bold"))
        self.lbl_tcp.pack()
        self.lbl_tcp_sub = tk.Label(self.t_box, text="TCP :29000", fg=self.TEXT_MUTED, bg=self.BG_CARD, font=("Segoe UI", 6, "bold"))
        self.lbl_tcp_sub.pack()

        # Tile 3: Loss
        self.l_box = tk.Frame(self.tiles_frame, bg=self.BG_CARD, padx=6, pady=4, highlightbackground="#334155", highlightthickness=1)
        self.l_box.pack(side="left", fill="both", expand=True, padx=2)
        self.lbl_loss = tk.Label(self.l_box, text="0.0%", fg=self.TEXT_GREEN, bg=self.BG_CARD, font=("Consolas", 14, "bold"))
        self.lbl_loss.pack()
        self.lbl_loss_sub = tk.Label(self.l_box, text="LOSS", fg=self.TEXT_MUTED, bg=self.BG_CARD, font=("Segoe UI", 6, "bold"))
        self.lbl_loss_sub.pack()

        # Tile 4: Jitter
        self.j_box = tk.Frame(self.tiles_frame, bg=self.BG_CARD, padx=6, pady=4, highlightbackground="#334155", highlightthickness=1)
        self.j_box.pack(side="left", fill="both", expand=True, padx=2)
        self.lbl_jitter = tk.Label(self.j_box, text="--", fg=self.TEXT_CYAN, bg=self.BG_CARD, font=("Consolas", 14, "bold"))
        self.lbl_jitter.pack()
        self.lbl_jitter_sub = tk.Label(self.j_box, text="JITTER", fg=self.TEXT_MUTED, bg=self.BG_CARD, font=("Segoe UI", 6, "bold"))
        self.lbl_jitter_sub.pack()

        # 3. Footer Bar (Process info / Window count)
        self.footer_bar = tk.Frame(self.content, bg=self.BG_DARK, pady=4)
        self.footer_bar.pack(fill="x")

        self.lbl_proc_count = tk.Label(self.footer_bar, text="🎮 Окон: 0", fg="#38bdf8", bg="#1e293b", font=("Segoe UI", 7, "bold"), padx=4, pady=1)
        self.lbl_proc_count.pack(side="left")

        self.lbl_proc_status = tk.Label(self.footer_bar, text="Поиск игры...", fg="#94a3b8", bg=self.BG_DARK, font=("Segoe UI", 7))
        self.lbl_proc_status.pack(side="left", padx=(6, 0))

    def _bind_events(self):
        # Enable Drag and Drop on header and title
        for w in (self.header, self.title_lbl, self.dot):
            w.bind("<Button-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_motion)
            w.bind("<ButtonRelease-1>", self._on_drag_release)

    def _on_drag_start(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _on_drag_motion(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_start_x)
        y = self.root.winfo_y() + (event.y - self._drag_start_y)
        self.root.geometry(f"+{x}+{y}")

    def _on_drag_release(self, event):
        self._save_config()

    def _cycle_opacity(self):
        levels = [0.95, 0.80, 0.60, 0.40]
        curr_idx = 0
        for i, l in enumerate(levels):
            if abs(self.opacity - l) < 0.05:
                curr_idx = i
                break
        next_idx = (curr_idx + 1) % len(levels)
        self.opacity = levels[next_idx]
        self.root.attributes("-alpha", self.opacity)
        self._save_config()

    def _toggle_compact(self):
        self.compact_mode = not self.compact_mode
        if self.compact_mode:
            self.footer_bar.pack_forget()
            self.t_box.pack_forget()
            self.j_box.pack_forget()
        else:
            self.t_box.pack(side="left", fill="both", expand=True, padx=2)
            self.l_box.pack(side="left", fill="both", expand=True, padx=2)
            self.j_box.pack(side="left", fill="both", expand=True, padx=2)
            self.footer_bar.pack(fill="x")
        self._save_config()

    def _poll_loop(self):
        while self.running:
            try:
                with urllib.request.urlopen(self.api_url, timeout=1.0) as resp:
                    if resp.status == 200:
                        self.latest_data = json.loads(resp.read().decode("utf-8"))
            except Exception:
                pass
            time.sleep(0.4)

    def _update_ui_loop(self):
        if not self.running:
            return

        data = self.latest_data
        if data:
            ping = data.get("ping", {})
            tcp = data.get("tcp_ping", {})
            proc = data.get("game_process", {})

            # 1. ICMP Ping
            last_rtt = ping.get("last_rtt")
            if last_rtt is not None:
                rtt_int = int(round(last_rtt))
                self.lbl_ping.config(text=str(rtt_int))
                if rtt_int < 60:
                    self.lbl_ping.config(fg=self.TEXT_GREEN)
                    self.dot.config(fg=self.TEXT_GREEN)
                elif rtt_int < 120:
                    self.lbl_ping.config(fg=self.TEXT_AMBER)
                    self.dot.config(fg=self.TEXT_AMBER)
                else:
                    self.lbl_ping.config(fg=self.TEXT_RED)
                    self.dot.config(fg=self.TEXT_RED)
            else:
                self.lbl_ping.config(text="LOST", fg=self.TEXT_RED)
                self.dot.config(fg=self.TEXT_RED)

            # 2. TCP Game Ping
            tcp_rtt = tcp.get("last_rtt")
            if tcp_rtt is not None:
                self.lbl_tcp.config(text=str(int(round(tcp_rtt))))
            else:
                self.lbl_tcp.config(text="--")

            # 3. Loss
            loss = ping.get("rolling_loss_pct", 0.0)
            self.lbl_loss.config(text=f"{loss:.1f}%")
            if loss == 0:
                self.lbl_loss.config(fg=self.TEXT_GREEN)
            elif loss < 3:
                self.lbl_loss.config(fg=self.TEXT_AMBER)
            else:
                self.lbl_loss.config(fg=self.TEXT_RED)

            # 4. Jitter
            jitter = ping.get("jitter", 0.0)
            self.lbl_jitter.config(text=f"{jitter:.1f}")

            # 5. Process tracking
            count = proc.get("instances_count", 0)
            self.lbl_proc_count.config(text=f"🎮 Окон: {count}")
            status_text = proc.get("status_text", "")
            if len(status_text) > 28:
                status_text = status_text[:28] + "..."
            self.lbl_proc_status.config(text=status_text)

        self.root.after(200, self._update_ui_loop)

    def close(self):
        self.running = False
        self._save_config()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = DesktopHudOverlay()
    app.run()
