#!/usr/bin/env python3
"""
NeonMouse — запуск.

  python main.py                     интерфейс
  python main.py --scan              найти мыши и показать отчёт
  python main.py --color ff0055      задать цвет всем мышам и записать в память
  python main.py --color 00ff00 --brightness 40
  python main.py --off               выключить подсветку
"""
import argparse
import logging
import sys


def main():
    ap = argparse.ArgumentParser(description="NeonMouse — управление подсветкой мышей")
    ap.add_argument("--scan", action="store_true", help="найти мыши и вывести отчёт")
    ap.add_argument("--color", metavar="RRGGBB", help="статичный цвет для всех мышей")
    ap.add_argument("--brightness", type=int, default=100, metavar="0-100")
    ap.add_argument("--off", action="store_true", help="выключить подсветку")
    args = ap.parse_args()

    if args.scan or args.color or args.off:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        from neonmouse import devices as D
        res = D.scan()
        print("\n".join(res.report))
        if args.color or args.off:
            try:
                h = (args.color or "000000").lstrip("#")
                k = max(0, min(100, args.brightness)) / 100
                col = (0, 0, 0) if args.off else tuple(int(int(h[i:i + 2], 16) * k) for i in (0, 2, 4))
            except ValueError:
                print("Цвет задаётся так: --color ff0055")
                return 2
            for m in res.mice:
                try:
                    m.apply([col] * len(m.zones), save=True, live=False)
                    print(f"✓ {m.name}: {'выключено' if args.off else '#' + h}")
                except Exception as e:
                    print(f"✗ {m.name}: {e}")
            for m in res.mice:
                m.close()
        return 0 if res.mice else 1

    from neonmouse.gui import App, RingLog
    ring = RingLog()
    ring.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[ring])
    App(ring).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
