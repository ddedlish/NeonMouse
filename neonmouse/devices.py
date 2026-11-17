"""
Драйверы NeonMouse: прямое управление подсветкой мышей по USB HID.
Сторонние программы не нужны, только библиотека hidapi.

  • Razer    — протокол Chroma (extended). Transaction-ID и светодиоды
               определяются автоматически, так что работают и модели не из таблицы.
  • Logitech — HID++ 2.0, функция 0x8070 (Color LED Effects).
               Проводные и беспроводные (Lightspeed); зоны берутся у самой мыши.
  • Bloody   — A4Tech, прямой режим (feature-report 0x07): V8M MAX, W60/W70/W90/W95.

Любая ошибка устройства превращается в DeviceError и никогда не роняет приложение.
"""
from __future__ import annotations

import logging
import threading
import time

try:
    import hid  # pip install hidapi
except Exception:  # pragma: no cover
    hid = None

log = logging.getLogger("neonmouse")
BLACK = (0, 0, 0)


class DeviceError(Exception):
    pass


# ======================================================================
# HID-слой (поддерживает оба популярных Python-пакета: hidapi и hid)
# ======================================================================
class _CyAdapter:
    """pip install hidapi"""
    def __init__(self, path):
        self.d = hid.device()
        self.d.open_path(path)

    def write(self, data): return self.d.write(list(data))
    def read(self, n, timeout_ms): return bytes(self.d.read(n, timeout_ms) or b"")
    def send_feature(self, data): return self.d.send_feature_report(list(data))
    def get_feature(self, rid, n): return bytes(self.d.get_feature_report(rid, n) or b"")
    def close(self): self.d.close()


class _CtAdapter:
    """pip install hid"""
    def __init__(self, path):
        self.d = hid.Device(path=path)

    def write(self, data): return self.d.write(bytes(data))
    def read(self, n, timeout_ms): return bytes(self.d.read(n, timeout_ms) or b"")
    def send_feature(self, data): return self.d.send_feature_report(bytes(data))
    def get_feature(self, rid, n): return bytes(self.d.get_feature_report(rid, n) or b"")
    def close(self): self.d.close()


def hid_available():
    return hid is not None and hasattr(hid, "enumerate")


def hid_enumerate():
    if not hid_available():
        return []
    try:
        return list(hid.enumerate(0, 0))
    except Exception as e:
        log.warning("Ошибка перечисления HID: %s", e)
        return []


def open_hid(path):
    if hid is None:
        raise DeviceError("не установлен модуль hidapi (pip install hidapi)")
    try:
        return _CyAdapter(path) if hasattr(hid, "device") else _CtAdapter(path)
    except Exception as e:
        raise DeviceError(f"не удалось открыть устройство: {e}") from e


class HidHandle:
    """Потокобезопасная обёртка: все обращения к USB идут через один замок."""
    def __init__(self, path, opener=open_hid):
        self.path = path
        self.lock = threading.RLock()
        self.dev = opener(path)
        self.closed = False

    def _call(self, fn, *args):
        with self.lock:
            if self.closed:
                raise DeviceError("устройство закрыто")
            try:
                res = fn(*args)
            except DeviceError:
                raise
            except Exception as e:
                raise DeviceError(f"ошибка USB: {e}") from e
            if isinstance(res, int) and res < 0:
                raise DeviceError("ошибка USB при передаче")
            return res

    def write(self, data): return self._call(self.dev.write, bytes(data))
    def read(self, n, timeout_ms): return self._call(self.dev.read, n, timeout_ms)
    def send_feature(self, data): return self._call(self.dev.send_feature, bytes(data))
    def get_feature(self, rid, n): return self._call(self.dev.get_feature, rid, n)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                try:
                    self.dev.close()
                except Exception:
                    pass


# ======================================================================
# Базовый класс мыши
# ======================================================================
def _clamp_color(c):
    try:
        return tuple(max(0, min(255, int(v))) for v in c[:3])
    except Exception:
        return BLACK


