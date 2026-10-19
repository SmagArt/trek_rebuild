# -*- coding: utf-8 -*-
"""FIT без потерь: чтение в сообщения с сырыми байтами полей и запись обратно.

Зачем свой кодек, а не fitparse. Исправленный файл должен нести всё, что
записали чужие часы: мощность, вертикальные колебания, поля Stryd
(developer fields), сплиты, устройство. Поэтому файл не собирается заново из
разобранной модели, а перекодируется: каждое сообщение уходит теми же байтами,
меняются только поля, которые правит движок. Определения (definition messages)
пишутся заново — ровно перед теми данными, у которых набор полей сменился.

Сжатые заголовки времени (compressed timestamp) на выходе раскрываются в
обычные: в сообщение дописывается поле 253 с вычисленным временем.
"""
import struct

FIT_EPOCH = 631065600            # 1989-12-31 00:00 UTC в unix-секундах
SC = 180.0 / 2 ** 31             # семициркли -> градусы

# номер базового типа (младшие 5 бит) -> (struct-формат, размер, невалидное значение)
BASE = {
    0x00: ('B', 1, 0xFF),        # enum
    0x01: ('b', 1, 0x7F),        # sint8
    0x02: ('B', 1, 0xFF),        # uint8
    0x03: ('h', 2, 0x7FFF),      # sint16
    0x04: ('H', 2, 0xFFFF),      # uint16
    0x05: ('i', 4, 0x7FFFFFFF),  # sint32
    0x06: ('I', 4, 0xFFFFFFFF),  # uint32
    0x07: (None, 1, None),       # string
    0x08: ('f', 4, None),        # float32
    0x09: ('d', 8, None),        # float64
    0x0A: ('B', 1, 0x00),        # uint8z
    0x0B: ('H', 2, 0x0000),      # uint16z
    0x0C: ('I', 4, 0x00000000),  # uint32z
    0x0D: (None, 1, None),       # byte
    0x0E: ('q', 8, 0x7FFFFFFFFFFFFFFF),
    0x0F: ('Q', 8, 0xFFFFFFFFFFFFFFFF),
    0x10: ('Q', 8, 0x0000000000000000),
}

# полные коды базовых типов для полей, которые движок дописывает сам
UINT8, UINT16, SINT32, UINT32, ENUM = 0x02, 0x84, 0x85, 0x86, 0x00

_CT = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
       0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


class FitError(ValueError):
    pass


def crc16(data, c=0):
    for b in data:
        t = _CT[c & 0xF]; c = (c >> 4) & 0x0FFF; c ^= t ^ _CT[b & 0xF]
        t = _CT[c & 0xF]; c = (c >> 4) & 0x0FFF; c ^= t ^ _CT[(b >> 4) & 0xF]
    return c


class Def:
    """Определение сообщения. Неизменяемое: правка набора полей — новый Def."""
    __slots__ = ('local', 'arch', 'gnum', 'fields', 'dev', 'key')

    def __init__(self, local, arch, gnum, fields, dev=()):
        self.local, self.arch, self.gnum = local, arch, gnum
        self.fields = tuple(fields)      # ((num, size, base_type), ...)
        self.dev = tuple(dev)            # ((num, size, dev_index), ...)
        self.key = (local, arch, gnum, self.fields, self.dev)

    def index(self, num):
        for i, f in enumerate(self.fields):
            if f[0] == num:
                return i
        return -1

    def with_field(self, num, size, bt):
        return Def(self.local, self.arch, self.gnum, self.fields + ((num, size, bt),), self.dev)

    def encode(self):
        e = '<' if self.arch == 0 else '>'
        b = bytes([0x40 | (0x20 if self.dev else 0) | self.local, 0, self.arch])
        b += struct.pack(e + 'HB', self.gnum, len(self.fields))
        for f in self.fields:
            b += bytes(f)
        if self.dev:
            b += bytes([len(self.dev)])
            for f in self.dev:
                b += bytes(f)
        return b


def _intern(cache, d):
    return cache.setdefault(d.key, d)


