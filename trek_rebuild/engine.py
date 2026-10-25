# -*- coding: utf-8 -*-
"""Перенос тренировки на нарисованный трек: один или несколько кусков FIT + GPX.

Происхождение — sport/bat/rebuild_track.py (личный пайплайн). Алгоритм тот же,
отличия публичной версии:
- файл не собирается заново, а перекодируется (fit.py): все поля чужих часов,
  датчиков и Stryd остаются, меняются координаты, дистанция, скорость точек и
  итоги кругов/тренировки;
- нет Zepp/Strava/ZeppBridge, подписи устройства и синтетической автопаузы —
  паузы берутся только те, что записали часы;
- круги часов сохраняются; перенарезаются по километру только авто-круги,
  которые часы резали по своей неверной дистанции;
- вход и выход — байты, без файловой системы (работает в браузере через Pyodide).

    rebuild(fits=[bytes, ...], gpx=bytes, distance='track'|'sensor', ...) -> (bytes, report)

--distance track   метры = длина нарисованной линии; поток часов задаёт только, как они
                   распределены по времени (стоянки остаются стоянками). Режим под глушилку.
--distance sensor  метры из потока distance часов как есть (Stryd, датчик колеса);
                   линия даёт только карту.
--distance time    метры = длина линии, распределены по времени движения: метрам
                   часов верить нельзя (велокомпьютер без датчика мерил по GPS).

Форма всегда из GPX. Якоря — секунды, где живой фикс часов лёг на линию (<15 м):
по времени точки GPX, если время в треке есть, иначе поиском вперёд по линии.
Между якорями точка ведётся по доле метров часов. Замёрзший фикс (координата стоит,
а метры идут) и хвост схождения фильтра якорями не считаются.
"""
import bisect
import datetime as dt
import xml.etree.ElementTree as ET

from . import fit
from .fit import SC, FIT_EPOCH, UINT8, UINT16, SINT32, UINT32, ENUM
from .geo import haversine, at_distance, seg_proj, ascent_descent

ANCHOR_M = 15.0      # фикс ближе к линии — якорь
SPACE_STEP = 5       # поиск якорей по месту: каждая N-я запись
FROZEN_S = 10        # координата стоит столько секунд...
FROZEN_MOVE_M = 30   # ...а метры часов за это время прибавились — фикс замёрз
CONVERGE_M = 20      # после заморозки фильтр сходится прыжками больше этого
SPIKE_M = 150        # одиночный выброс туда-обратно в GPX
WIN_REPORT_S = 120   # окна без якорей длиннее — в журнал
MAX_DT = 5           # сек: обрезка dt в интеграле скорости без потока distance

RATIO_WARN = 0.05    # датчик/линия: перерисовать
RATIO_NOTE = 0.03    # проверить глазами (только на окнах от 2 км)
RATIO_MIN_M = 1000
JOIN_M = 50
STRAIGHT_M = 500
JUMP_M = 25

# Потолок скорости за секунду по виду спорта (FIT sport), м/с. Ступенька быстрее —
# скачок якоря или прыжок после паузы.
SPEED_CAP = {1: 7.0, 11: 7.0, 17: 7.0, 2: 25.0}   # бег, ходьба, поход, вело
SPEED_CAP_DEFAULT = 15.0
DESPIKE_PAUSE_S = 60   # через паузу короче — раскладываем, длиннее — граница окна

# keep_live: где GPS живой, трек остаётся свой, трассой латаются только дыры
LIVE_MIN_S = 60        # кусок живого GPS короче — не доверяем
LIVE_JUMP_M = 30       # прыжок фикса сверх потолка скорости рвёт кусок
LIVE_NEAR_M = 100      # медиана дальше от трассы — подмена сигнала, не живой GPS
LIVE_BLEND_S = 120     # за столько секунд в дыре гаснет поправка от края живого куска

# ele: привязка по высоте барометра на холмистой трассе
ELE_GRID_M = 5.0       # шаг сетки по трассе
ELE_DT_S = 5           # шаг по времени движения (длинные записи — реже, не больше ~800 шагов)
ELE_RELIEF_M = 15      # перепад трассы меньше — высота ничего не скажет
ELE_SIGMA_H = 3.0      # м: разброс барометра против высоты трассы
ELE_BAND_M = (40, 0.15, 400)   # полоса поиска: 40 м у якоря + 15 % пути от него, не шире 400 м
ELE_GRADE = 0.04       # якорь — только где трасса идёт в горку или под горку
ELE_ANCHOR_S = 20      # не чаще раза в столько секунд
ELE_GAIN = 0.75        # высота должна сойтись хотя бы на четверть лучше, чем по шагомеру
ELE_RMS_MAX = 6.0      # и не хуже этого, иначе барометр или трасса не те

TIMER_START = 0
TIMER_STOPS = {1, 4, 8, 9}        # stop, stop_all, stop_disable, stop_disable_all

# глобальные номера сообщений
FILE_ID, SESSION, LAP, RECORD, EVENT, ACTIVITY = 0, 18, 19, 20, 21, 34
FILE_CREATOR, DEV_ID, FIELD_DESC = 49, 207, 206

AUTO_LAP_TARGETS = (1000.0, 1609.344)
NO_LOCAL = 3 * 3600                # пояс по умолчанию, если в файле нет local_timestamp


class RebuildError(ValueError):
    """Ошибка входных данных — текст для человека."""


# ── мелочи ──────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, offset):
        self.offset = offset

    def hm(self, x):
        return dt.datetime.fromtimestamp(x + FIT_EPOCH + self.offset, dt.timezone.utc).strftime('%H:%M:%S')

    def date(self, x):
        return dt.datetime.fromtimestamp(x + FIT_EPOCH + self.offset, dt.timezone.utc).strftime('%d.%m.%Y')


def hms(x):
    x = int(round(x))
    return f'{x // 3600}:{x % 3600 // 60:02d}:{x % 60:02d}'


def paused_spans(events):
    """[(stop_ts, start_ts)] — куски, где часы стояли на паузе."""
    spans, stop = [], None
    for e in events:
        if e['type'] == TIMER_START:
            if stop is not None:
                spans.append((stop, e['ts']))
                stop = None
        elif e['type'] in TIMER_STOPS and stop is None:
            stop = e['ts']
    return spans


def paused_between(spans, a, b):
    return sum(max(0, min(b, e) - max(a, s)) for s, e in spans)


