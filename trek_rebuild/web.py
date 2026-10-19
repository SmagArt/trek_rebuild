# -*- coding: utf-8 -*-
"""Прослойка для страницы (Pyodide): файлы с именами -> FIT + отчёт в JSON.

ZIP распаковывается: Garmin Connect и Strava отдают «оригинал» тренировки
архивом, а человек с телефона перетащит его как есть.
"""
import io
import json
import zipfile

from . import engine, routes


def _expand(files):
    """[(имя, байты)] -> [(имя, байты FIT)], GPX из архива — отдельно."""
    fits, gpx = [], None
    for name, data in files:
        low = name.lower()
        if low.endswith('.zip') or data[:2] == b'PK':
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise engine.RebuildError(f'{name}: архив не открывается') from None
            inner = [n for n in z.namelist() if n.lower().endswith('.fit')]
            if not inner:
                raise engine.RebuildError(f'{name}: в архиве нет FIT-файла')
            fits += [(n.rsplit('/', 1)[-1], z.read(n)) for n in inner]
        elif low.endswith('.gpx'):
            gpx = data
        else:
            fits.append((name, data))
    return fits, gpx


def run(files, gpx_name, gpx_data, opts_json):
    """files — [(имя, байты)], gpx_* — свой трек или None, opts — JSON с ключами
    route (id трассы или ''), distance, reverse, keep_spikes, ignore_gps, laps.
    -> (JSON отчёта, байты FIT или None)."""
    o = json.loads(opts_json)
    try:
        fits, gpx_in_zip = _expand(files)
        if not fits:
            raise engine.RebuildError('Нет файла тренировки — нужен FIT (или ZIP из Garmin/Strava).')
        params = dict(distance=o.get('distance', 'track'), ignore_gps=bool(o.get('ignore_gps')),
                      laps=o.get('laps', 'auto'))
        if o.get('route'):
            gpx, preset = routes.load(o['route'])
            params.update(preset)
            params['keep_spikes'] = params['keep_spikes'] or bool(o.get('keep_spikes'))
        else:
            gpx = gpx_data or gpx_in_zip
            if not gpx:
                raise engine.RebuildError('Выбери трассу из списка или загрузи свой трек (GPX).')
            params.update(reverse=bool(o.get('reverse')), keep_spikes=bool(o.get('keep_spikes')))
        data, rep = engine.rebuild([d for _, d in fits], gpx, **params)
        rep['ok'] = True
        rep['source'] = fits[0][0]
        return json.dumps(rep, ensure_ascii=False), data
    except engine.RebuildError as e:
        return json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False), None
    except Exception as e:  # noqa: BLE001 — человеку нужен текст, а не трейсбек
        return json.dumps({'ok': False, 'error': f'Внутренняя ошибка: {type(e).__name__}: {e}',
                           'bug': True}, ensure_ascii=False), None


def presets_json():
    return json.dumps(routes.listing(), ensure_ascii=False)
