# -*- coding: utf-8 -*-
"""Глушилка на чистом файле: эталон -> «заглушённая» копия -> движок -> сверка с эталоном.

Эталон — тренировка с правильным треком. Копия теряет GPS после первых
--live минут, а метры и скорость часов раздуваются в --stride раз (шагомер
Balance 2 под глушилкой давал 1.29–1.30). Движок должен вернуть дистанцию
трека и точки рядом с эталонными.

--drift D — перебор шагомера плывёт по ходу от stride·(1−D) до stride·(1+D): устал,
шаг короче, часы считают его прежним (Горный день 25.10: ×1.17 → 1.21 → 1.27 по кругам).
--alt-from — высоты записи взять из другого файла по времени (сырой файл часов с
барометром, если в эталоне высота уже трассы). --no-ele — без привязки по высоте.

    py -X utf8 tests/test_jammed.py эталон.fit --route naberezhnaya [--live 8] [--split]
    py -X utf8 tests/test_jammed.py out/gorny.fit --route gorny_3 --live 0 --stride 1.2 --drift 0.08 --alt-from часы.fit
"""
import argparse
import os
import statistics
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from trek_rebuild import engine, fit, routes  # noqa: E402
from trek_rebuild.fit import SC, UINT16, UINT32  # noqa: E402
from trek_rebuild.geo import haversine  # noqa: E402


def jam(data, live_s, stride, ts_from=None, ts_to=None, drift=0.0, alt=None):
    f = fit.read(data)
    recs = f.of(20)
    t0, t1 = recs[0].timestamp(), recs[-1].timestamp()
    prev_d = new_d = None
    for m in f.msgs:
        if m.gnum != 20:
            continue
        t = m.timestamp()
        if alt is not None and t in alt:
            m.set(78, alt[t], UINT32, f.cache)
            m.set(2, None, UINT16, f.cache)
        if ts_from is not None and not (ts_from <= t <= ts_to):
            continue
        if t - t0 >= live_s:
            m.set(0, None, 0x85, f.cache)
            m.set(1, None, 0x85, f.cache)
        k = stride * (1 + drift * (2 * (t - t0) / max(t1 - t0, 1) - 1))
        d = m.get(5)
        if d is not None:
            # метры копятся с переменным множителем — поток остаётся монотонным
            new_d = d * k if prev_d is None else new_d + (d - prev_d) * k
            prev_d = d
            m.set(5, new_d, UINT32, f.cache)
        if m.get(6) is not None:
            m.set(6, m.get(6) * k, UINT16, f.cache)
        if m.get(73) is not None:
            m.set(73, m.get(73) * k, UINT32, f.cache)
    return f


def altitudes(data):
    """Высота записи (сырое поле enhanced_altitude) по времени."""
    out = {}
    for m in fit.read(data).of(20):
        a = m.get(78)
        if a is None and m.get(2) is not None:
            a = m.get(2)
        if a is not None:
            out[m.timestamp()] = a
    return out


def split(f, at_ts):
    """Два куска: до at_ts и после (каждый со своими file_id/сессией)."""
    head = [m for m in f.msgs if m.gnum != 20 or m.timestamp() < at_ts]
    tail = [m for m in f.msgs if m.gnum not in (20, 19) or m.timestamp() >= at_ts + 30]
    a, b = fit.FitFile(head, f.protocol, f.profile), fit.FitFile(tail, f.protocol, f.profile)
    a.cache = b.cache = f.cache
    # во втором куске метры начинаются с нуля, как у новой записи часов
    first = next(m.get(5) for m in tail if m.gnum == 20)
    for m in tail:
        if m.gnum == 20 and m.get(5) is not None:
            m.set(5, m.get(5) - first, UINT32, f.cache)
    return [fit.write(a), fit.write(b)]


def positions(data):
    return {m.timestamp(): (m.get(0) * SC, m.get(1) * SC, m.get(5) / 100)
            for m in fit.read(data).of(20) if m.get(0) is not None and m.get(5) is not None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ref')
    ap.add_argument('--route', default='naberezhnaya')
    ap.add_argument('--live', type=float, default=8, help='минут живого GPS в начале')
    ap.add_argument('--stride', type=float, default=1.3)
    ap.add_argument('--split', action='store_true', help='порезать на два куска')
    ap.add_argument('--save', help='сохранить заглушённый вход сюда (для сверки со старым скриптом)')
    ap.add_argument('--drift', type=float, default=0.0, help='перебор шагомера плывёт ±D по ходу')
    ap.add_argument('--alt-from', help='высоты записи из этого файла (сырой барометр часов)')
    ap.add_argument('--no-ele', action='store_true', help='без привязки по высоте')
    a = ap.parse_args()

    ref = open(a.ref, 'rb').read()
    truth = positions(ref)
    alt = altitudes(open(a.alt_from, 'rb').read()) if a.alt_from else None
    f = jam(ref, a.live * 60, a.stride, drift=a.drift, alt=alt)
    if a.split:
        ts = sorted(truth)
        fits = split(f, ts[len(ts) // 2])
    else:
        fits = [fit.write(f)]
    if a.save:
        for k, d in enumerate(fits):
            open(a.save.replace('.fit', f'_{k + 1}.fit') if len(fits) > 1 else a.save, 'wb').write(d)

    gpx, opts = routes.load(a.route)
    out, rep = engine.rebuild(fits, gpx, ele=not a.no_ele, **opts)
    got = positions(out)
    common = sorted(set(truth) & set(got))
    off = [haversine(truth[t][:2], got[t][:2]) for t in common]
    dd = [abs(truth[t][2] - got[t][2]) for t in common]
    off_s = sorted(off)
    print(f'вход: часы {rep["watch_km"]} км ×{a.stride}, живой GPS {a.live:g} мин, кусков {len(fits)}')
    print(f'выход: {rep["result_km"]} км (эталон {truth[max(truth)][2] / 1000:.2f} км), {rep["laps"]}')
    print(f'сдвиг точек от эталона, м: медиана {statistics.median(off):.0f}, 95% {off_s[int(0.95 * len(off_s))]:.0f}, '
          f'макс {max(off):.0f} (сравнено {len(common)} точек)')
    print(f'расхождение метров по ходу, м: медиана {statistics.median(dd):.0f}, макс {max(dd):.0f}')
    for w in rep['warnings']:
        print('  ⚠ ' + w)
    for w in rep['notes']:
        print('  · ' + w)


if __name__ == '__main__':
    main()
