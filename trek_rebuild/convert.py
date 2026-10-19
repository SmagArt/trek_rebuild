# -*- coding: utf-8 -*-
"""Тренировка в GPX или TCX -> FIT с нуля, дальше её разбирает движок как обычно.

Для тех, у кого нет FIT: файл прогнали через gpx.studio или другой редактор,
часы отдают только GPX/TCX. Берётся то, что в файле есть: время, координаты,
высота, пульс, каденс, мощность, а из TCX ещё дистанция и скорость часов.
Кругов и полей датчиков сверх этого нет — их нет и в исходнике.
"""
import datetime as dt
import xml.etree.ElementTree as ET

from . import fit
from .fit import SC, FIT_EPOCH, UINT8, UINT16, SINT32, UINT32, ENUM
from .geo import haversine

SPORTS = {'running': 1, 'run': 1, '9': 1, 'cycling': 2, 'biking': 2, 'ride': 2, '1': 2,
          'walking': 11, 'walk': 11, 'hiking': 17, 'hike': 17}
PAUSE_GAP_S = 10     # дыра между точками длиннее — возможно, автопауза записи
PAUSE_MOVE_M = 100   # ...если края дыры ближе этого
PAUSE_SPEED = 0.5    # ...и по прямой медленнее, м/с
PAUSE_MAX_S = 600    # дыра длиннее — глушилка (на круге края сходятся), а не стоянка


class ConvertError(ValueError):
    """Файл не тренировка — текст для человека."""


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _kids(el, name):
    return [c for c in el if _local(c.tag) == name]


def _find(el, *path):
    for name in path:
        nxt = _kids(el, name) if el is not None else []
        el = nxt[0] if nxt else None
    return el


def _deep(el, names):
    """Первое значение из любого вложенного тега с одним из имён (расширения точек)."""
    for c in el.iter():
        if _local(c.tag).lower() in names and c.text and c.text.strip():
            try:
                return float(c.text)
            except ValueError:
                return None
    return None


def _time(text):
    try:
        t = dt.datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp()) - FIT_EPOCH


def _num(el):
    try:
        return float(el.text) if el is not None and el.text else None
    except ValueError:
        return None


def is_activity_gpx(data):
    """GPX с временем у точек — записанная тренировка, без времени — нарисованный трек."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False
    pts = [p for p in root.iter() if _local(p.tag) == 'trkpt']
    timed = sum(1 for p in pts if _find(p, 'time') is not None)
    return len(pts) >= 10 and timed >= 0.9 * len(pts)


def read_points(data, name):
    """-> (точки [{ts, lat, lon, ele, hr, cad, pwr, dist, sp}], код спорта)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ConvertError(f'{name}: файл не читается ({e})') from None
    kind = _local(root.tag)
    pts, sport = [], 1
    if kind == 'gpx':
        typ = next((c.text for c in root.iter() if _local(c.tag) == 'type' and c.text), '')
        sport = SPORTS.get(typ.strip().lower(), 1)
        for p in root.iter():
            if _local(p.tag) != 'trkpt':
                continue
            ts = _time(_find(p, 'time').text) if _find(p, 'time') is not None else None
            if ts is None:
                continue
            pts.append({'ts': ts, 'lat': float(p.attrib['lat']), 'lon': float(p.attrib['lon']),
                        'ele': _num(_find(p, 'ele')), 'hr': _deep(p, {'hr', 'heartrate'}),
                        'cad': _deep(p, {'cad', 'cadence', 'runcadence'}),
                        'pwr': _deep(p, {'power', 'watts'}), 'dist': None, 'sp': None})
    elif kind == 'TrainingCenterDatabase':
        act = next((a for a in root.iter() if _local(a.tag) == 'Activity'), None)
        if act is not None:
            sport = SPORTS.get(act.attrib.get('Sport', '').strip().lower(), 1)
        for p in root.iter():
            if _local(p.tag) != 'Trackpoint':
                continue
            ts = _time(_find(p, 'Time').text) if _find(p, 'Time') is not None else None
            if ts is None:
                continue
            pos = _find(p, 'Position')
            ext = _find(p, 'Extensions')
            pts.append({'ts': ts,
                        'lat': _num(_find(pos, 'LatitudeDegrees')), 'lon': _num(_find(pos, 'LongitudeDegrees')),
                        'ele': _num(_find(p, 'AltitudeMeters')), 'hr': _num(_find(p, 'HeartRateBpm', 'Value')),
                        'cad': _num(_find(p, 'Cadence')) or (_deep(ext, {'runcadence'}) if ext is not None else None),
                        'pwr': _deep(ext, {'watts'}) if ext is not None else None,
                        'dist': _num(_find(p, 'DistanceMeters')),
                        'sp': _deep(ext, {'speed'}) if ext is not None else None})
    else:
        raise ConvertError(f'{name}: это не GPX и не TCX')
    pts.sort(key=lambda q: q['ts'])
    out = []
    for q in pts:                                   # одна точка на секунду
        if out and q['ts'] <= out[-1]['ts']:
            continue
        out.append(q)
    if len(out) < 10:
        raise ConvertError(f'{name}: в файле нет записи тренировки (точек со временем меньше 10) — '
                           f'похоже, это нарисованный трек, его место в «Свой трек»')
    return out, sport


