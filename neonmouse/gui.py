"""Интерфейс NeonMouse (tkinter, без внешних зависимостей)."""
from __future__ import annotations

import colorsys
import copy
import json
import logging
import math
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, ttk

from . import __version__
from . import devices as D
from .effects import EFFECTS, ICONS, USES_COLOR, Engine, default_state, hsv, sanitize_state

CONFIG_PATH = Path.home() / ".neonmouse.json"
BG, PANEL, CARD, CARD_HI = "#0b0d12", "#12151c", "#191d27", "#232836"
ACCENT, ACCENT2, FG, MUTED, OKC, BAD = "#8b5cf6", "#22d3ee", "#eef0f6", "#7d8497", "#34d399", "#f87171"
FONT = "Segoe UI"
PRESETS = [(255, 0, 0), (255, 90, 0), (255, 210, 0), (140, 255, 0), (0, 255, 80), (0, 230, 210),
           (0, 120, 255), (40, 0, 255), (150, 0, 255), (255, 0, 160), (255, 255, 255), (255, 170, 90)]

log = logging.getLogger("neonmouse")


class RingLog(logging.Handler):
    def __init__(self, size=400):
        super().__init__()
        self.lines, self.size = [], size

    def emit(self, record):
        self.lines.append(self.format(record))
        del self.lines[:-self.size]


def hx(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c[:3])


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(c, bg, k):
    return tuple(int(bg[i] + (c[i] - bg[i]) * k) for i in range(3))


# ======================================================================
# Виджеты
# ======================================================================
class FlatButton(tk.Label):
    def __init__(self, master, text, command=None, bg=CARD, hover=CARD_HI, fg=FG,
                 font=(FONT, 10), padx=12, pady=7, **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, font=font, padx=padx, pady=pady,
                         cursor="hand2", **kw)
        self._bg, self._hover, self.command = bg, hover, command
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
        self.bind("<Button-1>", lambda e: self.command and self.command())

    def recolor(self, bg, hover=None, fg=None):
        self._bg, self._hover = bg, hover or bg
        self.configure(bg=bg, fg=fg or self.cget("fg"))


class Toggle(tk.Canvas):
    def __init__(self, master, value=True, command=None, bg=BG):
        super().__init__(master, width=62, height=32, bg=bg, highlightthickness=0, cursor="hand2")
        self.value, self.command = value, command
        self.bind("<Button-1>", self._click)
        self.draw()

    def draw(self):
        self.delete("all")
        col = ACCENT if self.value else "#3a4050"
        self.create_oval(2, 2, 30, 30, fill=col, outline=col)
        self.create_oval(32, 2, 60, 30, fill=col, outline=col)
        self.create_rectangle(16, 2, 46, 30, fill=col, outline=col)
        x = 46 if self.value else 16
        self.create_oval(x - 12, 4, x + 12, 28, fill="white", outline="white")

    def set(self, v):
        if bool(v) != self.value:
            self.value = bool(v)
            self.draw()

    def _click(self, _):
        self.value = not self.value
        self.draw()
        if self.command:
            self.command(self.value)


class Slider(tk.Canvas):
    PAD = 12

    def __init__(self, master, lo, hi, value, command, width=300, bg=PANEL):
        super().__init__(master, width=width, height=26, bg=bg, highlightthickness=0, cursor="hand2")
        self.lo, self.hi, self.value, self.command, self.w = lo, hi, value, command, width
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.draw()

    def draw(self):
        self.delete("all")
        p, y = self.PAD, 13
        x = p + (self.value - self.lo) / (self.hi - self.lo) * (self.w - 2 * p)
        self.create_line(p, y, self.w - p, y, fill="#2a2f3d", width=6, capstyle="round")
        self.create_line(p, y, max(p + 1, x), y, fill=ACCENT, width=6, capstyle="round")
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill="white", outline=ACCENT, width=2)

    def _drag(self, e):
        p = self.PAD
        v = self.lo + (e.x - p) / (self.w - 2 * p) * (self.hi - self.lo)
        v = int(round(max(self.lo, min(self.hi, v))))
        if v != self.value:
            self.value = v
            self.draw()
            self.command(v)

    def set(self, v):
        if v != self.value:
            self.value = v
            self.draw()


