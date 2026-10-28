"""despike: окно сглаживания накрывает короткую паузу часов — секунды паузы метров
не получают. Динамо 28.10.2026: пауза 3 с, первая запись после неё стоит через
дыру в 4 с (1 с хода), и раньше ей доставалось 4 секунды хода — на снятии паузы
+9 м за 1 с."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from trek_rebuild.engine import despike, paused_between  # noqa: E402

# 2 м/с; пауза часов 20–23, записей внутри нет; перед паузой ступенька +12 м
spans = [(20, 23)]
ts = list(range(0, 20)) + list(range(23, 40))
xs, x = [0.0], 0.0
for a, b in zip(ts, ts[1:]):
    x += 2.0 * (b - a - paused_between(spans, a, b)) + (12.0 if b == 18 else 0.0)
    xs.append(x)
fixed, left = despike(ts, xs, spans, 7.0)
assert fixed and not left, (fixed, left)
assert fixed[0][2] > ts.index(23) - ts.index(18), 'окно не накрыло паузу — тест ничего не проверяет'
speed = [(xs[k] - xs[k - 1]) / (ts[k] - ts[k - 1] - paused_between(spans, ts[k - 1], ts[k]))
         for k in range(1, len(ts))]
i = ts.index(23)
assert max(speed) <= 7.0, f'после сглаживания {max(speed):.1f} м/с'
print(f'OK  ступенька разложена, максимум {max(speed):.1f} м/с, на снятии паузы {speed[i - 1]:.1f} м/с')
