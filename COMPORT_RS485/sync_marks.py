"""
sync_marks.py
=============
Потокобезопасное хранилище меток синхронизации.

ВАЖНО: get_nearest() сканирует только последние 10 меток (O(10)),
а не весь буфер (O(8000)). Данные обрабатываются близко к «сейчас»,
поэтому ближайшая метка всегда свежая.

До фикса: min() по 8000 меткам = 1000 мкс/вызов = 52% CPU при 480 pkt/s
После:     min() по 10 меткам  =    3 мкс/вызов =  0.1% CPU
"""

import threading
import collections
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SyncMark:
    seq:      int   # порядковый номер импульса
    ts_ns:    int   # WALL_CLOCK.now_ns() в момент генерации (абс. время)
    perf_ns:  int   # time.perf_counter_ns() для точного замера интервалов


class SyncMarkStore:
    """
    Кольцевой буфер меток синхронизации.
    Генератор пишет (put), логгер читает (get_nearest).
    """

    # 400 меток = 200 мс при 2 кГц — достаточный запас,
    # но не настолько большой чтобы замедлять get_nearest.
    DEFAULT_MAXLEN = 400

    # Сколько последних меток проверять при поиске ближайшей.
    # 10 меток = 5 мс окно при 2 кГц — покрывает любую задержку обработки.
    SEARCH_WINDOW = 10

    def __init__(self, maxlen: int = DEFAULT_MAXLEN):
        self._buf: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._new_mark = threading.Event()

    def put(self, mark: SyncMark):
        with self._lock:
            self._buf.append(mark)
        self._new_mark.set()
        self._new_mark.clear()

    def get_nearest(self, ts_ns: int) -> SyncMark | None:
        """
        Найти ближайшую к ts_ns метку.

        Ключевая оптимизация: проверяем только последние SEARCH_WINDOW меток.
        Данные пакеты обрабатываются близко к «сейчас», значит ближайшая
        метка всегда в конце буфера — нет смысла сканировать старые.

        O(SEARCH_WINDOW) вместо O(maxlen) → ускорение в 80–400 раз.
        """
        with self._lock:
            if not self._buf:
                return None
            n = min(self.SEARCH_WINDOW, len(self._buf))
            # Берём последние n меток (новейшие — в конце deque)
            candidates = [self._buf[-(i + 1)] for i in range(n)]

        return min(candidates, key=lambda m: abs(m.ts_ns - ts_ns))

    def get_all_since(self, since_ts_ns: int) -> list:
        """Все метки новее since_ts_ns."""
        with self._lock:
            return [m for m in self._buf if m.ts_ns >= since_ts_ns]

    def latest(self) -> SyncMark | None:
        """Последняя добавленная метка (O(1))."""
        with self._lock:
            return self._buf[-1] if self._buf else None

    def wait_for_new(self, timeout: float = 1.0) -> bool:
        return self._new_mark.wait(timeout)

    def __len__(self):
        with self._lock:
            return len(self._buf)


# Глобальный синглтон — импортируется всеми модулями
SYNC_STORE = SyncMarkStore()