class Mouse:
    brand = "Unknown"
    demo = False
    can_save = True

    def __init__(self, name, zones, max_fps=20, uid=None, handle=None):
        self.name = name
        self.zones = list(zones) or ["Вся мышь"]
        self.max_fps = max_fps
        self.uid = uid or f"{self.brand}:{name}"
        self.handle = handle
        self.online = True
        self.failures = 0
        self.colors = [BLACK] * len(self.zones)
        self._last = None
        # служебные поля движка
        self.next_due = 0.0
        self.changed_at = 0.0
        self.persisted = True

    def apply(self, colors, save=False, live=True):
        """Отправить цвета зон. Одинаковые кадры повторно не отправляются."""
        colors = [_clamp_color(c) for c in list(colors)[: len(self.zones)]]
        colors += [BLACK] * (len(self.zones) - len(colors))
        if not save and colors == self._last:
            return False
        self._write(colors, save, live)
        self._last = colors
        self.colors = colors
        return True

    def _write(self, colors, save, live):
        raise NotImplementedError

    def close(self):
        self.online = False
        if self.handle is not None:
            self.handle.close()

    def __repr__(self):
        return f"<{self.brand} {self.name} zones={self.zones}>"


class DemoMouse(Mouse):
    brand = "Демо"
    demo = True
    can_save = False

    def __init__(self):
        super().__init__("Виртуальная мышь", ["Логотип", "Колёсико", "Подсветка"],
                         max_fps=30, uid="demo:1")

    def _write(self, colors, save, live):
        pass


# ======================================================================
# Razer (Chroma extended)
# ======================================================================
RAZER_VID = 0x1532
RZ_OK, RZ_BUSY, RZ_FAIL, RZ_TIMEOUT, RZ_UNSUPPORTED = 0x02, 0x01, 0x03, 0x04, 0x05
NOSTORE, VARSTORE = 0x00, 0x01
RAZER_LEDS = [(0x01, "Колёсико"), (0x04, "Логотип"), (0x05, "Подсветка"),
              (0x10, "Левая полоса"), (0x11, "Правая полоса")]
RAZER_MODELS = {
    0x0064: "Basilisk", 0x006C: "Mamba Elite", 0x0078: "Viper",
    0x007A: "Viper Ultimate (провод)", 0x007B: "Viper Ultimate (беспровод)",
    0x007C: "DeathAdder V2 Pro (провод)", 0x007D: "DeathAdder V2 Pro (беспровод)",
    0x0084: "DeathAdder V2", 0x0085: "Basilisk V2",
    0x0086: "Basilisk Ultimate (провод)", 0x0088: "Basilisk Ultimate (беспровод)",
    0x008A: "Viper Mini", 0x008C: "DeathAdder V2 Mini",
    0x008F: "Naga Pro (провод)", 0x0090: "Naga Pro (беспровод)",
    0x0091: "Viper 8KHz", 0x0099: "Basilisk V3",
    0x00AA: "Basilisk V3 Pro (провод)", 0x00AB: "Basilisk V3 Pro (беспровод)",
}
RAZER_TIDS = (0x1F, 0x3F, 0xFF)


def razer_crc(rep):
    crc = 0
    for b in rep[2:88]:
        crc ^= b
    return crc


def razer_report(tid, cls, cid, args):
    args = bytes(args)
    if len(args) > 80:
        raise ValueError("слишком много аргументов")
    r = bytearray(90)
    r[1] = tid
    r[5] = len(args)
    r[6] = cls
    r[7] = cid
    r[8:8 + len(args)] = args
    r[88] = razer_crc(r)
    return bytes(r)


def razer_parse(resp, cls, cid):
    """Возвращает статус ответа (учитывает, есть ли в начале байт Report ID)."""
    resp = bytes(resp)
    for k in (1, 0):
        if len(resp) >= k + 90 and resp[k + 6] == cls and resp[k + 7] == cid:
            return resp[k]
    return None


