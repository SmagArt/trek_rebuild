# -*- coding: utf-8 -*-
"""Встроенные клубные трассы Воронежа. Время точек у трасс не используется:
привязка всегда по месту, а не по выдуманному времени нарисованного трека."""
from pathlib import Path

HERE = Path(__file__).resolve().parent / 'routes'

# id -> (название, файл, обратное направление, не срезать развороты[, кругов])
PRESETS = {
    'naberezhnaya': ('Набережная DH, 22,1 км (туда-обратно)', 'naberezhnaya.gpx', False, True),
    'coferan_cw': ('Коферан, круг 10,1 км — по часовой', 'coferan.gpx', False, False),
    'coferan_ccw': ('Коферан, круг 10,1 км — против часовой', 'coferan.gpx', True, False),
    'gorny_1': ('Горный круг, Олимпик — 1 круг, 2 км', 'gorny_krug.gpx', False, False, 1),
    'gorny_3': ('Горный круг, Олимпик — 3 круга, 6 км', 'gorny_krug.gpx', False, False, 3),
    'gorny_10': ('Горный круг, Олимпик — 10 кругов, 20 км', 'gorny_krug.gpx', False, False, 10),
}


def _repeat(gpx, n):
    """Круг n раз подряд одной линией: точки trkseg повторяются."""
    text = gpx.decode('utf-8')
    head, rest = text.split('<trkseg>', 1)
    body, tail = rest.split('</trkseg>', 1)
    return (head + '<trkseg>' + body * n + '</trkseg>' + tail).encode('utf-8')


def load(pid):
    """-> (байты GPX, параметры для engine.rebuild)."""
    name, fname, reverse, keep, *laps = PRESETS[pid]
    gpx = (HERE / fname).read_bytes()
    if laps and laps[0] > 1:
        gpx = _repeat(gpx, laps[0])
    return gpx, {'reverse': reverse, 'keep_spikes': keep, 'timeless': True, 'preset': True}


def listing():
    return [(pid, p[0]) for pid, p in PRESETS.items()]
