# -*- coding: utf-8 -*-
"""Геометрия трека: расстояния, точка по пройденным метрам, проекция на отрезок."""
import math


def haversine(p, q):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    a = (math.sin((la2 - la1) / 2) ** 2 +
         math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def at_distance(pts, cum, d):
    """Линейная интерполяция точки трека по пройденной дистанции."""
    if d <= 0:
        return pts[0]
    if d >= cum[-1]:
        return pts[-1]
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo]
    f = (d - cum[lo]) / seg if seg > 0 else 0.0
    a, b = pts[lo], pts[hi]
    ele = None
    if a[2] is not None and b[2] is not None:
        ele = a[2] + (b[2] - a[2]) * f
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, ele


def seg_proj(a, b, q):
    """Расстояние от q до отрезка a-b в метрах и доля t на отрезке (локальная плоскость)."""
    kx, ky = 111320.0 * math.cos(math.radians(q[0])), 110540.0
    ax, ay = (a[1] - q[1]) * kx, (a[0] - q[0]) * ky
    bx, by = (b[1] - q[1]) * kx, (b[0] - q[0]) * ky
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
    return math.hypot(ax + t * dx, ay + t * dy), t


def ascent_descent(eles, window=5):
    """Набор/сброс по сглаженной высоте — по сырой дрожание накручивает метры."""
    xs = [e for e in eles if e is not None]
    if len(xs) < 2:
        return 0, 0
    sm = []
    for i in range(len(xs)):
        lo, hi = max(0, i - window), min(len(xs), i + window + 1)
        sm.append(sum(xs[lo:hi]) / (hi - lo))
    up = sum(max(0.0, b - a) for a, b in zip(sm, sm[1:]))
    dn = sum(max(0.0, a - b) for a, b in zip(sm, sm[1:]))
    return int(round(up)), int(round(dn))