class RazerMouse(Mouse):
    brand = "Razer"

    def __init__(self, handle, pid, product=""):
        self.h = handle
        self.tid = RAZER_TIDS[0]
        self.nostore_ok = True
        self.tid = self._detect_tid()
        if self.tid is None:
            raise DeviceError("мышь не отвечает на команды Chroma")
        found = [(i, n) for i, n in RAZER_LEDS
                 if self.transact(0x0F, 0x84, [VARSTORE, i, 0]) == RZ_OK]
        if not found:
            found = [(0x00, "Вся мышь")]
        self.leds = [i for i, _ in found]
        model = RAZER_MODELS.get(pid) or (product or f"Mouse {pid:04X}").replace("Razer ", "")
        wireless = "беспровод" in model
        super().__init__(f"Razer {model}", [n for _, n in found],
                         max_fps=15 if wireless else 25, uid=f"razer:{pid:04x}", handle=handle)
        for led in self.leds:  # максимальная аппаратная яркость, остальное делаем программно
            self.transact(0x0F, 0x04, [VARSTORE, led, 0xFF])

    def transact(self, cls, cid, args, tid=None):
        rep = razer_report(self.tid if tid is None else tid, cls, cid, args)
        with self.h.lock:
            self.h.send_feature(b"\x00" + rep)
            time.sleep(0.0012)
            for _ in range(12):
                st = razer_parse(self.h.get_feature(0, 91), cls, cid)
                if st not in (None, 0x00, RZ_BUSY):
                    return st
                time.sleep(0.003)
        return RZ_TIMEOUT

    def _detect_tid(self):
        for tid in RAZER_TIDS:
            for led in (0x04, 0x01, 0x00):
                if self.transact(0x0F, 0x84, [VARSTORE, led, 0], tid=tid) == RZ_OK:
                    return tid
        for tid in RAZER_TIDS:  # запасной вариант: запрос версии прошивки
            if self.transact(0x00, 0x81, [0, 0], tid=tid) == RZ_OK:
                return tid
        return None

    def _write(self, colors, save, live):
        for led, (r, g, b) in zip(self.leds, colors):
            store = NOSTORE if (live and not save and self.nostore_ok) else VARSTORE
            st = self.transact(0x0F, 0x02, [store, led, 0x01, 0x00, 0x00, 0x01, r, g, b])
            if st != RZ_OK and store == NOSTORE:
                self.nostore_ok = False
                st = self.transact(0x0F, 0x02, [VARSTORE, led, 0x01, 0x00, 0x00, 0x01, r, g, b])
            if st != RZ_OK:
                raise DeviceError(f"Razer вернула статус {st}")


# ======================================================================
# Logitech (HID++ 2.0, feature 0x8070)
# ======================================================================
LOGI_VID = 0x046D
SW_ID = 0x0B
F_NAME, F_LED, F_RGB = 0x0005, 0x8070, 0x8071
LOGI_RECEIVERS = {0xC52B, 0xC532, 0xC534, 0xC539, 0xC53A, 0xC53F, 0xC541, 0xC545, 0xC547, 0xC548}
ZONE_NAMES = {1: "Основная", 2: "Логотип", 3: "Левый бок", 4: "Правый бок", 5: "Общая"}
MOUSE_TYPES = {3, 4, 5}  # мышь, трекпад, трекбол


class Hidpp:
    def __init__(self, handle, devidx):
        self.h = handle
        self.idx = devidx
        self._features = {}

    def request(self, feat_idx, fn, params=(), timeout=0.25):
        func = ((fn & 0x0F) << 4) | SW_ID
        msg = bytes([0x11, self.idx, feat_idx, func]) + bytes(params)[:16].ljust(16, b"\0")
        with self.h.lock:
            self.h.write(msg)
            end = time.monotonic() + timeout
            while True:
                left = int((end - time.monotonic()) * 1000)
                if left <= 0:
                    return None
                r = self.h.read(32, left)
                if not r:
                    time.sleep(0.001)
                    continue
                if len(r) < 5 or r[0] not in (0x10, 0x11) or r[1] != self.idx:
                    continue  # уведомление или чужой ответ
                if r[2] == 0xFF and r[3] == feat_idx and r[4] == func:
                    return None  # ошибка HID++ 2.0
                if r[0] == 0x10 and r[2] == 0x8F:
                    return None  # ошибка HID++ 1.0
                if r[2] == feat_idx and r[3] == func:
                    return bytes(r[4:]).ljust(16, b"\0")

    def feature(self, fid):
        if fid not in self._features:
            r = self.request(0x00, 0, [fid >> 8, fid & 0xFF])
            self._features[fid] = r[0] if (r and r[0]) else None
        return self._features[fid]

    def alive(self):
        return self.request(0x00, 0, [0x00, 0x01]) is not None


