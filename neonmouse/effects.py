"""Эффекты подсветки и движок, который в отдельном потоке отправляет кадры на мыши."""
from __future__ import annotations

import colorsys
import math
import threading
import time

BLACK = (0, 0, 0)
EFFECTS = ["Статичный", "Дыхание", "Радуга", "Волна", "Стробоскоп",
           "Переход", "Огонь", "Полиция", "Звёзды"]
ICONS = {"Статичный": "■", "Дыхание": "◐", "Радуга": "◆", "Волна": "≈", "Стробоскоп": "✦",
         "Переход": "⇄", "Огонь": "▲", "Полиция": "◧", "Звёзды": "✧"}
USES_COLOR = {"Статичный", "Дыхание", "Стробоскоп", "Переход", "Звёзды"}
FADE_TIME = 0.35       # плавное включение/выключение, сек
PERSIST_AFTER = 1.5    # статичный цвет записывается в память мыши после паузы, сек


def default_state():
    return {"enabled": True, "effect": "Статичный", "color": [139, 92, 246],
            "color2": [34, 211, 238], "brightness": 100, "speed": 50,
            "per_zone": False, "zone_colors": []}


def _rgb(v, fb):
    try:
        out = [max(0, min(255, int(x))) for x in v]
        return out if len(out) == 3 else list(fb)
    except Exception:
        return list(fb)


def _int(v, lo, hi, fb):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return fb


def sanitize_state(s):
    """Проверяет настройки из файла: битый конфиг никогда не ломает приложение."""
    d = default_state()
    if not isinstance(s, dict):
        return d
    d["enabled"] = bool(s.get("enabled", True))
    d["effect"] = s.get("effect") if s.get("effect") in EFFECTS else "Статичный"
    d["color"] = _rgb(s.get("color"), d["color"])
    d["color2"] = _rgb(s.get("color2"), d["color2"])
    d["brightness"] = _int(s.get("brightness"), 0, 100, 100)
    d["speed"] = _int(s.get("speed"), 1, 100, 50)
    d["per_zone"] = bool(s.get("per_zone", False))
    zc = s.get("zone_colors")
    d["zone_colors"] = [_rgb(c, d["color"]) for c in zc[:16]] if isinstance(zc, list) else []
    return d


def hsv(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def base_color(st, i):
    if st["per_zone"] and i < len(st["zone_colors"]):
        return tuple(st["zone_colors"][i])
    return tuple(st["color"])


def render(st, n, t):
    """Кадр эффекта: список цветов по зонам (без учёта яркости)."""
    n = max(1, n)
    spd = st["speed"] / 50.0
    eff = st["effect"]
    if eff == "Дыхание":
        k = (1 - math.cos(t * spd * 2.2)) / 2
        return [lerp(BLACK, base_color(st, i), k * k * (3 - 2 * k)) for i in range(n)]
    if eff == "Радуга":
        return [hsv(t * spd * 0.15)] * n
    if eff == "Волна":
        return [hsv(t * spd * 0.2 + i / n * 0.5) for i in range(n)]
    if eff == "Стробоскоп":
        on = (t * spd * 5) % 1 < 0.35
        return [base_color(st, i) if on else BLACK for i in range(n)]
    if eff == "Переход":
        k = (1 - math.cos(t * spd * 1.6)) / 2
        return [lerp(tuple(st["color"]), tuple(st["color2"]), k)] * n
    if eff == "Огонь":
        out = []
        for i in range(n):
            f = 0.62 + 0.38 * math.sin(t * spd * 9 + i * 2.1) * math.sin(t * spd * 3.7 + i * 1.3)
            out.append((int(255 * f), int(95 * f * f), int(12 * f ** 3)))
        return out
    if eff == "Полиция":
        phase = int(t * spd * 4) % 2
        flash = (t * spd * 8) % 1 < 0.6
        red, blue = (255, 0, 20), (0, 40, 255)
        if n == 1:
            return [(red if phase else blue) if flash else BLACK]
        return [((red if (i + phase) % 2 == 0 else blue) if flash else BLACK) for i in range(n)]
    if eff == "Звёзды":
        out = []
        for i in range(n):
            k = max(0.0, math.sin(t * spd * (1.3 + 0.7 * i) + i * 2.4)) ** 6
            c = base_color(st, i)
            out.append(lerp(tuple(int(v * 0.08) for v in c), c, k))
        return out
    return [base_color(st, i) for i in range(n)]  # Статичный


class Engine(threading.Thread):
    """~120 тиков/с. Каждая мышь получает кадры со своей безопасной частотой (max_fps)."""

    def __init__(self, get_devices, get_state, on_error=None):
        super().__init__(daemon=True, name="neonmouse-engine")
        self.get_devices = get_devices
        self.get_state = get_state
        self.on_error = on_error or (lambda dev, err: None)
        self._halt = threading.Event()
        self._save_lock = threading.Lock()
        self._save = set()
        self.level = {}

    def request_save(self, uids):
        with self._save_lock:
            self._save.update(uids)

    def stop(self):
        self._halt.set()

    def step(self, now, t, dt):
        for dev in list(self.get_devices()):
            if not dev.online:
                continue
            st = self.get_state(dev.uid)
            target = 1.0 if st["enabled"] else 0.0
            lv = self.level.get(dev.uid, target)
            d = dt / FADE_TIME
            lv = min(target, lv + d) if lv < target else max(target, lv - d)
            self.level[dev.uid] = lv
            if now < dev.next_due:
                continue
            dev.next_due = now + 1.0 / max(1, dev.max_fps)
            k = lv * st["brightness"] / 100.0
            frame = [tuple(int(c * k) for c in col) for col in render(st, len(dev.zones), t)]
            animated = (st["effect"] != "Статичный" and st["enabled"]) or lv != target
            with self._save_lock:
                save = dev.uid in self._save
                self._save.discard(dev.uid)
            try:
                if dev.apply(frame, save=save, live=True):
                    dev.changed_at, dev.persisted = now, save
                elif save:
                    dev.persisted = True
                if not animated and not dev.persisted and now - dev.changed_at > PERSIST_AFTER:
                    dev.apply(frame, save=True, live=False)  # запомнить цвет в мыши
                    dev.persisted = True
                dev.failures = 0
            except Exception as e:
                dev.failures += 1
                dev.next_due = now + 0.3
                if dev.failures >= 4:
                    dev.online = False
                    try:
                        self.on_error(dev, e)
                    except Exception:
                        pass

    def run(self):
        t0 = last = time.monotonic()
        while not self._halt.is_set():
            now = time.monotonic()
            try:
                self.step(now, now - t0, now - last)
            except Exception:
                pass  # движок не останавливается ни при каких ошибках
            last = now
            self._halt.wait(1 / 120)