class ColorWheel(tk.Canvas):
    def __init__(self, master, size=180, command=None, on_center=None, bg=PANEL):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, cursor="hand2")
        self.c = c = size / 2
        self.r = r = c - 14
        self.command, self.on_center = command, on_center
        for a in range(0, 360, 3):
            self.create_arc(c - r, c - r, c + r, c + r, start=a, extent=3.8, style="arc",
                            width=22, outline=hx(hsv(a / 360)))
        self.center = self.create_oval(c - 44, c - 44, c + 44, c + 44, fill="#000", outline="#2a2f3d", width=2)
        self.label = self.create_text(c, c, text="", fill="white", font=(FONT, 9, "bold"))
        self.marker = self.create_oval(-20, -20, -10, -10, outline="white", width=3)
        self.bind("<Button-1>", self._click)
        self.bind("<B1-Motion>", self._drag)

    def _hit(self, e):
        return math.hypot(e.x - self.c, e.y - self.c)

    def _click(self, e):
        if self._hit(e) < 46:
            if self.on_center:
                self.on_center()
        else:
            self._drag(e)

    def _drag(self, e):
        if self._hit(e) < 46:
            return
        ang = math.degrees(math.atan2(-(e.y - self.c), e.x - self.c)) % 360
        if self.command:
            self.command(hsv(ang / 360))

    def show(self, color):
        self.itemconfigure(self.center, fill=hx(color))
        lum = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
        self.itemconfigure(self.label, text=hx(color).upper() + "\nнажмите", fill="#111" if lum > 150 else "white")
        h, s, _ = colorsys.rgb_to_hsv(*(v / 255 for v in color))
        if s < 0.2:
            self.coords(self.marker, -20, -20, -10, -10)
        else:
            a = math.radians(h * 360)
            x, y = self.c + self.r * math.cos(a), self.c - self.r * math.sin(a)
            self.coords(self.marker, x - 9, y - 9, x + 9, y + 9)


class MousePreview(tk.Canvas):
    W, H = 300, 420
    BODY = [(150, 58), (203, 72), (222, 132), (226, 222), (213, 292), (182, 328), (150, 336),
            (118, 328), (87, 292), (74, 222), (78, 132), (97, 72)]

    def __init__(self, master):
        super().__init__(master, width=self.W, height=self.H, bg=PANEL, highlightthickness=0)
        self._sig = None

    @staticmethod
    def regions(zones):
        m = {"logo": None, "wheel": None, "strip": None}
        for i, z in enumerate(zones):
            zl = z.lower()
            if "колёс" in zl or "колес" in zl:
                m["wheel"] = m["wheel"] if m["wheel"] is not None else i
            elif "логотип" in zl:
                m["logo"] = m["logo"] if m["logo"] is not None else i
            elif m["strip"] is None:
                m["strip"] = i
        free = [i for i in range(len(zones)) if i not in m.values()] or [0]
        for k in ("logo", "strip", "wheel"):
            if m[k] is None:
                m[k] = free.pop(0) if len(free) > 1 else free[0]
        return m

    def render(self, title, zones, colors, online):
        sig = (title, tuple(zones), tuple(colors), online)
        if sig == self._sig:
            return
        self._sig = sig
        self.delete("all")
        bg = rgb(PANEL)
        cols = [c if online else (40, 44, 55) for c in colors] or [(0, 0, 0)]
        m = self.regions(zones)
        get = lambda k: cols[min(m[k], len(cols) - 1)]
        glow = get("strip")
        for i in range(10, 0, -1):
            k = (1 - i / 10) ** 2 * 0.5
            e = i * 9
            self.create_oval(74 - e, 58 - e * 0.8, 226 + e, 336 + e * 0.8,
                             fill=hx(mix(glow, bg, k)), outline="")
        pts = [v for p in self.BODY for v in p]
        self.create_polygon(pts, smooth=True, fill="#1b1f29", outline="#323849", width=2)
        self.create_line(150, 60, 150, 150, fill="#323849", width=2)
        self.create_line(86, 150, 214, 150, fill="#272c38", width=1, smooth=True)
        s = hx(get("strip"))
        self.create_line(84, 150, 79, 222, 92, 288, fill=s, width=5, smooth=True, capstyle="round")
        self.create_line(216, 150, 221, 222, 208, 288, fill=s, width=5, smooth=True, capstyle="round")
        self.create_rectangle(141, 84, 159, 128, fill=hx(get("wheel")), outline="#0b0d12", width=2)
        lc = get("logo")
        self.create_oval(126, 214, 174, 262, fill=hx(mix(lc, bg, 0.35)), outline="")
        self.create_oval(134, 222, 166, 254, fill=hx(lc), outline="")
        self.create_text(150, 372, text=title, fill=FG, font=(FONT, 11, "bold"), width=280)
        legend = "   ".join(f"■ {z}" for z in zones[:5])
        self.create_text(150, 398, text=legend, fill=MUTED, font=(FONT, 9), width=290)