class LogitechMouse(Mouse):
    brand = "Logitech"

    def __init__(self, handle, devidx, pid):
        pp = Hidpp(handle, devidx)
        if not pp.alive():
            raise DeviceError("нет ответа")
        self.pp = pp
        name, dtype = self._identity()
        if dtype is not None and dtype not in MOUSE_TYPES:
            raise DeviceError(f"{name or 'устройство'}: не мышь (тип {dtype})")
        self.led = pp.feature(F_LED)
        if self.led is None:
            hint = " (новый протокол 0x8071 пока не поддерживается)" if pp.feature(F_RGB) else ""
            raise DeviceError(f"{name or 'мышь'}: нет управления подсветкой{hint}")
        self.zone_map = []
        info = pp.request(self.led, 0)
        for z in range(min(info[0] if info else 0, 8)):
            zi = pp.request(self.led, 1, [z])
            if not zi:
                continue
            loc, n_eff = (zi[1] << 8) | zi[2], zi[3]
            for e in range(min(n_eff, 16)):
                ei = pp.request(self.led, 2, [z, e])
                if ei and ((ei[2] << 8) | ei[3]) == 0x0001:  # эффект «статичный цвет»
                    self.zone_map.append((z, e, ZONE_NAMES.get(loc, f"Зона {z + 1}")))
                    break
        if not self.zone_map:
            raise DeviceError(f"{name or 'мышь'}: нет зон со статичным цветом")
        zones, seen = [], {}
        for _, _, zn in self.zone_map:
            seen[zn] = seen.get(zn, 0) + 1
            zones.append(zn if seen[zn] == 1 else f"{zn} {seen[zn]}")
        name = name or f"Mouse {pid:04X}"
        if not name.lower().startswith("logitech"):
            name = f"Logitech {name}"
        super().__init__(name, zones, max_fps=20 if devidx == 0xFF else 10,
                         uid=f"logitech:{pid:04x}:{devidx:02x}:{name}", handle=handle)

    def _identity(self):
        fi = self.pp.feature(F_NAME)
        if fi is None:
            return "", None
        r = self.pp.request(fi, 0)
        count = r[0] if r else 0
        name = b""
        while len(name) < count:
            chunk = self.pp.request(fi, 1, [len(name)])
            if not chunk:
                break
            name += chunk[: count - len(name)]
        t = self.pp.request(fi, 2)
        return name.decode("utf-8", "ignore").strip("\0 ").strip(), (t[0] if t else None)

    def _write(self, colors, save, live):
        persist = 0x01 if save else 0x00
        for (z, e, _), (r, g, b) in zip(self.zone_map, colors):
            params = [z, e, r, g, b, 0, 0, 0, 0, 0, 0, 0, persist]
            if self.pp.request(self.led, 3, params) is None:
                raise DeviceError("Logitech не подтвердила команду")


# ======================================================================
# A4Tech Bloody (W60/W70/W90/W95 Max, V8M MAX и др.)
# Протокол прямого управления: feature-report 0x07, заголовок 03 06.
# Источники: OpenRGB BloodyMouseController и github.com/sprinterz5/bloody-w95-linux.
# Драйвер отправляет ТОЛЬКО команды подсветки 07 03 06 xx. Опасные команды
# (07 28 — загрузчик, 07 21 — флеш, 07 2f — запись) не используются никогда.
# ======================================================================
A4TECH_VID = 0x09DA
BLOODY_USAGE_PAGE = 0xFF33
BLOODY_MAX_LEDS = 18  # байты 8..61 пакета
BLOODY_MODELS = {
    0x37EA: ("W60 Pro", None), 0x79EF: ("W70 Max", None), 0x3666: ("W90 Max", None),
    0x39B6: ("W90 Pro", None),
    0x1434: ("W95 Max", {"Колёсико": [14], "Логотип": [7],
                         "Подсветка": [0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13]}),
}