class Msg:
    """Сообщение данных: сырые байты каждого поля в порядке определения."""
    __slots__ = ('d', 'raw', 'devraw', 'cts', 'src')

    def __init__(self, d, raw, devraw=b'', cts=None, src=0):
        self.d, self.raw, self.devraw, self.cts, self.src = d, raw, devraw, cts, src

    @property
    def gnum(self):
        return self.d.gnum

    def get(self, num):
        """Скаляр поля как целое (или float), None — нет поля или невалидно."""
        i = self.d.index(num)
        if i < 0:
            return self.cts if num == 253 else None
        _, size, bt = self.d.fields[i]
        fmt, tsize, inv = BASE.get(bt & 0x1F, (None, 1, None))
        if fmt is None or size < tsize:
            return None
        e = '<' if self.d.arch == 0 else '>'
        val = struct.unpack_from(e + fmt, self.raw[i])[0]
        if inv is not None and val == inv:
            return None
        return val

    def get_bytes(self, num):
        i = self.d.index(num)
        return self.raw[i] if i >= 0 else None

    def set(self, num, val, bt, cache):
        """Записать скаляр. Поля нет — дописывается в определение (новый Def).
        None пишет невалидное значение типа."""
        i = self.d.index(num)
        if i < 0:
            fmt, tsize, _ = BASE[bt & 0x1F]
            self.d = _intern(cache, self.d.with_field(num, tsize, bt))
            self.raw = self.raw + [b'']
            i = len(self.raw) - 1
        _, size, fbt = self.d.fields[i]
        fmt, tsize, inv = BASE[fbt & 0x1F]
        if fmt is None or size != tsize:
            raise FitError(f'поле {num} сообщения {self.d.gnum}: размер {size} — не скаляр')
        e = '<' if self.d.arch == 0 else '>'
        if val is None:
            val = inv
        elif fmt in 'fd':
            val = float(val)
        else:
            val = _clamp(int(round(val)), fmt, inv)
        self.raw[i] = struct.pack(e + fmt, val)

    def copy(self):
        return Msg(self.d, list(self.raw), self.devraw, self.cts, self.src)

    def timestamp(self):
        return self.get(253)


_RANGE = {'B': (0, 0xFF), 'b': (-0x80, 0x7F), 'H': (0, 0xFFFF), 'h': (-0x8000, 0x7FFF),
          'I': (0, 0xFFFFFFFF), 'i': (-0x80000000, 0x7FFFFFFF),
          'Q': (0, 0xFFFFFFFFFFFFFFFF), 'q': (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF)}


def _clamp(v, fmt, inv):
    lo, hi = _RANGE[fmt]
    v = max(lo, min(v, hi))
    if v == inv:                     # не путать реальное значение с «нет данных»
        v = v - 1 if v > lo else v + 1
    return v


def make(cache, local, gnum, items):
    """Новое сообщение из [(номер поля, базовый тип, значение)], little-endian."""
    fields = [(num, BASE[bt & 0x1F][1], bt) for num, bt, _ in items]
    d = _intern(cache, Def(local, 0, gnum, fields))
    m = Msg(d, [b''] * len(fields))
    for num, bt, val in items:
        m.set(num, val, bt, cache)
    return m


class FitFile:
    def __init__(self, msgs, protocol=0x20, profile=2132):
        self.msgs, self.protocol, self.profile = msgs, protocol, profile
        self.cache = {}
        self.notes = []

    def of(self, gnum):
        return [m for m in self.msgs if m.d.gnum == gnum]


def read(data, src=0):
    """Байты FIT -> FitFile. Цепочка из нескольких FIT (chained) читается
    целиком в один список. Обрезанный хвост и битая CRC не мешают: берётся всё,
    что разобралось, — часы, умершие на финише, пишут именно такие файлы."""
    if len(data) < 12:
        raise FitError('файл слишком короткий для FIT')
    if data[0] < 12 or data[8:12] != b'.FIT':
        raise FitError('это не FIT-файл (нет сигнатуры .FIT)')
    cache, msgs, notes = {}, [], []
    protocol, profile = data[1], struct.unpack_from('<H', data, 2)[0]
    start = 0
    while start + 12 <= len(data) and data[start + 8:start + 12] == b'.FIT':
        start = _read_one(data, start, cache, msgs, notes, src)
    f = FitFile(msgs, protocol, profile)
    f.cache = cache
    f.notes = notes
    return f