# ======================================================================
# Приложение
# ======================================================================
class App(tk.Tk):
    def __init__(self, ring_log=None):
        super().__init__()
        self.ring_log = ring_log
        self.title(f"NeonMouse {__version__}")
        self.geometry("1160x720")
        self.minsize(1080, 680)
        self.configure(bg=BG)
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

        self.cfg = self.load_config()
        self.devices = []
        self.selected = self.cfg.get("selected", "all")
        self.edit_target = "color"
        self.events = queue.Queue()
        self.scanning = False
        self.report = []
        self.unsupported = []
        self._save_job = None
        self._hp_sig = None

        self.build_ui()
        self.engine = Engine(lambda: self.devices, self.state_for, self._engine_error)
        self.engine.start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_hotkeys()
        self.scan()
        self.after(33, self.tick)
        self.after(3000, self.hotplug_check)

    # ---------------- конфиг ----------------
    def load_config(self):
        cfg = {"states": {}, "profiles": {}, "selected": "all"}
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in ("states", "profiles"):
                    if isinstance(data.get(key), dict):
                        cfg[key] = {str(k): sanitize_state(v) for k, v in data[key].items()}
                if isinstance(data.get("selected"), str):
                    cfg["selected"] = data["selected"]
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Конфиг повреждён, используются настройки по умолчанию: %s", e)
        return cfg

    def save_config(self):
        self._save_job = None
        try:
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            log.warning("Не удалось сохранить настройки: %s", e)

    def schedule_save(self):
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(500, self.save_config)

    def state_for(self, uid):
        return self.cfg["states"].get(uid) or default_state()

    def targets(self):
        if self.selected == "all":
            return list(self.devices)
        return [d for d in self.devices if d.uid == self.selected] or list(self.devices)

    def cur_state(self):
        t = self.targets()
        return self.state_for(t[0].uid) if t else default_state()

    def update_state(self, **kw):
        for d in self.targets():
            st = self.cfg["states"].setdefault(d.uid, default_state())
            for k, v in kw.items():
                st[k] = copy.deepcopy(v)
        self.schedule_save()
        self.refresh_controls()

    # ---------------- построение UI ----------------
    def build_ui(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 10))
        logo = tk.Canvas(head, width=34, height=34, bg=BG, highlightthickness=0)
        logo.pack(side="left")
        for i in range(6, 0, -1):
            k = i / 6
            logo.create_oval(17 - 15 * k, 17 - 15 * k, 17 + 15 * k, 17 + 15 * k,
                             fill=hx(mix(rgb(ACCENT2), rgb(ACCENT), k)), outline="")
        tk.Label(head, text="NEON", bg=BG, fg=FG, font=(FONT, 17, "bold")).pack(side="left", padx=(10, 0))
        tk.Label(head, text="MOUSE", bg=BG, fg=ACCENT, font=(FONT, 17, "bold")).pack(side="left")
        self.status = tk.Label(head, text="", bg=BG, fg=MUTED, font=(FONT, 10))
        self.status.pack(side="left", padx=20)
        self.power = Toggle(head, True, self.on_power)
        self.power.pack(side="right")
        self.power_lbl = tk.Label(head, text="ПОДСВЕТКА", bg=BG, fg=FG, font=(FONT, 10, "bold"))
        self.power_lbl.pack(side="right", padx=10)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 16))

        side = tk.Frame(body, bg=PANEL, width=260)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.section(side, "УСТРОЙСТВА").pack(anchor="w", padx=14, pady=(14, 6))
        self.dev_list = tk.Frame(side, bg=PANEL)
        self.dev_list.pack(fill="x")
        bottom = tk.Frame(side, bg=PANEL)
        bottom.pack(side="bottom", fill="x", padx=12, pady=12)
        self.scan_btn = FlatButton(bottom, "⟳  Найти мыши", self.scan, bg=ACCENT, hover="#9d75ff",
                                   fg="white", font=(FONT, 10, "bold"))
        self.scan_btn.pack(fill="x", pady=3)
        FlatButton(bottom, "Диагностика", self.show_diagnostics).pack(fill="x", pady=3)
        FlatButton(bottom, "Поддерживаемые модели", self.show_models).pack(fill="x", pady=3)

        mid = tk.Frame(body, bg=PANEL, width=320)
        mid.pack(side="left", fill="y", padx=12)
        mid.pack_propagate(False)
        self.section(mid, "ПРЕДПРОСМОТР").pack(anchor="w", padx=14, pady=(14, 0))
        self.preview = MousePreview(mid)
        self.preview.pack(pady=6)
        self.hint = tk.Label(mid, text="Ctrl+Shift+L — вкл/выкл\nCtrl+Shift+E — следующий эффект\n"
                                       "Ctrl+Shift+↑/↓ — яркость", bg=PANEL, fg=MUTED,
                             font=(FONT, 9), justify="center")
        self.hint.pack(side="bottom", pady=12)

        right = tk.Frame(body, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(right, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=18, pady=10)

        self.section(inner, "ЭФФЕКТ").pack(anchor="w", pady=(4, 6))
        grid = tk.Frame(inner, bg=PANEL)
        grid.pack(fill="x")
        self.eff_btns = {}
        for i, e in enumerate(EFFECTS):
            b = FlatButton(grid, f"{ICONS[e]}  {e}", lambda e=e: self.on_effect(e), pady=8)
            b.grid(row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
            self.eff_btns[e] = b
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        self.section(inner, "ЦВЕТ").pack(anchor="w", pady=(14, 6))
        crow = tk.Frame(inner, bg=PANEL)
        crow.pack(fill="x")
        self.wheel = ColorWheel(crow, 180, self.set_color, self.pick_exact)
        self.wheel.pack(side="left")
        cright = tk.Frame(crow, bg=PANEL)
        cright.pack(side="left", fill="both", expand=True, padx=(16, 0))
        pg = tk.Frame(cright, bg=PANEL)
        pg.pack(anchor="w")
        for i, c in enumerate(PRESETS):
            sw = tk.Label(pg, bg=hx(c), width=3, height=1, cursor="hand2")
            sw.grid(row=i // 6, column=i % 6, padx=3, pady=3)
            sw.bind("<Button-1>", lambda e, c=c: self.set_color(c))
        tk.Label(cright, text="Что редактируем:", bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(10, 2))
        self.chips = tk.Frame(cright, bg=PANEL)
        self.chips.pack(anchor="w", fill="x")
        zr = tk.Frame(cright, bg=PANEL)
        zr.pack(anchor="w", pady=(10, 0))
        self.zone_toggle = Toggle(zr, False, self.on_per_zone, bg=PANEL)
        self.zone_toggle.pack(side="left")
        tk.Label(zr, text="  Свой цвет для каждой зоны", bg=PANEL, fg=FG, font=(FONT, 10)).pack(side="left")

        self.section(inner, "НАСТРОЙКИ").pack(anchor="w", pady=(14, 4))
        self.bri_lbl, self.bri = self.slider_row(inner, "Яркость", 0, 100, 100,
                                                 lambda v: self.update_state(brightness=v))
        self.spd_lbl, self.spd = self.slider_row(inner, "Скорость", 1, 100, 50,
                                                 lambda v: self.update_state(speed=v))

        self.section(inner, "ПРОФИЛИ").pack(anchor="w", pady=(14, 6))
        pr = tk.Frame(inner, bg=PANEL)
        pr.pack(fill="x")
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Dark.TCombobox", fieldbackground=CARD, background=CARD, foreground=FG,
                     arrowcolor=FG, bordercolor=CARD_HI, lightcolor=CARD, darkcolor=CARD)
        st.map("Dark.TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)])
        self.prof_var = tk.StringVar()
        self.prof_box = ttk.Combobox(pr, textvariable=self.prof_var, width=14, style="Dark.TCombobox",
                                     font=(FONT, 10))
        self.prof_box.pack(side="left", ipady=4)
        FlatButton(pr, "Сохранить", self.save_profile).pack(side="left", padx=4)
        FlatButton(pr, "Применить", self.load_profile).pack(side="left", padx=4)
        FlatButton(pr, "Удалить", self.delete_profile).pack(side="left", padx=4)
        FlatButton(pr, "В память мыши", self.save_to_mouse, bg="#1f3b35", hover="#285046",
                   fg=OKC).pack(side="right")
        self.refresh_profiles()

    def section(self, parent, text):
        return tk.Label(parent, text=text, bg=PANEL, fg=MUTED, font=(FONT, 9, "bold"))

    def slider_row(self, parent, title, lo, hi, val, cmd):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=title, bg=PANEL, fg=FG, font=(FONT, 10), width=10, anchor="w").pack(side="left")
        lbl = tk.Label(row, text=str(val), bg=PANEL, fg=FG, font=(FONT, 10, "bold"), width=4)

        def changed(v):
            lbl.configure(text=str(v))
            cmd(v)

        s = Slider(row, lo, hi, val, changed, width=380)
        s.pack(side="left")
        lbl.pack(side="left", padx=6)
        return lbl, s

    # ---------------- устройства ----------------
    def rebuild_devices(self):
        for w in self.dev_list.winfo_children():
            w.destroy()
        items = []
        if len(self.devices) > 1:
            items.append(("all", "Все устройства", f"{len(self.devices)} шт. · общие настройки", True))
        for d in self.devices:
            sub = f"{d.brand} · зон: {len(d.zones)}" + ("" if d.online else " · отключена")
            items.append((d.uid, d.name, sub, d.online))
        for uid in [u for u in self.unsupported][:4]:
            items.append((None, uid, "пока не поддерживается", False))
        for uid, title, sub, online in items:
            sel = uid is not None and (uid == self.selected or (self.selected == "all" and len(self.devices) == 1))
            bg = CARD_HI if sel else CARD
            card = tk.Frame(self.dev_list, bg=bg, cursor="hand2" if uid else "arrow")
            card.pack(fill="x", padx=12, pady=4)
            tk.Frame(card, bg=ACCENT if sel else bg, width=3).pack(side="left", fill="y")
            box = tk.Frame(card, bg=bg)
            box.pack(side="left", fill="both", expand=True, padx=10, pady=8)
            top = tk.Frame(box, bg=bg)
            top.pack(fill="x")
            tk.Label(top, text="●", bg=bg, fg=OKC if online else (BAD if uid else MUTED),
                     font=(FONT, 9)).pack(side="left")
            tk.Label(top, text=" " + title, bg=bg, fg=FG if uid else MUTED, font=(FONT, 10, "bold"),
                     anchor="w", wraplength=190, justify="left").pack(side="left")
            tk.Label(box, text=sub, bg=bg, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x")
            if uid:
                for w in (card, box, top, *box.winfo_children(), *top.winfo_children()):
                    w.bind("<Button-1>", lambda e, u=uid: self.select(u))

    def select(self, uid):
        self.selected = uid
        self.cfg["selected"] = uid
        self.edit_target = "color"
        self.schedule_save()
        self.rebuild_devices()
        self.refresh_controls()

    def scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.status.configure(text="Поиск мышей…", fg=MUTED)
        old, self.devices = self.devices, []

        def work():
            for d in old:
                try:
                    d.close()
                except Exception:
                    pass
            try:
                res = D.scan()
            except Exception as e:  # pragma: no cover
                res = D.ScanResult()
                res.report.append(f"Ошибка сканирования: {e}")
            self.events.put(("scan", res))

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, res):
        self.scanning = False
        self.report = res.report
        self.unsupported = res.unsupported
        mice = res.mice
        if mice:
            self.status.configure(text=f"✓ Подключено мышей: {len(mice)}", fg=OKC)
        else:
            mice = [D.DemoMouse()]
            msg = "Мыши не найдены — демо-режим"
            if not D.hid_available():
                msg = "Не установлен hidapi (pip install hidapi) — демо-режим"
            self.status.configure(text=msg, fg="#fbbf24")
        for d in mice:
            if d.uid not in self.cfg["states"]:
                self.cfg["states"][d.uid] = default_state()
        self.devices = mice
        if self.selected != "all" and self.selected not in {d.uid for d in mice}:
            self.selected = "all"
        self.rebuild_devices()
        self.refresh_controls()

    def hotplug_check(self):
        def work():
            sig = frozenset((e.get("vendor_id"), e.get("product_id"), e.get("serial_number") or "")
                            for e in D.hid_enumerate()
                            if e.get("vendor_id") in (D.RAZER_VID, D.LOGI_VID, D.A4TECH_VID))
            self.events.put(("hotplug", sig))
        if not self.scanning:
            threading.Thread(target=work, daemon=True).start()
        self.after(3000, self.hotplug_check)

    def _engine_error(self, dev, err):
        self.events.put(("error", dev.name, str(err)))

    # ---------------- обработчики ----------------
    def on_power(self, v):
        self.update_state(enabled=bool(v))

    def on_effect(self, e):
        self.update_state(effect=e)

    def on_per_zone(self, v):
        if v:
            for d in self.targets():
                st = self.cfg["states"].setdefault(d.uid, default_state())
                while len(st["zone_colors"]) < len(d.zones):
                    st["zone_colors"].append(list(st["color"]))
        else:
            self.edit_target = "color"
        self.update_state(per_zone=bool(v))

    def set_color(self, c):
        c = [int(v) for v in c[:3]]
        st = self.cur_state()
        tgt = self.edit_target
        if isinstance(tgt, int):
            for d in self.targets():
                s = self.cfg["states"].setdefault(d.uid, default_state())
                while len(s["zone_colors"]) < len(d.zones):
                    s["zone_colors"].append(list(s["color"]))
                if tgt < len(s["zone_colors"]):
                    s["zone_colors"][tgt] = list(c)
            kw = {}
        else:
            kw = {tgt: c}
        if st["effect"] not in USES_COLOR:
            kw["effect"] = "Статичный"  # выбор цвета = хотим его увидеть
        if tgt == "color2" and st["effect"] != "Переход":
            kw["effect"] = "Переход"
        self.update_state(**kw)

    def pick_exact(self):
        res = colorchooser.askcolor(color=hx(self.target_color()), title="Точный цвет", parent=self)
        if res and res[0]:
            self.set_color(res[0])

    def target_color(self):
        st = self.cur_state()
        t = self.edit_target
        if isinstance(t, int):
            zc = st["zone_colors"]
            return tuple(zc[t]) if t < len(zc) else tuple(st["color"])
        return tuple(st[t])

    def set_target(self, t):
        self.edit_target = t
        self.refresh_controls()

    def save_to_mouse(self):
        real = [d for d in self.targets() if d.can_save]
        if not real:
            t = self.targets()
            msg = ("Демо-мышь: сохранять некуда" if t and t[0].demo else
                   "Bloody не поддерживает запись: после переподключения вернётся её собственная подсветка")
            self.status.configure(text=msg, fg="#fbbf24")
            return
        self.engine.request_save([d.uid for d in real])
        self.status.configure(text="✓ Текущий цвет записан в память мыши", fg=OKC)

    # ---------------- профили ----------------
    def refresh_profiles(self):
        self.prof_box["values"] = sorted(self.cfg["profiles"].keys())

    def save_profile(self):
        name = self.prof_var.get().strip()
        if not name:
            self.status.configure(text="Введите имя профиля", fg="#fbbf24")
            return
        self.cfg["profiles"][name] = copy.deepcopy(self.cur_state())
        self.schedule_save()
        self.refresh_profiles()
        self.status.configure(text=f"✓ Профиль «{name}» сохранён", fg=OKC)

    def load_profile(self):
        name = self.prof_var.get().strip()
        p = self.cfg["profiles"].get(name)
        if p:
            self.update_state(**sanitize_state(p))
            self.status.configure(text=f"✓ Профиль «{name}» применён", fg=OKC)

    def delete_profile(self):
        name = self.prof_var.get().strip()
        if self.cfg["profiles"].pop(name, None) is not None:
            self.prof_var.set("")
            self.schedule_save()
            self.refresh_profiles()
            self.status.configure(text=f"Профиль «{name}» удалён", fg=MUTED)

    # ---------------- обновление UI ----------------
    def refresh_controls(self):
        st = self.cur_state()
        self.power.set(st["enabled"])
        self.power_lbl.configure(text="ПОДСВЕТКА ВКЛ" if st["enabled"] else "ПОДСВЕТКА ВЫКЛ",
                                 fg=FG if st["enabled"] else MUTED)
        for e, b in self.eff_btns.items():
            if e == st["effect"]:
                b.recolor(ACCENT, "#9d75ff", "white")
            else:
                b.recolor(CARD, CARD_HI, FG)
        self.bri.set(st["brightness"])
        self.bri_lbl.configure(text=str(st["brightness"]))
        self.spd.set(st["speed"])
        self.spd_lbl.configure(text=str(st["speed"]))
        self.zone_toggle.set(st["per_zone"])
        t = self.targets()
        zones = t[0].zones if t else []
        if isinstance(self.edit_target, int) and (not st["per_zone"] or self.edit_target >= len(zones)):
            self.edit_target = "color"
        self.wheel.show(self.target_color())
        for w in self.chips.winfo_children():
            w.destroy()
        chips = [("color", "Основной", tuple(st["color"])), ("color2", "Второй", tuple(st["color2"]))]
        if st["per_zone"]:
            chips = [(i, z, self._zone_col(st, i)) for i, z in enumerate(zones)] + chips[1:]
        for i, (key, text, col) in enumerate(chips):
            sel = key == self.edit_target
            b = FlatButton(self.chips, f"■ {text}", lambda k=key: self.set_target(k), padx=8, pady=4,
                           font=(FONT, 9), bg=CARD_HI if sel else CARD, fg=hx(col) if sum(col) > 90 else FG)
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="w")

    @staticmethod
    def _zone_col(st, i):
        zc = st["zone_colors"]
        return tuple(zc[i]) if i < len(zc) else tuple(st["color"])

    def tick(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "scan":
                    self._scan_done(ev[1])
                elif ev[0] == "error":
                    self.status.configure(text=f"⚠ {ev[1]} отключилась — переподключаюсь…", fg=BAD)
                    log.warning("%s: %s", ev[1], ev[2])
                    self.rebuild_devices()
                    self.after(1500, self.scan)
                elif ev[0] == "hotplug":
                    if self._hp_sig is not None and ev[1] != self._hp_sig and not self.scanning:
                        self.scan()
                    self._hp_sig = ev[1]
                elif ev[0] == "hotkey":
                    self.handle_hotkey(ev[1])
        except queue.Empty:
            pass
        t = self.targets()
        if t:
            d = t[0]
            title = d.name if (self.selected != "all" or len(self.devices) == 1) else "Все устройства"
            self.preview.render(title, d.zones, d.colors, d.online)
        else:
            self.preview.render("Поиск…", ["Вся мышь"], [(0, 0, 0)], False)
        self.after(33, self.tick)

    # ---------------- окна ----------------
    def _text_window(self, title, text):
        w = tk.Toplevel(self)
        w.title(title)
        w.configure(bg=BG)
        w.geometry("640x460")
        txt = tk.Text(w, bg=PANEL, fg=FG, insertbackground=FG, font=("Consolas", 10), bd=0,
                      padx=12, pady=10, wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        txt.insert("1.0", text)
        txt.configure(state="disabled")

        def copy_all():
            self.clipboard_clear()
            self.clipboard_append(text)
        FlatButton(w, "Скопировать", copy_all).pack(anchor="e", padx=10, pady=(0, 10))

    def show_diagnostics(self):
        lines = [f"NeonMouse {__version__}", "",
                 "hidapi: " + ("установлен" if D.hid_available() else "НЕ установлен (pip install hidapi)"),
                 ""] + (self.report or ["Сканирование ещё не выполнялось"])
        if self.ring_log and self.ring_log.lines:
            lines += ["", "Журнал:"] + self.ring_log.lines[-60:]
        lines += ["", "Если мышь не видна:",
                  "  1. Закройте Razer Synapse / Logitech G Hub / Bloody7 — они держат устройство.",
                  "  2. Linux: установите udev-правило из папки linux/ (см. README).",
                  "  3. Нажмите «Найти мыши»."]
        self._text_window("Диагностика", "\n".join(lines))

    def show_models(self):
        lines = ["RAZER — протокол Chroma (зоны и протокол определяются автоматически):", ""]
        lines += [f"  • {n}" for n in sorted(set(D.RAZER_MODELS.values()))]
        lines += ["  • и другие мыши Razer с Chroma: пробуются автоматически", "",
                  "LOGITECH — HID++ 2.0 (зоны мышь сообщает сама):", "",
                  "  Работает любая мышь Logitech с функцией 0x8070. Обычно это",
                  "  G303 Shroud, G403 Hero, G502 Hero / Lightspeed, G703, G903, G Pro Wireless",
                  "  и другие модели Lightsync прошлых поколений.",
                  "  Проводные и через приёмник Lightspeed.", "",
                  "BLOODY (A4Tech) — прямой режим подсветки:", "",
                  "  • V8M MAX",
                  "  • W60 Pro, W70 Max, W90 Max, W90 Pro, W95 Max",
                  "  • другие мыши Bloody с RGB определяются автоматически",
                  "  Цвет держится до переподключения мыши (в память не пишется).", "",
                  "ПОКА НЕ ПОДДЕРЖИВАЮТСЯ (найдутся, но управлять нельзя):",
                  "  SteelSeries, Corsair, HyperX, ASUS ROG, Roccat, Glorious, Cooler Master,",
                  "  MSI, Redragon; новые Logitech с протоколом 0x8071 (например, G502 X Plus)."]
        self._text_window("Поддерживаемые модели", "\n".join(lines))

    # ---------------- горячие клавиши ----------------
    def bind_hotkeys(self):
        for seq, name in (("<Control-Shift-L>", "power"), ("<Control-Shift-l>", "power"),
                          ("<Control-Shift-E>", "effect"), ("<Control-Shift-e>", "effect"),
                          ("<Control-Shift-Up>", "up"), ("<Control-Shift-Down>", "down")):
            self.bind_all(seq, lambda e, n=name: self.handle_hotkey(n))
        self._kb = None
        try:  # глобальные горячие клавиши (если установлен пакет keyboard)
            import keyboard
            for combo, name in (("ctrl+shift+l", "power"), ("ctrl+shift+e", "effect"),
                                ("ctrl+shift+up", "up"), ("ctrl+shift+down", "down")):
                keyboard.add_hotkey(combo, lambda n=name: self.events.put(("hotkey", n)))
            self._kb = keyboard
        except Exception:
            pass

    def handle_hotkey(self, name):
        st = self.cur_state()
        if name == "power":
            self.update_state(enabled=not st["enabled"])
        elif name == "effect":
            self.update_state(effect=EFFECTS[(EFFECTS.index(st["effect"]) + 1) % len(EFFECTS)])
        elif name == "up":
            self.update_state(brightness=min(100, st["brightness"] + 10))
        elif name == "down":
            self.update_state(brightness=max(0, st["brightness"] - 10))

    def on_close(self):
        self.engine.stop()
        self.engine.join(timeout=1.0)
        if self._kb:
            try:
                self._kb.unhook_all()
            except Exception:
                pass
        for d in self.devices:
            try:
                d.close()
            except Exception:
                pass
        if self._save_job:
            self.after_cancel(self._save_job)
        self.save_config()
        self.destroy()