def bloody_packet(sub, data=b""):
    """64 байта: 07 03 06 <sub> 00 00 00 00 <data...>"""
    pkt = bytearray(64)
    pkt[0:4] = bytes([0x07, 0x03, 0x06, sub])
    data = bytes(data)[:56]
    pkt[8:8 + len(data)] = data
    return bytes(pkt)


class BloodyMouse(Mouse):
    brand = "Bloody"
    can_save = False  # цвет держится до переподключения; запись во флеш не делаем ради безопасности

    def __init__(self, handle, pid, product=""):
        self.h = handle
        model, zmap = BLOODY_MODELS.get(pid, (None, None))
        if model is None:
            model = (product or f"Mouse {pid:04X}").replace("Bloody", "").replace("A4Tech", "").strip(" -")
            model = model or f"Mouse {pid:04X}"
        if zmap is None:  # карта светодиодов неизвестна — красим все слоты одним цветом
            zmap = {"Вся мышь": list(range(BLOODY_MAX_LEDS))}
        self.zmap = list(zmap.values())
        with handle.lock:  # «взять управление»: 07 03 06 01, затем 07 03 06 00 … 01
            handle.send_feature(bloody_packet(0x01))
            handle.send_feature(bloody_packet(0x00, [0x01]))
            self.echo = self._echo_ok()
        super().__init__(f"Bloody {model}", list(zmap.keys()), max_fps=30,
                         uid=f"bloody:{pid:04x}", handle=handle)

    def _echo_ok(self):
        try:
            r = self.h.get_feature(0x07, 64)
            return any(r[k:k + 2] == b"\x03\x06" for k in (0, 1))
        except Exception:
            return False

    def _write(self, colors, save, live):
        data = bytearray(3 * BLOODY_MAX_LEDS)
        for leds, (r, g, b) in zip(self.zmap, colors):
            for i in leds:
                if 0 <= i < BLOODY_MAX_LEDS:
                    data[3 * i:3 * i + 3] = bytes([r, g, b])
        self.h.send_feature(bloody_packet(0x02, data))


def _scan_bloody(pid, product, es, opener, res, label):
    if not any(_is_mouse_entry(e) for e in es) and pid not in BLOODY_MODELS:
        return  # клавиатура/коврик A4Tech
    paths = _unique_paths([e for e in es if e.get("usage_page") == BLOODY_USAGE_PAGE], lambda e: 0)
    if not paths and not any(e.get("usage_page") for e in es):  # система не сообщает usage
        paths = _unique_paths([e for e in es if e.get("interface_number") == 2], lambda e: 0)
    if not paths:
        ifs = sorted({(e.get("interface_number"), hex(e.get("usage_page") or 0)) for e in es})
        res.report.append(f"✗ {label}: нет канала управления подсветкой (интерфейсы: {ifs})")
        res.unsupported.append(f"Bloody {product or hex(pid)}")
        return
    err = ""
    for p in paths[:2]:
        h = None
        try:
            h = HidHandle(p, opener)
            m = BloodyMouse(h, pid, product)
            res.mice.append(m)
            res.report.append(f"✓ {m.name} (PID {pid:04X}): зоны {', '.join(m.zones)}, "
                              f"ответ мыши: {'есть' if m.echo else 'нет'}")
            return
        except Exception as ex:
            err = str(ex)
            if h:
                h.close()
    res.report.append(f"✗ {label}: {err}")
    res.unsupported.append(f"Bloody {product or hex(pid)}")


# ======================================================================
# Сканер
# ======================================================================
OTHER_VENDORS = {0x1038: "SteelSeries", 0x1B1C: "Corsair", 0x0951: "HyperX", 0x03F0: "HyperX",
                 0x0B05: "ASUS ROG", 0x1E7D: "Roccat", 0x258A: "Glorious", 0x2516: "Cooler Master",
                 0x1462: "MSI", 0x04D9: "Redragon"}


class ScanResult:
    def __init__(self):
        self.mice = []
        self.unsupported = []
        self.report = []


def _is_mouse_entry(e):
    return e.get("usage_page") == 0x01 and e.get("usage") == 0x02


