"""Тесты на эмуляторах мышей Razer и Logitech: python -m unittest discover tests"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from neonmouse import devices as D  # noqa: E402
from neonmouse.effects import EFFECTS, Engine, default_state, render, sanitize_state  # noqa: E402


class FakeRazer:
    """Эмулирует мышь Razer: проверяет CRC, transaction-id и поддерживаемые светодиоды."""
    def __init__(self, tid=0x3F, leds=(0x01, 0x04), report_id_prefix=True):
        self.tid, self.leds, self.prefix = tid, set(leds), report_id_prefix
        self.resp, self.statics, self.fail = b"", [], False

    def send_feature(self, data):
        if self.fail:
            raise OSError("device unplugged")
        assert len(data) == 91 and data[0] == 0
        rep = bytearray(data[1:])
        assert rep[88] == D.razer_crc(rep), "неверная CRC"
        cls, cid, args = rep[6], rep[7], rep[8:8 + rep[5]]
        status = D.RZ_OK if rep[1] == self.tid else D.RZ_FAIL
        if status == D.RZ_OK and cls == 0x0F:
            if args[1] not in self.leds:
                status = D.RZ_UNSUPPORTED
            elif cid == 0x02:
                assert rep[5] == 9 and args[2] == 0x01
                self.statics.append((args[0], args[1], tuple(args[6:9])))
        rep[0] = status
        self.resp = (b"\x00" if self.prefix else b"") + bytes(rep)
        return 91

    def get_feature(self, rid, n):
        return self.resp

    def write(self, d): raise OSError("not supported")
    def read(self, n, t): return b""
    def close(self): pass


class FakeLogitech:
    """Эмулирует мышь Logitech с HID++ 2.0 и 0x8070."""
    def __init__(self, devidx=0xFF, name="G502 HERO Gaming Mouse", dtype=3,
                 zones=((1, (0, 1, 3)), (2, (0, 3, 1))), features=(0x0000, 0x0001, 0x0005, 0x8070)):
        self.devidx, self.name, self.dtype, self.zones = devidx, name.encode(), dtype, zones
        self.features, self.q, self.sets, self.fail = list(features), [], [], False

    def write(self, d):
        if self.fail:
            raise OSError("gone")
        assert len(d) == 20 and d[0] == 0x11
        if d[1] != self.devidx:
            return 20
        fi, func, p = d[2], d[3], d[4:]
        fn = func >> 4
        self.q.append(bytes([0x10, d[1], 0x41, 0x00, 0, 0, 0]))  # постороннее уведомление
        rep = lambda params: self.q.append(bytes([0x11, d[1], fi, func]) + bytes(params).ljust(16, b"\0"))
        feat = self.features[fi] if fi < len(self.features) else None
        if fi == 0 and fn == 0:
            fid = (p[0] << 8) | p[1]
            rep([self.features.index(fid) if fid in self.features else 0])
        elif feat == 0x0005:
            if fn == 0: rep([len(self.name)])
            elif fn == 1: rep(self.name[p[0]:p[0] + 16])
            elif fn == 2: rep([self.dtype])
        elif feat == 0x8070:
            if fn == 0: rep([len(self.zones)])
            elif fn == 1:
                loc, eff = self.zones[p[0]]
                rep([p[0], 0, loc, len(eff)])
            elif fn == 2:
                rep([p[0], p[1], 0, self.zones[p[0]][1][p[1]]])
            elif fn == 3:
                self.sets.append((p[0], p[1], tuple(p[2:5]), p[12]))
                rep(p[:13])
        else:
            self.q.append(bytes([0x11, d[1], 0xFF, fi, func, 0x02]).ljust(20, b"\0"))
        return 20

    def read(self, n, timeout_ms):
        if self.q:
            return self.q.pop(0)
        time.sleep(min(timeout_ms, 3) / 1000)
        return b""

    def send_feature(self, d): raise OSError("no")
    def get_feature(self, r, n): raise OSError("no")
    def close(self): pass


class FakeBloody:
    """Эмулирует мышь Bloody: принимает только безопасные пакеты 07 03 06 xx, эхо через GET_FEATURE."""
    def __init__(self):
        self.sent, self.last = [], b""

    def send_feature(self, d):
        d = bytes(d)
        assert len(d) == 64 and d[:3] == b"\x07\x03\x06", d[:4].hex()
        assert d[3] in (0x00, 0x01, 0x02), "недопустимая команда"
        self.sent.append(d)
        self.last = d
        return 64

    def get_feature(self, rid, n): return self.last
    def write(self, d): raise OSError("no")
    def read(self, n, t): return b""
    def close(self): pass


def bloody_entries(pid=0x7A1C, product="Bloody V8M MAX"):
    base = dict(vendor_id=0x09DA, product_id=pid, product_string=product, serial_number="")
    return [dict(base, path=b"bl0", interface_number=0, usage_page=1, usage=2),
            dict(base, path=b"bl1", interface_number=1, usage_page=1, usage=6),
            dict(base, path=b"bl2", interface_number=2, usage_page=0xFF33, usage=0x0578)]


def entries():
    return [
        dict(path=b"rz0", vendor_id=0x1532, product_id=0x0084, product_string="Razer DeathAdder V2",
             interface_number=0, usage_page=1, usage=2, serial_number=""),
        dict(path=b"rz1", vendor_id=0x1532, product_id=0x0084, product_string="Razer DeathAdder V2",
             interface_number=1, usage_page=1, usage=6, serial_number=""),
        dict(path=b"lg0", vendor_id=0x046D, product_id=0xC08B, product_string="G502 HERO",
             interface_number=0, usage_page=1, usage=2, serial_number=""),
        dict(path=b"lg1s", vendor_id=0x046D, product_id=0xC08B, product_string="G502 HERO",
             interface_number=1, usage_page=0xFF00, usage=1, serial_number=""),
        dict(path=b"lg1", vendor_id=0x046D, product_id=0xC08B, product_string="G502 HERO",
             interface_number=1, usage_page=0xFF00, usage=2, serial_number=""),
        dict(path=b"ss", vendor_id=0x1038, product_id=0x1720, product_string="Rival 310",
             interface_number=0, usage_page=1, usage=2, serial_number=""),
        dict(path=b"kb", vendor_id=0x1532, product_id=0x0203, product_string="BlackWidow",
             interface_number=0, usage_page=1, usage=6, serial_number=""),
    ]


class Tests(unittest.TestCase):
    def setUp(self):
        self.rz, self.lg = FakeRazer(), FakeLogitech()
        self.fakes = {b"rz0": self.rz, b"lg1": self.lg}

    def opener(self, path):
        if path not in self.fakes:
            raise OSError("access denied")
        return self.fakes[path]

    def test_razer_report_layout(self):
        r = D.razer_report(0x3F, 0x0F, 0x02, [1, 4, 1, 0, 0, 1, 255, 0, 0])
        self.assertEqual(len(r), 90)
        self.assertEqual((r[1], r[5], r[6], r[7]), (0x3F, 9, 0x0F, 0x02))
        self.assertEqual(r[88], D.razer_crc(r))

    def test_scan_finds_both_and_unsupported(self):
        res = D.scan(enum=entries, opener=self.opener)
        names = [m.name for m in res.mice]
        self.assertEqual(len(res.mice), 2, res.report)
        self.assertIn("Razer DeathAdder V2", names)
        self.assertIn("Logitech G502 HERO Gaming Mouse", names)
        self.assertIn("SteelSeries Rival 310", res.unsupported)
        self.assertFalse(any("BlackWidow" in u for u in res.unsupported))

    def test_razer_tid_and_leds_autodetect(self):
        for tid, prefix in ((0x1F, True), (0x3F, False), (0xFF, True)):
            fake = FakeRazer(tid=tid, leds=(0x04,), report_id_prefix=prefix)
            m = D.RazerMouse(D.HidHandle(b"x", lambda p: fake), 0x0078)
            self.assertEqual(m.tid, tid)
            self.assertEqual(m.zones, ["Логотип"])
            m.apply([(10, 20, 30)])
            self.assertEqual(fake.statics[-1], (D.NOSTORE, 0x04, (10, 20, 30)))
            m.apply([(10, 20, 30)], save=True, live=False)
            self.assertEqual(fake.statics[-1][0], D.VARSTORE)

    def test_razer_dedup(self):
        m = D.RazerMouse(D.HidHandle(b"x", lambda p: self.rz), 0x0084)
        m.apply([(1, 2, 3), (4, 5, 6)])
        n = len(self.rz.statics)
        self.assertFalse(m.apply([(1, 2, 3), (4, 5, 6)]))
        self.assertEqual(len(self.rz.statics), n)

    def test_logitech_zones_and_write(self):
        m = D.LogitechMouse(D.HidHandle(b"x", lambda p: self.lg), 0xFF, 0xC08B)
        self.assertEqual(m.zones, ["Основная", "Логотип"])
        self.assertEqual(m.zone_map, [(0, 1, "Основная"), (1, 2, "Логотип")])
        m.apply([(255, 0, 0), (0, 0, 255)])
        self.assertEqual(self.lg.sets[-2:], [(0, 1, (255, 0, 0), 0), (1, 2, (0, 0, 255), 0)])
        m.apply([(255, 0, 0), (0, 0, 255)], save=True)
        self.assertEqual(self.lg.sets[-1][3], 1)

    def test_logitech_receiver_index(self):
        lg = FakeLogitech(devidx=2)
        es = [dict(path=b"rc", vendor_id=0x046D, product_id=0xC539, product_string="USB Receiver",
                   interface_number=2, usage_page=0xFF00, usage=2, serial_number="")]
        res = D.scan(enum=lambda: es, opener=lambda p: lg)
        self.assertEqual(len(res.mice), 1, res.report)
        self.assertEqual(res.mice[0].max_fps, 10)

    def test_logitech_not_mouse_and_no_led(self):
        kb = FakeLogitech(dtype=0)
        with self.assertRaises(D.DeviceError):
            D.LogitechMouse(D.HidHandle(b"x", lambda p: kb), 0xFF, 1)
        nl = FakeLogitech(features=(0x0000, 0x0001, 0x0005, 0x8071))
        with self.assertRaisesRegex(D.DeviceError, "0x8071"):
            D.LogitechMouse(D.HidHandle(b"x", lambda p: nl), 0xFF, 1)

    def test_effects_valid_ranges(self):
        st = default_state()
        st["per_zone"], st["zone_colors"] = True, [[255, 0, 0]]
        for e in EFFECTS:
            st["effect"] = e
            for n in (1, 2, 5):
                for t in (0, 0.37, 1.9, 123.4):
                    fr = render(st, n, t)
                    self.assertEqual(len(fr), n, e)
                    self.assertTrue(all(0 <= c <= 255 for px in fr for c in px), e)

    def test_sanitize_garbage(self):
        s = sanitize_state({"effect": "???", "color": "red", "brightness": "x", "zone_colors": 5})
        self.assertEqual(s, {**default_state(), "zone_colors": []})
        self.assertEqual(sanitize_state(None), default_state())

    def test_engine_fade_persist_and_unplug(self):
        m = D.RazerMouse(D.HidHandle(b"x", lambda p: self.rz), 0x0084)
        st = default_state()
        st["color"] = [200, 100, 0]
        errors = []
        eng = Engine(lambda: [m], lambda uid: st, lambda d, e: errors.append(e))
        t = 0.0
        for _ in range(60):  # 1 секунда
            t += 1 / 60
            eng.step(t, t, 1 / 60)
        self.assertEqual(m.colors, [(200, 100, 0)] * 2)
        for _ in range(120):  # пауза → автосохранение в память
            t += 1 / 60
            eng.step(t, t, 1 / 60)
        self.assertEqual(self.rz.statics[-1][0], D.VARSTORE)
        st["enabled"] = False
        for _ in range(12):
            t += 1 / 60
            eng.step(t, t, 1 / 60)
        mid = m.colors[0]
        self.assertTrue(0 < mid[0] < 200, f"плавное затухание, сейчас {mid}")
        for _ in range(40):
            t += 1 / 60
            eng.step(t, t, 1 / 60)
        self.assertEqual(m.colors, [(0, 0, 0)] * 2)
        self.rz.fail = True  # мышь выдернули
        st["enabled"] = True
        for _ in range(200):
            t += 1 / 60
            eng.step(t, t, 1 / 60)
        self.assertFalse(m.online)
        self.assertEqual(len(errors), 1)

    def test_bloody_v8m_max_detect_and_colors(self):
        fake = FakeBloody()
        res = D.scan(enum=bloody_entries, opener=lambda p: fake if p == b"bl2" else (_ for _ in ()).throw(OSError("denied")))
        self.assertEqual(len(res.mice), 1, res.report)
        m = res.mice[0]
        self.assertEqual(m.name, "Bloody V8M MAX")
        self.assertEqual(m.zones, ["Вся мышь"])
        self.assertTrue(m.echo)
        self.assertFalse(m.can_save)
        self.assertEqual(fake.sent[0][:4], bytes([7, 3, 6, 1]))
        self.assertEqual(fake.sent[1][:9], bytes([7, 3, 6, 0, 0, 0, 0, 0, 1]))
        m.apply([(255, 10, 20)])
        pkt = fake.sent[-1]
        self.assertEqual(pkt[:8], bytes([7, 3, 6, 2, 0, 0, 0, 0]))
        self.assertEqual(pkt[8:11], bytes([255, 10, 20]))
        self.assertEqual(pkt[8 + 3 * 17:8 + 3 * 18], bytes([255, 10, 20]))
        self.assertEqual(pkt[62:], b"\0\0")

    def test_bloody_w95_zone_map(self):
        fake = FakeBloody()
        m = D.BloodyMouse(D.HidHandle(b"x", lambda p: fake), 0x1434)
        self.assertEqual(m.zones, ["Колёсико", "Логотип", "Подсветка"])
        m.apply([(1, 1, 1), (2, 2, 2), (3, 3, 3)])
        pkt = fake.sent[-1]
        self.assertEqual(pkt[8 + 42:8 + 45], bytes([1, 1, 1]))   # LED 14 = колёсико
        self.assertEqual(pkt[8 + 21:8 + 24], bytes([2, 2, 2]))   # LED 7 = логотип
        self.assertEqual(pkt[8:11], bytes([3, 3, 3]))

    def test_bloody_keyboard_ignored(self):
        es = [dict(path=b"k", vendor_id=0x09DA, product_id=0xFA10, product_string="B820R",
                   interface_number=2, usage_page=0xFF33, usage=1, serial_number="")]
        res = D.scan(enum=lambda: es, opener=lambda p: FakeBloody())
        self.assertEqual(res.mice, [])

    def test_closed_handle_raises_device_error(self):
        h = D.HidHandle(b"x", lambda p: self.rz)
        m = D.RazerMouse(h, 0x0084)
        m.close()
        with self.assertRaises(D.DeviceError):
            m.apply([(1, 1, 1), (1, 1, 1)])


if __name__ == "__main__":
    unittest.main()