def _read_one(data, start, cache, msgs, notes, src):
    hsize = data[start]
    dsize = struct.unpack_from('<I', data, start + 4)[0]
    end = start + hsize + dsize
    if end + 2 > len(data):
        notes.append(f'файл обрезан: заявлено {dsize} байт данных, есть {len(data) - start - hsize}')
        end = len(data)
    elif crc16(data[start:end + 2]) != 0:
        notes.append('контрольная сумма файла не сошлась — разобрано как есть')
    pos, defs = start + hsize, {}
    last_ts = None
    n0 = len(msgs)
    try:
        _parse(data, pos, end, defs, cache, msgs, src, last_ts)
    except (struct.error, IndexError, FitError) as e:
        if len(msgs) == n0:
            raise FitError(f'файл не читается: {e}') from None
        notes.append(f'файл повреждён после {len(msgs) - n0} сообщений — взято всё до поломки')
        return len(data)
    return end + 2


def _parse(data, pos, end, defs, cache, msgs, src, last_ts):
    while pos < end:
        h = data[pos]; pos += 1
        if h & 0x80:                                   # сжатый заголовок времени
            local = (h >> 5) & 0x03
            off = h & 0x1F
            if last_ts is None:
                raise FitError('сжатое время до первой полной метки')
            t = (last_ts & ~0x1F) + off
            if off < (last_ts & 0x1F):
                t += 0x20
            last_ts = t
            cts = t
        elif h & 0x40:                                 # определение
            local = h & 0x0F
            arch = data[pos + 1]
            e = '<' if arch == 0 else '>'
            gnum, nf = struct.unpack_from(e + 'HB', data, pos + 2)
            pos += 5
            fields = [tuple(data[pos + 3 * k: pos + 3 * k + 3]) for k in range(nf)]
            pos += 3 * nf
            dev = []
            if h & 0x20:
                nd = data[pos]; pos += 1
                dev = [tuple(data[pos + 3 * k: pos + 3 * k + 3]) for k in range(nd)]
                pos += 3 * nd
            defs[local] = _intern(cache, Def(local, arch, gnum, fields, dev))
            continue
        else:
            local = h & 0x0F
            cts = None
        d = defs.get(local)
        if d is None:
            raise FitError(f'данные без определения (local {local}) на байте {pos - 1}')
        size_all = sum(f[1] for f in d.fields) + sum(f[1] for f in d.dev)
        if pos + size_all > end:
            raise IndexError('сообщение обрезано')
        raw = []
        for _, size, _ in d.fields:
            raw.append(data[pos:pos + size]); pos += size
        dlen = sum(f[1] for f in d.dev)
        devraw = data[pos:pos + dlen]; pos += dlen
        m = Msg(d, raw, devraw, cts, src)
        if cts is None:
            t = m.get(253)
            if t is not None:
                last_ts = t
        msgs.append(m)


def write(f):
    """FitFile -> байты. Заголовок 14 байт, определения — по мере смены набора полей."""
    body = bytearray()
    cur = {}
    cache = f.cache
    for m in f.msgs:
        if m.cts is not None and m.d.index(253) < 0:
            m = m.copy()
            m.set(253, m.cts, UINT32, cache)
        if cur.get(m.d.local) is not m.d:
            body += m.d.encode()
            cur[m.d.local] = m.d
        body.append(m.d.local)
        for r in m.raw:
            body += r
        body += m.devraw
    header = struct.pack('<BBHI4s', 14, f.protocol, f.profile, len(body), b'.FIT')
    header += struct.pack('<H', crc16(header))
    out = header + bytes(body)
    return out + struct.pack('<H', crc16(out))
