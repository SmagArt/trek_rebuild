# -*- coding: utf-8 -*-
r"""Локальный запуск движка (то же, что делает страница, но из консоли).

    py -X utf8 cli.py a.fit [b.fit ...] --gpx трек.gpx -o out.fit
    py -X utf8 cli.py a.fit --route naberezhnaya -o out.fit
    py -X utf8 cli.py --routes                      список встроенных трасс

--distance track   (по умолчанию) метры = длина трека — под глушилку
--distance sensor  метры часов/датчика как есть, трек — только карта
"""
import argparse
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trek_rebuild import engine, routes, web  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description='Перенос тренировки на нарисованный трек')
    ap.add_argument('src', nargs='*', help='куски .fit / .gpx / .tcx / .zip (порядок неважен)')
    ap.add_argument('--gpx', help='свой трек')
    ap.add_argument('--route', choices=list(routes.PRESETS), help='встроенная трасса')
    ap.add_argument('--routes', action='store_true', help='показать встроенные трассы')
    ap.add_argument('-o', '--out', help='куда записать .fit')
    ap.add_argument('--distance', default='track', choices=('track', 'sensor', 'time'))
    ap.add_argument('--reverse', action='store_true', help='трек в обратную сторону')
    ap.add_argument('--keep-spikes', action='store_true', help='не срезать развороты туда-обратно')
    ap.add_argument('--ignore-gps', action='store_true', help='не привязываться к GPS часов')
    ap.add_argument('--keep-live', action='store_true', help='где GPS живой — оставить свой трек')
    ap.add_argument('--laps', default='auto', choices=('auto', 'keep', 'km'))
    ap.add_argument('--anchors', default='auto', choices=('auto', 'time', 'space'))
    a = ap.parse_args()

    if a.routes:
        for pid, name in routes.listing():
            print(f'{pid:14} {name}')
        return
    if not a.src or not a.out or bool(a.gpx) == bool(a.route):
        ap.error('нужны куски .fit, -o и ровно одно из --gpx / --route')

    opts = dict(distance=a.distance, reverse=a.reverse, keep_spikes=a.keep_spikes,
                ignore_gps=a.ignore_gps, keep_live=a.keep_live, laps=a.laps, anchors=a.anchors)
    if a.route:
        gpx, preset = routes.load(a.route)
        opts.update(preset)
        opts['reverse'] = opts['reverse'] != a.reverse
        opts['keep_spikes'] = opts['keep_spikes'] or a.keep_spikes
    else:
        gpx = open(a.gpx, 'rb').read()
    try:
        fits, _, converted = web._expand([(os.path.basename(p), open(p, 'rb').read()) for p in a.src])
        data, rep = engine.rebuild([d for _, d in fits], gpx, **opts)
        if converted:
            rep['notes'].insert(0, web.CONVERTED_NOTE)
    except engine.RebuildError as e:
        sys.exit(f'Ошибка: {e}')
    with open(a.out, 'wb') as fh:
        fh.write(data)

    for line in rep['log']:
        print(line)
    print(f"\n{rep['date']} {rep['start']}–{rep['end']} | часы {rep['watch_km']} км -> {rep['result_km']} км "
          f"(трек {rep['track_km']} км) | общее {rep['elapsed']} | таймер {rep['timer']} | темп {rep['pace']} | "
          f"{rep['laps']} | {len(data)} байт -> {a.out}")
    print('\n══ ОСТАТОЧНЫЕ КОСЯКИ ══')
    if not rep['warnings'] and not rep['notes']:
        print('нет')
    for w in rep['warnings']:
        print('  ⚠ ' + w)
    for w in rep['notes']:
        print('  · ' + w)


if __name__ == '__main__':
    main()
