# -*- coding: utf-8 -*-
"""Кодек без правок: прочитать -> записать -> fitparse видит те же значения.

    py -X utf8 tests/test_roundtrip.py [файлы.fit ...]   (по умолчанию tests/samples/*.fit)
"""
import glob
import io
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fitparse  # noqa: E402
from trek_rebuild import fit  # noqa: E402


def dump(data):
    ff = fitparse.FitFile(io.BytesIO(data), check_crc=False)
    out = []
    for m in ff.get_messages():
        vals = {f.def_num if f.def_num is not None else f.name: f.raw_value for f in m.fields}
        dev = {f.name: f.raw_value for f in m.fields if getattr(f, 'field_def', None) is not None
               and getattr(f.field_def, 'dev_data_index', None) is not None}
        out.append((m.name, tuple(sorted(vals.items(), key=str)), tuple(sorted(dev.items(), key=str))))
    return out


def main():
    files = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, 'tests', 'samples', '*.fit')))
    bad = 0
    for path in files:
        src = open(path, 'rb').read()
        try:
            f = fit.read(src)
            out = fit.write(f)
            try:
                a = dump(src)
            except Exception as e:  # noqa: BLE001
                g = fit.read(out)
                print(f'ПРОПУСК {os.path.basename(path)}: fitparse не читает исходник ({e}); '
                      f'наш кодек: {len(f.msgs)} сообщений, перечитано {len(g.msgs)}, заметки {f.notes}')
                continue
            b = dump(out)
        except Exception as e:  # noqa: BLE001
            print(f'ОШИБКА {os.path.basename(path)}: {type(e).__name__}: {e}')
            bad += 1
            continue
        ok = fit.crc16(out) == 0
        diff = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]
        same = len(a) == len(b) and not diff
        print(f'{"OK " if same and ok else "РАЗНИЦА"} {os.path.basename(path)}: сообщений {len(a)}/{len(b)}, '
              f'байт {len(src)}/{len(out)}, crc {"ок" if ok else "битая"}')
        for i, x, y in diff[:3]:
            print(f'   #{i} {x[0]}:\n     было  {x[1][:12]}\n     стало {y[1][:12]}')
        bad += not (same and ok)
    print(f'итого: {len(files) - bad}/{len(files)} совпали')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
