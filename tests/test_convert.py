# -*- coding: utf-8 -*-
"""Тренировка в GPX/TCX: живой FIT -> GPX или TCX «как отдал редактор» с глушилкой
(середина без точек, в TCX — шагомер ×1.3) и паузой 5 мин -> страница (web.run).
Проверяет: файл читается, пауза — пауза, дыра от глушилки — не пауза, пульс на месте,
сдвиг точек от настоящего трека.

    py -X utf8 tests/test_convert.py [файлы.fit ...]
"""
import datetime as dt
import glob
import json
import os
import statistics
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from trek_rebuild import fit, web  # noqa: E402
from trek_rebuild.fit import SC, FIT_EPOCH  # noqa: E402
from trek_rebuild.geo import haversine  # noqa: E402
from test_foreign import gpx_from, track_of  # noqa: E402

PAUSE_S = 300


def iso(t):
    return dt.datetime.fromtimestamp(t + FIT_EPOCH, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def points(f):
    """Живые точки с пульсом и метрами; пауза PAUSE_S на 25% времени, глушилка — 40–80%."""
    rr = [m for m in f.of(20) if m.get(0) is not None and m.get(253) is not None and m.get(5) is not None]
    t0, t1 = rr[0].get(253), rr[-1].get(253)
    out, k = [], 1.0
    for m in rr:
        t = m.get(253)
        frac = (t - t0) / (t1 - t0)
        shift = PAUSE_S if frac >= 0.25 else 0
        jammed = 0.4 <= frac <= 0.8
        out.append({'ts': t + shift, 'lat': m.get(0) * SC, 'lon': m.get(1) * SC, 'hr': m.get(3),
                    'dist': m.get(5) / 100, 'jammed': jammed, 'orig_ts': t})
    return out


def to_gpx(pts):
    body = ''.join(
        f'<trkpt lat="{p["lat"]:.6f}" lon="{p["lon"]:.6f}"><time>{iso(p["ts"])}</time>'
        + (f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{p["hr"]}</gpxtpx:hr>'
           f'</gpxtpx:TrackPointExtension></extensions>' if p['hr'] else '') + '</trkpt>'
        for p in pts if not p['jammed'])
    return ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
            f'<trk><type>running</type><trkseg>{body}</trkseg></trk></gpx>').encode()


def to_tcx(pts):
    rows, carry, prev = [], 0.0, None
    for p in pts:
        k = 1.3 if p['jammed'] else 1.0
        carry = carry + (p['dist'] - prev) * k if prev is not None else p['dist']
        prev = p['dist']
        pos = '' if p['jammed'] else (f'<Position><LatitudeDegrees>{p["lat"]:.6f}</LatitudeDegrees>'
                                      f'<LongitudeDegrees>{p["lon"]:.6f}</LongitudeDegrees></Position>')
        hr = f'<HeartRateBpm><Value>{p["hr"]}</Value></HeartRateBpm>' if p['hr'] else ''
        rows.append(f'<Trackpoint><Time>{iso(p["ts"])}</Time>{pos}<DistanceMeters>{carry:.1f}</DistanceMeters>{hr}</Trackpoint>')
    return ('<?xml version="1.0"?><TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
            '<Activities><Activity Sport="Running"><Lap><Track>' + ''.join(rows)
            + '</Track></Lap></Activity></Activities></TrainingCenterDatabase>').encode()


def check(path):
    name = os.path.basename(path)
    try:
        f = fit.read(open(path, 'rb').read())
    except fit.FitError as e:
        return print(f'ПРОПУСК {name}: {e}')
    tr = track_of(f)
    if len(tr) < 200 or f.of(18) and f.of(18)[0].get(5) not in (1, 11, 17, None):
        return print(f'ПРОПУСК {name}: не беговая или нет трека')
    pts = points(f)
    route = gpx_from(tr)
    truth = {p['ts']: (p['lat'], p['lon']) for p in pts}
    real_km = tr[-1][2] / 100000
    for kind, data in (('gpx', to_gpx(pts)), ('tcx', to_tcx(pts))):
        js, out = web.run([(f'x.{kind}', data)], 'r.gpx', route, json.dumps({'route': '', 'distance': 'track'}))
        rep = json.loads(js)
        if not rep['ok']:
            print(f'ОШИБКА {name} [{kind}]: {rep["error"]}')
            continue
        g = fit.read(out)
        got = {m.get(253): (m.get(0) * SC, m.get(1) * SC) for m in g.of(20) if m.get(0) is not None}
        off = sorted(haversine(truth[t], got[t]) for t in set(truth) & set(got))
        hr = sum(m.get(3) is not None for m in g.of(20))
        el = pts[-1]['ts'] - pts[0]['ts']
        h, mnt, s = map(int, rep['timer'].split(':'))
        timer = h * 3600 + mnt * 60 + s
        pause_ok = abs((el - timer) - PAUSE_S) <= 15
        print(f'{"OK " if pause_ok else "ПАУЗА?"} {name} [{kind}]: {rep["result_km"]} км (по GPS {real_km:.2f}), '
              f'таймер {rep["timer"]} при общем {el // 60} мин, пульс у {hr} точек, сдвиг м: медиана '
              f'{statistics.median(off):.0f} / 95% {off[int(.95 * len(off))]:.0f} / макс {off[-1]:.0f}')
        for w in rep['warnings']:
            print('   ⚠ ' + w[:200])


def main():
    for p in sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, 'tests', 'samples', '*.fit'))):
        check(p)


if __name__ == '__main__':
    main()