def despike(ts, xs, spans, cap):
    """Гасит ступеньки быстрее cap м/с: метры окна вокруг ступеньки раскладываются
    ровно по секундам хода, окно растёт, пока средняя не уйдёт под потолок.
    Сумма и концы окна не меняются, в паузах метры стоят. Через паузу длиннее
    DESPIKE_PAUSE_S окно не идёт — там ступенька может быть настоящей (переезд).
    Возвращает [(ts, ступенька м, окно с)] того, что разложено, и что не вышло."""
    n = len(xs)
    in_pause = [any(s <= t < e for s, e in spans) for t in ts]
    hard = set()
    for s, e in spans:
        if e - s > DESPIKE_PAUSE_S:
            hard.update(i for i in range(n) if s <= ts[i] <= e)
    fixed, left = [], []
    i = 1
    while i < n:
        dt_ = ts[i] - ts[i - 1]
        step = xs[i] - xs[i - 1]
        if dt_ <= 0 or step <= cap * dt_:
            i += 1
            continue
        # цель — не потолок, а полуторный местный темп: иначе +38 м ляжет на 12 с
        # по 20 км/ч, и выброс просто станет шире
        near = sorted(xs[k] - xs[k - 1] for k in range(max(1, i - 60), min(n, i + 60))
                      if k != i and not in_pause[k] and ts[k] - ts[k - 1] == 1)
        target = min(cap * 0.8, max(1.5 * near[len(near) // 2], 1.0)) if near else cap * 0.8
        a, b, ok = i - 1, i, False
        for _ in range(300):
            mov = [k for k in range(a + 1, b + 1) if not in_pause[k]]
            if mov and (xs[b] - xs[a]) / sum(ts[k] - ts[k - 1] for k in mov) <= target:
                ok = True
                break
            grow = False
            if a > 0 and a - 1 not in hard:
                a -= 1; grow = True
            if b < n - 1 and b + 1 not in hard:
                b += 1; grow = True
            if not grow:
                break
        if not ok:
            left.append((ts[i], step))
            i += 1
            continue
        fixed.append((ts[i], step, ts[b] - ts[a]))
        mov = [k for k in range(a + 1, b + 1) if not in_pause[k]]
        secs = sum(ts[k] - ts[k - 1] for k in mov)
        total = xs[b] - xs[a]
        cur = xs[a]
        for k in range(a + 1, b + 1):
            if not in_pause[k]:
                cur += total * (ts[k] - ts[k - 1]) / secs
            xs[k] = min(cur, xs[b])
        i = b + 1
    return fixed, left


def pause_aware_speed(recs, spans):
    """Скорость ±2 записи по метрам, но окно не заходит в паузу, а в паузе — 0."""
    n = len(recs)
    pz = [any(s <= r['ts'] < e for s, e in spans) for r in recs]
    for x, r in enumerate(recs):
        if pz[x]:
            r['v'] = 0.0
            continue
        i = x
        while i > max(0, x - 2) and not pz[i - 1]:
            i -= 1
        j = x
        while j < min(n - 1, x + 2) and not pz[j + 1]:
            j += 1
        span = recs[j]['ts'] - recs[i]['ts']
        r['v'] = (recs[j]['dist'] - recs[i]['dist']) / span if j > i and span > 0 else 0.0


# ── чтение FIT ──────────────────────────────────────────────────────────────

def _alt(m):
    a = m.get(78)
    if a is not None:
        return a / 5 - 500
    a = m.get(2)
    return a / 5 - 500 if a is not None else None


def load_piece(f, idx, ignore_gps):
    recs, by_ts = [], {}
    for m in f.of(RECORD):
        t = m.timestamp()
        if t is None or t in by_ts:
            continue
        lat, lon = m.get(0), m.get(1)
        fix = not ignore_gps and lat is not None and lon is not None and not (lat == 0 and lon == 0)
        sp = m.get(73)
        sp = sp / 1000 if sp is not None else (m.get(6) / 1000 if m.get(6) is not None else 0.0)
        d = m.get(5)
        r = {'ts': t, 'lat': lat * SC if fix else None, 'lon': lon * SC if fix else None,
             'raw': d / 100 if d is not None else None, 'sp': sp, 'hr': m.get(3), 'cad': m.get(4),
             'alt': _alt(m), 'piece': idx}
        by_ts[t] = r
        recs.append(r)
    if not recs:
        raise RebuildError(f'Файл №{idx + 1}: в нём нет точек записи — это не тренировка?')
    recs.sort(key=lambda r: r['ts'])

    # Метры часов: поток distance, а без него — интеграл скорости с обрезкой dt
    has_dist = sum(r['raw'] is not None for r in recs) >= 0.9 * len(recs)
    has_speed = any(r['sp'] for r in recs)
    carry, prev_t = 0.0, recs[0]['ts']
    for r in recs:
        if has_dist:
            carry = carry if r['raw'] is None else max(carry, r['raw'])
        else:
            carry += r['sp'] * min(max(r['ts'] - prev_t, 0), MAX_DT)
        r['d'] = carry
        prev_t = r['ts']

    s = next(iter(f.of(SESSION)), None)
    # Без потока distance интеграл скорости расходится с итогом часов (Zepp: 20.5 против 26.0) —
    # подгоняем под дистанцию сессии, её часы и показывают
    total = s.get(9) / 100 if s is not None and s.get(9) else 0
    scaled = not has_dist and total > 0 and carry > 0
    if scaled:
        k = total / carry
        for r in recs:
            r['d'] *= k

    events = sorted(({'ts': m.timestamp(), 'type': m.get(1)}
                     for m in f.of(EVENT) if m.get(0) == 0 and m.timestamp() is not None),
                    key=lambda e: e['ts'])
    if not events or events[0]['type'] != TIMER_START:
        events.insert(0, {'ts': recs[0]['ts'], 'type': TIMER_START})
    if events[-1]['type'] == TIMER_START:
        events.append({'ts': recs[-1]['ts'], 'type': 4})

    a = next(iter(f.of(ACTIVITY)), None)
    offset = None
    if a is not None and a.get(5) is not None and a.timestamp() is not None:
        offset = a.get(5) - a.timestamp()
        if abs(offset) > 14 * 3600:
            offset = None
    return {'idx': idx, 'file': f, 'recs': recs, 'events': events, 'has_dist': has_dist or scaled,
            'has_speed': has_speed, 'offset': offset,
            'sport': s.get(5) if s is not None and s.get(5) is not None else 0}


def merge(pieces):
    """Куски подряд: метры сквозные, перекрытие по времени отбрасывается у позднего."""
    recs, events, off = [], [], 0.0
    for p in pieces:
        for r in p['recs']:
            r['d'] += off
        off = p['recs'][-1]['d']
        for r in p['recs']:
            if recs and r['ts'] <= recs[-1]['ts']:
                continue
            recs.append(r)
        events += p['events']
    events.sort(key=lambda e: e['ts'])
    return recs, events


# ── линия ───────────────────────────────────────────────────────────────────

def read_gpx(data):
    """Сегменты трека [(lat, lon, ele, t)], t — секунды FIT или None.
    Берутся трек (trkseg), а если его нет — маршрут (rte)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise RebuildError(f'GPX не читается: {e}') from None
    ns = root.tag.split('}')[0] + '}' if root.tag.startswith('{') else ''
    segs = []
    groups = [(seg, 'trkpt') for seg in root.iter(ns + 'trkseg')] or \
             [(rte, 'rtept') for rte in root.iter(ns + 'rte')]
    for seg, tag in groups:
        pts = []
        for p in seg.iter(ns + tag):
            te, el = p.find(ns + 'time'), p.find(ns + 'ele')
            t = None
            if te is not None and te.text:
                try:
                    t = int(dt.datetime.fromisoformat(te.text.strip().replace('Z', '+00:00')).timestamp()) - FIT_EPOCH
                except ValueError:
                    t = None
            try:
                ele = float(el.text) if el is not None and el.text else None
            except ValueError:
                ele = None
            pts.append((float(p.attrib['lat']), float(p.attrib['lon']), ele, t))
        if pts:
            segs.append(pts)
    if not segs or sum(len(s) for s in segs) < 2:
        raise RebuildError('В GPX нет точек трека.')
    timed = all(p[3] is not None for s in segs for p in s)
    return segs, timed


def reverse_segs(segs):
    """Трек в обратную сторону; время выкидывается — оно теперь бессмысленно."""
    return [[(p[0], p[1], p[2], None) for p in reversed(s)] for s in reversed(segs)]


def build_line(segs, keep_spikes=False):
    joins = [(a[-1], b[0], haversine(a[-1], b[0])) for a, b in zip(segs, segs[1:])]
    poly = []
    for s in segs:
        for p in s:
            if poly and haversine(poly[-1], p) < 0.5:
                continue
            poly.append(p)
    removed, changed = [], not keep_spikes
    while changed and len(poly) > 2:
        changed = False
        out = [poly[0]]
        for i in range(1, len(poly) - 1):
            a, b, c = out[-1], poly[i], poly[i + 1]
            dab, dbc, dac = haversine(a, b), haversine(b, c), haversine(a, c)
            if dab > SPIKE_M and dbc > SPIKE_M and dac < 0.3 * min(dab, dbc):
                removed.append((b, round(dab)))
                changed = True
                continue
            out.append(b)
        out.append(poly[-1])
        poly = out
    cum = [0.0]
    for a, b in zip(poly, poly[1:]):
        cum.append(cum[-1] + haversine(a, b))
    return poly, cum, joins, removed


# ── якоря ───────────────────────────────────────────────────────────────────

def frozen_mask(recs):
    """Записи без доверия к координатам: нет фикса, фикс замёрз на ходу, хвост схождения."""
    n = len(recs)
    bad = [r['lat'] is None for r in recs]
    spans, i = [], 0
    while i < n:
        if recs[i]['lat'] is None:
            i += 1
            continue
        j = i
        while j + 1 < n and recs[j + 1]['lat'] == recs[i]['lat'] and recs[j + 1]['lon'] == recs[i]['lon']:
            j += 1
        if recs[j]['ts'] - recs[i]['ts'] >= FROZEN_S and recs[j]['d'] - recs[i]['d'] > FROZEN_MOVE_M:
            k = j + 1
            while (k + 1 < n and recs[k]['lat'] is not None and recs[k + 1]['lat'] is not None and
                   haversine((recs[k]['lat'], recs[k]['lon']),
                             (recs[k + 1]['lat'], recs[k + 1]['lon'])) > CONVERGE_M):
                k += 1
            k = min(k + 3, n - 1)
            spans.append((i, k))
            for x in range(i, k + 1):
                bad[x] = True
        i = j + 1
    return bad, spans


def anchors_by_time(recs, bad, poly, cum):
    by_sec = {}
    for idx, p in enumerate(poly):
        by_sec.setdefault(p[3], []).append(idx)
    anchors = [(0, 0.0)]
    for x, r in enumerate(recs):
        if bad[x]:
            continue
        best = None
        for key in (r['ts'], r['ts'] - 1):
            for idx in by_sec.get(key, ()):
                d = haversine(poly[idx], (r['lat'], r['lon']))
                if d < ANCHOR_M and (best is None or d < best[1]):
                    best = (idx, d)
        if best and cum[best[0]] >= anchors[-1][1] and x > anchors[-1][0]:
            anchors.append((x, cum[best[0]]))
    return anchors


def anchors_by_space(recs, bad, poly, cum):
    """Без времени в GPX: фикс ищется на линии вперёд от прошлого якоря, в окне,
    которое растёт с метрами часов (после долгой заморозки фикс может быть далеко).

    Из проходов, где фикс лёг на линию, берётся ближайший к ожидаемому месту:
    прошлый якорь + метры часов × (длина линии / метры часов за всю запись).
    Раньше брался первый по ходу — на трассе туда-обратно редкий фикс с обратного
    пути цеплялся к прямому (тест fenix2, GPS раз в 15 мин: сдвиг до 1 км).
    При плотных якорях оба правила совпадают."""
    total = recs[-1]['d'] - recs[0]['d']
    scale = cum[-1] / total if total > 0 else 1.0
    anchors = [(0, 0.0)]
    for x in range(0, len(recs), SPACE_STEP):
        if bad[x]:
            continue
        lx, lL = anchors[-1]
        adv = recs[x]['d'] - recs[lx]['d']
        lo = max(0, bisect.bisect_right(cum, lL) - 1)
        hi = min(len(poly) - 1, bisect.bisect_right(cum, lL + adv * max(1.5, 1.5 * scale) + 500))
        q = (recs[x]['lat'], recs[x]['lon'])
        cands = []
        for i in range(lo, hi):
            d, t = seg_proj(poly[i], poly[i + 1], q)
            if d < ANCHOR_M:
                cands.append((cum[i] + t * (cum[i + 1] - cum[i]), d))
        if not cands:
            continue
        expect = lL + adv * scale
        # проходы ближе 100 м друг к другу — один и тот же; в нём — ближайший к фиксу
        best = min(cands, key=lambda c: abs(c[0] - expect))
        L = min((c for c in cands if abs(c[0] - best[0]) <= 100), key=lambda c: c[1])[0]
        if x > lx:
            anchors.append((x, max(L, lL)))
    return anchors


def live_stretches(recs, bad, poly, cap):
    """[(i, j)] — куски живого GPS: фикс есть и не замёрз, без прыжков быстрее потолка,
    не короче LIVE_MIN_S и по медиане не дальше LIVE_NEAR_M от трассы."""
    out, n, i = [], len(recs), 0
    while i < n:
        if bad[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not bad[j + 1]:
            a, b = recs[j], recs[j + 1]
            if haversine((a['lat'], a['lon']), (b['lat'], b['lon'])) > \
                    cap * max(b['ts'] - a['ts'], 1) + LIVE_JUMP_M:
                break
            j += 1
        if recs[j]['ts'] - recs[i]['ts'] >= LIVE_MIN_S:
            ds = sorted(min(seg_proj(poly[k], poly[k + 1], (recs[x]['lat'], recs[x]['lon']))[0]
                            for k in range(len(poly) - 1))
                        for x in range(i, j + 1, max(1, (j - i) // 30)))
            if ds[len(ds) // 2] <= LIVE_NEAR_M:
                out.append((i, j))
        i = j + 1
    return out


# ── круги ───────────────────────────────────────────────────────────────────

def auto_lap_step(laps_in):
    """Шаг авто-кругов часов в метрах или None, если круги ручные/по времени.
    Авто-круг часы режут по своей дистанции — после правки она другая, и круг
    «1 км» становится 770 м. Такие круги перенарезаются по исправленным метрам."""
    if len(laps_in) < 2:
        return None
    body = laps_in[:-1]
    trig = [m.get(24) for m in body]
    if any(t is not None and t != 2 for t in trig):        # 2 = distance
        return None
    dists = sorted((m.get(9) or 0) / 100 for m in body)
    med = dists[len(dists) // 2]
    for target in AUTO_LAP_TARGETS:
        if abs(med - target) <= 0.02 * target and all(abs(d - target) <= 0.03 * target for d in dists):
            return target
    return None


def cut_laps(recs, step, spans):
    laps, mark, prev_x, prev_cum = [], step, 0, recs[0]['dist']
    for x, r in enumerate(recs):
        last = x == len(recs) - 1
        if r['dist'] - recs[0]['dist'] >= mark or last:
            laps.append((prev_x, x))
            prev_x, prev_cum = x, r['dist']
            while mark <= r['dist'] - recs[0]['dist']:
                mark += step
    out = []
    for a, b in laps:
        if recs[b]['dist'] - recs[a]['dist'] > 1.0 and recs[b]['ts'] > recs[a]['ts']:
            out.append((a, b))
    return out


def window_stats(recs, a, b, spans):
    seg = recs[a:b + 1]
    hr = [r['hr'] for r in seg if r['hr']]
    cad = [r['cad'] for r in seg if r['cad']]
    start, end = recs[a]['ts'], recs[b]['ts']
    pause = paused_between(spans, start, end)
    return {'start': start, 'end': end, 'elapsed': end - start, 'timer': end - start - pause,
            'dist': recs[b]['dist'] - recs[a]['dist'],
            'max_v': max((r['v'] for r in seg), default=0.0),
            'avg_hr': round(sum(hr) / len(hr)) if hr else None, 'max_hr': max(hr) if hr else None,
            'avg_cad': round(sum(cad) / len(cad)) if cad else None,
            'p0': (recs[a]['plat'], recs[a]['plon']), 'p1': (recs[b]['plat'], recs[b]['plon'])}


def _rec_index(recs, t):
    """Индекс последней записи с ts <= t."""
    lo, hi = 0, len(recs) - 1
    if t <= recs[0]['ts']:
        return 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if recs[mid]['ts'] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def semi(x):
    return round(x / SC)


def set_speed_pair(m, num16, num32, val, cache, add16=True):
    """avg/max_speed: uint16 и enhanced uint32. Enhanced — только если он в сообщении был."""
    if m.d.index(num16) >= 0 or add16:
        m.set(num16, val * 1000, UINT16, cache)
    if m.d.index(num32) >= 0:
        m.set(num32, val * 1000, UINT32, cache)


# ── главный проход ──────────────────────────────────────────────────────────

def rebuild(fits, gpx, distance='track', reverse=False, keep_spikes=False, ignore_gps=False,
            anchors='auto', order=None, laps='auto', timeless=False, preset=False, keep_live=False,
            ele=True):
    """fits — байты кусков FIT (порядок неважен), gpx — байты трека.
    keep_live — где GPS часов живой, оставить его трек, трассой залатать только дыры.
    ele — на холмистой трассе с высотами уточнять место по барометру часов.
    reverse — трек в обратную сторону; timeless — не верить времени точек GPX
    (встроенные трассы, нарисованные в редакторе); preset — трек проверенный,
    замечания к его рисовке не нужны; laps: auto — авто-круги
    перенарезать по км, остальные сохранить; keep; km.
    Возвращает (байты FIT, отчёт). Ошибки входа — RebuildError с текстом для человека."""
    if distance not in ('track', 'sensor', 'time'):
        raise ValueError(distance)
    log, warns, notes = [], [], []

    files = []
    for i, data in enumerate(fits):
        try:
            f = fit.read(data, src=i)
        except fit.FitError as e:
            raise RebuildError(f'Файл №{i + 1}: {e}') from None
        for n in f.notes:
            notes.append(f'файл №{i + 1}: {n}')
        files.append(f)
    pieces = sorted((load_piece(f, i, ignore_gps) for i, f in enumerate(files)),
                    key=lambda p: p['recs'][0]['ts'])
    clock = Clock(next((p['offset'] for p in pieces if p['offset'] is not None), NO_LOCAL))
    hm = clock.hm

    if distance == 'sensor' and not all(p['has_dist'] for p in pieces):
        miss = [f'№{p["idx"] + 1}' for p in pieces if not p['has_dist']]
        raise RebuildError(f'В файле {", ".join(miss)} нет дистанции по точкам — режим «дистанция '
                           f'по часам» невозможен, выбери «дистанция по треку».')
    for p in pieces:
        r = p['recs']
        if not p['has_dist'] and not p['has_speed']:
            notes.append(f'файл №{p["idx"] + 1}: ни дистанции, ни скорости по точкам — '
                         f'метры по треку разложатся равномерно по времени')
        log.append(f'Файл №{p["idx"] + 1}: {hm(r[0]["ts"])}–{hm(r[-1]["ts"])}, '
                   f'часы намеряли {r[-1]["d"] / 1000:.2f} км, '
                   f'точек с GPS {sum(x["lat"] is not None for x in r)} из {len(r)}')
    for p, q in zip(pieces, pieces[1:]):
        gap = q['recs'][0]['ts'] - p['recs'][-1]['ts']
        if gap < 0:
            warns.append(f'файлы перекрываются по времени на {-gap} с ({hm(q["recs"][0]["ts"])}) — '
                         f'лишние секунды второго отброшены')
    watch_m = sum(p['recs'][-1]['d'] for p in pieces)
    recs, events = merge(pieces)
    n = len(recs)
    if distance == 'time' or recs[-1]['d'] <= recs[0]['d']:
        # Ход меряем временем движения: метров нет (GPX из редактора) или им нельзя
        # верить (time — велокомпьютер без датчика скорости мерил по глушёному GPS).
        # Без этого после дыры поиск якорей не знает, как далеко человек ушёл
        sp, acc = paused_spans(events), 0.0
        for a, b in zip(recs, recs[1:]):
            acc += (b['ts'] - a['ts']) - paused_between(sp, a['ts'], b['ts'])
            b['d'] = acc

    # ── линия ──
    segs, timed = read_gpx(gpx)
    if reverse:
        segs, timed = reverse_segs(segs), False
    elif timeless:
        segs, timed = [[(p[0], p[1], p[2], None) for p in s] for s in segs], False
    mode = anchors if anchors != 'auto' else ('time' if timed else 'space')
    if mode == 'time' and not timed:
        raise RebuildError('В GPX нет времени у всех точек — привязка по времени невозможна.')
    order = order or ('time' if timed else 'file')
    if order == 'time':
        segs.sort(key=lambda s: s[0][3])
    if timed:
        t0, t1 = recs[0]['ts'], recs[-1]['ts']
        for k, s in enumerate(segs):
            if not (t0 - 3600 <= s[0][3] <= t1 + 3600):
                notes.append(f'кусок GPX №{k + 1} ({len(s)} точек) помечен {clock.date(s[0][3])} {hm(s[0][3])} — '
                             f'вне записи: время выдумано при дорисовке, порядок кусков может быть неверным')
    poly, cum, joins, removed = build_line(segs, keep_spikes)
    if cum[-1] < 50:
        raise RebuildError('Трек в GPX короче 50 м — это точно нарисованный маршрут?')
    for p, q, d in joins:
        if d > JOIN_M:
            where = f'{hm(p[3])} -> {hm(q[3])}' if timed else f'({p[0]:.5f}, {p[1]:.5f})'
            warns.append(f'стык кусков трека {where}: разрыв {d:.0f} м — линия прыгает, дорисуй связку')
    log.append(f'Трек: {len(segs)} кусков, {len(poly)} точек, {cum[-1] / 1000:.2f} км, привязка по '
               f'{"времени" if mode == "time" else "месту"}')
    if removed:
        big = [d for _, d in removed if d > 300]
        notes.append('из трека убраны одиночные выбросы туда-обратно: '
                     + ', '.join(f'{d} м' for _, d in removed))
        if big:
            warns.append(f'из трека срезаны «выбросы» на {", ".join(map(str, big))} м — если это разворот '
                         f'туда-обратно, трек укоротился на {2 * sum(big)} м: включи «не срезать развороты»')

    # ── замёрзший GPS и якоря ──
    bad, frozen = frozen_mask(recs)
    for i, k in frozen:
        log.append(f'GPS замерзал {hm(recs[i]["ts"])}–{hm(recs[k]["ts"])}, часы прошли '
                   f'{recs[k]["d"] - recs[i]["d"]:.0f} м')
    anc = anchors_by_time(recs, bad, poly, cum) if mode == 'time' else anchors_by_space(recs, bad, poly, cum)
    if anc[-1][0] < n - 1:
        anc.append((n - 1, cum[-1]))
    else:
        anc[-1] = (n - 1, cum[-1])
    live = len(anc) - 2
    log.append(f'Точек привязки к треку: {max(live, 0)}')
    fixes = [(r['lat'], r['lon']) for x, r in enumerate(recs) if not bad[x]]
    if fixes:
        sample = fixes[::max(1, len(fixes) // 200)]
        near = min(min(seg_proj(poly[i], poly[i + 1], q)[0] for i in range(0, len(poly) - 1))
                   for q in sample)
        if near > 300:
            far = f'{near:.0f} м' if near < 10000 else f'{near / 1000:.0f} км'
            warns.append(f'GPS часов ни разу не подошёл к трассе ближе {far} — похоже, выбрана не та '
                         f'трасса. Если трасса та, а часы рисовали точки где попало (подмена сигнала), '
                         f'включи «Не верить GPS часов совсем»')
        elif live <= 0:
            notes.append('ни одна точка GPS часов не легла на трек ближе 15 м — метры разложены по ходу часов '
                         'от старта до финиша трека; проверь, что трек тот и нарисован в ту сторону')
    total_ratio = (recs[-1]['d'] - recs[0]['d']) / cum[-1]
    if distance == 'sensor' and abs(total_ratio - 1) > 0.05:
        warns.append(f'часы насчитали {(recs[-1]["d"] - recs[0]["d"]) / 1000:.2f} км, а трасса '
                     f'{cum[-1] / 1000:.2f} км ({total_ratio:.2f}) — если датчика не было или он отваливался, '
                     f'выбери «длине трассы»; если датчик был — проверь, ту ли трассу выбрал')
        sensor_off = True
    else:
        sensor_off = False

    # ── окна без якорей ──
    for (xa, La), (xb, Lb) in zip(anc, anc[1:]):
        if recs[xb]['ts'] - recs[xa]['ts'] <= WIN_REPORT_S:
            continue
        ds, lg = recs[xb]['d'] - recs[xa]['d'], Lb - La
        span = f'{hm(recs[xa]["ts"])}–{hm(recs[xb]["ts"])}'
        ratio = ds / lg if lg > 0 else None
        log.append(f'  без привязки {span}: часы {ds:.0f} м, трек {lg:.0f} м' +
                   (f', отношение {ratio:.3f}' if ratio else ''))
        if distance != 'sensor' or sensor_off:      # общий разнобой уже назван выше
            continue
        moving = lg > 0 and ds >= 0.5 * lg
        if moving and max(ds, lg) >= RATIO_MIN_M:
            dev = abs(ratio - 1)
            if dev > RATIO_WARN:
                warns.append(f'{span}: часы {ds:.0f} м против трека {lg:.0f} м ({ratio:.3f}) — '
                             f'кусок нарисован {"коротко" if ratio > 1 else "длинно"}, перерисовать')
            elif dev > RATIO_NOTE and max(ds, lg) >= 2 * RATIO_MIN_M:
                notes.append(f'{span}: часы/трек {ratio:.3f} ({ds:.0f} против {lg:.0f} м) — проверь глазами')
        elif lg == 0 and ds > 500:
            warns.append(f'{span}: часы прошли {ds:.0f} м, а трек стоит — трек не покрывает этот кусок')
        elif not moving and lg > 500:
            warns.append(f'{span}: часы почти стояли ({ds:.0f} м), а трек {lg:.0f} м — лишний кусок в треке?')

    # ── положение на линии и метры ──
    spans = paused_spans(events)
    # время в движении от начала: без метров часов трасса раскладывается по нему,
    # чтобы в паузах точка стояла
    mv, acc = [0.0] * n, 0.0
    for x in range(1, n):
        a, b = recs[x - 1]['ts'], recs[x]['ts']
        acc += (b - a) - paused_between(spans, a, b)
        mv[x] = acc
    L = positions(recs, anc, mv, distance)
    if ele and distance != 'sensor':
        extra = ele_anchors(recs, anc, L, mv, poly, cum, log, notes)
        if extra:
            anc = sorted(set(anc) | set(extra))
            L = positions(recs, anc, mv, distance)

    # ── ступеньки ──
    cap = SPEED_CAP.get(pieces[0]['sport'], SPEED_CAP_DEFAULT)
    tss = [r['ts'] for r in recs]
    fixed, left = despike(tss, L, spans, cap)
    if distance == 'sensor':
        dd = [r['d'] for r in recs]
        f2, l2 = despike(tss, dd, spans, cap)
        for r, x in zip(recs, dd):
            r['d'] = x
        fixed += f2
        left += l2
    if fixed:
        top = sorted(fixed, key=lambda s: -s[1])[:6]
        log.append(f'Скачки быстрее {cap:.0f} м/с сглажены: {len(fixed)} — '
                   + ', '.join(f'{hm(t)} +{s:.0f} м на {w} с' for t, s, w in top))
    for t, s in left:
        warns.append(f'{hm(t)}: скачок +{s:.0f} м за секунду не сгладился (рядом длинная пауза) — '
                     f'проверь трек, в ленте будет выброс скорости')

    for x, r in enumerate(recs):
        p = at_distance(poly, cum, L[x])
        r['plat'], r['plon'], r['lele'] = p[0], p[1], p[2]
        r['dist'] = r['d'] if distance == 'sensor' else L[x]
    if keep_live:
        _keep_live(recs, bad, poly, cum, L, spans, cap, distance, log, notes, hm)
    pause_aware_speed(recs, spans)

    # ── длинные прямые на линии ──
    straight = [(i, cum[i + 1] - cum[i]) for i in range(len(poly) - 1) if cum[i + 1] - cum[i] > STRAIGHT_M]
    if straight and not preset:
        items = [f'км {cum[i] / 1000:.1f} ({seg:.0f} м)' for i, seg in straight[:10]]
        notes.append(f'на треке прямые без промежуточных точек длиннее {STRAIGHT_M} м: {len(straight)} — '
                     + ', '.join(items) + (' …' if len(straight) > 10 else '')
                     + ' — дорога прямая или нарисовано напрямик?')

    # ── запись ──
    out_fit, lap_info, n_fill = _write(files, pieces, recs, events, spans, laps, warns)
    if n_fill:
        log.append(f'Паузы часов заполнены стоячими записями: {n_fill} с (иначе Strava считает дыру бегом)')
    data = fit.write(out_fit)

    # ── проверка результата ──
    g = fit.read(data)
    rr = [m for m in g.of(RECORD) if m.get(0) is not None]
    pz = set()
    for p, q in spans:
        pz.update(range(p, q + 1))
    jumps = []
    for a, b in zip(rr, rr[1:]):
        d = haversine((a.get(0) * SC, a.get(1) * SC), (b.get(0) * SC, b.get(1) * SC))
        if d > JUMP_M * max(1, b.timestamp() - a.timestamp()) and b.timestamp() not in pz:
            jumps.append((d, hm(b.timestamp())))
    if jumps:
        big = [j for j in jumps if j[0] > 100]
        text = (f'скачков координат >{JUMP_M} м за секунду вне пауз: {len(jumps)}, крупнее 100 м: {len(big)} — '
                + ', '.join(f'{t} ({d:.0f} м)' for d, t in sorted(jumps, reverse=True)[:8]))
        (warns if big else notes).append(text)

    elapsed = recs[-1]['ts'] - recs[0]['ts']
    paused = sum(min(e, recs[-1]['ts']) - max(s, recs[0]['ts']) for s, e in spans
                 if e > recs[0]['ts'] and s < recs[-1]['ts'])
    dist = recs[-1]['dist']
    report = {
        'date': clock.date(recs[0]['ts']),
        'start': hm(recs[0]['ts']), 'end': hm(recs[-1]['ts']),
        'elapsed': hms(elapsed), 'timer': hms(elapsed - paused),
        'watch_km': round(watch_m / 1000, 2), 'track_km': round(cum[-1] / 1000, 2),
        'result_km': round(dist / 1000, 2),
        'pace': _pace(dist, elapsed - paused),
        'speed': round(dist / 1000 / ((elapsed - paused) / 3600), 1) if elapsed > paused and dist > 0 else None,
        'sport': pieces[0]['sport'],
        'mode': distance, 'pieces': len(pieces), 'records': len(rr),
        'laps': lap_info, 'pause_fill': n_fill,
        'track': [(round(p[0], 6), round(p[1], 6)) for p in poly],
        'watch_gps': [(round(r['lat'], 6), round(r['lon'], 6)) for r in recs[::5] if r['lat'] is not None],
        'warnings': warns, 'notes': notes, 'log': log,
    }
    return data, report


def positions(recs, anc, mv, distance):
    """Место на линии для каждой записи: между якорями — по доле метров часов
    (метров нет — по доле времени движения)."""
    L = [0.0] * len(recs)
    for (xa, La), (xb, Lb) in zip(anc, anc[1:]):
        Da, Db = recs[xa]['d'], recs[xb]['d']
        ta, tb = mv[xa], mv[xb]
        if distance == 'sensor':
            byd = (Db - Da) > 0 and (Db - Da) >= 0.5 * (Lb - La)
        else:
            byd = (Db - Da) > 0
        for x in range(xa, xb + 1):
            if byd:
                f_ = (recs[x]['d'] - Da) / (Db - Da)
            else:
                f_ = (mv[x] - ta) / (tb - ta) if tb > ta else 0.0
            L[x] = La + (Lb - La) * min(max(f_, 0.0), 1.0)
    return L


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def ele_anchors(recs, anc, L, mv, poly, cum, log, notes):
    """Привязка по высоте. Шагомер под глушилкой врёт неравномерно (устал, шаг короче —
    перебор растёт), а барометр часов на холмистой трассе пишет её профиль. Высоту
    записи выравниваем с высотой трассы (DTW: по линии только вперёд, не быстрее
    потолка, шаги близки к шагам шагомера) в полосе вокруг хода шагомера; к уже
    известным якорям полоса сужается. Якоря ставятся там, где трасса заметно идёт
    вверх или вниз: на ровном высота места не знает. Не стало лучше — ничего не меняем."""
    if any(p[2] is None for p in poly):
        return []
    G = ELE_GRID_M
    M = int(cum[-1] // G) + 1
    E = [at_distance(poly, cum, j * G)[2] for j in range(M)]
    if max(E) - min(E) < ELE_RELIEF_M:
        return []
    n = len(recs)
    if sum(r['alt'] is not None for r in recs) < 0.8 * n or mv[-1] < 300:
        return []
    dt_s = max(ELE_DT_S, mv[-1] / 800)
    ks, last = [], None
    for x in range(n):
        if recs[x]['alt'] is not None and (last is None or mv[x] - mv[last] >= dt_s):
            ks.append(x)
            last = x
    if len(ks) < 30:
        return []
    alt = [recs[x]['alt'] for x in ks]
    exp = [L[x] / G for x in ks]
    aL = [a[1] for a in anc]
    b0, k0, bmax = ELE_BAND_M
    band = []
    for x in ks:
        i = bisect.bisect_left(aL, L[x])
        near = min(abs(L[x] - aL[j]) for j in (i - 1, i) if 0 <= j < len(aL))
        band.append(int(min(bmax, b0 + k0 * near) / G) + 1)

    def resid(path, off):
        e = [alt[k] - off[k] - E[path[k]] for k in range(len(ks))]
        return (sum(v * v for v in e) / len(e)) ** 0.5

    def offsets(path):
        # барометр уходит от высоты трассы на постоянную плюс медленный дрейф погоды —
        # скользящая медиана за ±10 мин
        d = [alt[k] - E[path[k]] for k in range(len(ks))]
        w = max(1, int(600 / dt_s))
        return [_median(d[max(0, k - w):k + w + 1]) for k in range(len(ks))]

    path = [min(max(int(round(v)), 0), M - 1) for v in exp]
    off = offsets(path)
    before = resid(path, off)
    for _ in range(2):
        path = _ele_dtw(alt, off, E, exp, band, [mv[x] for x in ks], SPEED_CAP_DEFAULT)
        if path is None:
            return []
        off = offsets(path)
    after = resid(path, off)
    if after > ELE_GAIN * before or after > ELE_RMS_MAX:
        notes.append(f'высота часов не помогла уточнить место на трассе (расхождение {before:.1f} → '
                     f'{after:.1f} м) — метры разложены по шагомеру')
        return []
    out, last_t = [], None
    for k in range(len(ks)):
        x, j = ks[k], path[k]
        lo, hi = max(0, j - 5), min(M - 1, j + 5)
        if abs(E[hi] - E[lo]) < ELE_GRADE * (hi - lo) * G:
            continue
        if last_t is not None and mv[x] - last_t < ELE_ANCHOR_S:
            continue
        if any(abs(x - a[0]) < 3 for a in anc):
            continue
        out.append((x, j * G))
        last_t = mv[x]
    allx = sorted(set(anc) | set(out))
    if any(b[1] < a[1] for a, b in zip(allx, allx[1:])):
        notes.append('высота часов разошлась с GPS по месту на трассе — привязка по высоте отключена')
        return []
    log.append(f'Привязка по высоте: {len(out)} точек, расхождение высоты с трассой '
               f'{before:.1f} → {after:.1f} м')
    return out


def _ele_dtw(alt, off, E, exp, band, mvk, cap):
    """Путь по сетке трассы для каждого шага по времени. Цена — квадрат расхождения
    высоты плюс отход шага от шага шагомера (медленный дрейф его ошибки дёшев)."""
    K, M, G = len(alt), len(E), ELE_GRID_M
    sh = 1.0 / ELE_SIGMA_H ** 2

    def window(k):
        return max(0, int(exp[k]) - band[k]), min(M - 1, int(exp[k]) + band[k])

    lo, hi = window(0)
    prev = {j: (alt[0] - off[0] - E[j]) ** 2 * sh for j in range(lo, hi + 1)}
    back = [None] * K
    for k in range(1, K):
        lo, hi = window(k)
        smax = int(cap * (mvk[k] - mvk[k - 1]) / G) + 1
        e = exp[k] - exp[k - 1]
        se = 1.0 / (0.3 * max(e, 0.0) + 1.0) ** 2
        pj = sorted(prev)
        pc = [prev[j] for j in pj]
        cur, bk = {}, {}
        base_h = alt[k] - off[k]
        for j in range(lo, hi + 1):
            i0 = bisect.bisect_left(pj, j - smax)
            i1 = bisect.bisect_right(pj, j)
            if i0 >= i1:
                continue
            best, bj = None, None
            for i in range(i0, i1):
                ds = j - pj[i] - e
                c = pc[i] + ds * ds * se
                if best is None or c < best:
                    best, bj = c, pj[i]
            dh = base_h - E[j]
            cur[j] = best + dh * dh * sh
            bk[j] = bj
        if not cur:
            return None
        prev, back[k] = cur, bk
    path, j = [0] * K, min(prev, key=prev.get)
    for k in range(K - 1, 0, -1):
        path[k] = j
        j = back[k][j]
    path[0] = j
    return path


def _shift(r, poly, cum, at):
    """Живой фикс минус точка трассы, где запись стоит по якорям (градусы)."""
    p = at_distance(poly, cum, at)
    return r['lat'] - p[0], r['lon'] - p[1]


def _keep_live(recs, bad, poly, cum, L, spans, cap, distance, log, notes, hm):
    """Живой GPS поверх трассы: на живых кусках координаты часов, в дырах — трасса,
    как её разложили якоря. В режиме трассы метры — по итоговым координатам."""
    stretches = live_stretches(recs, bad, poly, cap)
    if not stretches:
        notes.append('живых кусков GPS (от минуты, рядом с трассой) не нашлось — весь трек по трассе')
        return
    n = len(recs)
    live = [False] * n
    for i, j in stretches:
        for x in range(i, j + 1):
            live[x] = True
            r = recs[x]
            r['plat'], r['plon'], r['lele'] = r['lat'], r['lon'], None
    secs = sum(recs[j]['ts'] - recs[i]['ts'] for i, j in stretches)
    notes.append(f'свой GPS оставлен на {len(stretches)} кусках, {secs // 60} мин из '
                 f'{(recs[-1]["ts"] - recs[0]["ts"]) // 60} — остальное по трассе')
    log.append('Живой GPS: ' + ', '.join(f'{hm(recs[i]["ts"])}–{hm(recs[j]["ts"])}' for i, j in stretches))

    # Дыры: точка трассы плюс поправка «живой фикс − трасса» с краёв дыры,
    # которая гаснет за LIVE_BLEND_S — на стыках не остаётся прыжка, а середина
    # длинной дыры идёт ровно по трассе
    prev = [-1] + [k for _, k in stretches]
    nxt = [i for i, _ in stretches] + [n]
    for a, b in zip(prev, nxt):
        if b - a < 2:
            continue
        da = _shift(recs[a], poly, cum, L[a]) if a >= 0 else (0.0, 0.0)
        db = _shift(recs[b], poly, cum, L[b]) if b < n else (0.0, 0.0)
        ta, tb = recs[max(a, 0)]['ts'], recs[min(b, n - 1)]['ts']
        blend = min(LIVE_BLEND_S, max(tb - ta, 1))
        for x in range(a + 1, b):
            wa = max(0.0, 1 - (recs[x]['ts'] - ta) / blend) if a >= 0 else 0.0
            wb = max(0.0, 1 - (tb - recs[x]['ts']) / blend) if b < n else 0.0
            if wa + wb > 1:
                wa, wb = wa / (wa + wb), wb / (wa + wb)
            recs[x]['plat'] += da[0] * wa + db[0] * wb
            recs[x]['plon'] += da[1] * wa + db[1] * wb
    if distance == 'sensor':
        return
    pz = [any(s <= r['ts'] < e for s, e in spans) for r in recs]
    dist = [0.0] * n
    for x in range(1, n):
        step = 0.0 if pz[x] else haversine((recs[x - 1]['plat'], recs[x - 1]['plon']),
                                           (recs[x]['plat'], recs[x]['plon']))
        dist[x] = dist[x - 1] + step
    despike([r['ts'] for r in recs], dist, spans, cap)
    for r, d in zip(recs, dist):
        r['dist'] = d


def _pace(dist, timer):
    if dist <= 0 or timer <= 0:
        return None
    s = timer / (dist / 1000)
    return f'{int(s // 60)}:{int(round(s % 60)):02d}'.replace(':60', ':59')


def _write(files, pieces, recs, events, spans, laps_mode, warns):
    """Правка сообщений исходников -> один FitFile."""
    cache = {}
    for f in files:
        cache.update(f.cache)
    by_key = {(r['piece'], r['ts']): r for r in recs}
    order = [p['idx'] for p in pieces]
    first = files[order[0]]

    laps_in = [m for i in order for m in files[i].of(LAP)]
    step = None
    if laps_mode == 'km':
        step = 1000.0
    elif laps_mode == 'auto':
        step = auto_lap_step(laps_in)
    recut = step is not None or not laps_in

    out, seen_dev = [], set()
    sessions, activities = [], []
    last_rec, n_fill = None, 0
    for k, i in enumerate(order):
        f = files[i]
        for m in f.msgs:
            g = m.gnum
            if k > 0 and g in (FILE_ID, FILE_CREATOR):
                continue
            if g in (DEV_ID, FIELD_DESC):
                sig = (g, m.d.fields, tuple(m.raw), m.devraw)
                if sig in seen_dev:
                    continue
                seen_dev.add(sig)
            if g == SESSION:
                sessions.append(m)
                continue
            if g == ACTIVITY:
                activities.append(m)
                continue
            if g == LAP:
                if not recut:
                    out.append(_fix_lap(m, recs, spans, cache))
                continue
            if g == RECORD:
                t = m.timestamp()
                if t is None:
                    out.append(m)
                    continue
                r = by_key.get((i, t))
                if r is None:          # перекрытие с соседним куском — отброшено
                    continue
                fixed = _fix_record(m, r, cache)
                if last_rec is not None:
                    fill = _pause_fill(last_rec, t, spans, cache)
                    n_fill += len(fill)
                    out.extend(fill)
                out.append(fixed)
                last_rec = fixed
                continue
            out.append(m)

    lap_info = 'круги часов сохранены'
    if recut:
        step = step or 1000.0
        lap_msgs = []
        sport = pieces[0]['sport']
        for j, (a, b) in enumerate(cut_laps(recs, step, spans)):
            s = window_stats(recs, a, b, spans)
            sp = s['dist'] / s['timer'] if s['timer'] > 0 else 0.0
            lap_msgs.append(fit.make(cache, 15, LAP, [
                (254, UINT16, j), (253, UINT32, s['end']), (2, UINT32, s['start']),
                (3, SINT32, semi(s['p0'][0])), (4, SINT32, semi(s['p0'][1])),
                (5, SINT32, semi(s['p1'][0])), (6, SINT32, semi(s['p1'][1])),
                (7, UINT32, s['elapsed'] * 1000), (8, UINT32, s['timer'] * 1000),
                (9, UINT32, s['dist'] * 100), (13, UINT16, sp * 1000), (14, UINT16, s['max_v'] * 1000),
                (15, UINT8, s['avg_hr']), (16, UINT8, s['max_hr']), (17, UINT8, s['avg_cad']),
                (24, ENUM, 2), (25, ENUM, sport)]))      # 2 = по дистанции, последний — 7
        if lap_msgs:
            last = lap_msgs[-1]
            last.set(24, 7, ENUM, cache)
        out += lap_msgs
        lap_info = (f'круги перенарезаны по {step / 1000:g} км ({len(lap_msgs)})' if laps_in
                    else f'круги нарезаны по {step / 1000:g} км ({len(lap_msgs)})')

    # сессия: первая из первого куска, итоги — по всей записи
    s = sessions[0] if sessions else fit.make(cache, 14, SESSION, [
        (253, UINT32, recs[-1]['ts']), (2, UINT32, recs[0]['ts']), (5, ENUM, pieces[0]['sport'])])
    s = s.copy()
    w = window_stats(recs, 0, len(recs) - 1, spans)
    multi = len(pieces) > 1
    if multi or not sessions:
        s.set(253, w['end'], UINT32, cache)
        s.set(2, w['start'], UINT32, cache)
        s.set(7, w['elapsed'] * 1000, UINT32, cache)
        s.set(8, w['timer'] * 1000, UINT32, cache)
        cals = [x.get(11) for x in sessions if x.get(11) is not None]
        if cals:
            s.set(11, sum(cals), UINT16, cache)
    timer = (s.get(8) or 0) / 1000 or w['timer']
    s.set(9, w['dist'] * 100, UINT32, cache)
    set_speed_pair(s, 14, 124, w['dist'] / timer if timer else 0.0, cache)
    set_speed_pair(s, 15, 125, w['max_v'], cache)
    s.set(3, semi(w['p0'][0]), SINT32, cache)
    s.set(4, semi(w['p0'][1]), SINT32, cache)
    if s.d.index(29) >= 0:
        lats = [r['plat'] for r in recs]
        lons = [r['plon'] for r in recs]
        for num, val in ((29, max(lats)), (30, max(lons)), (31, min(lats)), (32, min(lons))):
            s.set(num, semi(val), SINT32, cache)
    if recut:
        s.set(26, len([m for m in out if m.gnum == LAP]), UINT16, cache)
        if s.d.index(25) >= 0:
            s.set(25, 0, UINT16, cache)
    if len(sessions) > 1 and not multi:
        warns.append('в файле несколько сессий (мультиспорт) — итоги пересчитаны только у первой')
        out.append(s)
        out += sessions[1:]
    else:
        out.append(s)

    a = activities[0].copy() if activities else None
    if a is not None:
        if multi:
            a.set(253, w['end'], UINT32, cache)
            a.set(0, (s.get(8) or 0), UINT32, cache)
            a.set(1, 1, UINT16, cache)
            lt = a.get(5)
            if lt is not None and activities[0].timestamp() is not None:
                a.set(5, w['end'] + lt - activities[0].timestamp(), UINT32, cache)
        out.append(a)

    res = fit.FitFile(out, first.protocol, first.profile)
    res.cache = cache
    return res, lap_info, n_fill


def _fix_record(m, r, cache):
    m = m.copy()
    m.set(0, semi(r['plat']), SINT32, cache)
    m.set(1, semi(r['plon']), SINT32, cache)
    m.set(5, r['dist'] * 100, UINT32, cache)
    if m.d.index(73) >= 0:
        m.set(73, r['v'] * 1000, UINT32, cache)
        if m.d.index(6) >= 0:
            m.set(6, r['v'] * 1000, UINT16, cache)
    else:
        m.set(6, r['v'] * 1000, UINT16, cache)
    if r['lele'] is not None and m.get(2) is None and m.get(78) is None:
        m.set(2, (r['lele'] + 500) * 5, UINT16, cache)
    if m.get(3) == 0:        # пульс 0 — датчик не дал значения; Strava рисует провал до нуля
        m.set(3, None, UINT8, cache)
    return m


def _pause_fill(last, t, spans, cache):
    """Дыра в записи целиком внутри паузы часов -> посекундные записи «стою»: место и
    метры прежние, скорость 0, без пульса и каденса. Иначе Strava тянет дыру как бег.
    Дыры на ходу (смарт-запись Garmin пишет раз в несколько секунд) не трогаем."""
    t0 = last.timestamp()
    if t - t0 <= 1 or paused_between(spans, t0, t) < t - t0 - 1:
        return []
    out = []
    for tt in range(t0 + 1, t):
        c = last.copy()
        c.set(253, tt, UINT32, cache)
        for fnum, typ in ((73, UINT32), (6, UINT16)):
            if c.get(fnum) is not None:
                c.set(fnum, 0, typ, cache)
        for fnum in (3, 4):
            if c.get(fnum) is not None:
                c.set(fnum, None, UINT8, cache)
        out.append(c)
    return out


def _fix_lap(m, recs, spans, cache):
    m = m.copy()
    t0, t1 = m.get(2), m.timestamp()
    if t0 is None or t1 is None:
        return m
    a, b = _rec_index(recs, t0), _rec_index(recs, t1)
    w = window_stats(recs, a, b, spans)
    timer = (m.get(8) or 0) / 1000 or w['timer']
    m.set(9, w['dist'] * 100, UINT32, cache)
    set_speed_pair(m, 13, 110, w['dist'] / timer if timer else 0.0, cache)
    set_speed_pair(m, 14, 111, w['max_v'], cache)
    for num, val in ((3, w['p0'][0]), (4, w['p0'][1]), (5, w['p1'][0]), (6, w['p1'][1])):
        m.set(num, semi(val), SINT32, cache)
    return m