def scan(enum=None, opener=None):
    res = ScanResult()
    rep = res.report.append
    if enum is None:
        if not hid_available():
            rep("✗ Модуль hidapi не установлен: выполните  pip install hidapi")
            return res
        enum = hid_enumerate
    opener = opener or open_hid
    entries = enum()
    rep(f"HID-устройств в системе: {len(entries)}")

    groups = {}
    for e in entries:
        vid = e.get("vendor_id")
        if vid in (RAZER_VID, LOGI_VID, A4TECH_VID):
            groups.setdefault((vid, e.get("product_id"), e.get("serial_number") or ""), []).append(e)

    brands = {RAZER_VID: "Razer", LOGI_VID: "Logitech", A4TECH_VID: "Bloody"}
    scanners = {RAZER_VID: _scan_razer, LOGI_VID: _scan_logitech, A4TECH_VID: _scan_bloody}
    for (vid, pid, _), es in groups.items():
        product = next((e.get("product_string") for e in es if e.get("product_string")), "") or ""
        label = f"{brands[vid]} {pid:04X} «{product}»"
        try:
            scanners[vid](pid, product, es, opener, res, label)
        except Exception as ex:  # страховка: сканер не падает никогда
            rep(f"✗ {label}: {ex}")

    seen = set()
    for e in entries:
        brand = OTHER_VENDORS.get(e.get("vendor_id"))
        key = (e.get("vendor_id"), e.get("product_id"))
        if brand and _is_mouse_entry(e) and key not in seen:
            seen.add(key)
            nm = f"{brand} {e.get('product_string') or hex(key[1])}"
            res.unsupported.append(nm)
            rep(f"• {nm}: найдена, драйвер для этого бренда пока не написан")
    rep(f"Итого подключено мышей: {len(res.mice)}")
    return res


def _unique_paths(es, key):
    out, seen = [], set()
    for e in sorted(es, key=key):
        p = e.get("path")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _scan_razer(pid, product, es, opener, res, label):
    if pid not in RAZER_MODELS and not any(_is_mouse_entry(e) for e in es):
        return  # клавиатура/гарнитура Razer — не наш случай
    paths = _unique_paths(es, lambda e: (e.get("interface_number", 0) not in (0, -1),
                                         not _is_mouse_entry(e)))[:3]
    err = "нет подходящего интерфейса"
    for p in paths:
        h = None
        try:
            h = HidHandle(p, opener)
            m = RazerMouse(h, pid, product)
            res.mice.append(m)
            res.report.append(f"✓ {m.name}: зоны {', '.join(m.zones)} (tid 0x{m.tid:02X})")
            return
        except Exception as ex:
            err = str(ex)
            if h:
                h.close()
    res.report.append(f"✗ {label}: {err}")
    res.unsupported.append(f"Razer {product or hex(pid)}")


def _scan_logitech(pid, product, es, opener, res, label):
    vendor = [e for e in es if e.get("usage_page") == 0xFF00]
    if not vendor:
        if any(e.get("usage_page") for e in es):
            return  # у устройства нет HID++ интерфейса
        vendor = es  # система не сообщает usage — пробуем все интерфейсы
    paths = _unique_paths(vendor, lambda e: e.get("usage") != 0x0002)
    receiver = pid in LOGI_RECEIVERS or "receiver" in product.lower()
    indices = list(range(1, 7)) if receiver else [0xFF]
    errors = []
    for p in paths:
        h = None
        try:
            h = HidHandle(p, opener)
        except Exception as ex:
            errors.append(str(ex))
            continue
        found = 0
        for idx in indices:
            try:
                m = LogitechMouse(h, idx, pid)
                res.mice.append(m)
                found += 1
                res.report.append(f"✓ {m.name}: зоны {', '.join(m.zones)}")
            except Exception as ex:
                if "нет ответа" not in str(ex):
                    errors.append(str(ex))
        if found:
            return
        h.close()
    if errors:
        res.report.append(f"✗ {label}: {errors[-1]}")
        if "не мышь" not in errors[-1]:
            res.unsupported.append(f"Logitech {product or hex(pid)}")
