# -*- coding: utf-8 -*-
"""Встроенные клубные трассы Воронежа. Время точек у трасс не используется:
привязка всегда по месту, а не по выдуманному времени нарисованного трека."""
from pathlib import Path

HERE = Path(__file__).resolve().parent / 'routes'

# id -> (название, файл, обратное направление, не срезать развороты)
PRESETS = {
    'naberezhnaya': ('Набережная DH, 22,1 км (туда-обратно)', 'naberezhnaya.gpx', False, True),
    'coferan_cw': ('Коферан, круг 10,1 км — по часовой', 'coferan.gpx', False, False),
    'coferan_ccw': ('Коферан, круг 10,1 км — против часовой', 'coferan.gpx', True, False),
}


def load(pid):
    """-> (байты GPX, параметры для engine.rebuild)."""
    name, fname, reverse, keep = PRESETS[pid]
    return (HERE / fname).read_bytes(), {'reverse': reverse, 'keep_spikes': keep, 'timeless': True, 'preset': True}


def listing():
    return [(pid, p[0]) for pid, p in PRESETS.items()]