def _standing(a, b):
    """Между точками дыры человек стоял: дистанция часов не росла, а без неё —
    дыра не длиннее PAUSE_MAX_S, края ближе PAUSE_MOVE_M и медленнее PAUSE_SPEED.
    Ошибка тут бьёт только по таймеру: при живом GPS по краям якоря и так держат
    точку на месте."""
    if a['dist'] is not None and b['dist'] is not None:
        return b['dist'] - a['dist'] < PAUSE_MOVE_M
    if b['ts'] - a['ts'] > PAUSE_MAX_S:
        return False
    if None in (a['lat'], b['lat']):
        return True
    d = haversine((a['lat'], a['lon']), (b['lat'], b['lon']))
    return d < min(PAUSE_MOVE_M, PAUSE_SPEED * (b['ts'] - a['ts']))


def to_fit(data, name):
    """Байты GPX/TCX -> байты FIT: file_id, старт, точки, стоп, круг, сессия, активность."""
    pts, sport = read_points(data, name)
    f = fit.FitFile([])
    c = f.cache
    t0, t1 = pts[0]['ts'], pts[-1]['ts']
    msgs = [fit.make(c, 0, 0, [(0, ENUM, 4), (1, UINT16, 255), (2, UINT16, 0),
                               (3, UINT32, 1), (4, UINT32, t0)]),
            fit.make(c, 2, 21, [(253, UINT32, t0), (0, ENUM, 0), (1, ENUM, 0)])]
    paused = 0
    for k, q in enumerate(pts):
        # дыра во времени, а человек на месте — автопауза записи: стоп и старт
        # таймера, как у часов. Сместился — это глушилка съела точки, бег идёт
        if k and q['ts'] - pts[k - 1]['ts'] > PAUSE_GAP_S and _standing(pts[k - 1], q):
            msgs += [fit.make(c, 2, 21, [(253, UINT32, pts[k - 1]['ts']), (0, ENUM, 0), (1, ENUM, 4)]),
                     fit.make(c, 2, 21, [(253, UINT32, q['ts']), (0, ENUM, 0), (1, ENUM, 0)])]
            paused += q['ts'] - pts[k - 1]['ts']
        items = [(253, UINT32, q['ts'])]
        if q['lat'] is not None and q['lon'] is not None:
            items += [(0, SINT32, q['lat'] / SC), (1, SINT32, q['lon'] / SC)]
        if q['ele'] is not None:
            items.append((2, UINT16, (q['ele'] + 500) * 5))
        if q['hr'] is not None:
            items.append((3, UINT8, q['hr']))
        if q['cad'] is not None:
            items.append((4, UINT8, q['cad']))
        if q['dist'] is not None:
            items.append((5, UINT32, q['dist'] * 100))
        if q['sp'] is not None:
            items.append((6, UINT16, q['sp'] * 1000))
        if q['pwr'] is not None:
            items.append((7, UINT16, q['pwr']))
        msgs.append(fit.make(c, 1, 20, items))
    el, tm = (t1 - t0) * 1000, (t1 - t0 - paused) * 1000
    msgs += [fit.make(c, 2, 21, [(253, UINT32, t1), (0, ENUM, 0), (1, ENUM, 4)]),
             fit.make(c, 3, 19, [(253, UINT32, t1), (0, ENUM, 9), (1, ENUM, 1), (2, UINT32, t0),
                                 (7, UINT32, el), (8, UINT32, tm), (9, UINT32, 0), (24, ENUM, 7)]),
             fit.make(c, 4, 18, [(253, UINT32, t1), (0, ENUM, 8), (1, ENUM, 1), (2, UINT32, t0),
                                 (5, ENUM, sport), (6, ENUM, 0), (7, UINT32, el), (8, UINT32, tm),
                                 (9, UINT32, 0), (25, UINT16, 0), (26, UINT16, 1)]),
             fit.make(c, 5, 34, [(253, UINT32, t1), (0, UINT32, tm), (1, UINT16, 1),
                                 (2, ENUM, 0), (3, ENUM, 26), (4, ENUM, 1)])]
    f.msgs = msgs
    return fit.write(f)
