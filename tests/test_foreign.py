# -*- coding: utf-8 -*-
"""Чужие часы: живой трек -> грубый GPX «как нарисовал человек» -> глушилка с
плавающим шагомером -> движок. Проверяет две вещи:

1. Ничего чужого не потеряно: у каждой точки все поля, кроме координат,
   дистанции и скорости, и все developer fields (Stryd) — байт в байт;
   прочие сообщения (кроме кругов, сессии, активности) — те же и столько же.
2. Точность: сдвиг точек от настоящего трека и итоговая дистанция.

    py -X utf8 tests/test_foreign.py [файлы.fit ...]   (по умолчанию tests/samples/*.fit)
"""
import glob
import io
import math
import os
import random
import statistics
import sys
from collections import Counter

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from trek_rebuild import engine, fit  # noqa: E402
from trek_rebuild.fit import SC, UINT16, UINT32  # noqa: E402
from trek_rebuild.geo import haversine  # noqa: E402

TOUCHED = {0, 1, 5, 6, 73, 2}          # координаты, метры, скорость (+ высота, если её не было)
DRAW_M = 50                            # узлы «нарисованного» трека — через столько метров


def track_of(f):
    pts = [(m.get(0) * SC, m.get(1) * SC, m.get(5)) for m in f.of(20)
           if m.get(0) is not None and m.get(1) is not None and m.get(5) is not None]
    return pts


def gpx_from(pts):
    keep, last = [pts[0]], pts[0]
    for p in pts[1:]:
        if haversine(last, p) >= DRAW_M:
            keep.append(p)
            last = p
    keep.append(pts[-1])
    body = ''.join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for la, lo, _ in keep)
    return (f'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">'
            f'<trk><trkseg>{body}</trkseg></trk></gpx>').encode()


def jam(data, seed=1, live_every=0):
    """GPS пропал, метры и скорость раздуты множителем 1.2–1.4, который плывёт
    раз в минуту. live_every — раз в столько минут GPS пробивается на минуту."""
    f = fit.read(data)
    rnd = random.Random(seed)
    k, carry, prev, t0, minute = 1.3, 0.0, None, None, -1
    for m in f.of(20):
        t = m.timestamp()
        t0 = t if t0 is None else t0
        if (t - t0) // 60 != minute:
            minute = (t - t0) // 60
            k = min(1.4, max(1.2, k + rnd.uniform(-0.01, 0.01)))
        live = live_every and minute % live_every == 0
        if not live:
            m.set(0, None, 0x85, f.cache) if m.d.index(0) >= 0 else None
            m.set(1, None, 0x85, f.cache) if m.d.index(1) >= 0 else None
        d = m.get(5)
        if d is not None:
            carry = carry + (d - prev) * k if prev is not None else d * k
            prev = d
            m.set(5, carry, UINT32, f.cache)
        for num, bt in ((6, UINT16), (73, UINT32)):
            if m.get(num) is not None:
                m.set(num, m.get(num) * k, bt, f.cache)
    return fit.write(f)


def by_occurrence(msgs):
    """{(ts, номер дубля): сообщение} — у части часов бывают записи с одним временем."""
    seen, out = Counter(), {}
    for m in msgs:
        t = m.timestamp()
        out[(t, seen[t])] = m
        seen[t] += 1
    return out


def field_map(m):
    return {num: m.raw[i] for i, (num, _, _) in enumerate(m.d.fields)}


def check(path, live_every=0):
    name = os.path.basename(path)
    src = open(path, 'rb').read()
    try:
        f = fit.read(src)
    except fit.FitError as e:
        return print(f'ПРОПУСК {name}: {e}')
    pts = track_of(f)
    if len(pts) < 200:
        return print(f'ПРОПУСК {name}: нет живого трека с дистанцией')
    jammed = jam(src, live_every=live_every)
    try:
        out, rep = engine.rebuild([jammed], gpx_from(pts), distance='track')
    except engine.RebuildError as e:
        return print(f'ОШИБКА  {name}: {e}')
    g = fit.read(out)

    # 1. сохранность
    lost = []
    ra = by_occurrence(fit.read(jammed).of(20))
    for key, m in by_occurrence(g.of(20)).items():
        o = ra.get(key)
        if o is None:
            continue
        fa, fb = field_map(o), field_map(m)
        for num, raw in fa.items():
            if num not in TOUCHED and fb.get(num) != raw:
                lost.append(f'record.{num}')
        if o.devraw != m.devraw:
            lost.append('record.dev')
    skip = {18, 19, 34}
    ca = Counter(m.gnum for m in f.msgs if m.gnum not in skip)
    cb = Counter(m.gnum for m in g.msgs if m.gnum not in skip)
    if ca != cb:
        lost.append(f'сообщения {dict(ca - cb)} / лишние {dict(cb - ca)}')
    import fitparse
    try:
        n_fp = sum(1 for _ in fitparse.FitFile(io.BytesIO(out)).get_messages())
        fp = f'fitparse читает ({n_fp})'
    except Exception as e:  # noqa: BLE001
        fp = f'fitparse НЕ читает: {e}'

    # 2. точность
    truth = {m.timestamp(): (m.get(0) * SC, m.get(1) * SC) for m in f.of(20)
             if m.get(0) is not None and m.get(1) is not None}
    got = {m.timestamp(): (m.get(0) * SC, m.get(1) * SC) for m in g.of(20) if m.get(0) is not None}
    off = sorted(haversine(truth[t], got[t]) for t in set(truth) & set(got))
    real_km = pts[-1][2] / 100000
    status = 'OK ' if not lost else 'ПОТЕРИ'
    mode = f'GPS раз в {live_every} мин' if live_every else 'GPS нет'
    print(f'{status} {name} [{mode}]: часы {rep["watch_km"]} -> {rep["result_km"]} км (было по GPS {real_km:.2f}), '
          f'сдвиг м: медиана {statistics.median(off):.0f} / 95% {off[int(.95 * len(off))]:.0f} / '
          f'макс {off[-1]:.0f}; {rep["laps"]}; {fp}')
    if lost:
        print('   потеряно: ' + ', '.join(sorted(Counter(lost))))
    for w in rep['warnings']:
        print('   ⚠ ' + w[:200])


def main():
    files = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, 'tests', 'samples', '*.fit')))
    for p in files:
        check(p)
        check(p, live_every=15)


if __name__ == '__main__':
    main()
